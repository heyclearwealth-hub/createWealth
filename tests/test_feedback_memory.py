"""Unit tests for feedback_memory.py"""
import json
import pytest
from pathlib import Path
import pipeline.feedback_memory as fm


def test_ingest_creates_item(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    item = fm.ingest("the hook is too weak and generic", slug="roth-ira")
    assert item["tag"] == "hook"
    assert item["resolved"] is False
    assert item["slug"] == "roth-ira"


def test_ingest_persists_to_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    fm.ingest("visual quality is poor", slug="budgeting-101")
    data = json.loads((tmp_path / "data" / "review_feedback.json").read_text())
    assert len(data["items"]) == 1
    assert data["items"][0]["tag"] == "visuals"


def test_tag_compliance():
    assert fm._tag_reason("contains earnings guarantee") == "compliance"
    assert fm._tag_reason("looks like financial advice") == "compliance"


def test_tag_pacing():
    assert fm._tag_reason("video pacing is too slow") == "pacing"


def test_tag_packaging():
    assert fm._tag_reason("title variant is misleading") == "packaging"


def test_tag_other():
    assert fm._tag_reason("something completely random") == "other"


def test_get_constraints_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    # No file exists → empty string
    assert fm.get_constraints() == ""


def test_get_constraints_returns_unresolved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    fm.ingest("hook lacks dollar figure", slug="s1")
    fm.ingest("pacing drags in middle section", slug="s2")

    constraints = fm.get_constraints()
    assert "PREVIOUS REVIEWER FEEDBACK" in constraints
    assert "hook lacks dollar figure" in constraints
    assert "pacing drags" in constraints


def test_mark_resolved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    fm.ingest("hook is weak", slug="s1")
    fm.ingest("hook lacks numbers", slug="s2")
    fm.ingest("pacing issue", slug="s3")

    count = fm.mark_resolved("hook")
    assert count == 2

    # Resolved items should not appear in constraints
    constraints = fm.get_constraints()
    assert "hook is weak" not in constraints
    assert "pacing issue" in constraints


def test_constraints_capped_at_three_per_tag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    for i in range(5):
        fm.ingest(f"hook problem number {i}", slug=f"s{i}")

    constraints = fm.get_constraints()
    # Should include at most 3 hook items
    count = constraints.count("hook problem number")
    assert count <= 3
