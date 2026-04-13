"""Unit tests for trends weight loading compatibility."""
import json

import pipeline.trends as tr


def test_load_weights_supports_nested_legacy_schema(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "topic_weights.json").write_text(
        json.dumps({"pillars": {"investing": {"weight": 1.5}}})
    )
    assert tr._load_weights()["investing"] == 1.5


def test_load_weights_supports_flat_numeric_schema(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "topic_weights.json").write_text(
        json.dumps({"pillars": {"investing": 1.25}})
    )
    assert tr._load_weights()["investing"] == 1.25
