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
