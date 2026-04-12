"""
ab_orchestrator.py — Manages title/thumbnail A/B experiments.
Mode: native_preferred — tries YouTube Studio native test first,
falls back to API metadata rotation if native test not started within SLA.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from pipeline import quota_guard

logger = logging.getLogger(__name__)

PERFORMANCE_FILE = Path("data/video_performance.json")
WORKSPACE_CANDIDATES_FILE = Path("workspace/package_candidates.json")
SLA_HOURS = int(os.environ.get("NATIVE_AB_SLA_HOURS", "24"))
MODE = os.environ.get("PACKAGING_EXPERIMENT_MODE", "native_preferred")
IMPRESSION_MIN = int(os.environ.get("IMPRESSION_MIN_FOR_EXPERIMENT", "1000"))
CTR_FLOOR = float(os.environ.get("CTR_FLOOR", "0.045"))


def _youtube_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube"],
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _load_performance() -> dict:
    if not PERFORMANCE_FILE.exists():
        return {"videos": []}
    with PERFORMANCE_FILE.open() as f:
        return json.load(f)


def _save_performance(data: dict) -> None:
    with PERFORMANCE_FILE.open("w") as f:
        json.dump(data, f, indent=2)


def _load_workspace_candidates() -> dict:
    if not WORKSPACE_CANDIDATES_FILE.exists():
        return {}
    with WORKSPACE_CANDIDATES_FILE.open() as f:
        return json.load(f)


def _titles_for_video(video_entry: dict) -> list[str]:
    packaging = video_entry.get("packaging_candidates")
    if isinstance(packaging, dict):
        titles = packaging.get("titles", [])
        if isinstance(titles, list) and titles:
            return [str(t) for t in titles if t]

    # Backward compatibility for older entries.
    workspace = _load_workspace_candidates()
    titles = workspace.get("titles", []) if isinstance(workspace, dict) else []
    return [str(t) for t in titles if t]


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def check_and_rotate(video_id: str) -> None:
    """
    For a published video:
    1. If native test was set up and within SLA — do nothing (let YouTube run it).
    2. If SLA exceeded and no native test started — rotate to next title candidate via API.
    3. Log result to video_performance.json.
    """
    if MODE == "disabled":
        return

    perf = _load_performance()
    video_entry = next((v for v in perf.get("videos", []) if v.get("video_id") == video_id), None)
    if not video_entry:
        logger.warning("No performance entry for video %s", video_id)
        return

    titles = _titles_for_video(video_entry)
    current_variant = int(video_entry.get("current_variant_index", 0) or 0)

    if len(titles) < 2:
        logger.info("Skipping A/B fallback for %s: fewer than 2 title candidates", video_id)
        return

    # Check if native test is active (field set by uploader.py after creation)
    native_test_started = video_entry.get("native_test_started", False)
    upload_time = video_entry.get("upload_time")
    if not upload_time:
        return

    try:
        upload_dt = datetime.fromisoformat(upload_time.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Invalid upload_time for %s: %s", video_id, upload_time)
        return

    hours_since_upload = (datetime.now(timezone.utc) - upload_dt).total_seconds() / 3600

    if native_test_started:
        logger.info("Native A/B test active for %s — no API rotation needed", video_id)
        return

    if hours_since_upload < SLA_HOURS:
        logger.info(
            "Within SLA (%.1fh / %dh) for %s — waiting for native test",
            hours_since_upload,
            SLA_HOURS,
            video_id,
        )
        return

    metrics = video_entry.get("metrics_48h") or video_entry.get("metrics_24h") or {}
    impressions = _safe_float(metrics.get("impressions"), 0.0)
    ctr = _safe_float(metrics.get("impressionClickThroughRate"), 0.0)

    if impressions > 0 and impressions < IMPRESSION_MIN:
        logger.info(
            "Insufficient impressions for %s (%d < %d) — skipping rotation",
            video_id,
            int(impressions),
            IMPRESSION_MIN,
        )
        return

    if impressions >= IMPRESSION_MIN and ctr >= CTR_FLOOR:
        logger.info(
            "CTR already healthy for %s (%.4f >= %.4f) — no rotation",
            video_id,
            ctr,
            CTR_FLOOR,
        )
        return

    # SLA exceeded, no native test — rotate to next variant
    next_variant = (current_variant + 1) % len(titles)
    next_title = titles[next_variant]

    if not next_title:
        logger.info("No valid next title variant for %s", video_id)
        return

    logger.info("SLA exceeded for %s — rotating title to variant %d: '%s'", video_id, next_variant, next_title)

    try:
        yt = _youtube_service()

        quota_guard.assert_budget("videos.list")
        resp = yt.videos().list(part="snippet", id=video_id).execute()
        quota_guard.charge("videos.list")

        items = resp.get("items", [])
        if not items:
            logger.warning("Video %s not found on YouTube", video_id)
            return

        snippet = items[0].get("snippet", {})
        snippet["title"] = next_title

        quota_guard.assert_budget("videos.update")
        yt.videos().update(part="snippet", body={"id": video_id, "snippet": snippet}).execute()
        quota_guard.charge("videos.update")

        logger.info("Title updated for %s → variant %d", video_id, next_variant)

        video_entry["current_variant_index"] = next_variant
        video_entry["last_rotated"] = datetime.now(timezone.utc).isoformat()
        _save_performance(perf)

    except Exception as exc:
        logger.error("Title rotation failed for %s: %s", video_id, exc)
