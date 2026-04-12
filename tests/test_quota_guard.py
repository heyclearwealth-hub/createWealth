"""Unit tests for quota_guard.py"""
import json
import pytest
from pathlib import Path
import pipeline.quota_guard as qg


def test_initial_remaining_is_full_budget(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(qg, "DAILY_BUDGET", 9000)

    assert qg.remaining() == 9000


def test_charge_deducts_units(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(qg, "DAILY_BUDGET", 9000)

    qg.charge("videos.insert")  # 1600
    assert qg.remaining() == 9000 - 1600


def test_charge_custom_units(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(qg, "DAILY_BUDGET", 9000)

    qg.charge("custom_op", units=200)
    assert qg.remaining() == 9000 - 200


def test_can_afford_true(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(qg, "DAILY_BUDGET", 9000)

    assert qg.can_afford("videos.insert") is True  # 1600 < 9000


def test_can_afford_false_after_charges(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(qg, "DAILY_BUDGET", 1000)

    assert qg.can_afford("videos.insert") is False  # 1600 > 1000


def test_assert_budget_raises_when_insufficient(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(qg, "DAILY_BUDGET", 100)

    with pytest.raises(RuntimeError, match="insufficient"):
        qg.assert_budget("videos.insert")


def test_assert_budget_passes_when_sufficient(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(qg, "DAILY_BUDGET", 9000)

    qg.assert_budget("videos.list")  # costs 1, should not raise


def test_quota_resets_on_new_day(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(qg, "DAILY_BUDGET", 9000)

    # Simulate yesterday's data
    budget_file = tmp_path / "data" / "api_budget.json"
    budget_file.write_text(json.dumps({"daily_units_used": 8000, "date": "2000-01-01"}))

    # Should reset because date is old
    assert qg.remaining() == 9000


def test_unknown_operation_costs_one_unit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(qg, "DAILY_BUDGET", 9000)

    qg.charge("mystery.operation")
    assert qg.remaining() == 8999
