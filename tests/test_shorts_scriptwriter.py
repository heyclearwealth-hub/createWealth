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


def test_hook_repair_then_trim_stays_within_budget():
    base = "9% " + " ".join(["token"] * 145)
    repaired = sw._repair_hook_opening(base, "hook missing consequence framing in opening beat")
    capped = sw._trim_script_to_max_words(repaired, max_words=140)
    assert sw._word_count(capped) <= 140


def test_repair_does_not_run_for_non_hook_reason():
    script = "10% budget plan can improve your spending this year."
    repaired = sw._repair_hook_opening(script, "word-count out of range (142, expected 110-140)")
    assert repaired == script
