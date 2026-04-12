"""Unit tests for packaging.py"""
import json
import pytest
from unittest.mock import patch
from pathlib import Path

SCRIPT_DATA = {
    "topic": "roth ira for beginners",
    "pillar": "investing",
    "slug": "roth-ira-for-beginners",
    "title": "Roth IRA Explained: Start in 2026",
    "hook_summary": "Missing a Roth IRA costs $180k by retirement.",
    "thumbnail_concept": "ROTH IRA: START NOW",
    "description": "A guide to Roth IRA for young professionals.",
}

VALID_RESPONSE = json.dumps({
    "default_index": 0,
    "titles": [
        "How to Open a Roth IRA in 2026 (Step by Step)",
        "Stop Making This Roth IRA Mistake in Your 20s",
        "$6,500 Roth IRA Limit: How to Max It Out Fast",
    ],
    "thumbnail_texts": [
        "ROTH IRA: STEP BY STEP",
        "STOP THIS MISTAKE",
        "MAX IT OUT NOW",
    ],
    "description_hook": "If you haven't opened a Roth IRA yet, this video shows you exactly how to start.",
})


def test_generate_returns_three_variants(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace").mkdir()

    with patch("pipeline.packaging._call_claude", return_value=VALID_RESPONSE):
        import pipeline.packaging as pk
        result = pk.generate(SCRIPT_DATA)

    assert len(result["titles"]) == 3
    assert len(result["thumbnail_texts"]) == 3
    assert result["experiment_state"] == "pending"


def test_generate_saves_to_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace").mkdir()

    with patch("pipeline.packaging._call_claude", return_value=VALID_RESPONSE):
        import pipeline.packaging as pk
        pk.generate(SCRIPT_DATA)

    candidates_file = tmp_path / "workspace" / "package_candidates.json"
    assert candidates_file.exists()
    data = json.loads(candidates_file.read_text())
    assert data["slug"] == "roth-ira-for-beginners"


def test_generate_falls_back_on_bad_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace").mkdir()

    with patch("pipeline.packaging._call_claude", return_value="not json"):
        import pipeline.packaging as pk
        result = pk.generate(SCRIPT_DATA)

    # Falls back to script defaults — title from script_data
    assert result["titles"][0] == SCRIPT_DATA["title"]
