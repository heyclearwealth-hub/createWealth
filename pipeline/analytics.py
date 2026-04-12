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
        "averageViewPercentage",
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


def fetch_recent(days_back: int = 2) -> None:
    """
    Fetch metrics for all videos uploaded within the last `days_back` days.
    Stores 24h and 48h snapshots based on hours since upload.
    """
    perf = _load_performance()
    yta = _youtube_analytics()
    now = datetime.now(timezone.utc)

    updated = False
    for entry in perf.get("videos", []):
        video_id = entry.get("video_id")
        if not video_id or video_id == "dry-run":
            continue

        upload_time_str = entry.get("upload_time")
        if not upload_time_str:
            continue

        upload_time = datetime.fromisoformat(upload_time_str.replace("Z", "+00:00"))
        hours_since = (now - upload_time).total_seconds() / 3600

        upload_date = upload_time.strftime("%Y-%m-%d")

        # 24h window
        if hours_since >= 24 and not entry.get("metrics_24h"):
            day1_end = (upload_time + timedelta(days=1)).strftime("%Y-%m-%d")
            try:
                m = _fetch_metrics(yta, video_id, upload_date, day1_end)
                if m:
                    entry["metrics_24h"] = m
                    updated = True
                    logger.info("Fetched 24h metrics for %s", video_id)
            except Exception as exc:
                logger.warning("24h fetch failed for %s: %s", video_id, exc)

        # 48h window
        if hours_since >= 48 and not entry.get("metrics_48h"):
            day2_end = (upload_time + timedelta(days=2)).strftime("%Y-%m-%d")
            try:
                m = _fetch_metrics(yta, video_id, upload_date, day2_end)
                if m:
                    entry["metrics_48h"] = m
                    updated = True
                    logger.info("Fetched 48h metrics for %s", video_id)
            except Exception as exc:
                logger.warning("48h fetch failed for %s: %s", video_id, exc)

    if updated:
        _save_performance(perf)
        logger.info("Performance data updated")
    else:
        logger.info("No new metrics to fetch")
