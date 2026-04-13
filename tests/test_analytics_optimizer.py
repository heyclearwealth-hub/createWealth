"""Unit tests for analytics.py and optimizer.py"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

import pipeline.optimizer as op


# ── _compute_score tests ──────────────────────────────────────────────────────

def test_compute_score_basic():
    metrics = {"estimatedMinutesWatched": 100, "impressions": 200}
    # (100 * 60) / 200 = 30.0
    assert op._compute_score(metrics) == 30.0


def test_compute_score_zero_impressions():
    assert op._compute_score({"estimatedMinutesWatched": 100, "impressions": 0}) == 0.0


def test_compute_score_missing_metrics():
    assert op._compute_score({}) == 0.0


def test_compute_score_none_values():
    assert op._compute_score({"estimatedMinutesWatched": None, "impressions": None}) == 0.0


# ── optimizer.run() tests ─────────────────────────────────────────────────────

def _make_perf(*entries):
    return {"videos": list(entries)}


def _make_entry(video_id, pillar, metrics_48h):
    return {
        "video_id": video_id,
        "pillar": pillar,
        "upload_time": datetime.now(timezone.utc).isoformat(),
        "metrics_48h": metrics_48h,
    }


def test_optimizer_scales_high_performing_pillar(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(op, "WTPI_FLOOR", 30.0)

    # 100 min / 100 impressions = 60s/impression > 30 floor → scale
    entry = _make_entry("vid1", "investing", {"estimatedMinutesWatched": 100, "impressions": 100})
    perf = _make_perf(entry)
    (tmp_path / "data" / "video_performance.json").write_text(json.dumps(perf))

    # Legacy nested schema should still be read correctly.
    weights = {"pillars": {"investing": {"weight": 1.0}}}
    (tmp_path / "data" / "topic_weights.json").write_text(json.dumps(weights))

    op.run()

    updated = json.loads((tmp_path / "data" / "topic_weights.json").read_text())
    assert isinstance(updated["pillars"]["investing"], float)
    assert updated["pillars"]["investing"] > 1.0


def test_optimizer_kills_low_performing_pillar(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(op, "WTPI_FLOOR", 45.0)

    # 10 min / 1000 impressions = 0.6s/impression < 45 floor → kill
    entry = _make_entry("vid2", "budgeting", {"estimatedMinutesWatched": 10, "impressions": 1000})
    perf = _make_perf(entry)
    (tmp_path / "data" / "video_performance.json").write_text(json.dumps(perf))

    weights = {"pillars": {"budgeting": 1.0}}
    (tmp_path / "data" / "topic_weights.json").write_text(json.dumps(weights))

    op.run()

    updated = json.loads((tmp_path / "data" / "topic_weights.json").read_text())
    assert updated["pillars"]["budgeting"] < 1.0


def test_optimizer_skips_dry_run_entry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    entry = _make_entry("dry-run", "investing", {"estimatedMinutesWatched": 100, "impressions": 100})
    perf = _make_perf(entry)
    (tmp_path / "data" / "video_performance.json").write_text(json.dumps(perf))

    weights = {"pillars": {"investing": 1.0}}
    (tmp_path / "data" / "topic_weights.json").write_text(json.dumps(weights))

    op.run()

    # Weight unchanged (dry-run skipped)
    updated = json.loads((tmp_path / "data" / "topic_weights.json").read_text())
    assert updated["pillars"]["investing"] == 1.0


def test_optimizer_writes_composite_score(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(op, "WTPI_FLOOR", 30.0)

    entry = _make_entry("vid3", "debt", {"estimatedMinutesWatched": 50, "impressions": 100})
    perf = _make_perf(entry)
    (tmp_path / "data" / "video_performance.json").write_text(json.dumps(perf))
    (tmp_path / "data" / "topic_weights.json").write_text(json.dumps({"pillars": {}}))

    op.run()

    updated_perf = json.loads((tmp_path / "data" / "video_performance.json").read_text())
    assert "composite_score" in updated_perf["videos"][0]
    # (50 * 60) / 100 = 30.0
    assert updated_perf["videos"][0]["composite_score"] == 30.0


def test_optimizer_weight_capped_at_max(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(op, "WTPI_FLOOR", 1.0)  # easy to exceed

    entry = _make_entry("vid4", "tax", {"estimatedMinutesWatched": 500, "impressions": 100})
    perf = _make_perf(entry)
    (tmp_path / "data" / "video_performance.json").write_text(json.dumps(perf))

    # Start near max weight
    weights = {"pillars": {"tax": op.MAX_WEIGHT}}
    (tmp_path / "data" / "topic_weights.json").write_text(json.dumps(weights))

    op.run()

    updated = json.loads((tmp_path / "data" / "topic_weights.json").read_text())
    assert updated["pillars"]["tax"] <= op.MAX_WEIGHT


def test_optimizer_weight_floored_at_min(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(op, "WTPI_FLOOR", 999.0)  # impossible to reach

    entry = _make_entry("vid5", "career_income", {"estimatedMinutesWatched": 1, "impressions": 100})
    perf = _make_perf(entry)
    (tmp_path / "data" / "video_performance.json").write_text(json.dumps(perf))

    weights = {"pillars": {"career_income": op.MIN_WEIGHT}}
    (tmp_path / "data" / "topic_weights.json").write_text(json.dumps(weights))

    op.run()

    updated = json.loads((tmp_path / "data" / "topic_weights.json").read_text())
    assert updated["pillars"]["career_income"] >= op.MIN_WEIGHT
