"""Unit tests for shorts_renderer.py."""
from PIL import Image
import pytest

import pipeline.shorts_renderer as sr


def test_build_background_frame_adds_motion_in_gradient_fallback():
    gradient = Image.linear_gradient("L").resize((sr.SHORT_W, sr.SHORT_H)).convert("RGBA")
    frame_a = sr._build_background_frame(0.0, gradient, None)
    frame_b = sr._build_background_frame(7.0, gradient, None)
    assert frame_a.size == (sr.SHORT_W, sr.SHORT_H)
    assert frame_b.size == (sr.SHORT_W, sr.SHORT_H)
    assert frame_a.tobytes() != frame_b.tobytes()


def test_build_visual_queries_includes_topic_hints_first():
    queries = sr._build_visual_queries(
        pillar="investing",
        topic="Roth IRA and ETF strategy",
        script_text="Use an ETF in a Roth account for long-term growth.",
    )
    assert queries
    assert any("roth ira" in q.lower() for q in queries[:4])
    assert any("etf" in q.lower() for q in queries[:6])


def test_deoverlap_label_overlays_removes_collisions():
    overlays = [
        {"type": "label", "text": "A", "start_time_s": 4.0, "duration_s": 2.0},
        {"type": "label", "text": "B", "start_time_s": 4.7, "duration_s": 2.0},
        {"type": "cta", "text": "Follow", "start_time_s": 45.0, "duration_s": 3.5},
    ]
    cleaned = sr._deoverlap_label_overlays(overlays, duration_s=49.0)
    labels = [ov for ov in cleaned if ov["type"] == "label"]
    assert len(labels) == 2
    assert sr._ov_start(labels[1]) >= sr._ov_end(labels[0])


def test_deoverlap_keeps_semantic_tail_label_but_drops_generic_tail_label():
    overlays = [
        {"type": "label", "text": "REAL COST", "start_time_s": 47.2, "duration_s": 1.6},
        {"type": "label", "text": "HIGH INTEREST FIRST", "start_time_s": 47.4, "duration_s": 1.6},
        {"type": "cta", "text": "Follow", "start_time_s": 45.0, "duration_s": 3.5},
    ]
    cleaned = sr._deoverlap_label_overlays(overlays, duration_s=49.0)
    kept_texts = [ov.get("text", "") for ov in cleaned if ov.get("type") == "label"]
    assert "REAL COST" not in kept_texts
    assert "HIGH INTEREST FIRST" in kept_texts


def test_make_spoken_caption_image_falls_back_without_timestamps():
    words = "this is a simple caption fallback check".split()
    img = sr._make_spoken_caption_image(words, [], t_mid=1.2)
    assert img.getbbox() is not None


def test_make_spoken_caption_image_stays_blank_before_audio_in_wps_fallback():
    words = "this is a simple caption fallback check".split()
    img = sr._make_spoken_caption_image(words, [], t_mid=0.05)
    assert img.getbbox() is None


def test_spoken_words_ignores_pause_markers():
    words = sr._spoken_words("Save [PAUSE] more money now.")
    assert words == ["Save", "more", "money", "now"]


def test_spoken_words_keeps_money_number_grouping():
    words = sr._spoken_words("Could not cover a $1,000 emergency.")
    assert "$1,000" in words


def test_sentence_end_indices_pause_and_punctuation():
    # "Save more" ends sentence at word 1 (PAUSE), "now" ends at word 2 (period)
    script = "Save more. [PAUSE] Invest now."
    ends = sr._sentence_end_indices(script)
    # words: Save(0) more(1) . Invest(2) now(3) .
    assert 1 in ends, f"Expected word 1 (more) to mark a sentence end, got {ends}"
    assert 3 in ends, f"Expected word 3 (now) to mark a sentence end, got {ends}"


def test_caption_slice_does_not_cross_sentence_boundary():
    words = "one two three four five six seven eight nine ten".split()
    # Sentence ends after word 4 ("five"), sentence 2 starts at word 5
    sent_ends = {4}
    # Active word is 5 (first word of second sentence) — start should not reach back before 5
    result = sr._caption_slice(words, active_idx=5, sent_ends=sent_ends)
    indices = [idx for _, idx in result]
    assert all(i >= 5 for i in indices), f"Caption crossed sentence boundary: {result}"


