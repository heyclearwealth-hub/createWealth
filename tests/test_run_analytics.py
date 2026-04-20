"""Unit tests for scripts/run_analytics.py."""
import scripts.run_analytics as ra


def test_unique_trackable_video_ids_dedupes_and_skips_dry_run():
    perf = {
        "videos": [
            {"video_id": "dry-run"},
            {"video_id": "vid-1"},
            {"video_id": "vid-1"},
            {"video_id": "vid-2"},
            {"video_id": ""},
            {},
        ]
    }

    assert ra._unique_trackable_video_ids(perf) == ["vid-1", "vid-2"]
