"""
test_render.py — Test text overlay rendering without any API calls.

Generates:
  - 2 synthetic dark-background video clips via FFmpeg (no Pexels)
  - 60s silence audio track (no ElevenLabs)
  - Renders with hardcoded overlays to workspace/output/test_render.mp4

Run:
    python scripts/test_render.py

Then open workspace/output/test_render.mp4 to review the text animations.
"""
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.renderer import _bin, render

WORKSPACE = Path("workspace")
TEST_OUTPUT = WORKSPACE / "output" / "test_render.mp4"
BGMUSIC = Path("pipeline/assets/bgmusic.mp3")

# Hardcoded case-study overlays for visual testing
# Timings are based on WPS=2.5: start_word/2.5 = seconds
TEST_OVERLAYS = [
    {
        "type": "title_card",
        "lines": ["Meet Sarah", "Nurse | Age 28 | $52k/year", "$38,000 in debt"],
        "start_word": 0,       # 0s
        "duration_s": 4.0,
    },
    {
        "type": "stat",
        "text": "$38,000",
        "start_word": 25,      # 10s
        "duration_s": 3.0,
    },
    {
        "type": "section",
        "text": "THE PROBLEM",
        "start_word": 50,      # 20s
        "duration_s": 2.5,
    },
    {
        "type": "stat",
        "text": "56% of Americans",
        "start_word": 75,      # 30s
        "duration_s": 3.0,
    },
    {
        "type": "section",
        "text": "THE TURNING POINT",
        "start_word": 100,     # 40s
        "duration_s": 2.5,
    },
    {
        "type": "stat",
        "text": "$847/month",
        "start_word": 112,     # 45s
        "duration_s": 3.0,
    },
    {
        "type": "section",
        "text": "THE RESULT",
        "start_word": 125,     # 50s
        "duration_s": 2.5,
    },
    {
        "type": "before_after",
        "before": "$38,000 debt\n$0 savings",
        "after": "$0 debt\n$12,000 saved",
        "start_word": 137,     # 55s
        "duration_s": 5.0,
    },
]


def _make_test_clip(path: Path, duration: int = 30, color: str = "0x1a1a2e") -> None:
    """Generate a solid-color test clip at 1280x720."""
    cmd = [
        _bin("ffmpeg"), "-y",
        "-f", "lavfi",
        "-i", f"color=c={color}:size=1280x720:duration={duration}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-an",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"Test clip generation failed:\n{result.stderr[-300:]}")


def _make_silence(path: Path, duration: int = 62) -> None:
    """Generate a silent AAC audio file."""
    cmd = [
        _bin("ffmpeg"), "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r=44100:cl=stereo:duration={duration}",
        "-c:a", "aac", "-b:a", "128k",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"Silence generation failed:\n{result.stderr[-300:]}")


def main() -> None:
    print("Setting up workspace...")
    for d in [WORKSPACE / "clips", WORKSPACE / "norm", WORKSPACE / "output"]:
        d.mkdir(parents=True, exist_ok=True)

    clip1 = WORKSPACE / "clips" / "test_clip1.mp4"
    clip2 = WORKSPACE / "clips" / "test_clip2.mp4"
    silence = WORKSPACE / "test_silence.aac"

    if not BGMUSIC.exists():
        print(f"WARNING: {BGMUSIC} not found — bgmusic will be missing")

    print("Generating test clips...")
    _make_test_clip(clip1, duration=35, color="0x1a1a2e")   # dark blue
    _make_test_clip(clip2, duration=35, color="0x0d2137")   # dark navy

    print("Generating silence audio (62s)...")
    _make_silence(silence, duration=62)

    print("Rendering with text overlays...")
    output = render(
        clip_paths=[clip1, clip2],
        voiceover_path=silence,
        bgmusic_path=BGMUSIC,
        output_path=TEST_OUTPUT,
        text_overlays=TEST_OVERLAYS,
    )

    size_kb = output.stat().st_size / 1024
    print(f"\nDone: {output}  ({size_kb:.0f} KB)")
    print("Open workspace/output/test_render.mp4 to review text animations.")


if __name__ == "__main__":
    main()
