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
import re
import shutil
import subprocess
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

BACKGROUNDS = [
    [(15, 15, 25), (30, 30, 60)],    # deep navy  (investing)
    [(10, 20, 10), (20, 50, 30)],    # dark green (career_income)
    [(25, 10, 10), (60, 20, 20)],    # dark red   (debt)
    [(15, 10, 25), (40, 20, 60)],    # deep purple(tax)
    [(10, 20, 30), (20, 50, 80)],    # ocean blue (budgeting)
]


# ---------------------------------------------------------------------------
# Font helpers
# ---------------------------------------------------------------------------

@lru_cache(maxsize=64)
def _get_font(size: int):
    from PIL import ImageFont
    candidates = [
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


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------

def _make_gradient_background(color_top: tuple, color_bottom: tuple,
                               w: int = SHORT_W, h: int = SHORT_H):
    from PIL import Image
    img = Image.new("RGB", (1, 2))
    img.putpixel((0, 0), color_top)
    img.putpixel((0, 1), color_bottom)
    return img.resize((w, h), Image.BILINEAR)


def _make_background_image(pillar: str = "investing"):
    """Gradient background with watermark. Returns RGBA PIL Image."""
    from PIL import ImageDraw
    palette_map = {
        "investing":    BACKGROUNDS[0],
        "career_income": BACKGROUNDS[1],
        "debt":         BACKGROUNDS[2],
        "tax":          BACKGROUNDS[3],
        "budgeting":    BACKGROUNDS[4],
    }
    colors = palette_map.get(pillar, BACKGROUNDS[0])
    bg = _make_gradient_background(colors[0], colors[1]).convert("RGBA")
    draw = ImageDraw.Draw(bg)
    font = _get_font(44)
    wm_text = "ClearWealth"
    wm_w = _text_width(draw, wm_text, font)
    wm_x = (SHORT_W - wm_w) // 2
    wm_y = int(SHORT_H * 0.05)
    # Dark backing rect improves legibility on any background
    pad = 8
    draw.rounded_rectangle(
        [(wm_x - pad, wm_y - pad // 2), (wm_x + wm_w + pad, wm_y + 44 + pad // 2)],
        radius=8, fill=(0, 0, 0, 80),
    )
    draw.text((wm_x, wm_y), wm_text, font=font, fill=(255, 255, 255, 140))
    return bg


# ---------------------------------------------------------------------------
# Pexels background video
# ---------------------------------------------------------------------------

# Single source of truth for pillar → Pexels search queries (shared with footage.py).
from pipeline.footage import PILLAR_VISUAL_QUERIES as PILLAR_BG_QUERIES


def _fetch_pexels_clip(pillar: str, work_dir: Path) -> Path | None:
    """
    Download one Pexels video clip relevant to the pillar.
    Returns the raw downloaded path, or None if unavailable.
    """
    import requests

    api_key = os.environ.get("PEXELS_API_KEY", "")
    if not api_key:
        return None

    queries = PILLAR_BG_QUERIES.get(pillar, PILLAR_BG_QUERIES["investing"])
    headers = {"Authorization": api_key}

    for query in queries:
        try:
            resp = requests.get(
                "https://api.pexels.com/videos/search",
                headers=headers,
                params={"query": query, "per_page": 10, "orientation": "portrait", "size": "medium"},
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
            dest = work_dir / "bg_raw.mp4"
            try:
                dl = requests.get(chosen["link"], stream=True, timeout=60)
                dl.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open("wb") as fh:
                    for chunk in dl.iter_content(65536):
                        if chunk:
                            fh.write(chunk)
                logger.info("Downloaded Pexels bg clip: %s (query=%s)", dest, query)
                return dest
            except Exception as exc:
                logger.warning("Pexels download failed: %s", exc)
                if dest.exists():
                    dest.unlink()

    return None


def _prepare_bg_video(raw_clip: Path, work_dir: Path, duration: float) -> Path:
    """
    Crop to 9:16, darken, loop/trim to `duration`. Returns processed video path.
    The darkening (brightness=-0.25) ensures text stays legible over any footage.
    """
    out = work_dir / "bg_processed.mp4"
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


def _extract_bg_frames(video_path: Path, work_dir: Path, fps: float = BG_FRAME_FPS) -> list[tuple[float, Path]]:
    """
    Extract frames at `fps` from video. Returns list of (timestamp, frame_path) sorted by time.
    At 6fps a 40s video yields ~240 frames — noticeably smoother background motion.
    """
    frame_dir = work_dir / "bg_frames"
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


def _build_background_frame(
    t_mid: float,
    gradient_img,           # RGBA PIL Image
    bg_frames: list | None, # list of (t, Path) or None
    gradient_blend: float = 0.40,
) -> "PIL.Image.Image":
    """
    Composite background for a segment centred at t_mid.
    - With bg_frames: blend darkened video frame with gradient (keeps brand colours).
    - Without bg_frames: use gradient only.
    """
    from PIL import Image

    if bg_frames:
        frame_path = _closest_bg_frame(bg_frames, t_mid)
        video_frame = Image.open(frame_path).convert("RGB").resize((SHORT_W, SHORT_H))
        # Blend: (1-alpha)*video + alpha*gradient  →  subtle brand colour tint over footage
        blended = Image.blend(video_frame, gradient_img.convert("RGB"), alpha=gradient_blend)
        return blended.convert("RGBA")
    else:
        return gradient_img.copy()


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
        lines, font = _wrap_fit_lines(draw, text, max_width=int(w * 0.78), start_size=66, min_size=34, max_lines=2)
        block_h = sum(_text_height(draw, ln, font) for ln in lines) + 8 * max(0, len(lines) - 1)
        pad = 18
        y0 = label_y0 if label_y0 is not None else int(h * 0.72)
        card_w = int(w * 0.82)
        x0 = (w - card_w) // 2
        draw.rounded_rectangle(
            [(x0, y0), (x0 + card_w, y0 + block_h + pad * 2)], radius=16, fill=(255, 255, 255, 28)
        )
        _draw_multiline_centered(draw, lines, y0 + pad, font, (255, 255, 255, 255), w, gap=8)

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
        y0 = int(h * 0.80)
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

def _ov_start(ov: dict) -> float:
    """Return overlay start time. Uses ElevenLabs word timestamp when available."""
    if "start_time_s" in ov:
        return float(ov["start_time_s"])
    return round(int(ov.get("start_word", 0)) / WPS, 2)


def _ov_end(ov: dict) -> float:
    return round(_ov_start(ov) + float(ov.get("duration_s", 3.0)), 2)


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
        # Compute start time: use real word timestamp when available, else WPS constant
        if word_timestamps and start_word < len(word_timestamps):
            start_time_s = float(word_timestamps[start_word])
        else:
            start_time_s = start_word / WPS
        if start_time_s >= duration_s:
            continue
        cleaned = dict(ov)
        cleaned["type"] = kind
        cleaned["start_word"] = start_word
        cleaned["start_time_s"] = round(start_time_s, 3)
        cleaned["duration_s"] = dur
        safe.append(cleaned)
    safe.sort(key=lambda item: item["start_word"])
    return safe


def _inject_cadence_labels(overlays: list, vo_duration: float) -> list[dict]:
    """
    Insert label cards until no gap exceeds MAX_VISUAL_GAP_S.

    Iterates until every gap is covered: a single pass only bisects each
    original gap once, but a long gap (e.g. 10s) needs multiple bisections
    to stay under the 2s threshold. The loop re-scans after each insertion.
    """
    cadence_labels = ["REAL EXAMPLE", "SIMPLE MATH", "THIS IS KEY", "TIME MATTERS"]
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
                    "start_word": int(midpoint * WPS),
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

def _mean_volume_db(path: Path) -> float | None:
    result = subprocess.run(
        [_bin("ffmpeg"), "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    )
    text = (result.stderr or "") + "\n" + (result.stdout or "")
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", text)
    return float(m.group(1)) if m else None


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
    mean_db = _mean_volume_db(output_path)
    if mean_db is not None and mean_db < -19.5:
        raise RuntimeError(f"Audio too quiet: mean {mean_db:.1f} dB")


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
    if not voiceover_path.exists():
        raise FileNotFoundError(f"Voiceover not found: {voiceover_path}")

    work_dir = WORKSPACE / "short_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    vo_duration = _get_voiceover_duration(voiceover_path)
    logger.info("Short voiceover duration: %.1fs", vo_duration)

    pillar = script_data.get("pillar", "investing")
    bg_img = _make_background_image(pillar)

    # Pexels background video (optional — falls back to gradient if unavailable)
    bg_frames: list | None = None
    if os.environ.get("PEXELS_API_KEY"):
        try:
            raw_clip = _fetch_pexels_clip(pillar, work_dir)
            if raw_clip:
                processed = _prepare_bg_video(raw_clip, work_dir, vo_duration)
                bg_frames = _extract_bg_frames(processed, work_dir, fps=BG_FRAME_FPS)
                logger.info("Background video ready: %d frames", len(bg_frames))
        except Exception as exc:
            logger.warning("Background video unavailable, using gradient: %s", exc)

    word_timestamps: list[float] = script_data.get("word_timestamps") or []
    if word_timestamps:
        logger.info("Using ElevenLabs word timestamps (%d words)", len(word_timestamps))

    # Step 1: clean overlays from script
    overlays = _sanitize_overlays(script_data.get("overlays", []), vo_duration, word_timestamps)

    # Step 2: inject cadence labels into actual visual gaps
    overlays = _inject_cadence_labels(overlays, vo_duration)
    overlays = _sanitize_overlays(overlays, vo_duration)  # re-sort + re-clamp after injection
    logger.info("Total overlays after cadence injection: %d", len(overlays))

    # Step 3: segment boundaries from overlay start/end times + background cadence
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

    # Step 4: composite each segment frame with Pillow
    seg_dir = work_dir / "segments"
    seg_dir.mkdir(exist_ok=True)
    concat_lines: list[str] = []

    for i in range(len(events_sorted) - 1):
        t_start = events_sorted[i]
        t_end   = events_sorted[i + 1]
        t_mid   = (t_start + t_end) / 2
        duration = t_end - t_start

        active = [ov for ov in overlays if _ov_start(ov) <= t_mid < _ov_end(ov)]

        frame = _build_background_frame(t_mid, bg_img, bg_frames)
        label_next_y = int(SHORT_H * 0.72)
        for ov in active:
            if ov.get("type") == "label":
                frame = Image.alpha_composite(frame, _make_overlay_image(ov, label_y0=label_next_y))
                label_next_y += _label_card_height(_clean_text(ov.get("text", "")).upper()) + 8
            else:
                frame = Image.alpha_composite(frame, _make_overlay_image(ov))

        seg_path = seg_dir / f"seg_{i:03d}.png"
        frame.convert("RGB").save(seg_path)
        concat_lines.append(f"file '{seg_path.resolve()}'\nduration {duration:.4f}")

    # FFmpeg concat demuxer requires the last entry to be repeated without a duration line
    if concat_lines:
        last_seg = seg_dir / f"seg_{len(events_sorted) - 2:03d}.png"
        concat_lines.append(f"file '{last_seg.resolve()}'")

    concat_file = work_dir / "segments.txt"
    concat_file.write_text("\n".join(concat_lines))
    logger.info("Composited %d segment frames via Pillow", len(events_sorted) - 1)

    # Step 5: audio filter (loudnorm + optional bgmusic)
    if bgmusic_path.exists():
        audio_filter = (
            "[1:a]volume=1.0[voice];"
            "[2:a]volume=0.09[music];"
            "[voice][music]amix=inputs=2:duration=first[mix];"
            f"[mix]loudnorm=I={TARGET_LOUDNESS}:TP=-1.5:LRA=7[a]"
        )
        audio_inputs = ["-i", str(voiceover_path), "-i", str(bgmusic_path)]
    else:
        audio_filter = f"[1:a]loudnorm=I={TARGET_LOUDNESS}:TP=-1.5:LRA=7[a]"
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
        "-c:a", "aac", "-b:a", "128k",
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
