"""Unit tests for shorts.py"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import pipeline.shorts as sh


PIPELINE_JSON = {
    "title": "Roth IRA Explained: Start in 2026",
    "hook_summary": "Missing a Roth IRA costs $180k by retirement.",
    "slug": "roth-ira-for-beginners",
    "pillar": "investing",
}


# ── Window picking ────────────────────────────────────────────────────────────

def test_pick_window_defaults_to_first_55s():
    with patch("pipeline.shorts._ffprobe_duration", return_value=600.0):
        start, duration = sh._pick_window(Path("fake.mp4"), preferred_start=0.0)
    assert start == 0.0
    assert duration == sh.MAX_DURATION_S


def test_pick_window_respects_preferred_start():
    with patch("pipeline.shorts._ffprobe_duration", return_value=600.0):
        start, duration = sh._pick_window(Path("fake.mp4"), preferred_start=120.0)
    assert start == 120.0
    assert duration == sh.MAX_DURATION_S


def test_pick_window_falls_back_when_too_close_to_end():
    # preferred_start=580 on a 600s video — only 20s left, < MIN_DURATION_S
    with patch("pipeline.shorts._ffprobe_duration", return_value=600.0):
        start, duration = sh._pick_window(Path("fake.mp4"), preferred_start=580.0)
    assert start == sh.DEFAULT_START_S
    assert duration <= sh.MAX_DURATION_S


def test_pick_window_caps_at_max_duration():
    with patch("pipeline.shorts._ffprobe_duration", return_value=40.0):
        start, duration = sh._pick_window(Path("fake.mp4"), preferred_start=0.0)
    assert duration <= sh.MAX_DURATION_S
    assert duration >= sh.MIN_DURATION_S


# ── FFmpeg command building ───────────────────────────────────────────────────

def test_build_ffmpeg_cmd_contains_drawtext():
    cmd = sh._build_ffmpeg_cmd(
        video_path=Path("video.mp4"),
        audio_path=Path("audio.mp3"),
        start=0.0,
        duration=55.0,
        caption_text="Test caption",
        cta_text="Watch full video",
        output_path=Path("out.mp4"),
    )
    cmd_str = " ".join(cmd)
    assert "drawtext" in cmd_str
    assert "Test caption" in cmd_str
    assert "Watch full video" in cmd_str


def test_build_ffmpeg_cmd_vertical_dimensions():
    cmd = sh._build_ffmpeg_cmd(
        video_path=Path("video.mp4"),
        audio_path=Path("audio.mp3"),
        start=0.0,
        duration=55.0,
        caption_text="Cap",
        cta_text="CTA",
        output_path=Path("out.mp4"),
    )
    cmd_str = " ".join(cmd)
    assert f"{sh.SHORT_W}:{sh.SHORT_H}" in cmd_str


def test_caption_truncated_at_80_chars(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace" / "output").mkdir(parents=True)

    long_caption = "A" * 100
    pipeline_json_long = {**PIPELINE_JSON, "hook_summary": long_caption}

    captured_cmd = {}

    def fake_run(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        # Create a fake output file
        out = tmp_path / "workspace" / "output" / "short_video.mp4"
        out.write_bytes(b"x" * 100_000)
        return MagicMock(returncode=0, stderr="")

    with patch("pipeline.shorts._ffprobe_duration", return_value=600.0):
        with patch("subprocess.run", side_effect=fake_run):
            sh.create_short(
                video_path=tmp_path / "video.mp4",
                audio_path=tmp_path / "audio.mp3",
                pipeline_json=pipeline_json_long,
            )

    # Caption in cmd should end with "..."
    cmd_str = " ".join(captured_cmd["cmd"])
    assert "..." in cmd_str


# ── Full render ───────────────────────────────────────────────────────────────

def test_create_short_raises_if_output_too_small(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace" / "output").mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        out = tmp_path / "workspace" / "output" / "short_video.mp4"
        out.write_bytes(b"tiny")  # < 50KB
        return MagicMock(returncode=0, stderr="")

    with patch("pipeline.shorts._ffprobe_duration", return_value=600.0):
        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="too small"):
                sh.create_short(
                    video_path=tmp_path / "video.mp4",
                    audio_path=tmp_path / "audio.mp3",
                    pipeline_json=PIPELINE_JSON,
                )


def test_create_short_raises_on_ffmpeg_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace" / "output").mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        return MagicMock(returncode=1, stderr="some ffmpeg error")

    with patch("pipeline.shorts._ffprobe_duration", return_value=600.0):
        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="Shorts render failed"):
                sh.create_short(
                    video_path=tmp_path / "video.mp4",
                    audio_path=tmp_path / "audio.mp3",
                    pipeline_json=PIPELINE_JSON,
                )


def test_create_short_returns_path_on_success(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace" / "output").mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        out = tmp_path / "workspace" / "output" / "short_video.mp4"
        out.write_bytes(b"x" * 100_000)
        return MagicMock(returncode=0, stderr="")

    with patch("pipeline.shorts._ffprobe_duration", return_value=600.0):
        with patch("subprocess.run", side_effect=fake_run):
            result = sh.create_short(
                video_path=tmp_path / "video.mp4",
                audio_path=tmp_path / "audio.mp3",
                pipeline_json=PIPELINE_JSON,
                output_path=tmp_path / "workspace" / "output" / "short_video.mp4",
            )

    assert result.name == "short_video.mp4"
