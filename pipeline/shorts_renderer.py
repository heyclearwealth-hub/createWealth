"""
shorts_renderer.py — Renders a YouTube Short (9:16 vertical) using:
  - Pexels video background (darkened, brand-blended with gradient) when PEXELS_API_KEY is set,
    falling back to a static gradient if not.
  - Overlays pre-baked into background frames via Pillow compositing (no FFmpeg filter graph)
  - Cadence labels injected to fill any visual gap > MAX_VISUAL_GAP_S
  - Loudness-normalized audio mix

Output: workspace/output/short_original.mp4
"""
import json
import logging
import math
import os
import random
import re
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from pipeline.renderer import _bin

logger = logging.getLogger(__name__)

WORKSPACE = Path("workspace")
OUTPUT_SHORT = WORKSPACE / "output" / "short_original.mp4"
BGMUSIC_PATH = Path("pipeline/assets/bgmusic.mp3")

SHORT_W = 1080
SHORT_H = 1920

WPS = 2.5               # words per second at voiceover pace
MAX_VISUAL_GAP_S = 2.0  # max seconds of blank screen before injecting a cadence label
MAX_LINE_CHARS = 20     # fallback char-wrap width (word-boundary fallback only)
TARGET_LOUDNESS = -16.0
BG_FRAME_FPS = 6.0      # background frame extraction rate (higher = smoother motion)
BG_CADENCE_S = 0.5      # background refresh cadence for segment generation
BG_MIN_CUT_S = 1.5
BG_MAX_CUT_S = 3.0
BG_ABS_MAX_SHOT_S = 4.0
BG_TARGET_CLIPS = 4
BG_MIN_SOURCES = 2
BG_MAX_PER_QUERY = 1

CAPTION_WINDOW_WORDS = 7
MAX_CONCURRENT_LABELS = 1
LABEL_MIN_GAP_S = 0.35
CTA_SAFE_TAIL_S = 4.0
MIX_TRUE_PEAK_TARGET = -3.0

# Import pillar gradients from thumbnail_gen to keep a single source of truth.
# New pillars added there will automatically apply here too.
try:
    from pipeline.thumbnail_gen import PILLAR_GRADIENTS as _PILLAR_GRADIENTS
    from pipeline.thumbnail_gen import DEFAULT_GRADIENT as _DEFAULT_GRADIENT
except Exception:
    logger.warning("thumbnail_gen gradients unavailable; using renderer defaults")
    _PILLAR_GRADIENTS = {
        "investing": ((15, 15, 25), (30, 30, 60)),
        "career_income": ((10, 20, 10), (20, 50, 30)),
        "debt": ((25, 10, 10), (60, 20, 20)),
        "tax": ((15, 10, 25), (40, 20, 60)),
        "budgeting": ((10, 20, 30), (20, 50, 80)),
    }
    _DEFAULT_GRADIENT = _PILLAR_GRADIENTS["investing"]

# Keep BACKGROUNDS for any legacy references (mapped from the shared dict).
BACKGROUNDS = [
    [_PILLAR_GRADIENTS["investing"][0],     _PILLAR_GRADIENTS["investing"][1]],
    [_PILLAR_GRADIENTS["career_income"][0], _PILLAR_GRADIENTS["career_income"][1]],
    [_PILLAR_GRADIENTS["debt"][0],          _PILLAR_GRADIENTS["debt"][1]],
    [_PILLAR_GRADIENTS["tax"][0],           _PILLAR_GRADIENTS["tax"][1]],
    [_PILLAR_GRADIENTS["budgeting"][0],     _PILLAR_GRADIENTS["budgeting"][1]],
]


# ---------------------------------------------------------------------------
# Font helpers
# ---------------------------------------------------------------------------

