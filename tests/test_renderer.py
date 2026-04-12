"""Unit tests for renderer.py"""
import json
import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call


def _mock_ffprobe_duration(path):
    """Return a fixed duration based on filename."""
    name = Path(path).name
    if "voiceover" in name:
        return 600.0  # 10 minutes
    if "bgmusic" in name:
        return 300.0
    return 30.0  # each clip = 30s


# ── Normalization tests ───────────────────────────────────────────────────────

def test_normalize_clip_calls_ffmpeg(tmp_path):
    src = tmp_path / "clip_00.mp4"
    src.touch()
    dest = tmp_path / "clip_00_norm.mp4"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        import pipeline.renderer as r
        r._normalize_clip(src, dest)

    cmd = mock_run.call_args[0][0]
    assert "ffmpeg" in cmd[0]
    assert "libx264" in cmd
    assert "ultrafast" in cmd
    assert "-an" in cmd


def test_normalize_clip_raises_on_ffmpeg_error(tmp_path):
    src = tmp_path / "clip.mp4"
    src.touch()
    dest = tmp_path / "clip_norm.mp4"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="some ffmpeg error")
        import pipeline.renderer as r
        with pytest.raises(RuntimeError, match="normalize failed"):
            r._normalize_clip(src, dest)


# ── Concat list tests ─────────────────────────────────────────────────────────

def test_write_concat_list(tmp_path):
    clips = [tmp_path / f"clip_{i}.mp4" for i in range(3)]
    output = tmp_path / "concat.txt"
    import pipeline.renderer as r
    r._write_concat_list(clips, output)
    content = output.read_text()
    for clip in clips:
        assert str(clip.resolve()) in content


# ── Clip loop tests ───────────────────────────────────────────────────────────

def test_clips_looped_when_shorter_than_voiceover(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace" / "norm").mkdir(parents=True)
    (tmp_path / "workspace" / "output").mkdir(parents=True)

    # Create mock clips, voiceover, bgmusic
    clips = []
    for i in range(3):
        p = tmp_path / f"clip_{i}.mp4"
        p.write_bytes(b"fake")
        clips.append(p)
    voiceover = tmp_path / "workspace" / "voiceover.mp3"
    voiceover.write_bytes(b"fake")
    bgmusic = tmp_path / "pipeline" / "assets" / "bgmusic.mp3"
    bgmusic.parent.mkdir(parents=True)
    bgmusic.write_bytes(b"fake")

    # 3 clips × 30s = 90s, voiceover = 200s → needs looping
    durations = {"voiceover.mp3": 200.0}

    def fake_ffprobe(path):
        name = Path(path).name
        return durations.get(name, 30.0)

    output_file = tmp_path / "workspace" / "output" / "final_video.mp4"

    def fake_run(cmd, **kwargs):
        mock = MagicMock(returncode=0, stderr="", stdout=json.dumps({"format": {"duration": "200"}}))
        # create output file when pass 2 runs
        if "concat" in cmd and "-map" in cmd:
            output_file.write_bytes(b"x" * 200_000)
        return mock

    with patch("pipeline.renderer._ffprobe_duration", side_effect=fake_ffprobe):
        with patch("subprocess.run", side_effect=fake_run):
            import pipeline.renderer as r
            result = r.render(clips, voiceover, bgmusic, output_file)

    assert result == output_file
    assert output_file.exists()


# ── ffprobe output verification tests ────────────────────────────────────────

def test_render_raises_if_output_too_small(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace" / "norm").mkdir(parents=True)
    (tmp_path / "workspace" / "output").mkdir(parents=True)

    clips = [tmp_path / "clip_0.mp4"]
    clips[0].write_bytes(b"fake")
    voiceover = tmp_path / "workspace" / "voiceover.mp3"
    voiceover.write_bytes(b"fake")
    bgmusic = tmp_path / "pipeline" / "assets" / "bgmusic.mp3"
    bgmusic.parent.mkdir(parents=True)
    bgmusic.write_bytes(b"fake")

    output_file = tmp_path / "workspace" / "output" / "final_video.mp4"

    def fake_run(cmd, **kwargs):
        # pass 2 creates a tiny file
        if "-map" in cmd:
            output_file.write_bytes(b"tiny")
        return MagicMock(returncode=0, stderr="", stdout=json.dumps({"format": {"duration": "30"}}))

    with patch("pipeline.renderer._ffprobe_duration", return_value=30.0):
        with patch("subprocess.run", side_effect=fake_run):
            import pipeline.renderer as r
            with pytest.raises(RuntimeError, match="too small"):
                r.render(clips, voiceover, bgmusic, output_file)
