"""Unit tests for shorts_scriptwriter hook validation behavior."""

import pipeline.shorts_scriptwriter as sw


def test_assess_hook_strength_accepts_leaving_money_phrasing():
    script = (
        "401k match means you are leaving free money on the table for retirement. "
        "Most people skip this and lose years of compound growth."
    )
    ok, reason = sw.assess_hook_strength(script)
    assert ok is True
    assert reason == "ok"


def test_repair_hook_opening_injects_signal_words():
    script = "8% invested monthly can change your retirement path if you stay consistent."
    repaired = sw._repair_hook_opening(script, "hook missing pain framing in opening beat")
    assert repaired.startswith("8% of people lose free money fast.")


def test_repair_hook_opening_injects_consequence_terms():
    script = "12% debt payoff feels good in month one but can be the slower path."
    repaired = sw._repair_hook_opening(script, "hook missing consequence framing in opening beat")
    ok, reason = sw.assess_hook_strength(repaired)
    assert ok is True
    assert reason == "ok"


def test_trim_script_to_max_words_caps_length():
    script = " ".join(["word"] * 160)
    trimmed = sw._trim_script_to_max_words(script, max_words=140)
    assert sw._word_count(trimmed) <= 140
    assert trimmed.endswith(".")


def test_pad_script_to_min_words_reaches_floor():
    script = " ".join(["word"] * 109) + "."
    padded = sw._pad_script_to_min_words(script, min_words=110)
    assert sw._word_count(padded) >= 110


def test_ensure_numeric_opening_rewrites_when_missing_number():
    script = "Most people ignore this rule and lose money over time. Build an emergency fund now."
    fixed = sw._ensure_numeric_opening(script, topic={"topic": "emergency fund rule"})
    assert sw._first_token(fixed)[0].isdigit()
    assert "emergency fund rule" in fixed.lower()


def test_hook_repair_then_trim_stays_within_budget():
    base = "9% " + " ".join(["token"] * 145)
    repaired = sw._repair_hook_opening(base, "hook missing consequence framing in opening beat")
    capped = sw._trim_script_to_max_words(repaired, max_words=140)
    assert sw._word_count(capped) <= 140


def test_repair_does_not_run_for_non_hook_reason():
    script = "10% budget plan can improve your spending this year."
    repaired = sw._repair_hook_opening(script, "word-count out of range (142, expected 110-140)")
    assert repaired == script


def test_fit_script_word_budget_handles_short_and_long():
    short_script = " ".join(["word"] * 109)
    long_script = " ".join(["word"] * 160)
    short_fit = sw._fit_script_word_budget(short_script, min_words=110, max_words=140)
    long_fit = sw._fit_script_word_budget(long_script, min_words=110, max_words=140)
    assert 110 <= sw._word_count(short_fit) <= 140
    assert 110 <= sw._word_count(long_fit) <= 140


def test_retime_overlays_for_script_edit_scales_positions():
    data = {
        "overlays": [
            {"type": "hook_number", "start_word": 0, "text": "5%"},
            {"type": "label", "start_word": 50, "text": "MATH"},
            {"type": "cta", "start_word": 120, "text": "FOLLOW"},
        ]
    }
    old_script = " ".join(["word"] * 140)
    new_script = " ".join(["word"] * 112)
    sw._retime_overlays_for_script_edit(data, old_script, new_script)
    overlays = data["overlays"]
    assert overlays[0]["start_word"] == 0
    assert overlays[1]["start_word"] < 50
    assert overlays[2]["start_word"] == max(112 - int(3.0 * sw.WPS), 0)
