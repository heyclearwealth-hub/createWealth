"""
shorts_renderer.py — Renders a YouTube Short (9:16 vertical) using:
  - Pexels video background (darkened, brand-blended with gradient) when PEXELS_API_KEY is set,
    falling back to a static gradient if not.
  - Overlays pre-baked into background frames via Pillow compositing (no FFmpeg filter graph)
  - Cadence labels injected to fill any visual gap > MAX_VISUAL_GAP_S
  - Loudness-normalized audio mix

Output: workspace/output/short_original.mp4
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import shutil
import subprocess
import threading
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
MAX_VISUAL_GAP_S = 1.7  # max seconds of blank screen before injecting a cadence label
MAX_LINE_CHARS = 20     # fallback char-wrap width (word-boundary fallback only)
TARGET_LOUDNESS = -14.0  # YouTube Shorts normalises to -14 dB LUFS on mobile
BG_FRAME_FPS = 6.0      # background frame extraction rate (higher = smoother motion)
BG_CADENCE_S = 0.5      # background refresh cadence for segment generation
BG_MIN_CUT_S = 0.75
BG_MAX_CUT_S = 1.45
BG_ABS_MAX_SHOT_S = 1.7
BG_TARGET_CLIPS = 10
BG_MIN_SOURCES = 2
BG_MAX_PER_QUERY = 2

CAPTION_WINDOW_WORDS = 7
MAX_CONCURRENT_LABELS = 1
LABEL_MIN_GAP_S = 0.35
CTA_SAFE_TAIL_S = 3.0
MAX_CADENCE_LABELS = 8   # cap on auto-injected cadence labels — more causes cognitive overload
MIX_TRUE_PEAK_TARGET = -3.0
SHORT_MIN_DURATION_S = float(os.environ.get("SHORT_MIN_DURATION_S", "34"))
SHORT_MAX_DURATION_S = float(os.environ.get("SHORT_MAX_DURATION_S", "44"))
SHORT_AUTOFIT_VOICEOVER = os.environ.get("SHORT_AUTOFIT_VOICEOVER", "1").lower() in {"1", "true", "yes"}
SHORT_AUTOFIT_TARGET_MARGIN_S = float(os.environ.get("SHORT_AUTOFIT_TARGET_MARGIN_S", "0.2"))
SHORT_AUTOFIT_MIN_RATE = float(os.environ.get("SHORT_AUTOFIT_MIN_RATE", "0.90"))
SHORT_AUTOFIT_MAX_RATE = float(os.environ.get("SHORT_AUTOFIT_MAX_RATE", "1.15"))
BG_BRIGHTNESS = -0.12
BG_SATURATION = 0.92
BG_BLUR = "1:1"
HOOK_INTERRUPT_AT_S = 0.55
HOOK_INTERRUPT_DURATION_S = 1.15
HOOK_SCENE_DEADLINE_S = 1.0
FREEZE_WARN_MIN_S = 0.75
FREEZE_WARN_TOTAL_S = 3.5
MIN_VIDEO_BITRATE = "6M"
MAX_VIDEO_BITRATE = "8M"
VIDEO_BUF_SIZE = "12M"

GENERIC_LABEL_TEXTS = {
    "REAL EXAMPLE",
    "SIMPLE MATH",
    "THIS IS KEY",
    "TIME MATTERS",
    "START SMALL",
    "STAY CONSISTENT",
    "REAL RETURNS",
    "THE MATH",
    "PROOF POINT",
    "MONEY MOVE",
    "DEBT TRAP",
    "REAL COST",
    "BREAK FREE",
    "TAX FACT",
    "THE RULE",
    "SALARY MOVE",
    "WHY IT MATTERS",
    "REAL IMPACT",
    "THE ACTION",
}

LABEL_ACCENT_COLORS = [
    (255, 220, 50, 255),   # brand yellow
    (210, 236, 255, 255),  # cool blue
    (255, 255, 255, 255),  # neutral white
]

FINANCE_PILLARS = {"investing", "budgeting", "debt", "tax", "career_income"}
FINANCIAL_SIGNAL_RE = re.compile(
    r"\$|%|\b(invest|investing|portfolio|stocks?|etf|crypto|retire(?:ment)?|debt|budget|tax|income|salary|interest)\b",
    re.IGNORECASE,
)
ADVICE_SIGNAL_RE = re.compile(
    r"\b(you should|do this|avoid this|buy|sell|max out|open (?:a|an)|start now)\b",
    re.IGNORECASE,
)
from pipeline.text_utils import fix_finance_acronyms as _fix_finance_acronyms
WORD_TOKEN_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?|[A-Za-z]+(?:[-'][A-Za-z]+)*")

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



def _clean_overlay_copy(text: str, sentence_case: bool = False) -> str:
    cleaned = _fix_finance_acronyms(_clean_text(text))
    cleaned = re.sub(r"\s+([,.!?])", r"\1", cleaned)
    if sentence_case and cleaned and cleaned[0].isalpha() and cleaned.upper() != cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def _caption_display_word(word: str, capitalize: bool = False) -> str:
    display = _fix_finance_acronyms(str(word or ""))
    if capitalize and display and display[0].isalpha() and display.upper() != display:
        display = display[0].upper() + display[1:]
    return display


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
    # Remove stage markers like [PAUSE] so caption indexing matches spoken audio.
    cleaned = re.sub(r"\[[^\]]+\]", " ", str(script_text or ""))
    return WORD_TOKEN_RE.findall(cleaned)


def _sentence_end_indices(script_text: str) -> set[int]:
    """
    Return the set of word indices after which a sentence break occurs.
    Breaks are: [PAUSE] markers and sentence-ending punctuation (. ! ?) after a word.
    """
    text = str(script_text or "")
    # Mark sentence breaks before stripping markers
    text = re.sub(r"\[PAUSE\]", " __BRK__ ", text)
    text = re.sub(r"(?<=[A-Za-z0-9])[.!?]+(?=\s|$)", " __BRK__", text)
    # Remove all remaining bracket markers
    text = re.sub(r"\[[^\]]+\]", " ", text)
    word_idx = 0
    ends: set[int] = set()
    for token in text.split():
        if token == "__BRK__":
            if word_idx > 0:
                ends.add(word_idx - 1)
        elif WORD_TOKEN_RE.search(token):
            word_idx += 1
    return ends


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


def _caption_slice(
    words: list[str],
    active_idx: int,
    window: int = CAPTION_WINDOW_WORDS,
    sent_ends: "set[int] | None" = None,
) -> list[tuple[str, int]]:
    if not words or active_idx < 0:
        return []
    start = max(0, active_idx - 1)
    end = min(len(words), start + window)
    if end - start < min(4, len(words)):
        start = max(0, end - window)

    if sent_ends:
        # Clamp start: don't reach back into a prior sentence
        prev_break = max((b for b in sent_ends if b < active_idx), default=-1)
        start = max(start, prev_break + 1)
        # Clamp end: don't spill into the next sentence
        next_break = min((b for b in sent_ends if b >= active_idx), default=len(words) - 1)
        end = min(end, next_break + 1)  # include the sentence-final word

    return [(words[i], i) for i in range(start, end)]


def _make_spoken_caption_image(
    words: list[str],
    word_timestamps: list[float] | None,
    t_mid: float,
    y_ratio: float = 0.62,
    w: int = SHORT_W,
    h: int = SHORT_H,
    sent_ends: "set[int] | None" = None,
):
    """
    Word-synced caption strip. Highlights the active spoken word.
    """
    from PIL import Image, ImageDraw

    if not words:
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))

    if word_timestamps:
        active_idx = _active_word_idx(word_timestamps, t_mid)
        if active_idx is None:
            return Image.new("RGBA", (w, h), (0, 0, 0, 0))
    else:
        # Fallback when TTS alignment is unavailable: approximate active word from WPS.
        # Start captions one token later so word 0 is not highlighted before speech begins.
        active_idx = min(int(t_mid * WPS) - 1, len(words) - 1)

    token_slice = _caption_slice(words, active_idx, sent_ends=sent_ends)
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
    y0 = int(h * y_ratio)
    box_pad_x = 26
    box_pad_y = 18
    draw.rounded_rectangle(
        [(int(w * 0.06), y0 - box_pad_y), (int(w * 0.94), y0 + total_h + box_pad_y)],
        radius=18,
        fill=(0, 0, 0, 170),
    )

    y = y0
    for line in lines:
        display_line = [
            (_caption_display_word(word, capitalize=(idx_pos == 0)), idx)
            for idx_pos, (word, idx) in enumerate(line)
        ]
        line_text = " ".join(wd for wd, _ in display_line)
        line_w = _text_width(draw, line_text, font)
        x = (w - line_w) // 2
        for i, (word, idx) in enumerate(display_line):
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
    wm_text = os.environ.get("CHANNEL_BRAND_NAME", "ClearWealth")
    wm_w = _text_width(draw, wm_text, font)
    wm_x = (SHORT_W - wm_w) // 2
    wm_y = int(SHORT_H * 0.05)
    # Dark backing rect improves legibility on any background
    pad = 8
    wm_h = _text_height(draw, wm_text, font)
    draw.rounded_rectangle(
        [(wm_x - pad, wm_y - pad // 2), (wm_x + wm_w + pad, wm_y + wm_h + pad // 2)],
        radius=8, fill=(0, 0, 0, 108),
    )
    draw.text((wm_x, wm_y), wm_text, font=font, fill=(255, 255, 255, 180))
    return bg


def _build_background_frame(t_s: float, bg_img, bg_frame):
    """
    Backward-compatible helper used by tests.
    Produces a subtle animated pan on the gradient fallback so static frames
    don't look frozen when real video background is unavailable.
    """
    from PIL import Image

    if bg_frame is not None:
        return bg_frame.convert("RGBA").resize((SHORT_W, SHORT_H), Image.Resampling.BILINEAR)

    scale = 1.08
    src = bg_img.convert("RGBA").resize(
        (int(SHORT_W * scale), int(SHORT_H * scale)),
        Image.Resampling.BILINEAR,
    )
    max_x = max(0, src.width - SHORT_W)
    max_y = max(0, src.height - SHORT_H)
    x = int(max_x * ((math.sin(t_s * 0.63) + 1.0) / 2.0))
    y = int(max_y * ((math.cos(t_s * 0.47) + 1.0) / 2.0))
    return src.crop((x, y, x + SHORT_W, y + SHORT_H))


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
                        time.sleep(1)
            if downloaded:
                clips.append(dest)
                logger.info("Downloaded Pexels bg clip %d/%d: %s (query=%s)", len(clips), target_count, dest, query)
                if len(clips) >= target_count:
                    break
                per_query_downloads += 1
                if per_query_downloads >= BG_MAX_PER_QUERY:
                    break

    return clips


def _prepare_bg_video(raw_clip: Path, work_dir: Path, duration: float, tag: str = "0") -> Path:
    """
    Crop to 9:16, lightly darken, loop/trim to `duration`. Returns processed video path.
    We keep footage brighter than before to improve feed-stop visibility on mobile.
    """
    out = work_dir / f"bg_processed_{tag}.mp4"
    overscan_w = int(SHORT_W * 1.14)
    overscan_h = int(SHORT_H * 1.14)
    vf = (
        f"scale={overscan_w}:{overscan_h}:force_original_aspect_ratio=increase,"
        f"crop={SHORT_W}:{SHORT_H}:"
        f"'(in_w-{SHORT_W})/2 + (in_w-{SHORT_W})*0.32*sin(t*0.67)':"
        f"'(in_h-{SHORT_H})/2 + (in_h-{SHORT_H})*0.30*cos(t*0.54)',"
        f"eq=brightness={BG_BRIGHTNESS}:saturation={BG_SATURATION},"
        f"boxblur={BG_BLUR}"
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


def _build_bg_montage_plan(duration_s: float, source_count: int, seed_hint: str = "") -> list[tuple[float, float, int]]:
    """
    Build pacing plan tuned for Shorts retention:
    - guaranteed early first cut (<1s) so the opening does not feel static
    - fast cuts thereafter
    - avoid repeating a source in the last few shots
    Returns list of (start, end, source_idx).
    """
    if source_count <= 0:
        return []
    rng = random.Random(f"{seed_hint}:{duration_s:.2f}:{source_count}")
    t = 0.0
    recent: list[int] = []   # tracks last min(3, source_count-1) used indices
    avoid_n = min(3, max(1, source_count - 1))
    plan: list[tuple[float, float, int]] = []
    while t < duration_s:
        remaining = duration_s - t
        if not plan:
            shot = min(0.75, remaining)
        else:
            shot = min(rng.uniform(BG_MIN_CUT_S, BG_MAX_CUT_S), BG_ABS_MAX_SHOT_S, remaining)
        shot = max(0.35, shot)
        choices = list(range(source_count))
        for prev in recent[-avoid_n:]:
            if prev in choices and len(choices) > 1:
                choices.remove(prev)
        idx = rng.choice(choices)
        plan.append((round(t, 3), round(t + shot, 3), idx))
        t += shot
        recent.append(idx)
        if len(recent) > avoid_n:
            recent.pop(0)
    return plan


def _build_gradient_bg_video(bg_img, vo_duration: float, work_dir: Path) -> Path:
    """
    Generate a smooth 30fps animated background video from the gradient PIL image.

    Uses FFmpeg scale+crop with time-varying offsets to create a subtle Ken-Burns-style
    pan/zoom that matches the Pillow animation formula.  Falls back to a static gradient
    if the expression-based filter fails (older FFmpeg).
    """
    from PIL import Image

    # Save a 1.15× upscaled gradient so panning never reveals black borders.
    overscan_w = int(SHORT_W * 1.15)
    overscan_h = int(SHORT_H * 1.15)
    gradient_png = work_dir / "bg_gradient.png"
    bg_img.convert("RGB").resize(
        (overscan_w, overscan_h), Image.Resampling.BICUBIC
    ).save(str(gradient_png))

    bg_video = work_dir / "bg.mp4"
    pad_x = (overscan_w - SHORT_W) // 2   # e.g. ~81px at 1080
    pad_y = (overscan_h - SHORT_H) // 2   # e.g. ~144px at 1920
    # Oscillation stays inside the padding so the crop never clips.
    osc_x = int(pad_x * 0.75)
    osc_y = int(pad_y * 0.75)

    vf = (
        f"scale={overscan_w}:{overscan_h}:flags=bilinear,"
        f"crop={SHORT_W}:{SHORT_H}:"
        f"'{pad_x}+sin(t*0.31)*{osc_x}':"
        f"'{pad_y}+cos(t*0.27)*{osc_y}'"
    )
    cmd = [
        _bin("ffmpeg"), "-y",
        "-loop", "1", "-r", "30", "-i", str(gradient_png),
        "-vf", vf,
        "-t", f"{vo_duration + 1.0:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(bg_video),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        logger.warning(
            "Animated gradient bg failed (%s) — using static fallback",
            result.stderr[-150:].strip(),
        )
        cmd_static = [
            _bin("ffmpeg"), "-y",
            "-loop", "1", "-r", "30", "-i", str(gradient_png),
            "-vf", f"scale={SHORT_W}:{SHORT_H}",
            "-t", f"{vo_duration + 1.0:.3f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            str(bg_video),
        ]
        result2 = subprocess.run(cmd_static, capture_output=True, text=True, timeout=60)
        if result2.returncode != 0:
            raise RuntimeError(f"Static gradient bg also failed: {result2.stderr[-300:]}")
    logger.info("Gradient bg video: %s", bg_video.name)
    return bg_video


def _build_montage_bg_video(
    processed_clips: list[Path],
    montage_plan: list[tuple[float, float, int]],
    vo_duration: float,
    work_dir: Path,
) -> Path:
    """
    Concatenate processed Pexels clips into a single smooth background video
    according to the montage plan (cut-style editing, no frame extraction needed).
    """
    seg_dir = work_dir / "bg_segs"
    if seg_dir.exists():
        shutil.rmtree(seg_dir, ignore_errors=True)
    seg_dir.mkdir(exist_ok=True)

    seg_paths: list[Path] = []
    for i, (seg_start, seg_end, src_idx) in enumerate(montage_plan):
        seg_dur = round(seg_end - seg_start, 3)
        if seg_dur < 0.1:
            continue
        src = processed_clips[min(src_idx, len(processed_clips) - 1)]
        seg_path = seg_dir / f"bg_seg_{i:03d}.mp4"
        cmd = [
            _bin("ffmpeg"), "-y",
            "-ss", f"{seg_start:.3f}",
            "-i", str(src),
            "-t", f"{seg_dur:.3f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-an",
            str(seg_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.warning("bg_seg %d failed, skipping: %s", i, result.stderr[-80:].strip())
            continue
        seg_paths.append(seg_path)

    if not seg_paths:
        raise RuntimeError("No background segments generated from montage plan")

    concat_txt = seg_dir / "bg_concat.txt"
    concat_txt.write_text("\n".join(f"file '{p.resolve()}'" for p in seg_paths))

    bg_video = work_dir / "bg.mp4"
    cmd_cat = [
        _bin("ffmpeg"), "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_txt),
        "-c:v", "copy",
        str(bg_video),
    ]
    result_cat = subprocess.run(cmd_cat, capture_output=True, text=True, timeout=120)
    if result_cat.returncode != 0:
        raise RuntimeError(f"Montage concat failed: {result_cat.stderr[-300:]}")
    logger.info("Montage bg video: %s (%d shots)", bg_video.name, len(seg_paths))
    return bg_video


# ---------------------------------------------------------------------------
# Overlay card renderers
# ---------------------------------------------------------------------------

def _label_card_height(text: str, w: int = SHORT_W) -> int:
    """Return the pixel height of the card that _make_overlay_image would render for a label."""
    from PIL import Image, ImageDraw
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    # Parameters must match _make_overlay_image's label branch exactly.
    lines, font = _wrap_fit_lines(draw, text, max_width=int(w * 0.76), start_size=60, min_size=32, max_lines=2)
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
        y0 = label_y0 if label_y0 is not None else int(h * 0.58)
        card_w = int(w * 0.82)
        x0 = (w - card_w) // 2
        draw.rounded_rectangle(
            [(x0, y0), (x0 + card_w, y0 + block_h + pad * 2)], radius=16, fill=(255, 255, 255, 28)
        )
        accent_idx = (sum(ord(ch) for ch in text) % len(LABEL_ACCENT_COLORS)) if text else 2
        accent = LABEL_ACCENT_COLORS[accent_idx]
        _draw_multiline_centered(draw, lines, y0 + pad, font, accent, w, gap=8)

    elif otype == "proof_tag":
        text = _clean_text(overlay.get("text", "SOURCE"))
        if overlay.get("plain_text", False):
            lines, font = _wrap_fit_lines(
                draw,
                text[:96],
                max_width=int(w * 0.90),
                start_size=42,
                min_size=30,
                max_lines=2,
            )
            block_h = sum(_text_height(draw, ln, font) for ln in lines) + 8 * max(0, len(lines) - 1)
            pad = 16
            card_w = int(w * 0.92)
            x0 = (w - card_w) // 2
            y0 = int(h * 0.10)
            draw.rounded_rectangle(
                [(x0, y0), (x0 + card_w, y0 + block_h + pad * 2)],
                radius=14,
                fill=(0, 0, 0, 205),
            )
            _draw_multiline_centered(draw, lines, y0 + pad, font, (232, 240, 255, 248), w, gap=8)
            return img
        else:
            label = f"SOURCE: {text}"[:74]
        font = _get_font(32)
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
        card_w = int(w * 0.90)
        card_h = int(h * 0.30)
        x0 = (w - card_w) // 2
        y0 = int(h * 0.34)
        x1 = x0 + card_w
        y1 = y0 + card_h
        mid = x0 + card_w // 2

        draw.rounded_rectangle([(x0, y0), (x1, y1)], radius=20, fill=(0, 0, 0, 205))
        draw.line([(mid, y0 + 18), (mid, y1 - 18)], fill=(115, 115, 115, 205), width=3)
        draw.line([(x0 + 18, y0 + int(card_h * 0.30)), (x1 - 18, y0 + int(card_h * 0.30))], fill=(115, 115, 115, 180), width=2)

        hfont = _get_font(42)
        draw.text((x0 + int(card_w * 0.12), y0 + 20), "BEFORE", font=hfont, fill=(255, 92, 92, 255))
        draw.text((x0 + int(card_w * 0.62), y0 + 20), "AFTER", font=hfont, fill=(96, 230, 130, 255))

        body_y = y0 + int(card_h * 0.34)
        left_lines, left_font = _wrap_fit_lines(draw, left.replace("\n", " "), int(card_w * 0.40), 46, 28, max_lines=3)
        right_lines, right_font = _wrap_fit_lines(draw, right.replace("\n", " "), int(card_w * 0.40), 46, 28, max_lines=3)

        for i, line in enumerate(left_lines[:3]):
            draw.text((x0 + int(card_w * 0.06), body_y + i * 58), line, font=left_font, fill=(245, 245, 245, 255))
        for i, line in enumerate(right_lines[:3]):
            draw.text((x0 + int(card_w * 0.56), body_y + i * 58), line, font=right_font, fill=(245, 245, 245, 255))

    elif otype == "timeline":
        # 3-column progression table: NOW / 5 YEARS / 20 YEARS with dollar values.
        # Most screenshot-worthy format — viewers share to show the compounding math.
        labels = [
            overlay.get("col1_label", "NOW"),
            overlay.get("col2_label", "5 YEARS"),
            overlay.get("col3_label", "20 YEARS"),
        ]
        values = [
            overlay.get("col1_value", "—"),
            overlay.get("col2_value", "—"),
            overlay.get("col3_value", "—"),
        ]
        card_w = int(w * 0.90)
        card_h = int(h * 0.22)
        x0 = (w - card_w) // 2
        y0 = int(h * 0.38)
        x1 = x0 + card_w
        y1 = y0 + card_h
        col_w = card_w // 3

        draw.rounded_rectangle([(x0, y0), (x1, y1)], radius=20, fill=(0, 0, 0, 215))
        # Vertical dividers
        for div in (1, 2):
            div_x = x0 + col_w * div
            draw.line([(div_x, y0 + 16), (div_x, y1 - 16)], fill=(90, 90, 90, 200), width=2)

        label_font = _get_font(34)
        value_font_start = 72
        value_font_min = 38

        for i, (lbl, val) in enumerate(zip(labels, values)):
            col_x0 = x0 + col_w * i
            col_cx = col_x0 + col_w // 2
            # Label (small, grey, top of column)
            lbl_w = _text_width(draw, lbl, label_font)
            lbl_x = col_cx - lbl_w // 2
            lbl_y = y0 + 18
            draw.text((lbl_x + 1, lbl_y + 1), lbl, font=label_font, fill=(0, 0, 0, 160))
            draw.text((lbl_x, lbl_y), lbl, font=label_font, fill=(180, 180, 180, 230))
            # Value (large, yellow, bottom of column)
            val_lines, val_font = _wrap_fit_lines(
                draw, val, max_width=int(col_w * 0.88),
                start_size=value_font_start, min_size=value_font_min, max_lines=2,
            )
            val_block_h = sum(_text_height(draw, ln, val_font) for ln in val_lines) + 4 * max(0, len(val_lines) - 1)
            val_y = y0 + int(card_h * 0.46) + (int(card_h * 0.46) - val_block_h) // 2
            for vi, vline in enumerate(val_lines):
                vline_w = _text_width(draw, vline, val_font)
                vline_x = col_cx - vline_w // 2
                draw.text((vline_x + 2, val_y + 2), vline, font=val_font, fill=(0, 0, 0, 180))
                draw.text((vline_x, val_y), vline, font=val_font, fill=(255, 220, 50, 255))
                val_y += _text_height(draw, vline, val_font) + 4

    elif otype == "cta":
        text = _clean_text(overlay.get("text", "Follow for more"))
        lines, font = _wrap_fit_lines(draw, text, max_width=int(w * 0.80), start_size=58, min_size=32, max_lines=2)
        block_h = sum(_text_height(draw, ln, font) for ln in lines) + 8 * max(0, len(lines) - 1)
        pad = 24
        y0 = int(h * 0.66)
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

# Per-thread flags for log deduplication — thread-safe across concurrent render() calls.
_render_local = threading.local()


def _get_wps_warned() -> bool:
    return getattr(_render_local, "wps_fallback_warned", False)


def _set_wps_warned(v: bool) -> None:
    _render_local.wps_fallback_warned = v


def _get_caption_warned() -> bool:
    return getattr(_render_local, "caption_fallback_warned", False)


def _set_caption_warned(v: bool) -> None:
    _render_local.caption_fallback_warned = v


def _ov_start(ov: dict) -> float:
    """Return overlay start time. Uses ElevenLabs word timestamp when available."""
    if "start_time_s" in ov:
        return float(ov["start_time_s"])
    start_word = int(ov.get("start_word", 0))
    if not _get_wps_warned():
        logger.debug(
            "One or more overlays using WPS fallback for timing — "
            "ensure word_timestamps are available for precise sync"
        )
        _set_wps_warned(True)
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
            ov_mid = (_ov_start(ov) + _ov_end(ov)) / 2
            proof_start = round(max(_ov_start(ov) + 0.2, ov_mid - 0.8), 3)
            # Clamp duration so proof tag never overruns the video end.
            proof_dur = round(min(1.6, max(0.1, duration_s - proof_start)), 3)
            if proof_start < duration_s - 0.2:
                result.append({
                    "type": "proof_tag",
                    "text": stat_citations[cite_idx],
                    "start_time_s": proof_start,
                    "start_word": ov.get("start_word", 0),
                    "duration_s": proof_dur,
                })
                cite_idx += 1
    return result


def _inject_hook_interrupt(overlays: list[dict], duration_s: float, pillar: str = "") -> list[dict]:
    """
    Ensure at least one non-hook visual event lands in the first second.
    This lowers early swipe risk when only a static hook card is present.
    """
    if duration_s <= 1.2:
        return overlays
    result = list(overlays)
    hook = next(
        (ov for ov in result if ov.get("type") == "hook_number" and _ov_start(ov) <= 0.35),
        None,
    )
    if not hook:
        return result
    has_early_non_hook = any(
        ov.get("type") != "hook_number" and _ov_start(ov) <= 0.9
        for ov in result
    )
    if has_early_non_hook:
        return result
    labels = _PILLAR_CADENCE_LABELS.get(pillar, _DEFAULT_CADENCE_LABELS)
    text = labels[0] if labels else "WATCH THIS"
    start = min(max(0.35, _ov_start(hook) + HOOK_INTERRUPT_AT_S), duration_s - 0.9)
    dur = min(HOOK_INTERRUPT_DURATION_S, max(0.8, duration_s - start - 0.1))
    result.append(
        {
            "type": "label",
            "text": text,
            "start_time_s": round(start, 3),
            "duration_s": round(dur, 2),
        }
    )
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


def _has_existing_finance_disclaimer(overlays: list[dict]) -> bool:
    for ov in overlays:
        if ov.get("type") != "proof_tag":
            continue
        text = _clean_text(ov.get("text", "")).lower()
        if "not advice" in text or "not financial advice" in text:
            return True
    return False


def _needs_financial_disclaimer(overlays: list[dict], script_data: dict) -> bool:
    pillar = _clean_text(script_data.get("pillar", "")).lower()
    if pillar in FINANCE_PILLARS:
        return True

    corpus_parts: list[str] = []
    for ov in overlays:
        corpus_parts.extend([
            _clean_text(ov.get("text", "")),
            _clean_text(ov.get("left", "")),
            _clean_text(ov.get("right", "")),
            _clean_text(ov.get("subtitle", "")),
            _clean_text(ov.get("col1_value", "")),
            _clean_text(ov.get("col2_value", "")),
            _clean_text(ov.get("col3_value", "")),
        ])
    corpus_parts.extend([
        _clean_text(script_data.get("voiceover_script", "")),
        _clean_text(script_data.get("description", "")),
    ])
    corpus = " ".join(p for p in corpus_parts if p)
    return bool(FINANCIAL_SIGNAL_RE.search(corpus) or ADVICE_SIGNAL_RE.search(corpus))


def _sanitize_overlays(
    overlays: list, duration_s: float, word_timestamps: list[float] | None = None
) -> list[dict]:
    safe = []
    for ov in overlays:
        kind = str((ov or {}).get("type", "")).strip()
        if kind not in {"hook_number", "label", "comparison", "timeline", "cta"}:
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
        dur = min(dur, max(0.1, round(duration_s - start_time_s - 0.05, 2)))
        cleaned = dict(ov)
        cleaned["type"] = kind
        cleaned["start_word"] = start_word
        cleaned["start_time_s"] = round(start_time_s, 3)
        cleaned["duration_s"] = dur
        if kind == "hook_number":
            cleaned["text"] = _clean_overlay_copy(cleaned.get("text", ""))
            if "subtitle" in cleaned:
                cleaned["subtitle"] = _clean_overlay_copy(cleaned.get("subtitle", ""), sentence_case=True)
        elif kind == "label":
            cleaned["text"] = _clean_overlay_copy(cleaned.get("text", ""))
        elif kind == "comparison":
            cleaned["left"] = _clean_overlay_copy(cleaned.get("left", ""), sentence_case=True)
            cleaned["right"] = _clean_overlay_copy(cleaned.get("right", ""), sentence_case=True)
        elif kind == "timeline":
            for col in ("col1_label", "col2_label", "col3_label"):
                cleaned[col] = _clean_overlay_copy(cleaned.get(col, "—")).upper()
            for col in ("col1_value", "col2_value", "col3_value"):
                cleaned[col] = _clean_overlay_copy(cleaned.get(col, "—"), sentence_case=False)
        elif kind == "cta":
            cleaned["text"] = _clean_overlay_copy(cleaned.get("text", ""), sentence_case=True)
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
        text_up = _clean_text(label.get("text", "")).upper()

        if start >= tail_cutoff:
            # Keep semantic (non-generic) labels by moving them a bit earlier;
            # drop generic cadence labels near CTA to reduce clutter.
            if text_up in GENERIC_LABEL_TEXTS:
                continue
            start = max(next_free, tail_cutoff - min(1.2, dur))

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
        if label_idx >= MAX_CADENCE_LABELS:
            break

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
    else:
        logger.warning(
            "cadence label injection hit 200-iteration cap — "
            "some visual gaps > %.1fs may remain unfilled",
            MAX_VISUAL_GAP_S,
        )

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


def _freeze_durations(path: Path) -> list[float]:
    result = subprocess.run(
        [
            _bin("ffmpeg"),
            "-hide_banner",
            "-i",
            str(path),
            "-vf",
            f"freezedetect=n=0.003:d={FREEZE_WARN_MIN_S}",
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    text = (result.stderr or "") + "\n" + (result.stdout or "")
    return [float(m.group(1)) for m in re.finditer(r"freeze_duration:\s*([0-9.]+)", text)]


def _scene_change_times(path: Path) -> list[float]:
    result = subprocess.run(
        [
            _bin("ffmpeg"),
            "-hide_banner",
            "-i",
            str(path),
            "-filter:v",
            "select='gt(scene,0.25)',metadata=print",
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    text = (result.stderr or "") + "\n" + (result.stdout or "")
    return [float(m.group(1)) for m in re.finditer(r"pts_time:([0-9.]+)", text)]


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
    if abs(duration - expected_duration) > 1.5:
        raise RuntimeError(f"Duration drift: rendered {duration:.1f}s vs voiceover {expected_duration:.1f}s")
    mean_db, max_db = _mean_volume_db(output_path)
    # Both checks are warnings — loudnorm can undershoot 2–4 dB on short clips
    # and hard-crashing a good render would be worse than a slightly quiet video.
    if mean_db is not None and mean_db < -19.5:
        logger.warning(
            "Audio mean %.1f dB below -19.5 floor after loudnorm — check TTS output quality",
            mean_db,
        )
    # YouTube loudness normalization flattens clipped/over-compressed audio.
    if max_db is not None and max_db > MIX_TRUE_PEAK_TARGET:
        logger.warning(
            "Audio peak %.1f dB exceeds -3 dBTP — may sound flat after YouTube loudness normalization",
            max_db,
        )
    freeze_durations = _freeze_durations(output_path)
    if freeze_durations:
        freeze_total = sum(freeze_durations)
        if freeze_total > FREEZE_WARN_TOTAL_S:
            logger.warning(
                "Freeze detector found %.1fs of near-static footage (threshold %.1fs) — "
                "consider swapping slow-pan b-roll clips for more motion-rich alternatives",
                freeze_total,
                FREEZE_WARN_TOTAL_S,
            )
    scene_times = _scene_change_times(output_path)
    if scene_times and scene_times[0] > HOOK_SCENE_DEADLINE_S:
        logger.warning(
            "First detected scene change at %.2fs (>%.2fs) — add a faster opening pattern interrupt",
            scene_times[0],
            HOOK_SCENE_DEADLINE_S,
        )


def _get_voiceover_duration(path: Path) -> float:
    result = subprocess.run(
        [_bin("ffprobe"), "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def _compute_voiceover_autofit_rate(duration_s: float) -> float | None:
    """
    Return an atempo multiplier to pull duration into target range, or None when
    no safe auto-fit should be attempted.
    """
    if SHORT_MIN_DURATION_S <= duration_s <= SHORT_MAX_DURATION_S:
        return None
    if duration_s <= 0:
        return None

    margin = max(0.0, SHORT_AUTOFIT_TARGET_MARGIN_S)
    if duration_s > SHORT_MAX_DURATION_S:
        target_duration = max(SHORT_MIN_DURATION_S + 0.1, SHORT_MAX_DURATION_S - margin)
    else:
        target_duration = min(SHORT_MAX_DURATION_S - 0.1, SHORT_MIN_DURATION_S + margin)

    if target_duration <= 0:
        return None

    rate = duration_s / target_duration
    if rate < SHORT_AUTOFIT_MIN_RATE or rate > SHORT_AUTOFIT_MAX_RATE:
        return None
    return rate


def _atempo_filter_chain(rate: float) -> str:
    """
    Build an FFmpeg atempo chain for a combined speed multiplier.
    """
    rate = float(rate)
    if rate <= 0:
        raise ValueError("atempo rate must be > 0")
    factors: list[float] = []
    while rate > 2.0:
        factors.append(2.0)
        rate /= 2.0
    while rate < 0.5:
        factors.append(0.5)
        rate /= 0.5
    factors.append(rate)
    return ",".join(f"atempo={factor:.5f}" for factor in factors)


def _retime_word_timestamps(word_timestamps: list[float], speed_rate: float) -> list[float]:
    if not word_timestamps:
        return []
    if speed_rate <= 0:
        return list(word_timestamps)
    return [max(0.0, float(ts) / speed_rate) for ts in word_timestamps]


def _autofit_voiceover_duration(
    voiceover_path: Path,
    vo_duration: float,
    word_timestamps: list[float],
    work_dir: Path,
) -> tuple[Path, float, list[float]]:
    """
    Auto-fit minor duration misses by applying atempo to voiceover and retiming
    word timestamps. Returns the possibly-updated voiceover path/duration/timestamps.
    """
    if not SHORT_AUTOFIT_VOICEOVER:
        return voiceover_path, vo_duration, word_timestamps

    rate = _compute_voiceover_autofit_rate(vo_duration)
    if rate is None:
        return voiceover_path, vo_duration, word_timestamps

    adjusted_voiceover = work_dir / f"{voiceover_path.stem}_autofit{voiceover_path.suffix or '.mp3'}"
    filter_chain = _atempo_filter_chain(rate)
    cmd = [
        _bin("ffmpeg"),
        "-y",
        "-i",
        str(voiceover_path),
        "-vn",
        "-filter:a",
        filter_chain,
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(adjusted_voiceover),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        logger.warning(
            "Voiceover auto-fit failed (rate %.3fx): %s",
            rate,
            result.stderr[-400:] if result.stderr else "unknown ffmpeg error",
        )
        return voiceover_path, vo_duration, word_timestamps

    new_duration = _get_voiceover_duration(adjusted_voiceover)
    new_timestamps = _retime_word_timestamps(word_timestamps, rate)
    logger.info(
        "Auto-fit voiceover duration %.1fs -> %.1fs (atempo %.3fx)",
        vo_duration,
        new_duration,
        rate,
    )
    return adjusted_voiceover, new_duration, new_timestamps


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

    Strategy: smooth bg video + transparent overlay layer
    ──────────────────────────────────────────────────────
    1. Build smooth 30fps background video:
       - Pexels available → montage of processed clips (real footage, no frame extraction).
       - Fallback → animated gradient via FFmpeg scale+crop with time-varying pan offset.
    2. Sanitize script overlays; inject cadence labels into visual gaps.
    3. Compute segment boundaries from overlay start/end times + BG_CADENCE_S ticks.
    4. For each segment, render OVERLAY-ONLY transparent RGBA PNGs (no background baked in).
    5. FFmpeg composites overlay PNG sequence onto the smooth bg video via overlay filter,
       giving true 30fps motion rather than the old 2fps slideshow effect.
    6. Mix voiceover + optional sidechain-ducked background music with loudnorm.
    """
    # Reset per-thread log-dedup flags so each render() call gets at most one warning.
    _set_wps_warned(False)
    _set_caption_warned(False)

    if not voiceover_path.exists():
        raise FileNotFoundError(f"Voiceover not found: {voiceover_path}")

    work_dir = WORKSPACE / "short_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    word_timestamps: list[float] = list(script_data.get("word_timestamps") or [])
    vo_duration = _get_voiceover_duration(voiceover_path)
    voiceover_path, vo_duration, word_timestamps = _autofit_voiceover_duration(
        voiceover_path,
        vo_duration,
        word_timestamps,
        work_dir,
    )
    if word_timestamps:
        script_data["word_timestamps"] = word_timestamps
    logger.info("Short voiceover duration: %.1fs", vo_duration)
    if not (SHORT_MIN_DURATION_S <= vo_duration <= SHORT_MAX_DURATION_S):
        raise RuntimeError(
            f"Voiceover duration {vo_duration:.1f}s is outside the "
            f"{SHORT_MIN_DURATION_S:.0f}–{SHORT_MAX_DURATION_S:.0f}s target range. "
            "Check the script word count or TTS speed settings."
        )

    pillar = script_data.get("pillar", "investing")
    topic_slug = re.sub(r"[^a-z0-9]+", "-", script_data.get("topic", pillar).lower()).strip("-")
    bg_img = _make_background_image(pillar)

    # Pexels background video — smooth montage (real 30fps footage, no frame extraction)
    bg_video: Path | None = None
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
                processed_clips: list[Path] = []
                for ci, clip in enumerate(raw_clips):
                    processed = _prepare_bg_video(clip, work_dir, vo_duration, tag=str(ci))
                    processed_clips.append(processed)
                montage_plan = _build_bg_montage_plan(
                    vo_duration, len(processed_clips), seed_hint=topic_slug
                )
                bg_video = _build_montage_bg_video(
                    processed_clips, montage_plan, vo_duration, work_dir
                )
                logger.info(
                    "Background montage ready: %d clips, %d shots",
                    len(processed_clips), len(montage_plan),
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
            bg_video = None

    # Gradient animated background when Pexels is unavailable or failed
    if bg_video is None:
        bg_video = _build_gradient_bg_video(bg_img, vo_duration, work_dir)

    if word_timestamps:
        logger.info("Using ElevenLabs word timestamps (%d words)", len(word_timestamps))
    else:
        logger.warning(
            "Word timestamps unavailable — using WPS timing fallback for overlays "
            "(captions will be less precise)."
        )

    # Step 1: clean overlays from script
    overlays = _sanitize_overlays(script_data.get("overlays", []), vo_duration, word_timestamps)
    overlays = _inject_hook_interrupt(overlays, vo_duration, pillar=pillar)

    # Step 2: inject cadence labels into actual visual gaps (pillar-specific copy)
    _overlays_before_cadence = len(overlays)
    overlays = _inject_cadence_labels(overlays, vo_duration, pillar=pillar)
    overlays = _sanitize_overlays(overlays, vo_duration, word_timestamps)  # re-sort + re-clamp after injection
    overlays = _deoverlap_label_overlays(overlays, vo_duration)
    _overlays_after_cadence = len(overlays)
    logger.info(
        "Overlays: %d from script → +%d cadence injected → %d after deoverlap",
        _overlays_before_cadence,
        _overlays_after_cadence - _overlays_before_cadence,
        _overlays_after_cadence,
    )

    # Step 3: inject proof tags AFTER sanitize (proof_tag is not in the sanitize allowlist)
    stat_citations = script_data.get("stat_citations") or []
    overlays = _inject_proof_tags(overlays, stat_citations, vo_duration)

    # Step 3b: inject on-screen "Educational only. Not financial advice." disclaimer.
    # Use a robust finance-signal check (not only '$' or '%') so advisory claims
    # without explicit symbols still get a disclosure card.
    has_financial_claim = _needs_financial_disclaimer(overlays, script_data)
    if has_financial_claim and not _has_existing_finance_disclaimer(overlays):
        # Place disclaimer before the actual CTA start so they never co-render.
        # Use the real CTA start from the overlay list (not an estimate) so that
        # early-placed CTAs don't cause the disclaimer to overlap them.
        cta_starts = [_ov_start(ov) for ov in overlays if ov.get("type") == "cta"]
        cta_start_approx = min(cta_starts) if cta_starts else max(0.0, vo_duration - CTA_SAFE_TAIL_S)
        disclaimer_end_target = cta_start_approx - 0.3
        disclaimer_dur = 2.0
        disclaimer_start = max(0.0, round(disclaimer_end_target - disclaimer_dur, 2))
        overlays.append({
            "type": "proof_tag",
            "text": "Educational only. Not financial advice.",
            "plain_text": True,
            "start_time_s": disclaimer_start,
            "duration_s": disclaimer_dur,
        })
        logger.info("Injected on-screen financial disclaimer at %.1fs (ends before CTA at %.1fs)",
                    disclaimer_start, cta_start_approx)

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

    # Step 5: render OVERLAY-ONLY transparent PNGs (background supplied by bg_video)
    # Each PNG is pure RGBA with a fully transparent background. The smooth bg_video
    # (real footage or animated gradient) is composited on top via FFmpeg overlay,
    # eliminating the slideshow effect that plagued the old per-segment static frame approach.
    spoken_words_list = _spoken_words(script_data.get("voiceover_script", ""))
    sent_ends = _sentence_end_indices(script_data.get("voiceover_script", ""))
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

        # Transparent frame: overlays only, no background (background comes from bg_video)
        frame = Image.new("RGBA", (SHORT_W, SHORT_H), (0, 0, 0, 0))
        label_next_y = int(SHORT_H * 0.58)
        for ov in active:
            if ov.get("type") == "label":
                frame = Image.alpha_composite(frame, _make_overlay_image(ov, label_y0=label_next_y))
                label_next_y += _label_card_height(_clean_text(ov.get("text", "")).upper()) + 8
            else:
                frame = Image.alpha_composite(frame, _make_overlay_image(ov))

        # Word-synced spoken captions (phrase-by-phrase highlight, active word in yellow)
        has_active_cta = any(ov.get("type") == "cta" for ov in active)
        has_active_label = bool(active_labels)
        if spoken_words_list and not has_active_cta:
            if not word_timestamps and not _get_caption_warned():
                logger.warning(
                    "Word-synced caption timestamps unavailable — using WPS caption fallback."
                )
                _set_caption_warned(True)
            # Keep captions clear of active label cards.
            caption_y = 0.68 if has_active_label else 0.62
            caption_img = _make_spoken_caption_image(
                spoken_words_list,
                word_timestamps,
                t_mid,
                y_ratio=caption_y,
                sent_ends=sent_ends,
            )
            frame = Image.alpha_composite(frame, caption_img)

        seg_path = seg_dir / f"seg_{i:03d}.png"
        frame.save(seg_path)   # keep RGBA — alpha channel is composited by FFmpeg overlay
        concat_lines.append(f"file '{seg_path.resolve()}'\nduration {duration:.4f}")

    # FFmpeg concat demuxer: last entry repeated without a duration line
    if concat_lines:
        last_seg = seg_dir / f"seg_{len(events_sorted) - 2:03d}.png"
        concat_lines.append(f"file '{last_seg.resolve()}'")

    concat_file = work_dir / "segments.txt"
    concat_file.write_text("\n".join(concat_lines))
    logger.info("Composited %d overlay segment frames via Pillow", len(events_sorted) - 1)

    # Step 6: FFmpeg encode
    # Input layout:
    #   0 — bg_video      (smooth 30fps background: Pexels montage or animated gradient)
    #   1 — overlay PNGs  (transparent RGBA PNG sequence from concat demuxer)
    #   2 — voiceover     (audio)
    #   3 — bgmusic       (audio, optional)
    #
    # filter_complex:
    #   [0:v][1:v]overlay  → composites overlay onto smooth background (fixes slideshow)
    #   audio chain        → voiceover + optional sidechain-ducked music + loudnorm
    music_enabled = os.environ.get("SHORT_MUSIC", "1").lower() in {"1", "true", "yes"}
    if bgmusic_path.exists() and music_enabled:
        # voiceover=input 2, bgmusic=input 3
        audio_filter = (
            "[2:a]highpass=f=100,lowpass=f=12000,"
            "acompressor=threshold=-17dB:ratio=2.2:attack=15:release=180,"
            "volume=1.05,asplit=2[voice_main][voice_sc];"
            "[3:a]volume=0.11[raw_music];"
            "[raw_music][voice_sc]sidechaincompress=threshold=0.015:ratio=6:attack=5:release=200[music_ducked];"
            "[voice_main][music_ducked]amix=inputs=2:duration=first[mix];"
            f"[mix]loudnorm=I={TARGET_LOUDNESS}:TP={MIX_TRUE_PEAK_TARGET}:LRA=7[a]"
        )
        audio_inputs = ["-i", str(voiceover_path), "-i", str(bgmusic_path)]
    else:
        # voiceover=input 2
        audio_filter = (
            "[2:a]highpass=f=100,lowpass=f=12000,"
            "acompressor=threshold=-17dB:ratio=2.2:attack=15:release=180:makeup=3,"
            f"loudnorm=I={TARGET_LOUDNESS}:TP={MIX_TRUE_PEAK_TARGET}:LRA=7[a]"
        )
        audio_inputs = ["-i", str(voiceover_path)]

    full_filter = (
        f"[0:v]fps=30,setsar=1[bg_fps];"
        f"[bg_fps][1:v]overlay=format=auto:shortest=1[v_base];"
        "[v_base]eq=brightness=0.03:saturation=1.08,unsharp=5:5:0.55:5:5:0.0[v];"
        f"{audio_filter}"
    )

    cmd = [
        _bin("ffmpeg"), "-y",
        "-i", str(bg_video),                                       # input 0: smooth bg
        "-f", "concat", "-safe", "0", "-i", str(concat_file),     # input 1: overlay PNGs
        *audio_inputs,                                              # inputs 2 (+ 3)
        "-filter_complex", full_filter,
        "-map", "[v]",
        "-map", "[a]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-g", "60",
        "-b:v", MIN_VIDEO_BITRATE,
        "-maxrate", MAX_VIDEO_BITRATE,
        "-bufsize", VIDEO_BUF_SIZE,
        "-c:a", "aac", "-b:a", "160k", "-ac", "2",
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
        # bg_segs/ created by _build_montage_bg_video
        shutil.rmtree(work_dir / "bg_segs", ignore_errors=True)
        for clip_file in work_dir.glob("bg_raw_*.mp4"):
            clip_file.unlink(missing_ok=True)
        for clip_file in work_dir.glob("bg_processed_*.mp4"):
            clip_file.unlink(missing_ok=True)
        for f in work_dir.glob("bg*.mp4"):
            f.unlink(missing_ok=True)
        for f in work_dir.glob("bg*.png"):
            f.unlink(missing_ok=True)
        concat_file.unlink(missing_ok=True)
        logger.debug("Cleaned up segment frames and bg temp files: %s", work_dir)

    if not output_path.exists() or output_path.stat().st_size < 50_000:
        raise RuntimeError(f"Short output missing or too small: {output_path}")

    _quality_gate(output_path, vo_duration)

    logger.info("Short rendered: %s (%.1f MB)", output_path, output_path.stat().st_size / 1_048_576)
    return output_path
