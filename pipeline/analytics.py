"""
analytics.py — Fetch 24h and 48h performance metrics for recent uploads.

Metrics fetched per video:
- views
- estimatedMinutesWatched  (watch time)
- impressions
- impressionClickThroughRate (CTR)
- averageViewDuration
- averageViewPercentage

Saves to data/video_performance.json under metrics_24h / metrics_48h.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from pipeline import quota_guard

logger = logging.getLogger(__name__)

PERFORMANCE_FILE = Path("data/video_performance.json")

ANALYTICS_METRICS = ",".join(
    [
        "views",
        "estimatedMinutesWatched",
        "impressions",
        "impressionClickThroughRate",
        "averageViewDuration",
        "averageViewPercentage",   # completion rate proxy — weighted heavily for Shorts
        "subscribersGained",       # follow conversion signal
        "subscribersLost",
    ]
)


def _youtube_analytics():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/yt-analytics.readonly"],
    )
    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)


def _load_performance() -> dict:
    if not PERFORMANCE_FILE.exists():
        return {"videos": []}
    with PERFORMANCE_FILE.open() as f:
        return json.load(f)


def _save_performance(data: dict) -> None:
    PERFORMANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with PERFORMANCE_FILE.open("w") as f:
        json.dump(data, f, indent=2)


def _fetch_metrics(yta, video_id: str, start_date: str, end_date: str) -> dict:
    """Fetch metrics for a single video over a date range."""
    quota_guard.assert_budget("youtubeAnalytics.reports.query")
    resp = yta.reports().query(
        ids="channel==MINE",
        startDate=start_date,
        endDate=end_date,
        metrics=ANALYTICS_METRICS,
        dimensions="video",
        filters=f"video=={video_id}",
    ).execute()
    quota_guard.charge("youtubeAnalytics.reports.query")

    rows = resp.get("rows", [])
    if not rows:
        return {}

    col_headers = [h["name"] for h in resp.get("columnHeaders", [])]
    row = rows[0]
    return dict(zip(col_headers, row))


ARCHIVE_AFTER_DAYS = int(os.environ.get("ANALYTICS_ARCHIVE_DAYS", "30"))


def fetch_recent(days_back: int = 2) -> None:
    """
    Fetch metrics for all videos uploaded within the last `days_back` days.
    Stores 24h and 48h snapshots based on hours since upload.
    Videos older than ARCHIVE_AFTER_DAYS are skipped to avoid wasting quota.
    """
    perf = _load_performance()
    yta = _youtube_analytics()
    now = datetime.now(timezone.utc)

    updated = False
    skipped_archive = 0
    skipped_days_back = 0
    for entry in perf.get("videos", []):
        video_id = entry.get("video_id")
        if not video_id or video_id == "dry-run":
            continue

        upload_time_str = entry.get("upload_time")
        if not upload_time_str:
            continue

        upload_time = datetime.fromisoformat(upload_time_str.replace("Z", "+00:00"))
        hours_since = (now - upload_time).total_seconds() / 3600

        # Respect requested scan window first (used by scheduler cadence and manual runs).
        if days_back > 0 and hours_since > days_back * 24:
            skipped_days_back += 1
            continue

        # Skip videos beyond the archive window — no point querying forever.
        if hours_since > ARCHIVE_AFTER_DAYS * 24:
            skipped_archive += 1
            continue

        upload_date = upload_time.strftime("%Y-%m-%d")

        # 24h window — retry if empty (uploader seeds {} as placeholder).
        if hours_since >= 24 and not entry.get("metrics_24h"):
            day1_end = (upload_time + timedelta(days=1)).strftime("%Y-%m-%d")
            try:
                m = _fetch_metrics(yta, video_id, upload_date, day1_end)
                if m:
                    entry["metrics_24h"] = m
                    updated = True
                    logger.info("Fetched 24h metrics for %s", video_id)
                else:
                    logger.info("24h metrics not yet available for %s (YouTube lag)", video_id)
            except Exception as exc:
                logger.warning("24h fetch failed for %s: %s — will retry next run", video_id, exc)

        # 48h window
        if hours_since >= 48 and not entry.get("metrics_48h"):
            day2_end = (upload_time + timedelta(days=2)).strftime("%Y-%m-%d")
            try:
                m = _fetch_metrics(yta, video_id, upload_date, day2_end)
                if m:
                    entry["metrics_48h"] = m
                    updated = True
                    logger.info("Fetched 48h metrics for %s", video_id)
                else:
                    logger.info("48h metrics not yet available for %s (YouTube lag)", video_id)
            except Exception as exc:
                logger.warning("48h fetch failed for %s: %s — will retry next run", video_id, exc)

    if skipped_archive:
        logger.info("Skipped %d archived videos (>%d days old)", skipped_archive, ARCHIVE_AFTER_DAYS)
    if skipped_days_back:
        logger.info("Skipped %d videos outside days_back=%d window", skipped_days_back, days_back)

    if updated:
        _save_performance(perf)
        logger.info("Performance data updated")
    else:
        logger.info("No new metrics to fetch")
