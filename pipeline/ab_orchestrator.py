"""
ab_orchestrator.py — Manages title/thumbnail A/B experiments.
Mode: native_preferred — tries YouTube Studio native test first,
falls back to API metadata rotation if native test not started within SLA.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from pipeline import quota_guard

logger = logging.getLogger(__name__)

PERFORMANCE_FILE = Path("data/video_performance.json")
WORKSPACE_CANDIDATES_FILE = Path("workspace/package_candidates.json")
SLA_HOURS = int(os.environ.get("NATIVE_AB_SLA_HOURS", "24"))
MODE = os.environ.get("PACKAGING_EXPERIMENT_MODE", "native_preferred")
IMPRESSION_MIN = int(os.environ.get("IMPRESSION_MIN_FOR_EXPERIMENT", "1000"))
CTR_FLOOR = float(os.environ.get("CTR_FLOOR", "0.045"))  # absolute fallback only
# A video must be this far *below* channel average before we rotate (0.85 = 15% below avg).
CTR_RELATIVE_THRESHOLD = float(os.environ.get("CTR_RELATIVE_THRESHOLD", "0.85"))
# Early-rotation gate: Shorts get 70% of algorithmic push in first 2h.
# If a title gets enough impressions fast but CTR is weak, rotate before SLA expires.
EARLY_ROTATION_HOURS = float(os.environ.get("EARLY_ROTATION_HOURS", "2.0"))
EARLY_IMPRESSION_MIN = int(os.environ.get("EARLY_IMPRESSION_MIN", "500"))
EARLY_CTR_THRESHOLD = float(os.environ.get("EARLY_CTR_THRESHOLD", "0.04"))
# Completion rate threshold — rotate if Shorts completion is below this (Shorts-specific signal).
COMPLETION_MIN_PERCENT = float(os.environ.get("COMPLETION_MIN_PERCENT", "25.0"))


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


def _thumbnail_variants_for_video(video_entry: dict) -> list[str]:
    packaging = video_entry.get("packaging_candidates")
    if not isinstance(packaging, dict):
        return []

    variants = packaging.get("thumbnail_texts", [])
    if not isinstance(variants, list):
        return []

    cleaned = [" ".join(str(v or "").split()).strip() for v in variants]
    cleaned = [v for v in cleaned if v]
    if len(cleaned) < 2:
        return []
    return cleaned


def _resolve_thumbnail_for_variant(video_entry: dict, variant_index: int) -> Path | None:
    variants = _thumbnail_variants_for_video(video_entry)
    if not variants or variant_index < 0:
        return None

    try:
        from pipeline.thumbnail_gen import generate_thumbnails
    except Exception as exc:
        logger.warning("thumbnail_gen unavailable for A/B rotation: %s", exc)
        return None

    try:
        generated = generate_thumbnails(variants, pillar=str(video_entry.get("pillar", "") or ""))
    except Exception as exc:
        logger.warning("Thumbnail generation failed during A/B rotation: %s", exc)
        return None

    if variant_index >= len(generated):
        return None

    path = Path(generated[variant_index])
    if not path.exists():
        return None
    return path


def check_and_rotate(video_id: str) -> None:
    """
    For a published video:
    1. If native test was set up and within SLA — do nothing (let YouTube run it).
    2. If SLA exceeded and no native test started — rotate to next metadata variant via API.
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

    metrics = video_entry.get("metrics_48h") or video_entry.get("metrics_24h") or {}
    impressions = _safe_float(metrics.get("impressions"), 0.0)
    ctr = _safe_float(metrics.get("impressionClickThroughRate"), 0.0)
    # Use None sentinel so we can distinguish "API returned 0%" (low completion) from
    # "field not present yet" (no data). _safe_float with default=None preserves the None.
    _raw_completion = metrics.get("averageViewPercentage")
    completion_pct: float | None = None if _raw_completion is None else _safe_float(_raw_completion, 0.0)

    # Early-rotation gate: Shorts get 70% of algorithmic push in the first 2h.
    # If we have enough signal and CTR is already weak, don't wait for the full SLA.
    if (
        hours_since_upload <= EARLY_ROTATION_HOURS
        and impressions >= EARLY_IMPRESSION_MIN
        and ctr < EARLY_CTR_THRESHOLD
    ):
        logger.info(
            "Early rotation triggered for %s: %.1fh old, %d impressions, CTR=%.4f < %.4f threshold",
            video_id, hours_since_upload, int(impressions), ctr, EARLY_CTR_THRESHOLD,
        )
        # Fall through to rotation logic below.

    elif hours_since_upload < SLA_HOURS:
        logger.info(
            "Within SLA (%.1fh / %dh) for %s — waiting for native test",
            hours_since_upload,
            SLA_HOURS,
            video_id,
        )
        return

    if impressions > 0 and impressions < IMPRESSION_MIN:
        logger.info(
            "Insufficient impressions for %s (%d < %d) — skipping rotation",
            video_id,
            int(impressions),
            IMPRESSION_MIN,
        )
        return

    if impressions >= IMPRESSION_MIN:
        # Compute channel-average CTR from all other videos that have enough impressions.
        channel_ctrs = [
            _safe_float(
                (v.get("metrics_48h") or v.get("metrics_24h") or {}).get("impressionClickThroughRate"),
                0.0,
            )
            for v in perf.get("videos", [])
            if v.get("video_id") != video_id
            and _safe_float(
                (v.get("metrics_48h") or v.get("metrics_24h") or {}).get("impressions"), 0.0
            ) >= IMPRESSION_MIN
        ]
        if channel_ctrs:
            channel_avg_ctr = sum(channel_ctrs) / len(channel_ctrs)
            healthy_threshold = channel_avg_ctr * CTR_RELATIVE_THRESHOLD
        else:
            # No channel history yet — fall back to absolute floor.
            healthy_threshold = CTR_FLOOR
            logger.info(
                "No healthy channel history available — using absolute CTR_FLOOR=%.3f for %s",
                CTR_FLOOR, video_id,
            )

        ctr_healthy = ctr >= healthy_threshold

        # Completion rate check (Shorts-specific signal — weighted more than CTR by the algorithm).
        # completion_pct=None  → API hasn't populated the field yet, skip check.
        # completion_pct=0.0   → API returned zero, which is real data (very poor completion).
        completion_healthy = True
        if completion_pct is not None:
            completion_healthy = completion_pct >= COMPLETION_MIN_PERCENT
            if not completion_healthy:
                logger.info(
                    "Completion rate low for %s (%.1f%% < %.1f%% min) — rotation warranted",
                    video_id, completion_pct, COMPLETION_MIN_PERCENT,
                )

        if ctr_healthy and completion_healthy:
            completion_str = f"{completion_pct:.1f}%" if completion_pct is not None else "n/a"
            logger.info(
                "Performance healthy for %s (CTR=%.4f >= %.4f, completion=%s) — no rotation",
                video_id, ctr, healthy_threshold, completion_str,
            )
            return
        if not ctr_healthy:
            logger.info(
                "CTR underperforming for %s (%.4f < %.4f threshold) — rotating",
                video_id, ctr, healthy_threshold,
            )

    # SLA exceeded (or early gate triggered), no native test — rotate to next variant
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

        thumbnail_path = _resolve_thumbnail_for_variant(video_entry, next_variant)
        if thumbnail_path:
            try:
                quota_guard.assert_budget("thumbnails.set")
                yt.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/png"),
                ).execute()
                quota_guard.charge("thumbnails.set")
                video_entry["current_thumbnail_variant_index"] = next_variant
                video_entry["thumbnail_path"] = str(thumbnail_path)
                logger.info("Thumbnail updated for %s → variant %d", video_id, next_variant)
            except Exception as exc:
                logger.warning("Thumbnail rotation failed for %s: %s", video_id, exc)
        else:
            logger.info("No thumbnail variants available for %s; rotated title only", video_id)

        video_entry["current_variant_index"] = next_variant
        video_entry["last_rotated"] = datetime.now(timezone.utc).isoformat()
        _save_performance(perf)

    except Exception as exc:
        logger.error("Title rotation failed for %s: %s", video_id, exc)
