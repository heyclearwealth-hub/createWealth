"""Unit tests for uploader.py."""
import json
from unittest.mock import patch

import pytest
import pipeline.uploader as up


def test_normalize_candidates_drops_risky_variants():
    raw = {
        "default_index": 0,
        "titles": [
            "Guaranteed 50% weekly return",
            "Practical ETF setup for beginners",
        ],
        "thumbnail_texts": [
            "RISK-FREE MONEY",
            "ETF SETUP MADE SIMPLE",
        ],
        "description_hook": "You will make money fast with this method.",
    }

    normalized, titles, default_idx = up._normalize_candidates(raw, "Fallback Title")

    assert titles == ["Practical ETF setup for beginners"]
    assert default_idx == 0
    assert normalized["description_hook"] == ""
    assert normalized["thumbnail_texts"] == ["ETF SETUP MADE SIMPLE"]


def test_normalize_candidates_uses_safe_fallback_when_all_titles_risky():
    raw = {"default_index": 0, "titles": ["Get rich quick with this one trick"]}
    normalized, titles, default_idx = up._normalize_candidates(raw, "You will make money daily")

    assert titles == [up.FINANCE_SAFE_FALLBACK_TITLE]
    assert normalized["titles"] == [up.FINANCE_SAFE_FALLBACK_TITLE]
    assert default_idx == 0


def test_record_upload_replaces_existing_video_entry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    perf_file = tmp_path / "data" / "video_performance.json"
    perf_file.write_text(
        json.dumps(
            {
                "videos": [
                    {"video_id": "vid-1", "slug": "old"},
                    {"video_id": "vid-2", "slug": "other"},
                ]
            }
        )
    )
    monkeypatch.setattr(up, "PERFORMANCE_FILE", perf_file)

    up._record_upload(
        video_id="vid-1",
        pipeline_json={"slug": "new", "pillar": "investing"},
        title="Fresh Title",
        candidates={"default_index": 0, "titles": ["Fresh Title"]},
        default_variant_index=0,
    )

    saved = json.loads(perf_file.read_text())
    ids = [v["video_id"] for v in saved["videos"]]
    assert ids.count("vid-1") == 1
    assert ids.count("vid-2") == 1
    updated = next(v for v in saved["videos"] if v["video_id"] == "vid-1")
    assert updated["slug"] == "new"
    assert updated["current_thumbnail_variant_index"] == 0


@pytest.mark.parametrize(
    "title",
    [
        "Guaranteed profits with this strategy",
        "Risk-free returns for beginners",
        "Get rich quick in 30 days",
        "Make $500/day with this trick",
    ],
)
def test_upload_title_gate_blocks_risky_titles(title):
    with pytest.raises(ValueError, match="Upload blocked by title safety gate"):
        up._assert_upload_title_safe(title)


def test_upload_title_gate_allows_safe_title():
    up._assert_upload_title_safe("How to Build a Long-Term ETF Plan")


def test_upload_blocks_before_youtube_insert_when_title_is_risky(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace/output").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/video_performance.json").write_text('{"videos":[]}')
    (tmp_path / "data/series_map.json").write_text("{}")
    (tmp_path / "data/api_budget.json").write_text('{"daily_units_used":0,"date":"1970-01-01"}')

    video_path = tmp_path / "workspace/output/final_video.mp4"
    video_path.write_bytes(b"x" * 200_000)

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(up, "DRY_RUN", False)
    monkeypatch.setattr(up, "PERFORMANCE_FILE", tmp_path / "data/video_performance.json")
    monkeypatch.setattr(up, "PACKAGE_CANDIDATES_PATH", tmp_path / "workspace/package_candidates.json")

    pipeline_json = {
        "title": "Neutral fallback title",
        "description": "desc",
        "tags": [],
        "pillar": "investing",
        "slug": "s",
    }

    with patch("pipeline.uploader._normalize_candidates", return_value=(
        {"default_index": 0, "titles": ["Make $400/day fast"], "thumbnail_texts": [], "description_hook": ""},
        ["Make $400/day fast"],
        0,
    )):
        with patch("pipeline.uploader._youtube_service") as mock_youtube:
            with pytest.raises(ValueError, match="Upload blocked by title safety gate"):
                up.upload(pipeline_json, video_path)
            mock_youtube.assert_not_called()
