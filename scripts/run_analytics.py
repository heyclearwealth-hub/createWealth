"""
run_analytics.py — Entrypoint for Workflow 4 (analytics-and-optimize).

Steps:
1. Check API quota budget before proceeding
2. Fetch 24h/48h metrics for recent uploads
3. Run optimizer (compute composite scores, update pillar weights)
4. Run A/B orchestrator for all tracked videos
"""
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

PERFORMANCE_FILE = Path("data/video_performance.json")


def _unique_trackable_video_ids(perf: dict) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for entry in perf.get("videos", []):
        video_id = str(entry.get("video_id", "") or "").strip()
        if not video_id or video_id == "dry-run" or video_id in seen:
            continue
        seen.add(video_id)
        ids.append(video_id)
    return ids


def main() -> None:
    from pipeline import quota_guard

    # Guard: need headroom for analytics queries
    if not quota_guard.can_afford("youtubeAnalytics.reports.query"):
        logger.warning("Insufficient quota for analytics run — deferring")
        sys.exit(0)

    # Step 1: Fetch metrics
    from pipeline.analytics import fetch_recent

    fetch_recent(days_back=3)

    # Step 2: Run optimizer
    from pipeline.optimizer import run as run_optimizer

    run_optimizer()

    # Step 3: A/B orchestration for all tracked videos
    from pipeline.ab_orchestrator import check_and_rotate

    if not PERFORMANCE_FILE.exists():
        logger.info("No performance data — skipping A/B orchestration")
        return

    with PERFORMANCE_FILE.open() as f:
        perf = json.load(f)

    for video_id in _unique_trackable_video_ids(perf):
        try:
            check_and_rotate(video_id)
        except Exception as exc:
            logger.warning("A/B rotation failed for %s: %s", video_id, exc)

    logger.info("Analytics and optimize run complete")


if __name__ == "__main__":
    main()
