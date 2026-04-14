"""Unit tests for shorts_renderer.py."""
from PIL import Image

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
