"""Unit tests for hook_gate.py"""
import json
import pytest
from unittest.mock import patch


STRONG_HOOK = (
    "If you are 27 and putting two hundred dollars a month into a savings account instead of a Roth IRA, "
    "you are going to leave over one hundred and eighty thousand dollars on the table by the time you retire. "
    "I am going to show you exactly why that happens and the three steps to fix it before your next paycheck. "
    "This is not complicated. Most people just were never taught this."
)

WEAK_HOOK = (
    "Hey guys, welcome back to the channel. Today we are going to be talking about Roth IRAs. "
    "This is a really important topic for a lot of people. "
    "So in today's video I am going to go over everything you need to know."
)

PASS_RESPONSE = json.dumps({"score": 0.88, "pass": True, "reason": "Strong numeric hook", "issues": []})
FAIL_RESPONSE = json.dumps({"score": 0.45, "pass": False, "reason": "Generic opener, no number", "issues": ["starts with Hey guys", "no specific dollar amount"]})


def test_strong_hook_passes():
    with patch("pipeline.hook_gate._call_claude", return_value=PASS_RESPONSE):
        import pipeline.hook_gate as hg
        result = hg.score_hook(STRONG_HOOK, threshold=0.75)
    assert result["pass"] is True
    assert result["score"] >= 0.75


def test_weak_hook_fails():
    with patch("pipeline.hook_gate._call_claude", return_value=FAIL_RESPONSE):
        import pipeline.hook_gate as hg
        result = hg.score_hook(WEAK_HOOK, threshold=0.75)
    assert result["pass"] is False
    assert result["score"] < 0.75
    assert len(result["issues"]) > 0


def test_threshold_respected():
    low_score = json.dumps({"score": 0.72, "pass": True, "reason": "ok", "issues": []})
    with patch("pipeline.hook_gate._call_claude", return_value=low_score):
        import pipeline.hook_gate as hg
        # 0.72 should fail at threshold 0.75
        result = hg.score_hook(STRONG_HOOK, threshold=0.75)
    assert result["pass"] is False


def test_parse_error_defaults_to_fail():
    with patch("pipeline.hook_gate._call_claude", return_value="not json at all"):
        import pipeline.hook_gate as hg
        result = hg.score_hook(STRONG_HOOK, threshold=0.75)
    assert result["pass"] is False
    assert result["score"] == 0.0


def test_hook_extraction_word_limit():
    import pipeline.hook_gate as hg
    long_script = " ".join(["word"] * 200)
    hook = hg._extract_hook(long_script, word_limit=75)
    assert len(hook.split()) == 75


def test_gate_returns_score_dict():
    with patch("pipeline.hook_gate._call_claude", return_value=PASS_RESPONSE):
        import pipeline.hook_gate as hg
        script_data = {"script": STRONG_HOOK}
        result = hg.gate(script_data, threshold=0.75)
    assert "score" in result
    assert "pass" in result
    assert "hook_text" in result
