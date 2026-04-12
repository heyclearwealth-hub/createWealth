"""Unit tests for scriptwriter.py"""
import json
import math
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path


TOPIC = {"keyword": "roth ira for beginners", "pillar": "investing", "slug": "roth-ira-for-beginners"}

VALID_SCRIPT_DATA = {
    "topic": "roth ira for beginners",
    "pillar": "investing",
    "slug": "roth-ira-for-beginners",
    "title": "Roth IRA Explained: Start in 2026",
    "description": "A full guide to the Roth IRA.\n\n⚠️ This video uses AI-generated voiceover and AI-assisted script writing.\n⚠️ This is for educational purposes only. Not financial advice.",
    "tags": ["roth ira", "investing"],
    "hook_summary": "Shows how missing a Roth IRA costs $180k by retirement.",
    "thumbnail_concept": "ROTH IRA: START NOW",
    "script": "If you are 27 and putting money in a savings account instead of a Roth IRA, you are leaving over 180,000 dollars on the table by retirement. " * 30,
    "stat_citations": ["Federal Reserve 2025 Consumer Finance Survey"],
    "pillar_playlist_bridge": "Watch our investing playlist for what to do next.",
}


def _make_claude_response(content: str):
    msg = MagicMock()
    msg.content = [MagicMock(text=content)]
    return msg


# ── JSON retry tests ──────────────────────────────────────────────────────────

def test_valid_json_on_first_try(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "finance_script.md").write_text("system prompt")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "last_scripts.json").write_text("[]")
    (tmp_path / "data" / "review_feedback.json").write_text('{"items": []}')

    raw_json = json.dumps(VALID_SCRIPT_DATA)
    compliance_json = json.dumps({"compliance": "pass"})

    with patch("pipeline.scriptwriter._call_claude", side_effect=[raw_json, compliance_json]):
        import pipeline.scriptwriter as sw
        result = sw.generate(TOPIC)
    assert result["slug"] == "roth-ira-for-beginners"


def test_bad_json_retries_and_succeeds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "finance_script.md").write_text("system prompt")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "last_scripts.json").write_text("[]")
    (tmp_path / "data" / "review_feedback.json").write_text('{"items": []}')

    bad_response = "Here is your script: not json at all"
    good_json = json.dumps(VALID_SCRIPT_DATA)
    compliance_json = json.dumps({"compliance": "pass"})

    with patch("pipeline.scriptwriter._call_claude", side_effect=[bad_response, good_json, compliance_json]):
        import pipeline.scriptwriter as sw
        result = sw.generate(TOPIC)
    assert result["title"] == VALID_SCRIPT_DATA["title"]


def test_all_json_retries_fail(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "finance_script.md").write_text("system prompt")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "last_scripts.json").write_text("[]")
    (tmp_path / "data" / "review_feedback.json").write_text('{"items": []}')

    with patch("pipeline.scriptwriter._call_claude", return_value="not json at all"):
        import pipeline.scriptwriter as sw
        with pytest.raises(RuntimeError, match="invalid JSON"):
            sw._generate_script(TOPIC)


# ── Uniqueness / cosine similarity tests ─────────────────────────────────────

def test_cosine_identical_scripts():
    import pipeline.scriptwriter as sw
    text = "this is a test script about roth ira investing money"
    assert sw._cosine_similarity(text, text) == pytest.approx(1.0, abs=0.01)


def test_cosine_different_scripts():
    import pipeline.scriptwriter as sw
    a = "roth ira investing compound interest tax free retirement"
    b = "basketball football sports game score team player"
    assert sw._cosine_similarity(a, b) < 0.2


def test_similar_script_triggers_retry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "finance_script.md").write_text("system prompt")
    (tmp_path / "data").mkdir()

    past_script = VALID_SCRIPT_DATA["script"]
    (tmp_path / "data" / "last_scripts.json").write_text(json.dumps([past_script]))
    (tmp_path / "data" / "review_feedback.json").write_text('{"items": []}')

    # All calls return the same (too-similar) script + compliance pass
    similar_data = dict(VALID_SCRIPT_DATA)
    compliance_json = json.dumps({"compliance": "pass"})

    with patch("pipeline.scriptwriter._call_claude",
               side_effect=[json.dumps(similar_data), compliance_json] * 3):
        import pipeline.scriptwriter as sw
        with pytest.raises(RuntimeError, match="similar"):
            sw.generate(TOPIC)


# ── Compliance check tests ────────────────────────────────────────────────────

def test_compliance_fail_triggers_retry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "finance_script.md").write_text("system prompt")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "last_scripts.json").write_text("[]")
    (tmp_path / "data" / "review_feedback.json").write_text('{"items": []}')

    # Make scripts different enough to pass similarity but keep failing compliance
    def make_unique(i):
        d = dict(VALID_SCRIPT_DATA)
        d["script"] = f"unique script number {i} " * 50 + " ".join(str(j) for j in range(i * 100, i * 100 + 100))
        return json.dumps(d)

    fail_compliance = json.dumps({"compliance": "fail", "reason": "earnings guarantee found"})

    side_effects = []
    for i in range(3):
        side_effects.append(make_unique(i))
        side_effects.append(fail_compliance)

    with patch("pipeline.scriptwriter._call_claude", side_effect=side_effects):
        import pipeline.scriptwriter as sw
        with pytest.raises(RuntimeError, match="compliance"):
            sw.generate(TOPIC)
