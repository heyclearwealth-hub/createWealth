"""Unit tests for scripts/run_shorts.py"""
import scripts.run_shorts as rs


def test_select_long_video_skips_shorts_and_already_linked(monkeypatch):
    perf = {
        "videos": [
            {
                "video_id": "short123",
                "content_type": "short",
                "upload_time": "2026-04-10T10:00:00+00:00",
            },
            {
                "video_id": "long_old",
                "content_type": "long",
                "upload_time": "2026-04-10T09:00:00+00:00",
                "short_video_id": "already-made",
            },
            {
                "video_id": "long_new",
                "content_type": "long",
                "upload_time": "2026-04-10T11:00:00+00:00",
            },
        ]
    }

    monkeypatch.delenv("TARGET_LONG_VIDEO_ID", raising=False)
    picked = rs._select_long_video(perf)

    assert picked is not None
    assert picked["video_id"] == "long_new"


def test_select_long_video_honors_target(monkeypatch):
    perf = {
        "videos": [
            {"video_id": "long_a", "content_type": "long", "upload_time": "2026-04-10T10:00:00+00:00"},
            {"video_id": "long_b", "content_type": "long", "upload_time": "2026-04-10T11:00:00+00:00"},
        ]
    }

    monkeypatch.setenv("TARGET_LONG_VIDEO_ID", "long_a")
    picked = rs._select_long_video(perf)

    assert picked is not None
    assert picked["video_id"] == "long_a"


def test_link_short_to_long_updates_entry():
    perf = {
        "videos": [
            {"video_id": "long1"},
            {"video_id": "long2"},
        ]
    }

    rs._link_short_to_long(perf, "long2", "short99")

    assert perf["videos"][1]["short_video_id"] == "short99"
    assert "short_created_at" in perf["videos"][1]
