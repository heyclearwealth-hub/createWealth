"""Unit tests for shorts_scriptwriter hook validation behavior."""

import re

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


def test_repair_hook_opening_handles_dollar_number_without_bad_grammar():
    script = "$500 mistake can delay your goals if you ignore this."
    repaired = sw._repair_hook_opening(script, "hook missing pain framing in opening beat")
    assert "$500 of people" not in repaired
    assert "$500 you lose early" in repaired


def test_trim_script_to_max_words_caps_length():
    script = " ".join(["word"] * 160)
    trimmed = sw._trim_script_to_max_words(script, max_words=140)
    assert sw._word_count(trimmed) <= 140
    assert trimmed.endswith(".")


def test_pad_script_to_min_words_reaches_floor():
    script = " ".join(["word"] * 109) + "."
    padded = sw._pad_script_to_min_words(script, min_words=110)
    assert sw._word_count(padded) >= 110


def test_pad_script_to_min_words_varies_with_topic_context():
    debt_script = "Pay your high interest debt first."
    tax_script = "Fix your W-4 withholding now."
    debt_padded = sw._pad_script_to_min_words(debt_script, min_words=116)
    tax_padded = sw._pad_script_to_min_words(tax_script, min_words=116)
    assert debt_padded != tax_padded


def test_ensure_numeric_opening_rewrites_when_missing_number():
    script = "Most people ignore this rule and lose money over time. Build an emergency fund now."
    fixed = sw._ensure_numeric_opening(script, topic={"topic": "emergency fund rule"})
    assert re.search(r"\d", sw._first_token(fixed))
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


def test_normalize_text_preserves_finance_acronym_casing():
    assert sw._normalize_text("apr vs apy for ira", "fallback") == "APR vs APY for IRA"


def test_word_budget_defaults_match_short_target_window():
    assert sw.MIN_WORDS >= 80
    assert sw.MAX_WORDS <= 110


def test_polish_voiceover_script_removes_awkward_leadin_and_formats_number():
    raw = "Know Here's the exact number. Could not cover a $1 000 emergency."
    polished = sw._polish_voiceover_script(raw)
    assert "Know Here's" not in polished
    assert "$1,000" in polished


def test_engagement_blueprint_injects_multiple_comparison_tables():
    script = (
        "56% of people miss this. [PAUSE] If you spend $500 now it can become $1,200 later. "
        "Do one fix and keep $700."
    )
    overlays = [{"type": "hook_number", "text": "56%", "start_word": 0, "duration_s": 4.0}]
    topic = {"pillar": "debt"}
    patched = sw._apply_engagement_blueprint(overlays, script, topic)
    comparisons = [ov for ov in patched if ov.get("type") == "comparison"]
    assert len(comparisons) >= 2


def test_polish_voiceover_script_fixes_percent_people_and_removes_you_know_tail():
    raw = "4.7% people lose money over years. That's the piece most people skip. you know."
    polished = sw._polish_voiceover_script(raw)
    assert "4.7% of people" in polished
    assert "you know." not in polished.lower()


def test_ensure_numeric_opening_percent_uses_of_people():
    script = "Most people miss this and lose money over years. The average drag is 4.7% each year."
    fixed = sw._ensure_numeric_opening(script, topic={"topic": "market timing"})
    assert re.match(r"^\d+(?:\.\d+)?%\s+of people\b", fixed.lower())


def test_engagement_blueprint_adds_comment_prompt_label():
    script = (
        "4.7% of people miss this. [PAUSE] Invest $500 every month and stop trying to time entries. "
        "Do this for years and let consistency win."
    )
    overlays = [{"type": "hook_number", "text": "4.7%", "start_word": 0, "duration_s": 4.0}]
    topic = {"pillar": "investing"}
    patched = sw._apply_engagement_blueprint(overlays, script, topic)
    labels = [str(ov.get("text", "")).upper() for ov in patched if ov.get("type") == "label"]
    assert any("COMMENT" in text for text in labels)