def test_plain_text_proof_tag_is_centered_banner():
    overlay = {"type": "proof_tag", "text": "Educational only. Not financial advice.", "plain_text": True}
    img = sr._make_overlay_image(overlay)
    assert img.getbbox() is not None


def test_needs_financial_disclaimer_true_for_finance_pillar_without_symbols():
    overlays = [{"type": "label", "text": "Start with broad-market funds"}]
    script_data = {
        "pillar": "investing",
        "voiceover_script": "Build a simple plan and stay consistent.",
    }
    assert sr._needs_financial_disclaimer(overlays, script_data) is True


def test_needs_financial_disclaimer_false_for_non_finance_copy():
    overlays = [{"type": "label", "text": "Sunrise over mountain lake"}]
    script_data = {
        "pillar": "travel",
        "voiceover_script": "Pack light and enjoy the trail.",
    }
    assert sr._needs_financial_disclaimer(overlays, script_data) is False


def test_build_bg_montage_plan_starts_with_fast_cut():
    plan = sr._build_bg_montage_plan(duration_s=20.0, source_count=4, seed_hint="apr-apy")
    assert plan
    first_start, first_end, _ = plan[0]
    assert first_start == 0.0
    assert first_end <= 0.8


def test_caption_display_word_uppercases_finance_acronyms():
    assert sr._caption_display_word("apr") == "APR"
    assert sr._caption_display_word("apy") == "APY"
    assert sr._caption_display_word("ira") == "IRA"


def test_inject_hook_interrupt_adds_early_label_when_missing():
    overlays = [{"type": "hook_number", "text": "0.5%", "start_time_s": 0.0, "duration_s": 4.0}]
    patched = sr._inject_hook_interrupt(overlays, duration_s=40.0, pillar="debt")
    labels = [ov for ov in patched if ov.get("type") == "label"]
    assert labels
    assert sr._ov_start(labels[0]) <= 1.0


def test_compute_voiceover_autofit_rate_for_small_overshoot(monkeypatch):
    monkeypatch.setattr(sr, "SHORT_MIN_DURATION_S", 34.0)
    monkeypatch.setattr(sr, "SHORT_MAX_DURATION_S", 44.0)
    monkeypatch.setattr(sr, "SHORT_AUTOFIT_TARGET_MARGIN_S", 0.2)
    monkeypatch.setattr(sr, "SHORT_AUTOFIT_MIN_RATE", 0.90)
    monkeypatch.setattr(sr, "SHORT_AUTOFIT_MAX_RATE", 1.15)
    rate = sr._compute_voiceover_autofit_rate(46.0)
    assert rate is not None
    assert rate == pytest.approx(46.0 / 43.8, rel=1e-6)


def test_compute_voiceover_autofit_rate_none_for_large_miss(monkeypatch):
    monkeypatch.setattr(sr, "SHORT_MIN_DURATION_S", 34.0)
    monkeypatch.setattr(sr, "SHORT_MAX_DURATION_S", 44.0)
    monkeypatch.setattr(sr, "SHORT_AUTOFIT_TARGET_MARGIN_S", 0.2)
    monkeypatch.setattr(sr, "SHORT_AUTOFIT_MIN_RATE", 0.90)
    monkeypatch.setattr(sr, "SHORT_AUTOFIT_MAX_RATE", 1.15)
    assert sr._compute_voiceover_autofit_rate(60.0) is None


def test_atempo_filter_chain_handles_large_rates():
    chain = sr._atempo_filter_chain(2.5)
    assert chain == "atempo=2.00000,atempo=1.25000"


def test_retime_word_timestamps_scales_for_speedup():
    scaled = sr._retime_word_timestamps([0.0, 1.0, 2.5], speed_rate=1.25)
    assert scaled[0] == 0.0
    assert scaled[1] == pytest.approx(0.8, rel=1e-6)
    assert scaled[2] == pytest.approx(2.0, rel=1e-6)
