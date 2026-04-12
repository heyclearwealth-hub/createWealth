"""
renderer.py — FFmpeg two-pass video renderer.
Pass 1: normalize each clip to 1280x720 / 30fps / H.264 / no audio
Pass 2: concat normalized clips + mix voiceover + bgmusic
"""
import json
import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

WORKSPACE = Path("workspace")
OUTPUT_PATH = WORKSPACE / "output" / "final_video.mp4"
BGMUSIC_PATH = Path("pipeline/assets/bgmusic.mp3")
VOICEOVER_PATH = WORKSPACE / "voiceover.mp3"
NORM_DIR = WORKSPACE / "norm"

NORM_WIDTH = 1280
NORM_HEIGHT = 720
NORM_FPS = 30


def _ffprobe_duration(path: Path) -> float:
    """Return duration in seconds from ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
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
        "ffmpeg", "-y",
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

    # ── Pass 2: concat + audio mix ───────────────────────────────────────────
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-i", str(voiceover_path),
        "-i", str(bgmusic_path),
        "-filter_complex",
        "[1:a]volume=1.0[voice];[2:a]volume=0.15[music];[voice][music]amix=inputs=2:duration=first[a]",
        "-map", "0:v",
        "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
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
