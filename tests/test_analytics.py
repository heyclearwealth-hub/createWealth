"""Unit tests for analytics.py."""
import json

import pipeline.analytics as an


def test_fetch_recent_respects_days_back(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    perf = {
        "videos": [
            {
                "video_id": "v-old",
                "upload_time": "2000-01-01T00:00:00+00:00",
                "metrics_24h": {},
                "metrics_48h": {},
            }
        ]
    }
    (tmp_path / "data" / "video_performance.json").write_text(json.dumps(perf))

    monkeypatch.setattr(an, "_youtube_analytics", lambda: object())
    called = {"count": 0}

    def _fake_fetch(*args, **kwargs):
        called["count"] += 1
        return {}

    monkeypatch.setattr(an, "_fetch_metrics", _fake_fetch)
    an.fetch_recent(days_back=1)
    assert called["count"] == 0
