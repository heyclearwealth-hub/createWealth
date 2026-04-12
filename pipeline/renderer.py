"""
renderer.py — FFmpeg two-pass video renderer.
Pass 1: normalize each clip to 1280x720 / 30fps / H.264 / no audio
Pass 2: concat normalized clips + mix voiceover + bgmusic
"""
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Resolve ffmpeg/ffprobe — Homebrew on Apple Silicon installs to /opt/homebrew/bin
# which may not be in PATH when invoked by CI or system Python.
def _bin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        candidate = Path(prefix) / name
        if candidate.exists():
            return str(candidate)
    return name  # fall back and let subprocess raise a clear error

WORKSPACE = Path("workspace")
WPS = 2.5  # words per second (150 wpm voiceover)
OUTPUT_PATH = WORKSPACE / "output" / "final_video.mp4"
BGMUSIC_PATH = Path("pipeline/assets/bgmusic.mp3")
VOICEOVER_PATH = WORKSPACE / "voiceover.mp3"
NORM_DIR = WORKSPACE / "norm"

NORM_WIDTH = 1280
NORM_HEIGHT = 720
NORM_FPS = 30


def _get_font(size: int):
    """Load a system font at the given size, falling back to Pillow's built-in."""
    from PIL import ImageFont
    candidates = [
        # macOS
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Arial Unicode.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        # Ubuntu / GitHub Actions
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


def _draw_text_centered(draw, text: str, y: int, font, fill, width: int, shadow: bool = True):
    """Draw text horizontally centered with optional drop shadow."""
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    x = (width - tw) // 2
    if shadow:
        draw.text((x + 3, y + 3), text, font=font, fill=(0, 0, 0, 200))
    draw.text((x, y), text, font=font, fill=fill)


def _make_overlay_image(overlay: dict, w: int = NORM_WIDTH, h: int = NORM_HEIGHT):
    """Render one overlay dict to a transparent RGBA PIL Image."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    otype = overlay.get("type")

    if otype == "title_card":
        lines = overlay.get("lines", [])
        # Semi-transparent dark band
        draw.rectangle([(0, int(h * 0.28)), (w, int(h * 0.62))], fill=(0, 0, 0, 170))
        y_pos = [int(h * 0.31), int(h * 0.42), int(h * 0.52)]
        sizes = [64, 38, 46]
        fills = [(255, 255, 255, 255), (220, 220, 220, 255), (255, 220, 50, 255)]
        for i, line in enumerate(lines[:3]):
            font = _get_font(sizes[i])
            _draw_text_centered(draw, line, y_pos[i], font, fills[i], w)

    elif otype == "stat":
        text = overlay.get("text", "")
        font = _get_font(96)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad = 24
        x0 = (w - tw) // 2 - pad
        y0 = int(h * 0.38)
        draw.rectangle([(x0, y0 - pad // 2), (x0 + tw + pad * 2, y0 + th + pad)], fill=(0, 0, 0, 180))
        _draw_text_centered(draw, text, y0, font, (255, 255, 255, 255), w)

    elif otype == "section":
        text = overlay.get("text", "").upper()
        font = _get_font(52)
        draw.rectangle([(0, int(h * 0.05)), (w, int(h * 0.14))], fill=(0, 0, 0, 160))
        _draw_text_centered(draw, text, int(h * 0.07), font, (255, 220, 50, 255), w)

    elif otype == "before_after":
        before_lines = [l for l in overlay.get("before", "").split("\n") if l.strip()]
        after_lines = [l for l in overlay.get("after", "").split("\n") if l.strip()]
        draw.rectangle([(0, int(h * 0.22)), (w, int(h * 0.78))], fill=(0, 0, 0, 170))
        # Divider
        draw.line([(w // 2, int(h * 0.24)), (w // 2, int(h * 0.76))], fill=(150, 150, 150, 200), width=2)
        # Headers
        hfont = _get_font(48)
        draw.text((int(w * 0.10), int(h * 0.25)), "BEFORE", font=hfont, fill=(255, 80, 80, 255))
        draw.text((int(w * 0.60), int(h * 0.25)), "AFTER", font=hfont, fill=(80, 220, 100, 255))
        # Values
        vfont = _get_font(36)
        for i, line in enumerate(before_lines[:3]):
            draw.text((int(w * 0.06), int(h * (0.38 + i * 0.11))), line, font=vfont, fill=(255, 255, 255, 255))
        for i, line in enumerate(after_lines[:3]):
            draw.text((int(w * 0.56), int(h * (0.38 + i * 0.11))), line, font=vfont, fill=(255, 255, 255, 255))

    return img


def _render_overlay_frames(text_overlays: list, work_dir: Path) -> list:
    """
    Generate a transparent PNG for each overlay using Pillow.
    Returns overlays list with 'frame_path' added to each entry.
    Falls back to empty list if Pillow is not installed.
    """
    try:
        from PIL import Image  # noqa: F401 — verify Pillow is available
    except ImportError:
        logger.warning("Pillow not installed — skipping text overlays. Run: pip install Pillow")
        return []

    overlay_dir = work_dir / "overlays"
    overlay_dir.mkdir(parents=True, exist_ok=True)

    result = []
    for i, overlay in enumerate(text_overlays):
        img = _make_overlay_image(overlay)
        frame_path = overlay_dir / f"overlay_{i:02d}.png"
        img.save(frame_path)
        result.append({**overlay, "frame_path": str(frame_path)})
        logger.debug("Rendered overlay %d: type=%s path=%s", i, overlay.get("type"), frame_path)

    logger.info("Rendered %d overlay frames", len(result))
    return result


def _build_overlay_filter_chain(overlays: list, vo_idx: int, bg_idx: int) -> tuple:
    """
    Build FFmpeg inputs list and filter_complex string for PNG overlay chain.
    Returns (extra_inputs, filter_complex, video_map_label).
    """
    extra_inputs = []
    for ov in overlays:
        extra_inputs.extend(["-loop", "1", "-i", str(ov["frame_path"])])

    chains = []
    prev = "0:v"
    for i, ov in enumerate(overlays):
        out = f"vout{i}" if i < len(overlays) - 1 else "vout"
        start = round(int(ov.get("start_word", 0)) / WPS, 2)
        end = round(start + float(ov.get("duration_s", 3.0)), 2)
        chains.append(
            f"[{prev}][{i + 1}:v]overlay=0:0:enable='between(t,{start},{end})'[{out}]"
        )
        prev = out

    audio = (
        f"[{vo_idx}:a]volume=1.0[voice];"
        f"[{bg_idx}:a]volume=0.15[music];"
        "[voice][music]amix=inputs=2:duration=first[a]"
    )
    filter_complex = ";".join(chains) + ";" + audio
    return extra_inputs, filter_complex, "[vout]"


def _ffprobe_duration(path: Path) -> float:
    """Return duration in seconds from ffprobe."""
    result = subprocess.run(
        [
            _bin("ffprobe"), "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            str(path),
        ],
        capture_output=True, text=True, timeout=30, check=True,
    )
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def _normalize_clip(src: Path, dest: Path) -> None:
    """Pass 1: normalize a single clip to the intermediate profile."""
    cmd = [
        _bin("ffmpeg"), "-y",
        "-i", str(src),
        "-vf", (
            f"scale={NORM_WIDTH}:{NORM_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={NORM_WIDTH}:{NORM_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={NORM_FPS},"
            "setsar=1"
        ),
        "-r", str(NORM_FPS),
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "18",
        "-an",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg normalize failed for {src.name}:\n{result.stderr[-500:]}")


def _write_concat_list(clip_paths: list[Path], output: Path) -> None:
    with output.open("w") as f:
        for p in clip_paths:
            f.write(f"file '{p.resolve()}'\n")


def render(
    clip_paths: list[Path],
    voiceover_path: Path = VOICEOVER_PATH,
    bgmusic_path: Path = BGMUSIC_PATH,
    output_path: Path = OUTPUT_PATH,
    text_overlays: Optional[list] = None,
) -> Path:
    """
    Full two-pass render.
    Returns path to the final video file.
    """
    if not clip_paths:
        raise ValueError("No clips provided to renderer")
    if not voiceover_path.exists():
        raise FileNotFoundError(f"Voiceover not found: {voiceover_path}")
    if not bgmusic_path.exists():
        raise FileNotFoundError(f"Background music not found: {bgmusic_path}")

    NORM_DIR.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Get voiceover duration to know how much footage we need
    vo_duration = _ffprobe_duration(voiceover_path)
    logger.info("Voiceover duration: %.1fs", vo_duration)

    # ── Pass 1: normalize each clip ──────────────────────────────────────────
    norm_clips: list[Path] = []
    for i, src in enumerate(clip_paths):
        dest = NORM_DIR / f"clip_{i:02d}_norm.mp4"
        logger.info("Normalizing clip %d/%d: %s", i + 1, len(clip_paths), src.name)
        _normalize_clip(src, dest)
        norm_clips.append(dest)

    # Loop clips until total duration covers voiceover
    total_norm_duration = sum(_ffprobe_duration(p) for p in norm_clips)
    while total_norm_duration < vo_duration:
        logger.info("Total clip duration %.1fs < voiceover %.1fs — looping clips", total_norm_duration, vo_duration)
        extra_clips = []
        for src in norm_clips[:]:
            dest = NORM_DIR / f"clip_loop_{len(extra_clips):02d}_norm.mp4"
            if not dest.exists():
                import shutil
                shutil.copy2(src, dest)
            extra_clips.append(dest)
            total_norm_duration += _ffprobe_duration(dest)
            if total_norm_duration >= vo_duration:
                break
        norm_clips.extend(extra_clips)

    # ── Write concat list ────────────────────────────────────────────────────
    concat_list = NORM_DIR / "concat.txt"
    _write_concat_list(norm_clips, concat_list)

    # ── Pass 2: concat + audio mix (+ optional text overlays) ────────────────
    rendered_overlays = _render_overlay_frames(text_overlays or [], NORM_DIR) if text_overlays else []

    if rendered_overlays:
        n = len(rendered_overlays)
        vo_idx = n + 1
        bg_idx = n + 2
        extra_inputs, filter_complex, video_map = _build_overlay_filter_chain(
            rendered_overlays, vo_idx, bg_idx
        )
        video_codec = ["-c:v", "libx264", "-preset", "fast", "-crf", "20"]
        logger.info("Applying %d text overlay(s) — re-encoding video", n)
    else:
        extra_inputs = []
        filter_complex = "[1:a]volume=1.0[voice];[2:a]volume=0.15[music];[voice][music]amix=inputs=2:duration=first[a]"
        video_map = "0:v"
        video_codec = ["-c:v", "copy"]

    # -shortest stops encoding when the voiceover (shortest finite input) ends.
    # Required when overlay inputs use -loop 1 (infinite PNG streams).
    cmd = [
        _bin("ffmpeg"), "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        *extra_inputs,
        "-i", str(voiceover_path),
        "-i", str(bgmusic_path),
        "-filter_complex", filter_complex,
        "-map", video_map,
        "-map", "[a]",
        *video_codec,
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-shortest",
        str(output_path),
    ]
    logger.info("Running Pass 2 concat + audio mix")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg Pass 2 failed:\n{result.stderr[-1000:]}")

    # ── Verify output ────────────────────────────────────────────────────────
    if not output_path.exists() or output_path.stat().st_size < 100_000:
        raise RuntimeError(f"Output file missing or too small: {output_path}")

    final_duration = _ffprobe_duration(output_path)
    if abs(final_duration - vo_duration) > 5.0:
        logger.warning(
            "Output duration %.1fs differs from voiceover %.1fs by more than 5s",
            final_duration, vo_duration,
        )
    else:
        logger.info("Output duration %.1fs — OK", final_duration)

    logger.info("Render complete: %s (%.1f MB)", output_path, output_path.stat().st_size / 1_048_576)
    return output_path
