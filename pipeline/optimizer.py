"""
optimizer.py — Watch-time-per-impression scoring and kill/scale rules.

Composite score = estimatedMinutesWatched * 60 / max(1, impressions)
Units: seconds of watch time per impression

Kill/scale thresholds (configurable via env vars):
  WATCH_TIME_PER_IMPRESSION_FLOOR: minimum score to keep pillar active (default 45s)
  RETENTION_30S_FLOOR: minimum retention ratio proxy (default 0.40)
  END_SCREEN_CTR_FLOOR: minimum end-screen CTR (default 0.008)

Optimizer updates:
  data/topic_weights.json  — pillar weights for trends.py
  data/video_performance.json  — composite_score per video
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PERFORMANCE_FILE = Path("data/video_performance.json")
WEIGHTS_FILE = Path("data/topic_weights.json")

WTPI_FLOOR = float(os.environ.get("WATCH_TIME_PER_IMPRESSION_FLOOR", "45"))
RETENTION_30S_FLOOR = float(os.environ.get("RETENTION_30S_FLOOR", "0.40"))
END_SCREEN_CTR_FLOOR = float(os.environ.get("END_SCREEN_CTR_FLOOR", "0.008"))

SCALE_MULTIPLIER = 1.25
KILL_MULTIPLIER = 0.75
MIN_WEIGHT = 0.1
MAX_WEIGHT = 5.0


def _load_performance() -> dict:
    if not PERFORMANCE_FILE.exists():
        return {"videos": []}
    with PERFORMANCE_FILE.open() as f:
        return json.load(f)


def _save_performance(data: dict) -> None:
    with PERFORMANCE_FILE.open("w") as f:
        json.dump(data, f, indent=2)


def _load_weights() -> dict:
    if not WEIGHTS_FILE.exists():
        return {}
    with WEIGHTS_FILE.open() as f:
        return json.load(f)


def _save_weights(data: dict) -> None:
    WEIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with WEIGHTS_FILE.open("w") as f:
        json.dump(data, f, indent=2)


def _read_pillar_weight(weights: dict, pillar: str) -> float:
    """
    Backward-compatible pillar weight reader.
    Supports both:
      {"pillars": {"investing": 1.0}}
      {"pillars": {"investing": {"weight": 1.0}}}
    """
    raw = weights.get("pillars", {}).get(pillar, 1.0)
    if isinstance(raw, dict):
        return _to_float(raw.get("weight")) or 1.0
    return _to_float(raw) or 1.0


def _write_pillar_weight(weights: dict, pillar: str, value: float) -> None:
    """
    Normalize to flat numeric schema:
      {"pillars": {"investing": 1.25}}
    """
    weights.setdefault("pillars", {})[pillar] = value


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compute_score(metrics: dict) -> float:
    """
    Compute watch_time_per_impression score (seconds).
    Returns 0.0 if metrics are missing or impressions == 0.
    """
    watch_minutes = _to_float(metrics.get("estimatedMinutesWatched")) or 0.0
    impressions = _to_float(metrics.get("impressions")) or 0.0
    if impressions <= 0:
        return 0.0
    return (watch_minutes * 60.0) / impressions


def _retention_proxy(metrics: dict) -> float | None:
    """
    Uses averageViewPercentage as a retention proxy when 30s retention is unavailable.
    Returns value in [0,1] when available.
    """
    avg_view_pct = _to_float(metrics.get("averageViewPercentage"))
    if avg_view_pct is None:
        return None
    if avg_view_pct > 1.0:
        return avg_view_pct / 100.0
    return avg_view_pct


def _end_screen_ctr(metrics: dict) -> float | None:
    # Optional metric; may be absent in many channels/reports.
    return _to_float(metrics.get("endScreenElementClickRate"))


def _passes_quality_floors(metrics: dict) -> bool:
    retention = _retention_proxy(metrics)
    end_screen_ctr = _end_screen_ctr(metrics)

    if retention is not None and retention < RETENTION_30S_FLOOR:
        return False
    if end_screen_ctr is not None and end_screen_ctr < END_SCREEN_CTR_FLOOR:
        return False
    return True


def run() -> None:
    """
    Main optimizer pass.
    1. Compute composite scores for all videos with 48h/24h metrics.
    2. Adjust pillar weights based on kill/scale rules.
    3. Save updated performance + weights files.
    """
    perf = _load_performance()
    weights = _load_weights()

    pillar_scores: dict[str, list[float]] = {}
    pillar_quality: dict[str, list[bool]] = {}

    for entry in perf.get("videos", []):
        video_id = entry.get("video_id", "")
        if not video_id or video_id == "dry-run":
            continue

        metrics = entry.get("metrics_48h") or entry.get("metrics_24h") or {}
        if not metrics:
            continue

        score = _compute_score(metrics)
        entry["composite_score"] = round(score, 2)

        retention = _retention_proxy(metrics)
        if retention is not None:
            entry["retention_proxy"] = round(retention, 4)

        end_screen_ctr = _end_screen_ctr(metrics)
        if end_screen_ctr is not None:
            entry["end_screen_ctr"] = round(end_screen_ctr, 4)

        pillar = entry.get("pillar", "unknown")
        pillar_scores.setdefault(pillar, []).append(score)
        pillar_quality.setdefault(pillar, []).append(_passes_quality_floors(metrics))

    _save_performance(perf)

    # Update pillar weights
    for pillar, scores in pillar_scores.items():
        avg_score = sum(scores) / len(scores)
        quality_ok = all(pillar_quality.get(pillar, [True]))
        current = _read_pillar_weight(weights, pillar)

        if avg_score >= WTPI_FLOOR and quality_ok:
            new_weight = min(MAX_WEIGHT, current * SCALE_MULTIPLIER)
            action = "scale"
        else:
            new_weight = max(MIN_WEIGHT, current * KILL_MULTIPLIER)
            action = "kill/cooldown"

        new_weight = round(new_weight, 3)
        _write_pillar_weight(weights, pillar, new_weight)
        logger.info(
            "Pillar '%s': avg_score=%.1fs, quality_ok=%s, weight %.3f → %.3f (%s)",
            pillar,
            avg_score,
            quality_ok,
            current,
            new_weight,
            action,
        )

    _save_weights(weights)
    logger.info("Optimizer run complete")
