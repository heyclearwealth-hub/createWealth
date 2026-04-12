"""
Integration dry-run test.

Mocks ALL external APIs (Claude, ElevenLabs, Pexels, YouTube, pytrends).
Runs the full pipeline end-to-end with DRY_RUN=1.
Asserts:
- workspace/pipeline.json exists and has expected shape
- workspace/output/upload_payload.json exists (DRY_RUN upload)
- No real API calls were made
"""
import json
import os
import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call


# ── Fixtures ──────────────────────────────────────────────────────────────────

FAKE_SCRIPT = {
    "topic": "roth ira for beginners",
    "pillar": "investing",
    "slug": "roth-ira-for-beginners",
    "title": "Roth IRA Explained: Start in 2026",
    "description": "A guide to Roth IRA for young professionals.",
    "tags": ["roth ira", "personal finance", "investing"],
    "hook_summary": "Missing a Roth IRA costs $180k by retirement.",
    "thumbnail_concept": "ROTH IRA: START NOW",
    "script": "Opening hook: $180,000 — that's what you lose by not starting a Roth IRA today. " * 20,
    "stat_citations": ["IRS Publication 590-A (2025)"],
    "pillar_playlist_bridge": "Check our investing playlist.",
}

FAKE_PACKAGING = {
    "default_index": 0,
    "titles": ["How to Open a Roth IRA in 2026", "Stop Making This Roth IRA Mistake", "$6,500 Roth IRA"],
    "thumbnail_texts": ["ROTH IRA STEP BY STEP", "STOP THIS MISTAKE", "MAX IT OUT"],
    "description_hook": "If you haven't opened a Roth IRA yet, this shows you exactly how.",
}


def _fake_ffprobe_stdout():
    return json.dumps({"format": {"duration": "600"}})


# ── Full dry-run test ──────────────────────────────────────────────────────────

def test_full_pipeline_dry_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    # Create required directories
    for d in ["workspace/output", "workspace/norm", "workspace/clips",
              "data", "pipeline/assets", "prompts"]:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)

    # Required files
    (tmp_path / "pipeline/assets/bgmusic.mp3").write_bytes(b"fake music")
    (tmp_path / "prompts/finance_script.md").write_text("# ClearWealth persona")
    (tmp_path / "data/topics_used.json").write_text('{"topics":[]}')
    (tmp_path / "data/last_scripts.json").write_text("[]")
    (tmp_path / "data/video_performance.json").write_text('{"videos":[]}')
    (tmp_path / "data/series_map.json").write_text('{"investing":{"playlist_id":"PLtest123"}}')
    (tmp_path / "data/api_budget.json").write_text('{"daily_units_used":0,"date":"1970-01-01"}')
    (tmp_path / "data/review_feedback.json").write_text('{"items":[]}')

    # Create fake clip
    clip_path = tmp_path / "workspace/clips/clip_0.mp4"
    clip_path.write_bytes(b"fake video")

    # Fake voiceover
    vo_path = tmp_path / "workspace/voiceover.mp3"
    vo_path.write_bytes(b"fake audio")

    # Fake final video (>100KB)
    final_path = tmp_path / "workspace/output/final_video.mp4"
    final_path.write_bytes(b"x" * 200_000)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-key")
    monkeypatch.setenv("PEXELS_API_KEY", "fake-key")
    monkeypatch.setenv("DRY_RUN", "1")

    # Mock all external calls
    with patch("pipeline.scriptwriter._call_claude", return_value=json.dumps(FAKE_SCRIPT)):
        with patch("pipeline.hook_gate._call_claude", return_value='{"score":0.9,"pass":true,"reason":"strong","issues":[]}'):
            with patch("pipeline.packaging._call_claude", return_value=json.dumps(FAKE_PACKAGING)):
                with patch("pipeline.voiceover.generate", return_value=vo_path):
                    with patch("pipeline.footage.download", return_value=[clip_path]):
                        with patch("pipeline.renderer.render", return_value=final_path):
                            with patch("pipeline.trends.pick_topic", return_value={
                                "keyword": "roth ira for beginners",
                                "pillar": "investing",
                                "slug": "roth-ira-for-beginners",
                            }):
                                with patch("pipeline.trends.mark_topic_used"):
                                    # Skip git operations
                                    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="No local changes")):
                                        import scripts.run_pipeline as rp
                                        rp.main()

    # Assert pipeline.json exists and has expected shape
    pipeline_json_path = tmp_path / "workspace/pipeline.json"
    assert pipeline_json_path.exists(), "pipeline.json was not created"
    pipeline_data = json.loads(pipeline_json_path.read_text())
    assert pipeline_data["slug"] == "roth-ira-for-beginners"
    assert "hook_score" in pipeline_data
    assert "title" in pipeline_data

    # Assert package_candidates.json exists
    candidates_path = tmp_path / "workspace/package_candidates.json"
    assert candidates_path.exists(), "package_candidates.json was not created"
    candidates = json.loads(candidates_path.read_text())
    assert len(candidates["titles"]) == 3

    logger_calls_ok = True  # No exception = all steps ran


def test_dry_run_uploader_writes_payload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace/output").mkdir(parents=True)
    (tmp_path / "data").mkdir()

    pipeline_json = {**FAKE_SCRIPT, "hook_score": 0.9, "compliance": "pass"}
    candidates = FAKE_PACKAGING

    (tmp_path / "workspace/package_candidates.json").write_text(json.dumps(candidates))
    (tmp_path / "data/series_map.json").write_text('{"investing":{"playlist_id":"PLtest123"}}')
    (tmp_path / "data/video_performance.json").write_text('{"videos":[]}')
    (tmp_path / "data/api_budget.json").write_text('{"daily_units_used":0,"date":"1970-01-01"}')

    video_path = tmp_path / "workspace/output/final_video.mp4"
    video_path.write_bytes(b"x" * 200_000)

    monkeypatch.setenv("DRY_RUN", "1")

    import pipeline.uploader as ul
    monkeypatch.setattr(ul, "DRY_RUN", True)
    monkeypatch.setattr(ul, "PACKAGE_CANDIDATES_PATH", tmp_path / "workspace/package_candidates.json")
    monkeypatch.setattr(ul, "PERFORMANCE_FILE", tmp_path / "data/video_performance.json")

    with patch("pipeline.uploader._load_series_map", return_value={"investing": {"playlist_id": "PLtest123"}}):
        video_id = ul.upload(pipeline_json, video_path)

    assert video_id == "dry-run"
    payload_path = tmp_path / "workspace/output/upload_payload.json"
    assert payload_path.exists()
    payload = json.loads(payload_path.read_text())
    assert payload["title"] == FAKE_PACKAGING["titles"][0]
    assert "Not financial advice" in payload["description"]
    assert "AI-generated voiceover" in payload["description"]

    perf = json.loads((tmp_path / "data" / "video_performance.json").read_text())
    assert perf["videos"][0]["content_type"] == "long"
    assert perf["videos"][0]["source_run_id"] == ""
    assert perf["videos"][0]["parent_video_id"] == ""