@lru_cache(maxsize=64)
def _get_font(size: int):
    from PIL import ImageFont
    # Bundled font checked first — guarantees consistent look across macOS/Linux/CI.
    # To use: place any .ttf bold font at pipeline/assets/brand_font.ttf
    _asset_font = Path(__file__).parent / "assets" / "brand_font.ttf"
    candidates = [
        str(_asset_font),                                                        # bundled brand font
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Arial Unicode.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default(size=size)


def _clean_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _text_width(draw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _text_height(draw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


def _truncate_to_width(draw, text: str, font, max_width: int) -> str:
    if not text:
        return ""
    out = text
    while out and _text_width(draw, out, font) > max_width:
        if len(out) <= 1:
            break
        out = out[:-1]
    return out.rstrip() + ("..." if out != text else "")


def _wrap_fit_lines(
    draw,
    text: str,
    max_width: int,
    start_size: int,
    min_size: int,
    max_lines: int = 2,
):
    """
    Try progressively smaller font sizes until the text fits within max_lines.
    Hard fallback: split on word boundaries (not character boundaries) and truncate.
    """
    words = _clean_text(text).split()
    if not words:
        return [""], _get_font(min_size)

    for size in range(start_size, min_size - 1, -2):
        font = _get_font(size)
        lines: list[str] = []
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if _text_width(draw, candidate, font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)

        if len(lines) <= max_lines and all(
            _text_width(draw, line, font) <= max_width for line in lines
        ):
            return lines, font

    # Hard fallback — split on word boundaries, not characters
    font = _get_font(min_size)
    words_per_line = math.ceil(len(words) / max_lines)
    lines = [
        " ".join(words[i: i + words_per_line])
        for i in range(0, len(words), words_per_line)
    ][:max_lines]
    lines = [_truncate_to_width(draw, line, font, max_width) for line in lines]
    return lines, font


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _draw_centered(draw, text: str, y: int, font, fill, width: int, shadow: bool = True):
    tw = _text_width(draw, text, font)
    x = (width - tw) // 2
    if shadow:
        draw.text((x + 3, y + 3), text, font=font, fill=(0, 0, 0, 180))
    draw.text((x, y), text, font=font, fill=fill)


def _draw_multiline_centered(
    draw, lines: list[str], y: int, font, fill, width: int, gap: int = 8, shadow: bool = True
) -> int:
    """Draw centred multi-line text. Returns the y position after the last line."""
    current_y = y
    for line in lines:
        _draw_centered(draw, line, current_y, font, fill, width, shadow=shadow)
        current_y += _text_height(draw, line, font) + gap
    return current_y


def _spoken_words(script_text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9$%']+", str(script_text or ""))


def _active_word_idx(word_timestamps: list[float], t: float) -> int | None:
    if not word_timestamps:
        return None
    idx = None
    for i, start in enumerate(word_timestamps):
        if start <= t:
            idx = i
        else:
            break
    return idx


def _caption_slice(words: list[str], active_idx: int, window: int = CAPTION_WINDOW_WORDS) -> list[tuple[str, int]]:
    if not words or active_idx < 0:
        return []
    start = max(0, active_idx - 2)
    end = min(len(words), start + window)
    if end - start < min(4, len(words)):
        start = max(0, end - window)
    return [(words[i], i) for i in range(start, end)]


def _make_spoken_caption_image(
    words: list[str],
    word_timestamps: list[float],
    t_mid: float,
    w: int = SHORT_W,
    h: int = SHORT_H,
):
    """
    Word-synced caption strip. Highlights the active spoken word.
    """
    from PIL import Image, ImageDraw

    if not words or not word_timestamps:
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))

    active_idx = _active_word_idx(word_timestamps, t_mid)
    if active_idx is None:
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))

    token_slice = _caption_slice(words, active_idx)
    if not token_slice:
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _get_font(50)
    max_width = int(w * 0.88)
    space_w = _text_width(draw, " ", font)

    lines: list[list[tuple[str, int]]] = [[]]
    current_w = 0
    for word, idx in token_slice:
        ww = _text_width(draw, word, font)
        add_w = ww if not lines[-1] else ww + space_w
        if lines[-1] and current_w + add_w > max_width and len(lines) < 2:
            lines.append([])
            current_w = 0
            add_w = ww
        if len(lines) == 2 and current_w + add_w > max_width:
            break
        lines[-1].append((word, idx))
        current_w += add_w

    line_h = _text_height(draw, "Ag", font)
    total_h = len(lines) * line_h + (len(lines) - 1) * 12
    y0 = int(h * 0.74)
    box_pad_x = 26
    box_pad_y = 18
    draw.rounded_rectangle(
        [(int(w * 0.06), y0 - box_pad_y), (int(w * 0.94), y0 + total_h + box_pad_y)],
        radius=18,
        fill=(0, 0, 0, 170),
    )

    y = y0
    for line in lines:
        line_text = " ".join(wd for wd, _ in line)
        line_w = _text_width(draw, line_text, font)
        x = (w - line_w) // 2
        for i, (word, idx) in enumerate(line):
            if i > 0:
                x += space_w
            fill = (255, 220, 50, 255) if idx == active_idx else (255, 255, 255, 245)
            draw.text((x + 2, y + 2), word, font=font, fill=(0, 0, 0, 200))
            draw.text((x, y), word, font=font, fill=fill)
            x += _text_width(draw, word, font)
        y += line_h + 12
    return img


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------

def _make_gradient_background(color_top: tuple, color_bottom: tuple,
                               w: int = SHORT_W, h: int = SHORT_H):
    from PIL import Image
    img = Image.new("RGB", (1, 2))
    img.putpixel((0, 0), color_top)
    img.putpixel((0, 1), color_bottom)
    return img.resize((w, h), Image.Resampling.BILINEAR)


