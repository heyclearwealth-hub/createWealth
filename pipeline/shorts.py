"""
shorts.py — Extract a Short clip from an approved long-form video.

Steps:
1. Find best 35–60s window via Claude (picks highest-value shareable moment from script)
2. Re-encode to vertical 9:16 (1080x1920) with black pillarbox
3. Burn in captions from the best-moment summary (top-third safe zone)
4. Overlay CTA text: "Watch full video ↑ Link in bio" (bottom-third safe zone)
5. Save to workspace/output/short_video.mp4
"""
import json
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

WORKSPACE = Path("workspace")
OUTPUT_SHORT = WORKSPACE / "output" / "short_video.mp4"

# Target dimensions for YouTube Shorts
SHORT_W = 1080
SHORT_H = 1920

# Default clip window if no analytics available
DEFAULT_START_S = 0
DEFAULT_DURATION_S = 55

MIN_DURATION_S = 35
MAX_DURATION_S = 60


def _ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def _ask_claude_best_moment(pipeline_json: dict) -> tuple[float, str]:
    """
    Ask Claude to identify the single best 45-60s shareable moment in the script.
    Returns (start_seconds_estimate, caption_text).
    Falls back to (DEFAULT_START_S, hook_summary) on any error.
    """
    fallback_caption = pipeline_json.get("hook_summary", pipeline_json.get("title", ""))
    fallback_start = DEFAULT_START_S

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.info("ANTHROPIC_API_KEY not set — using default Shorts window")
        return fallback_start, fallback_caption

    script = pipeline_json.get("script", "")
    title = pipeline_json.get("title", "")
    hook_summary = pipeline_json.get("hook_summary", "")

    if not script:
        return fallback_start, fallback_caption

    # Estimate words-per-second for timing (voiceover is ~150 wpm = 2.5 words/sec)
    WPS = 2.5

    prompt = f"""You are a YouTube Shorts editor. Given this script for a long-form finance video, identify the single best 45-60 second clip to make a standalone YouTube Short.

The best Short moment:
- Contains a surprising, counterintuitive, or emotionally resonant insight
- Is self-contained (viewer understands the point without watching the full video)
- Has a clear "wow" or "I never thought of it that way" reaction
- Is NOT the very beginning (the hook is for the long video, not a Short)

Video title: {title}
Hook summary: {hook_summary}

Script (word positions matter for timing):
---
{script[:3000]}
---

Respond with ONLY this JSON (no explanation):
{{
  "start_word_index": <integer — index of the first word of the best clip>,
  "caption": "<10 words max — punchy caption that captures the insight for the Short>",
  "reason": "<one sentence why this is the best moment>"
}}"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=256,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text
        raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw.strip())
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("No JSON in Claude response")
        data = json.loads(match.group())

        word_index = int(data.get("start_word_index", 0))
        caption = str(data.get("caption", fallback_caption)).strip()

        # Convert word index to seconds
        start_s = max(fallback_start, word_index / WPS)

        logger.info("Claude Shorts picker: start=%.1fs caption='%s' reason='%s'",
                    start_s, caption, data.get("reason", ""))
        return start_s, caption or fallback_caption

    except Exception as exc:
        logger.warning("Claude Shorts picker failed: %s — using default window", exc)
        return fallback_start, fallback_caption


def _pick_window(video_path: Path, preferred_start: float = 0.0) -> tuple[float, float]:
    """
    Return (start_seconds, duration_seconds) for the Short clip.
    Clips the video at preferred_start; caps at MAX_DURATION_S.
    Falls back to DEFAULT_START_S if preferred_start yields < MIN_DURATION_S of content.
    """
    try:
        total = _ffprobe_duration(video_path)
    except Exception:
        total = 600.0  # assume 10 min if probe fails

    available = total - preferred_start
    if available < MIN_DURATION_S:
        preferred_start = DEFAULT_START_S
        available = total

    duration = min(MAX_DURATION_S, available)
    return preferred_start, duration


def _build_ffmpeg_cmd(
    video_path: Path,
    audio_path: Path,
    start: float,
    duration: float,
    caption_text: str,
    cta_text: str,
    output_path: Path,
) -> list[str]:
    """
    Build the ffmpeg command to produce a vertical Short with burned-in text.
    Uses drawtext filter for caption and CTA overlay.
    Sanitizes text (escapes backslashes, %, single quotes, and colons for ffmpeg drawtext).
    """
    def _esc(text: str) -> str:
        # Order matters: backslash first, then chars special to ffmpeg drawtext
        # % must become %% — drawtext treats % as a strftime format specifier
        return (
            text.replace("\\", "\\\\")
                .replace("%", "%%")
                .replace("'", "\\'")
                .replace(":", "\\:")
        )

    caption_safe = _esc(caption_text)
    cta_safe = _esc(cta_text)

    # Scale + pad to 1080x1920 (portrait), then burn caption + CTA
    vf = (
        f"scale={SHORT_W}:{SHORT_H}:force_original_aspect_ratio=decrease,"
        f"pad={SHORT_W}:{SHORT_H}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"drawtext=text='{caption_safe}':fontsize=52:fontcolor=white:borderw=3:"
        f"x=(w-text_w)/2:y=h*0.12:line_spacing=8,"
        f"drawtext=text='{cta_safe}':fontsize=44:fontcolor=yellow:borderw=3:"
        f"x=(w-text_w)/2:y=h*0.82"
    )

    return [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-t", str(duration),
        "-i", str(video_path),
        "-i", str(audio_path),
        "-filter_complex",
        f"[0:v]{vf}[v];[1:a]atrim=start={start}:duration={duration},asetpts=PTS-STARTPTS[a]",
        "-map", "[v]",
        "-map", "[a]",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-r", "30",
        "-movflags", "+faststart",
        str(output_path),
    ]


def create_short(
    video_path: Path,
    audio_path: Path,
    pipeline_json: dict,
    preferred_start: float | None = None,
    cta_text: str = "Watch full video ↑ Link in bio",
    output_path: Path = OUTPUT_SHORT,
) -> Path:
    """
    Create a YouTube Short from a long-form video.
    Uses Claude to pick the best 45-60s shareable moment from the script.
    Returns path to the short output file.
    """
    # Use Claude to find best moment; override with preferred_start if explicitly passed
    if preferred_start is None:
        claude_start, caption_text = _ask_claude_best_moment(pipeline_json)
    else:
        claude_start = preferred_start
        caption_text = pipeline_json.get("hook_summary", pipeline_json.get("title", ""))

    # Cap caption at 80 chars to fit on screen
    if len(caption_text) > 80:
        caption_text = caption_text[:77] + "..."

    start, duration = _pick_window(video_path, claude_start)
    logger.info("Short window: start=%.1fs duration=%.1fs", start, duration)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = _build_ffmpeg_cmd(
        video_path=video_path,
        audio_path=audio_path,
        start=start,
        duration=duration,
        caption_text=caption_text,
        cta_text=cta_text,
        output_path=output_path,
    )

    logger.info("Rendering Short...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"Shorts render failed:\n{result.stderr[-500:]}")

    if not output_path.exists() or output_path.stat().st_size < 50_000:
        raise RuntimeError(f"Short output missing or too small: {output_path}")

    logger.info("Short created: %s (%.1f MB)", output_path, output_path.stat().st_size / 1_048_576)
    return output_path
