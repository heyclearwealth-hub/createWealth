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


# ── % escaping in drawtext ────────────────────────────────────────────────────

def test_build_ffmpeg_cmd_escapes_percent():
    """Finance captions often contain % (e.g. '20% return'). Must become %% for drawtext."""
    cmd = sh._build_ffmpeg_cmd(
        video_path=Path("video.mp4"),
        audio_path=Path("audio.mp3"),
        start=0.0,
        duration=55.0,
        caption_text="Invest 20% of income",
        cta_text="Earn 5% more",
        output_path=Path("out.mp4"),
    )
    vf = next(c for c in cmd if "drawtext" in c)
    assert "20%%" in vf
    assert "5%%" in vf
    assert "20%" not in vf.replace("20%%", "")  # no bare % remaining


# ── Claude best-moment picker ─────────────────────────────────────────────────

PIPELINE_JSON_WITH_SCRIPT = {
    **PIPELINE_JSON,
    "script": "word " * 400,  # 400 words ~ 160s of content
}


def test_ask_claude_best_moment_uses_api_key(monkeypatch):
    """Returns start time and caption when Claude responds with valid JSON."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    fake_response = MagicMock()
    fake_response.content = [MagicMock(text='{"start_word_index": 100, "caption": "The real cost of waiting", "reason": "counterintuitive insight"}')]

    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = fake_response
        start, caption = sh._ask_claude_best_moment(PIPELINE_JSON_WITH_SCRIPT)

    assert start == pytest.approx(100 / 2.5)
    assert caption == "The real cost of waiting"


def test_ask_claude_best_moment_fallback_no_api_key(monkeypatch):
    """Falls back to DEFAULT_START_S and hook_summary when no API key."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    start, caption = sh._ask_claude_best_moment(PIPELINE_JSON_WITH_SCRIPT)
    assert start == sh.DEFAULT_START_S
    assert caption == PIPELINE_JSON_WITH_SCRIPT["hook_summary"]


def test_ask_claude_best_moment_fallback_on_bad_json(monkeypatch):
    """Falls back gracefully when Claude returns malformed JSON."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    fake_response = MagicMock()
    fake_response.content = [MagicMock(text="not json at all")]

    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = fake_response
        start, caption = sh._ask_claude_best_moment(PIPELINE_JSON_WITH_SCRIPT)

    assert start == sh.DEFAULT_START_S
    assert caption == PIPELINE_JSON_WITH_SCRIPT["hook_summary"]


def test_ask_claude_best_moment_fallback_on_api_error(monkeypatch):
    """Falls back gracefully when the Anthropic API raises an exception."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.side_effect = Exception("network error")
        start, caption = sh._ask_claude_best_moment(PIPELINE_JSON_WITH_SCRIPT)

    assert start == sh.DEFAULT_START_S
    assert caption == PIPELINE_JSON_WITH_SCRIPT["hook_summary"]


def test_create_short_uses_claude_picker_when_no_preferred_start(tmp_path, monkeypatch):
    """create_short calls _ask_claude_best_moment when preferred_start is not given."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace" / "output").mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        out = tmp_path / "workspace" / "output" / "short_video.mp4"
        out.write_bytes(b"x" * 100_000)
        return MagicMock(returncode=0, stderr="")

    with patch("pipeline.shorts._ask_claude_best_moment", return_value=(90.0, "The key insight")) as mock_picker:
        with patch("pipeline.shorts._ffprobe_duration", return_value=600.0):
            with patch("subprocess.run", side_effect=fake_run):
                sh.create_short(
                    video_path=tmp_path / "video.mp4",
                    audio_path=tmp_path / "audio.mp3",
                    pipeline_json=PIPELINE_JSON,
                    output_path=tmp_path / "workspace" / "output" / "short_video.mp4",
                )

    mock_picker.assert_called_once_with(PIPELINE_JSON)


def test_create_short_skips_claude_when_preferred_start_given(tmp_path, monkeypatch):
    """create_short does NOT call _ask_claude_best_moment when preferred_start is explicit."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace" / "output").mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        out = tmp_path / "workspace" / "output" / "short_video.mp4"
        out.write_bytes(b"x" * 100_000)
        return MagicMock(returncode=0, stderr="")

    with patch("pipeline.shorts._ask_claude_best_moment") as mock_picker:
        with patch("pipeline.shorts._ffprobe_duration", return_value=600.0):
            with patch("subprocess.run", side_effect=fake_run):
                sh.create_short(
                    video_path=tmp_path / "video.mp4",
                    audio_path=tmp_path / "audio.mp3",
                    pipeline_json=PIPELINE_JSON,
                    preferred_start=30.0,
                    output_path=tmp_path / "workspace" / "output" / "short_video.mp4",
                )

    mock_picker.assert_not_called()
