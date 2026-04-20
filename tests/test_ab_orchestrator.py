"""Unit tests for ab_orchestrator.py"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

import pipeline.ab_orchestrator as ab


def _make_perf(video_id, upload_offset_hours, native_test_started=False, variant_index=0):
    upload_time = (datetime.now(timezone.utc) - timedelta(hours=upload_offset_hours)).isoformat()
    return {
        "videos": [{
            "video_id": video_id,
            "upload_time": upload_time,
            "native_test_started": native_test_started,
            "current_variant_index": variant_index,
        }]
    }


def _make_candidates(titles=None):
    return {"titles": titles or ["Title A", "Title B", "Title C"]}


def test_skips_when_native_test_active(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "workspace").mkdir()

    # native test already started, only 10h since upload
    perf = _make_perf("vid1", upload_offset_hours=10, native_test_started=True)
    (tmp_path / "data" / "video_performance.json").write_text(json.dumps(perf))
    (tmp_path / "workspace" / "package_candidates.json").write_text(json.dumps(_make_candidates()))

    with patch("pipeline.ab_orchestrator._youtube_service") as mock_yt:
        ab.check_and_rotate("vid1")
        mock_yt.assert_not_called()


def test_skips_within_sla(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(ab, "SLA_HOURS", 24)

    # Only 10h since upload, SLA is 24h
    perf = _make_perf("vid2", upload_offset_hours=10, native_test_started=False)
    (tmp_path / "data" / "video_performance.json").write_text(json.dumps(perf))
    (tmp_path / "workspace" / "package_candidates.json").write_text(json.dumps(_make_candidates()))

    with patch("pipeline.ab_orchestrator._youtube_service") as mock_yt:
        ab.check_and_rotate("vid2")
        mock_yt.assert_not_called()


def test_rotates_title_after_sla(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(ab, "SLA_HOURS", 24)

    # 30h since upload, SLA exceeded, no native test
    perf = _make_perf("vid3", upload_offset_hours=30, native_test_started=False, variant_index=0)
    (tmp_path / "data" / "video_performance.json").write_text(json.dumps(perf))
    (tmp_path / "workspace" / "package_candidates.json").write_text(json.dumps(_make_candidates()))

    mock_yt = MagicMock()
    mock_yt.videos().list().execute.return_value = {
        "items": [{"snippet": {"title": "Title A", "description": ""}}]
    }
    mock_yt.videos().update().execute.return_value = {}

    with patch("pipeline.ab_orchestrator._youtube_service", return_value=mock_yt):
        ab.check_and_rotate("vid3")

    # videos().update() should have been called with next title (variant 1 = "Title B")
    update_call = mock_yt.videos().update.call_args
    body = update_call.kwargs["body"] if update_call.kwargs else update_call[1]["body"]
    assert body["snippet"]["title"] == "Title B"


def test_skips_when_only_one_title(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(ab, "SLA_HOURS", 1)

    perf = _make_perf("vid4", upload_offset_hours=5, native_test_started=False)
    (tmp_path / "data" / "video_performance.json").write_text(json.dumps(perf))
    (tmp_path / "workspace" / "package_candidates.json").write_text(
        json.dumps({"titles": ["Only Title"]})
    )

    with patch("pipeline.ab_orchestrator._youtube_service") as mock_yt:
        ab.check_and_rotate("vid4")
        mock_yt.assert_not_called()


def test_variant_index_wraps_around(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(ab, "SLA_HOURS", 1)

    # Currently at last variant (index 2), should wrap to 0
    perf = _make_perf("vid5", upload_offset_hours=5, native_test_started=False, variant_index=2)
    (tmp_path / "data" / "video_performance.json").write_text(json.dumps(perf))
    (tmp_path / "workspace" / "package_candidates.json").write_text(json.dumps(_make_candidates()))

    mock_yt = MagicMock()
    mock_yt.videos().list().execute.return_value = {
        "items": [{"snippet": {"title": "Title C", "description": ""}}]
    }
    mock_yt.videos().update().execute.return_value = {}

    with patch("pipeline.ab_orchestrator._youtube_service", return_value=mock_yt):
        ab.check_and_rotate("vid5")

    update_call = mock_yt.videos().update.call_args
    body = update_call.kwargs["body"] if update_call.kwargs else update_call[1]["body"]
    assert body["snippet"]["title"] == "Title A"  # wrapped back to index 0


def test_disabled_mode_skips(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ab, "MODE", "disabled")

    with patch("pipeline.ab_orchestrator._youtube_service") as mock_yt:
        ab.check_and_rotate("any-video")
        mock_yt.assert_not_called()


def test_uses_video_entry_packaging_candidates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(ab, "SLA_HOURS", 1)

    perf = _make_perf("vid6", upload_offset_hours=5, native_test_started=False, variant_index=0)
    perf["videos"][0]["packaging_candidates"] = {"titles": ["Entry Title A", "Entry Title B"]}
    (tmp_path / "data" / "video_performance.json").write_text(json.dumps(perf))

    mock_yt = MagicMock()
    mock_yt.videos().list().execute.return_value = {
        "items": [{"snippet": {"title": "Entry Title A", "description": ""}}]
    }
    mock_yt.videos().update().execute.return_value = {}

    with patch("pipeline.ab_orchestrator._youtube_service", return_value=mock_yt):
        with patch("pipeline.ab_orchestrator.quota_guard.assert_budget"):
            with patch("pipeline.ab_orchestrator.quota_guard.charge"):
                ab.check_and_rotate("vid6")

    update_call = mock_yt.videos().update.call_args
    body = update_call.kwargs["body"] if update_call.kwargs else update_call[1]["body"]
    assert body["snippet"]["title"] == "Entry Title B"


def test_rotates_thumbnail_when_variant_available(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(ab, "SLA_HOURS", 1)

    perf = _make_perf("vid-thumb", upload_offset_hours=5, native_test_started=False, variant_index=0)
    perf["videos"][0]["packaging_candidates"] = {
        "titles": ["Title A", "Title B"],
        "thumbnail_texts": ["THUMB A", "THUMB B"],
    }
    perf_path = tmp_path / "data" / "video_performance.json"
    perf_path.write_text(json.dumps(perf))

    thumb_file = tmp_path / "thumb_b.png"
    thumb_file.write_bytes(b"x")

    mock_yt = MagicMock()
    mock_yt.videos().list().execute.return_value = {
        "items": [{"snippet": {"title": "Title A", "description": ""}}]
    }
    mock_yt.videos().update().execute.return_value = {}
    mock_yt.thumbnails().set().execute.return_value = {}

    with patch("pipeline.ab_orchestrator._youtube_service", return_value=mock_yt):
        with patch("pipeline.ab_orchestrator._resolve_thumbnail_for_variant", return_value=thumb_file):
            with patch("pipeline.ab_orchestrator.quota_guard.assert_budget"):
                with patch("pipeline.ab_orchestrator.quota_guard.charge"):
                    ab.check_and_rotate("vid-thumb")

    thumb_set_calls = mock_yt.thumbnails().set.call_args_list
    assert any(call.kwargs.get("videoId") == "vid-thumb" for call in thumb_set_calls)
    updated = json.loads(perf_path.read_text())
    assert updated["videos"][0]["current_thumbnail_variant_index"] == 1