def _make_background_image(pillar: str = "investing"):
    """Gradient background with watermark. Returns RGBA PIL Image."""
    from PIL import ImageDraw
    top, bottom = _PILLAR_GRADIENTS.get(pillar, _DEFAULT_GRADIENT)
    bg = _make_gradient_background(top, bottom).convert("RGBA")
    draw = ImageDraw.Draw(bg)
    font = _get_font(46)
    wm_text = "ClearWealth"
    wm_w = _text_width(draw, wm_text, font)
    wm_x = (SHORT_W - wm_w) // 2
    wm_y = int(SHORT_H * 0.05)
    # Dark backing rect improves legibility on any background
    pad = 8
    draw.rounded_rectangle(
        [(wm_x - pad, wm_y - pad // 2), (wm_x + wm_w + pad, wm_y + 46 + pad // 2)],
        radius=8, fill=(0, 0, 0, 108),
    )
    draw.text((wm_x, wm_y), wm_text, font=font, fill=(255, 255, 255, 180))
    return bg


# ---------------------------------------------------------------------------
# Pexels background video
# ---------------------------------------------------------------------------

# Single source of truth for pillar → Pexels search queries (shared with footage.py).
from pipeline.footage import PILLAR_VISUAL_QUERIES as PILLAR_BG_QUERIES

VISUAL_TOPIC_HINTS = {
    "401k": ["401k retirement account phone app", "retirement portfolio statement closeup"],
    "roth": ["roth ira retirement savings planning", "retirement account app checking"],
    "etf": ["etf investing app smartphone", "stock index fund chart phone"],
    "index": ["index fund chart smartphone closeup", "long term investing app user"],
    "compound": ["compound interest chart growth animation style", "savings growth calculator screen"],
    "credit": ["credit card statement bills desk", "person paying credit card bill"],
    "debt": ["debt payoff planning notebook", "credit card debt stress closeup"],
    "budget": ["monthly budget spreadsheet laptop", "family budgeting expenses notebook"],
    "tax": ["tax return paperwork laptop", "irs forms desk closeup"],
    "salary": ["salary negotiation office meeting", "pay raise celebration office"],
    "income": ["extra income side hustle laptop", "paycheck direct deposit phone"],
}


def _build_visual_queries(pillar: str, topic: str = "", script_text: str = "") -> list[str]:
    """Build topic-aware visual queries so b-roll matches spoken content."""
    base_queries = list(PILLAR_BG_QUERIES.get(pillar, PILLAR_BG_QUERIES["investing"]))
    hint_queries: list[str] = []
    haystack = f"{topic} {script_text}".lower()
    for key, queries in VISUAL_TOPIC_HINTS.items():
        if key in haystack:
            hint_queries.extend(queries)

    if topic:
        topic_clean = " ".join(re.findall(r"[a-z0-9]+", topic.lower())).strip()
        if topic_clean:
            hint_queries.append(f"{topic_clean} finance smartphone closeup")
            hint_queries.append(f"{topic_clean} money planning person")

    merged = hint_queries + base_queries
    deduped: list[str] = []
    seen: set[str] = set()
    for query in merged:
        q = " ".join(str(query).split()).strip()
        if not q or q in seen:
            continue
        seen.add(q)
        deduped.append(q)
    return deduped


def _fetch_pexels_clips(
    pillar: str,
    work_dir: Path,
    target_count: int = BG_TARGET_CLIPS,
    topic: str = "",
    script_text: str = "",
) -> list[Path]:
    """
    Download several Pexels clips for montage pacing.
    Returns zero or more raw clip paths.
    """
    import requests

    api_key = os.environ.get("PEXELS_API_KEY", "")
    if not api_key:
        return []

    queries = _build_visual_queries(pillar, topic=topic, script_text=script_text)
    headers = {"Authorization": api_key}
    clips: list[Path] = []
    seen_links: set[str] = set()

    for query in queries:
        if len(clips) >= target_count:
            break
        per_query_downloads = 0
        try:
            resp = requests.get(
                "https://api.pexels.com/videos/search",
                headers=headers,
                params={"query": query, "per_page": 15, "orientation": "portrait", "size": "medium"},
                timeout=20,
            )
            resp.raise_for_status()
            videos = resp.json().get("videos", [])
        except Exception as exc:
            logger.warning("Pexels search failed (%s): %s", query, exc)
            continue

        for video in videos:
            files = sorted(video.get("video_files", []), key=lambda f: f.get("width", 0), reverse=True)
            chosen = next(
                (f for f in files if f.get("width", 0) >= 720 and "video/" in f.get("file_type", "")),
                None,
            )
            if not chosen:
                continue
            link = str(chosen.get("link", "")).strip()
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            dest = work_dir / f"bg_raw_{len(clips):02d}.mp4"
            downloaded = False
            for attempt in range(3):
                try:
                    dl = requests.get(link, stream=True, timeout=60)
                    dl.raise_for_status()
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with dest.open("wb") as fh:
                        for chunk in dl.iter_content(65536):
                            if chunk:
                                fh.write(chunk)
                    downloaded = True
                    break
                except Exception as exc:
                    logger.warning("Pexels download attempt %d/3 failed: %s", attempt + 1, exc)
                    if dest.exists():
                        dest.unlink()
                    if attempt < 2:
                        time.sleep(5)
            if downloaded:
                clips.append(dest)
                logger.info("Downloaded Pexels bg clip %d/%d: %s (query=%s)", len(clips), target_count, dest, query)
                if len(clips) >= target_count:
                    break
                per_query_downloads += 1
                if per_query_downloads >= BG_MAX_PER_QUERY:
                    break

    return clips


def _fetch_pexels_clip(pillar: str, work_dir: Path) -> Path | None:
    """
    Backward-compatible single-clip helper.
    """
    clips = _fetch_pexels_clips(pillar, work_dir, target_count=1)
    return clips[0] if clips else None


def _prepare_bg_video(raw_clip: Path, work_dir: Path, duration: float, tag: str = "0") -> Path:
    """
    Crop to 9:16, darken, loop/trim to `duration`. Returns processed video path.
    The darkening (brightness=-0.25) ensures text stays legible over any footage.
    """
    out = work_dir / f"bg_processed_{tag}.mp4"
    vf = (
        f"scale={SHORT_W}:{SHORT_H}:force_original_aspect_ratio=increase,"
        f"crop={SHORT_W}:{SHORT_H},"
        "eq=brightness=-0.25:saturation=0.80,"
        "boxblur=2:2"
    )
    cmd = [
        _bin("ffmpeg"), "-y",
        "-stream_loop", "-1",
        "-i", str(raw_clip),
        "-t", str(duration + 1.0),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-an",
        str(out),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"bg_video prepare failed:\n{result.stderr[-500:]}")
    return out


def _extract_bg_frames(
    video_path: Path,
    work_dir: Path,
    fps: float = BG_FRAME_FPS,
    tag: str = "0",
) -> list[tuple[float, Path]]:
    """
    Extract frames at `fps` from video. Returns list of (timestamp, frame_path) sorted by time.
    At 6fps a 40s video yields ~240 frames — noticeably smoother background motion.
    """
    frame_dir = work_dir / f"bg_frames_{tag}"
    if frame_dir.exists():
        shutil.rmtree(frame_dir, ignore_errors=True)
    frame_dir.mkdir(exist_ok=True)
    cmd = [
        _bin("ffmpeg"), "-y",
        "-i", str(video_path),
        "-vf", f"fps={fps}",
        str(frame_dir / "frame_%04d.png"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"bg frame extraction failed:\n{result.stderr[-300:]}")

    frames: list[tuple[float, Path]] = []
    for p in sorted(frame_dir.glob("frame_*.png")):
        idx = int(p.stem.split("_")[1]) - 1   # 0-based
        t = idx / fps
        frames.append((t, p))
    return frames


def _closest_bg_frame(frames: list[tuple[float, Path]], t: float) -> Path:
    """Return the path of the frame whose timestamp is closest to t."""
    return min(frames, key=lambda x: abs(x[0] - t))[1]


def _sample_bg_frame(frames: list[tuple[float, Path]], t: float) -> Path:
    """
    Sample a frame by looping local clip time instead of drifting to last frame.
    """
    if not frames:
        raise ValueError("No background frames available")
    max_t = frames[-1][0] if frames[-1][0] > 0 else 0.01
    local_t = t % max_t
    return _closest_bg_frame(frames, local_t)


def _build_bg_montage_plan(duration_s: float, source_count: int, seed_hint: str = "") -> list[tuple[float, float, int]]:
    """
    Build pacing plan: frequent cuts (1.5s–3.0s), never longer than 4s.
    Returns list of (start, end, source_idx).
    """
    if source_count <= 0:
        return []
    rng = random.Random(f"{seed_hint}:{duration_s:.2f}:{source_count}")
    t = 0.0
    prev_idx = -1
    plan: list[tuple[float, float, int]] = []
    while t < duration_s:
        shot = min(rng.uniform(BG_MIN_CUT_S, BG_MAX_CUT_S), BG_ABS_MAX_SHOT_S, duration_s - t)
        choices = list(range(source_count))
        if prev_idx in choices and len(choices) > 1:
            choices.remove(prev_idx)
        idx = rng.choice(choices)
        plan.append((round(t, 3), round(t + shot, 3), idx))
        t += shot
        prev_idx = idx
    return plan


def _bg_source_for_time(plan: list[tuple[float, float, int]], t: float) -> tuple[int, float]:
    """
    Return (source_idx, segment_start_time) for time t from montage plan.
    """
    if not plan:
        return 0, 0.0
    for start, end, idx in plan:
        if start <= t < end:
            return idx, start
    start, _end, idx = plan[-1]
    return idx, start


def _build_background_frame(
    t_mid: float,
    gradient_img,           # RGBA PIL Image
    bg_frames: list | None, # list[list[(t, Path)]] or None
    montage_plan: list[tuple[float, float, int]] | None = None,
    gradient_blend: float = 0.40,
) -> "PIL.Image.Image":
    """
    Composite background for a segment centred at t_mid.
    - With bg_frames: blend darkened video frame with gradient (keeps brand colours).
    - Without bg_frames: use gradient only.
    """
    from PIL import Image

    if bg_frames:
        source_idx, seg_start = _bg_source_for_time(montage_plan or [], t_mid)
        chosen_source = bg_frames[min(max(source_idx, 0), len(bg_frames) - 1)]
        frame_path = _sample_bg_frame(chosen_source, t_mid - seg_start)
        video_frame = Image.open(frame_path).convert("RGB").resize((SHORT_W, SHORT_H))
        # Blend: (1-alpha)*video + alpha*gradient  →  subtle brand colour tint over footage
        blended = Image.blend(video_frame, gradient_img.convert("RGB"), alpha=gradient_blend)
        return blended.convert("RGBA")
    else:
        # Fallback motion so visuals never feel static when stock clips are unavailable.
        base = gradient_img.convert("RGB")
        zoom = 1.04 + (0.015 * math.sin(t_mid * 0.55))
        scaled_w = int(SHORT_W * zoom)
        scaled_h = int(SHORT_H * zoom)
        resized = base.resize((scaled_w, scaled_h), Image.BICUBIC)
        offset_x = int((scaled_w - SHORT_W) / 2 + math.sin(t_mid * 0.31) * 14)
        offset_y = int((scaled_h - SHORT_H) / 2 + math.cos(t_mid * 0.27) * 22)
        left = max(0, min(offset_x, scaled_w - SHORT_W))
        top = max(0, min(offset_y, scaled_h - SHORT_H))
        moving = resized.crop((left, top, left + SHORT_W, top + SHORT_H))
        return moving.convert("RGBA")


# ---------------------------------------------------------------------------
# Overlay card renderers
# ---------------------------------------------------------------------------

def _label_card_height(text: str, w: int = SHORT_W) -> int:
    """Return the pixel height of the card that _make_overlay_image would render for a label."""
    from PIL import Image, ImageDraw
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines, font = _wrap_fit_lines(draw, text, max_width=int(w * 0.78), start_size=66, min_size=34, max_lines=2)
    block_h = sum(_text_height(draw, ln, font) for ln in lines) + 8 * max(0, len(lines) - 1)
    return block_h + 18 * 2  # pad = 18


def _make_overlay_image(overlay: dict, w: int = SHORT_W, h: int = SHORT_H, label_y0: int | None = None):
    """Render one overlay dict to a transparent RGBA PIL Image."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    otype = overlay.get("type")

    if otype == "hook_number":
        text = _clean_text(overlay.get("text", ""))
        subtitle = _clean_text(overlay.get("subtitle", ""))

        text_lines, text_font = _wrap_fit_lines(
            draw, text, max_width=int(w * 0.86), start_size=170, min_size=72, max_lines=2
        )
        text_block_h = sum(_text_height(draw, ln, text_font) for ln in text_lines) + \
                       8 * max(0, len(text_lines) - 1)

        subtitle_lines, subtitle_font, subtitle_block_h = [], None, 0
        if subtitle:
            subtitle_lines, subtitle_font = _wrap_fit_lines(
                draw, subtitle, max_width=int(w * 0.84), start_size=60, min_size=34, max_lines=2
            )
            subtitle_block_h = sum(_text_height(draw, ln, subtitle_font) for ln in subtitle_lines) + \
                                6 * max(0, len(subtitle_lines) - 1)

        pad_y = 22
        card_w = int(w * 0.90)
        card_h = text_block_h + subtitle_block_h + pad_y * 2 + (12 if subtitle_lines else 0)
        x0 = (w - card_w) // 2
        y0 = int(h * 0.26)
        draw.rounded_rectangle([(x0, y0), (x0 + card_w, y0 + card_h)], radius=28, fill=(0, 0, 0, 205))
        current_y = y0 + pad_y
        current_y = _draw_multiline_centered(draw, text_lines, current_y, text_font, (255, 220, 50, 255), w, gap=8)
        if subtitle_lines and subtitle_font is not None:
            current_y += 4
            _draw_multiline_centered(draw, subtitle_lines, current_y, subtitle_font, (215, 215, 215, 235), w, gap=6)

    elif otype == "label":
        text = _clean_text(overlay.get("text", "")).upper()
        lines, font = _wrap_fit_lines(draw, text, max_width=int(w * 0.76), start_size=60, min_size=32, max_lines=2)
        # Warn if the rendered text was truncated (joined lines shorter than input)
        rendered_text = " ".join(lines)
        if len(rendered_text) < len(text) - 3:
            logger.warning("Label text clipped during render: '%s' → '%s'", text, rendered_text)
        block_h = sum(_text_height(draw, ln, font) for ln in lines) + 8 * max(0, len(lines) - 1)
        pad = 18
        y0 = label_y0 if label_y0 is not None else int(h * 0.68)
        card_w = int(w * 0.82)
        x0 = (w - card_w) // 2
        draw.rounded_rectangle(
            [(x0, y0), (x0 + card_w, y0 + block_h + pad * 2)], radius=16, fill=(255, 255, 255, 28)
        )
        _draw_multiline_centered(draw, lines, y0 + pad, font, (255, 255, 255, 255), w, gap=8)

    elif otype == "proof_tag":
        text = _clean_text(overlay.get("text", "SOURCE"))
        if overlay.get("plain_text", False):
            label = text[:74]
        else:
            label = f"SOURCE: {text}"[:74]
        font = _get_font(26)
        pad_x, pad_y = 16, 10
        tw = _text_width(draw, label, font)
        th = _text_height(draw, label, font)
        x1 = int(w * 0.96)
        x0 = x1 - tw - pad_x * 2
        y0 = int(h * 0.14)
        y1 = y0 + th + pad_y * 2
        draw.rounded_rectangle([(x0, y0), (x1, y1)], radius=12, fill=(0, 0, 0, 190))
        draw.text((x0 + pad_x, y0 + pad_y), label, font=font, fill=(210, 230, 255, 245))

    elif otype == "comparison":
        left = _clean_text(overlay.get("left", ""))
        right = _clean_text(overlay.get("right", ""))
        draw.rectangle([(0, int(h * 0.35)), (w, int(h * 0.65))], fill=(0, 0, 0, 180))
        draw.line([(w // 2, int(h * 0.37)), (w // 2, int(h * 0.63))], fill=(100, 100, 100, 200), width=3)
        hfont = _get_font(44)
        draw.text((int(w * 0.08), int(h * 0.37)), "BEFORE", font=hfont, fill=(255, 80, 80, 255))
        draw.text((int(w * 0.58), int(h * 0.37)), "AFTER",  font=hfont, fill=(80, 220, 100, 255))
        left_lines,  left_font  = _wrap_fit_lines(draw, left.replace("\n", " "),  int(w * 0.40), 52, 30, max_lines=3)
        right_lines, right_font = _wrap_fit_lines(draw, right.replace("\n", " "), int(w * 0.40), 52, 30, max_lines=3)
        for i, line in enumerate(left_lines[:3]):
            draw.text((int(w * 0.06), int(h * (0.44 + i * 0.07))), line, font=left_font,  fill=(255, 255, 255, 255))
        for i, line in enumerate(right_lines[:3]):
            draw.text((int(w * 0.56), int(h * (0.44 + i * 0.07))), line, font=right_font, fill=(255, 255, 255, 255))

    elif otype == "cta":
        text = _clean_text(overlay.get("text", "Follow for more"))
        lines, font = _wrap_fit_lines(draw, text, max_width=int(w * 0.80), start_size=58, min_size=32, max_lines=2)
        block_h = sum(_text_height(draw, ln, font) for ln in lines) + 8 * max(0, len(lines) - 1)
        pad = 24
        y0 = int(h * 0.76)
        card_w = int(w * 0.84)
        x0 = (w - card_w) // 2
        draw.rounded_rectangle(
            [(x0, y0), (x0 + card_w, y0 + block_h + pad * 2)], radius=20, fill=(255, 220, 50, 230)
        )
        _draw_multiline_centered(draw, lines, y0 + pad, font, (0, 0, 0, 255), w, gap=8, shadow=False)

    return img


# ---------------------------------------------------------------------------
# Overlay helpers
# ---------------------------------------------------------------------------

_wps_fallback_warned: bool = False  # deduplicate per-process; reset per render via render()


def _ov_start(ov: dict) -> float:
    """Return overlay start time. Uses ElevenLabs word timestamp when available."""
    global _wps_fallback_warned
    if "start_time_s" in ov:
        return float(ov["start_time_s"])
    start_word = int(ov.get("start_word", 0))
    if not _wps_fallback_warned:
        logger.debug(
            "One or more overlays using WPS fallback for timing — "
            "ensure word_timestamps are available for precise sync"
        )
        _wps_fallback_warned = True
    return round(start_word / WPS, 2)


def _ov_end(ov: dict) -> float:
    return round(_ov_start(ov) + float(ov.get("duration_s", 3.0)), 2)


def _inject_proof_tags(
    overlays: list[dict], stat_citations: list[str], duration_s: float
) -> list[dict]:
    """
    For each stat citation, attach a 1.6s proof_tag near the first matching
    hook_number or comparison overlay.  Adds trust signals ("SPIVA 2025") for
    money claims without cluttering every frame.
    """
    if not stat_citations:
        return overlays
    result = list(overlays)
    cite_idx = 0
    for ov in overlays:
        if cite_idx >= len(stat_citations):
            break
        if ov.get("type") in {"hook_number", "comparison"}:
            # Center the proof tag in the middle of the overlay window so it's
            # visible for the full tag duration even on short overlays.
            ov_mid = (_ov_start(ov) + _ov_end(ov)) / 2
            proof_start = round(max(_ov_start(ov) + 0.2, ov_mid - 0.8), 3)
            if proof_start < duration_s - 1.5:
                result.append({
                    "type": "proof_tag",
                    "text": stat_citations[cite_idx],
                    "start_time_s": proof_start,
                    "start_word": ov.get("start_word", 0),
                    "duration_s": 1.6,
                })
                cite_idx += 1
    return result


def _check_label_overlaps(overlays: list[dict]) -> list[str]:
    """Return warning strings for any label windows that visually overlap in time."""
    labels = [(ov, _ov_start(ov), _ov_end(ov)) for ov in overlays if ov.get("type") == "label"]
    warnings: list[str] = []
    for i, (ov_a, s_a, e_a) in enumerate(labels):
        for ov_b, s_b, e_b in labels[i + 1:]:
            if s_b < e_a and s_a < e_b:
                warnings.append(
                    f"label overlap: '{ov_a.get('text')}' ({s_a:.1f}–{e_a:.1f}s) "
                    f"↔ '{ov_b.get('text')}' ({s_b:.1f}–{e_b:.1f}s)"
                )
    return warnings


def _sanitize_overlays(
    overlays: list, duration_s: float, word_timestamps: list[float] | None = None
) -> list[dict]:
    safe = []
    for ov in overlays:
        kind = str((ov or {}).get("type", "")).strip()
        if kind not in {"hook_number", "label", "comparison", "cta"}:
            continue
        try:
            start_word = max(0, int((ov or {}).get("start_word", 0)))
        except (TypeError, ValueError):
            start_word = 0
        try:
            dur = max(1.2, min(float((ov or {}).get("duration_s", 3.0)), 5.0))
        except (TypeError, ValueError):
            dur = 3.0
        # Compute start time.
        # Priority:
        #   1. Injected overlays (e.g. cadence labels) that carry start_time_s but no
        #      start_word key — trust their precise float directly.
        #   2. Script overlays with start_word + real ElevenLabs word timestamps.
        #   3. Preserved start_time_s from a prior sanitize pass.
        #   4. WPS constant fallback (least accurate).
        if "start_word" not in (ov or {}) and "start_time_s" in (ov or {}):
            # Injected label with precise time — trust it, skip word-index lookup.
            start_time_s = float((ov or {})["start_time_s"])
        elif word_timestamps and start_word < len(word_timestamps):
            start_time_s = float(word_timestamps[start_word])
        elif "start_time_s" in (ov or {}):
            start_time_s = float((ov or {})["start_time_s"])
        else:
            start_time_s = start_word / WPS
        if start_time_s >= duration_s:
            continue
        # Clamp so overlay always ends before the video finishes.
        dur = min(dur, max(0.5, round(duration_s - start_time_s - 0.05, 2)))
        cleaned = dict(ov)
        cleaned["type"] = kind
        cleaned["start_word"] = start_word
        cleaned["start_time_s"] = round(start_time_s, 3)
        cleaned["duration_s"] = dur
        safe.append(cleaned)
    safe.sort(key=lambda item: item["start_time_s"])
    return safe


def _deoverlap_label_overlays(overlays: list[dict], duration_s: float, min_gap_s: float = LABEL_MIN_GAP_S) -> list[dict]:
    """
    Prevent stacked/overlapping labels by shifting later labels forward.
    Drops labels that cannot fit cleanly before the tail CTA-safe window.
    """
    out: list[dict] = []
    labels = [dict(ov) for ov in overlays if ov.get("type") == "label"]
    non_labels = [dict(ov) for ov in overlays if ov.get("type") != "label"]
    labels.sort(key=_ov_start)

    # Keep final tail cleaner for CTA + outro readability.
    tail_cutoff = max(0.0, duration_s - CTA_SAFE_TAIL_S)
    next_free = 0.0
    for label in labels:
        start = _ov_start(label)
        dur = float(label.get("duration_s", 2.0))

        # Preserve final on-screen disclaimer if present.
        if "EDUCATIONAL ONLY" in str(label.get("text", "")).upper():
            label["start_time_s"] = round(max(start, duration_s - 2.2), 3)
            label["duration_s"] = min(2.0, max(0.8, duration_s - label["start_time_s"] - 0.05))
            out.append(label)
            continue

        if start >= tail_cutoff:
            continue

        start = max(start, next_free)
        end = min(start + dur, tail_cutoff)
        if end - start < 0.8:
            continue
        label["start_time_s"] = round(start, 3)
        label["duration_s"] = round(end - start, 2)
        out.append(label)
        next_free = end + min_gap_s

    merged = non_labels + out
    merged.sort(key=_ov_start)
    return merged


_PILLAR_CADENCE_LABELS: dict[str, list[str]] = {
    "investing":     ["REAL RETURNS", "THE MATH", "PROOF POINT", "TIME MATTERS"],
    "budgeting":     ["REAL EXAMPLE", "SIMPLE MATH", "MONEY MOVE", "THIS IS KEY"],
    "debt":          ["DEBT TRAP", "REAL COST", "THE MATH", "BREAK FREE"],
    "tax":           ["TAX FACT", "THE RULE", "PROOF POINT", "THIS IS KEY"],
    "career_income": ["SALARY MOVE", "WHY IT MATTERS", "THE MATH", "REAL IMPACT"],
}
_DEFAULT_CADENCE_LABELS = ["REAL EXAMPLE", "SIMPLE MATH", "THIS IS KEY", "TIME MATTERS"]


def _inject_cadence_labels(overlays: list, vo_duration: float, pillar: str = "") -> list[dict]:
    """
    Insert label cards until no gap exceeds MAX_VISUAL_GAP_S.

    Iterates until every gap is covered: a single pass only bisects each
    original gap once, but a long gap (e.g. 10s) needs multiple bisections
    to stay under the 2s threshold. The loop re-scans after each insertion.
    """
    cadence_labels = _PILLAR_CADENCE_LABELS.get(pillar, _DEFAULT_CADENCE_LABELS)
    injected = list(overlays)
    label_idx = 0

    for _ in range(200):   # hard cap — prevents infinite loops on degenerate input
        # Rebuild bounds from current injected list each iteration
        bounds: set[float] = {0.0, vo_duration}
        for ov in injected:
            s, e = _ov_start(ov), _ov_end(ov)
            if 0 < s < vo_duration:
                bounds.add(s)
            if 0 < e < vo_duration:
                bounds.add(e)
        timeline = sorted(bounds)

        filled = True
        for i in range(len(timeline) - 1):
            gap = timeline[i + 1] - timeline[i]
            if gap > MAX_VISUAL_GAP_S:
                midpoint = timeline[i] + gap / 2.0
                injected.append({
                    "type": "label",
                    "text": cadence_labels[label_idx % len(cadence_labels)],
                    # Store precise float time directly — no start_word → avoids int-truncation drift
                    "start_time_s": round(midpoint, 3),
                    "duration_s": min(2.1, gap - 0.2),
                })
                label_idx += 1
                filled = False
                break   # restart scan with updated timeline

        if filled:
            break

    return injected


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def _mean_volume_db(path: Path) -> tuple[float | None, float | None]:
    """Returns (mean_db, max_db) from ffmpeg volumedetect. Either may be None."""
    result = subprocess.run(
        [_bin("ffmpeg"), "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    )
    text = (result.stderr or "") + "\n" + (result.stdout or "")
    m_mean = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", text)
    m_max = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", text)
    mean_db = float(m_mean.group(1)) if m_mean else None
    max_db = float(m_max.group(1)) if m_max else None
    return mean_db, max_db


# ---------------------------------------------------------------------------
# Video probe / quality gate
# ---------------------------------------------------------------------------

def _probe_video(path: Path) -> dict:
    result = subprocess.run(
        [_bin("ffprobe"), "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(result.stdout)


def _quality_gate(output_path: Path, expected_duration: float) -> None:
    probe = _probe_video(output_path)
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("Short output missing video stream")
    if int(video.get("width", 0)) != SHORT_W or int(video.get("height", 0)) != SHORT_H:
        raise RuntimeError(f"Wrong dimensions: {video.get('width')}x{video.get('height')}")
    if str(video.get("pix_fmt", "")) != "yuv420p":
        raise RuntimeError(f"Wrong pixel format: {video.get('pix_fmt')}")
    duration = float(probe.get("format", {}).get("duration", 0.0) or 0.0)
    if abs(duration - expected_duration) > 3.0:
        raise RuntimeError(f"Duration drift: rendered {duration:.1f}s vs voiceover {expected_duration:.1f}s")
    mean_db, max_db = _mean_volume_db(output_path)
    if mean_db is not None and mean_db < -19.5:
        raise RuntimeError(f"Audio too quiet: mean {mean_db:.1f} dB")
    # YouTube loudness normalization flattens clipped/over-compressed audio.
    # Warn if peak exceeds our safer -3 dBTP target.
    if max_db is not None and max_db > MIX_TRUE_PEAK_TARGET:
        logger.warning(
            "Audio peak %.1f dB exceeds -3 dBTP — may sound flat after YouTube loudness normalization",
            max_db,
        )


def _get_voiceover_duration(path: Path) -> float:
    result = subprocess.run(
        [_bin("ffprobe"), "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


# ---------------------------------------------------------------------------
# Main render entry point
# ---------------------------------------------------------------------------

def render(
    voiceover_path: Path,
    script_data: dict,
    output_path: Path = OUTPUT_SHORT,
    bgmusic_path: Path = BGMUSIC_PATH,
) -> Path:
    """
    Render a standalone YouTube Short.

    Strategy: Pillow pre-compositing (fast, CI-safe)
    ─────────────────────────────────────────────────
    1. Build gradient background image (Pillow).
    2. Sanitize script overlays; inject cadence labels into visual gaps.
    3. Compute segment boundaries from all overlay start/end times.
    4. For each segment, composite background + active overlays into a PNG (Pillow).
    5. FFmpeg concat demuxer encodes the PNG sequence — no overlay filter graph,
       no -loop 1 inputs, no timeout risk.
    6. Mix voiceover + background music with loudnorm.
    """
    global _wps_fallback_warned
    _wps_fallback_warned = False  # reset so each render gets at most one WPS fallback warning

    if not voiceover_path.exists():
        raise FileNotFoundError(f"Voiceover not found: {voiceover_path}")

    work_dir = WORKSPACE / "short_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    vo_duration = _get_voiceover_duration(voiceover_path)
    logger.info("Short voiceover duration: %.1fs", vo_duration)
    if not (35.0 <= vo_duration <= 65.0):
        raise RuntimeError(
            f"Voiceover duration {vo_duration:.1f}s is outside the 35–65s target range. "
            "Check the script word count or TTS speed settings."
        )

    pillar = script_data.get("pillar", "investing")
    topic_slug = re.sub(r"[^a-z0-9]+", "-", script_data.get("topic", pillar).lower()).strip("-")
    bg_img = _make_background_image(pillar)

    # Pexels background video — true montage pacing (3-6 clips, cuts every 1.5–3s)
    bg_frames_sources: list | None = None
    montage_plan: list | None = None
    if os.environ.get("PEXELS_API_KEY"):
        try:
            raw_clips = _fetch_pexels_clips(
                pillar,
                work_dir,
                target_count=BG_TARGET_CLIPS,
                topic=str(script_data.get("topic", "")),
                script_text=str(script_data.get("voiceover_script", "")),
            )
            if raw_clips:
                bg_frames_sources = []
                for ci, clip in enumerate(raw_clips):
                    processed = _prepare_bg_video(clip, work_dir, vo_duration, tag=str(ci))
                    frames = _extract_bg_frames(processed, work_dir, fps=BG_FRAME_FPS, tag=str(ci))
                    bg_frames_sources.append(frames)
                montage_plan = _build_bg_montage_plan(
                    vo_duration, len(bg_frames_sources), seed_hint=topic_slug
                )
                logger.info(
                    "Background montage ready: %d clips, %d shots",
                    len(bg_frames_sources), len(montage_plan),
                )
                if len(raw_clips) < BG_MIN_SOURCES:
                    logger.warning(
                        "Only %d background source clip(s) fetched (<%d preferred). "
                        "Continuing with available clip(s) plus overlay cadence.",
                        len(raw_clips),
                        BG_MIN_SOURCES,
                    )
        except Exception as exc:
            logger.warning("Background video unavailable, using gradient: %s", exc)
            bg_frames_sources = None
            montage_plan = None

    word_timestamps: list[float] = script_data.get("word_timestamps") or []
    if word_timestamps:
        logger.info("Using ElevenLabs word timestamps (%d words)", len(word_timestamps))
    else:
        logger.warning(
            "Word timestamps unavailable — using WPS timing fallback for overlays "
            "(captions will be less precise)."
        )

    # Step 1: clean overlays from script
    overlays = _sanitize_overlays(script_data.get("overlays", []), vo_duration, word_timestamps)

    # Step 2: inject cadence labels into actual visual gaps (pillar-specific copy)
    _overlays_before_cadence = len(overlays)
    overlays = _inject_cadence_labels(overlays, vo_duration, pillar=pillar)
    overlays = _sanitize_overlays(overlays, vo_duration, word_timestamps)  # re-sort + re-clamp after injection
    overlays = _deoverlap_label_overlays(overlays, vo_duration)
    logger.info(
        "Overlays: %d from script → %d cadence-injected → %d total after sanitize",
        _overlays_before_cadence,
        len(overlays) - _overlays_before_cadence,
        len(overlays),
    )

    # Step 3: inject proof tags AFTER sanitize (proof_tag is not in the sanitize allowlist)
    stat_citations = script_data.get("stat_citations") or []
    overlays = _inject_proof_tags(overlays, stat_citations, vo_duration)

    # Step 3b: inject on-screen "Educational only. Not financial advice." disclaimer
    # if any overlay contains a dollar amount or percentage — required for finance content.
    has_financial_claim = any(
        "$" in str(ov.get("text", "")) or "%" in str(ov.get("text", ""))
        or "$" in str(ov.get("left", "")) or "$" in str(ov.get("right", ""))
        for ov in overlays
    )
    if has_financial_claim:
        disclaimer_start = max(0.0, round(vo_duration - 2.2, 2))
        overlays.append({
            "type": "proof_tag",
            "text": "Educational only. Not advice.",
            "plain_text": True,
            "start_time_s": disclaimer_start,
            "duration_s": 2.0,
        })
        logger.info("Injected on-screen financial disclaimer at %.1fs", disclaimer_start)

    overlays = _deoverlap_label_overlays(overlays, vo_duration)

    logger.info("Total overlays after cadence + proof injection: %d", len(overlays))

    # Pre-render quality check: warn on overlapping label windows
    for warn_msg in _check_label_overlaps(overlays):
        logger.warning("Quality gate: %s", warn_msg)

    # Step 4: segment boundaries from overlay start/end times + background cadence
    from PIL import Image
    events: set[float] = {0.0, vo_duration}
    for ov in overlays:
        s, e = _ov_start(ov), _ov_end(ov)
        if 0 < s < vo_duration:
            events.add(s)
        if 0 < e < vo_duration:
            events.add(e)
    t = BG_CADENCE_S
    while t < vo_duration:
        events.add(round(t, 2))
        t += BG_CADENCE_S
    events_sorted = sorted(events)

    # Step 5: composite each segment frame with Pillow
    spoken_words_list = _spoken_words(script_data.get("voiceover_script", ""))
    seg_dir = work_dir / "segments"
    seg_dir.mkdir(exist_ok=True)
    concat_lines: list[str] = []

    for i in range(len(events_sorted) - 1):
        t_start = events_sorted[i]
        t_end   = events_sorted[i + 1]
        t_mid   = (t_start + t_end) / 2
        duration = t_end - t_start

        all_active = [ov for ov in overlays if _ov_start(ov) <= t_mid < _ov_end(ov)]

        # Enforce one label at a time to reduce cognitive overload
        active_non_labels = [ov for ov in all_active if ov.get("type") != "label"]
        active_labels = [ov for ov in all_active if ov.get("type") == "label"]
        active = active_non_labels + active_labels[:MAX_CONCURRENT_LABELS]

        frame = _build_background_frame(t_mid, bg_img, bg_frames_sources, montage_plan=montage_plan)
        label_next_y = int(SHORT_H * 0.68)
        for ov in active:
            if ov.get("type") == "label":
                frame = Image.alpha_composite(frame, _make_overlay_image(ov, label_y0=label_next_y))
                label_next_y += _label_card_height(_clean_text(ov.get("text", "")).upper()) + 8
            else:
                frame = Image.alpha_composite(frame, _make_overlay_image(ov))

        # Word-synced spoken captions (phrase-by-phrase highlight, active word in yellow)
        has_active_cta = any(ov.get("type") == "cta" for ov in active)
        if word_timestamps and spoken_words_list and not has_active_cta:
            caption_img = _make_spoken_caption_image(spoken_words_list, word_timestamps, t_mid)
            frame = Image.alpha_composite(frame, caption_img)

        seg_path = seg_dir / f"seg_{i:03d}.png"
        frame.convert("RGB").save(seg_path)
        concat_lines.append(f"file '{seg_path.resolve()}'\nduration {duration:.4f}")

    # FFmpeg concat demuxer: last entry repeated without a duration line
    if concat_lines:
        last_seg = seg_dir / f"seg_{len(events_sorted) - 2:03d}.png"
        concat_lines.append(f"file '{last_seg.resolve()}'")

    concat_file = work_dir / "segments.txt"
    concat_file.write_text("\n".join(concat_lines))
    logger.info("Composited %d segment frames via Pillow", len(events_sorted) - 1)

    # Step 6: audio filter — sidechain ducking under voice + final limiter at -1.0 dBTP
    # SHORT_MUSIC=0 disables background music (useful when music tone doesn't fit content).
    music_enabled = os.environ.get("SHORT_MUSIC", "1").lower() in {"1", "true", "yes"}
    if bgmusic_path.exists() and music_enabled:
        audio_filter = (
            "[1:a]highpass=f=85,lowpass=f=12000,"
            "acompressor=threshold=-17dB:ratio=2.2:attack=15:release=180,"
            "volume=1.05,asplit=2[voice_main][voice_sc];"
            "[2:a]volume=0.11[raw_music];"
            # Sidechain compress: voice triggers music level reduction while speaking
            "[raw_music][voice_sc]sidechaincompress=threshold=0.015:ratio=6:attack=5:release=200[music_ducked];"
            "[voice_main][music_ducked]amix=inputs=2:duration=first[mix];"
            f"[mix]loudnorm=I={TARGET_LOUDNESS}:TP={MIX_TRUE_PEAK_TARGET}:LRA=7[a]"
        )
        audio_inputs = ["-i", str(voiceover_path), "-i", str(bgmusic_path)]
    else:
        audio_filter = (
            "[1:a]highpass=f=85,lowpass=f=12000,"
            "acompressor=threshold=-17dB:ratio=2.2:attack=15:release=180:makeup=3,"
            f"loudnorm=I={TARGET_LOUDNESS}:TP={MIX_TRUE_PEAK_TARGET}:LRA=7[a]"
        )
        audio_inputs = ["-i", str(voiceover_path)]

    # Step 6: FFmpeg encode — concat image sequence + audio only
    # -vf is valid here: video comes from concat demuxer (not from filter_complex),
    # while filter_complex handles audio only.
    cmd = [
        _bin("ffmpeg"), "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        *audio_inputs,
        "-filter_complex", audio_filter,
        "-map", "0:v",
        "-map", "[a]",
        "-vf", f"scale={SHORT_W}:{SHORT_H}:force_original_aspect_ratio=disable,fps=30,setsar=1",
        "-c:v", "libx264", "-preset", "fast", "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-g", "60",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    logger.info("Encoding Short (%d segments, %d overlays)...", len(events_sorted) - 1, len(overlays))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(f"Short render failed:\n{result.stderr[-1000:]}")
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)
        logger.debug("Cleaned up segment frames: %s", seg_dir)

    if not output_path.exists() or output_path.stat().st_size < 50_000:
        raise RuntimeError(f"Short output missing or too small: {output_path}")

    _quality_gate(output_path, vo_duration)

    logger.info("Short rendered: %s (%.1f MB)", output_path, output_path.stat().st_size / 1_048_576)
    return output_path
