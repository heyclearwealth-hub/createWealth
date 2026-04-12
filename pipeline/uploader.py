"""
uploader.py — YouTube Data API v3 resumable upload.

Features:
- containsSyntheticMedia: True (AI voiceover disclosure)
- DRY_RUN=1 mode: writes upload_payload.json instead of calling YouTube
- Sets playlist membership and series end-screen bridge
- Saves upload result to data/video_performance.json entry
"""
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

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
PERFORMANCE_FILE = Path("data/video_performance.json")
OUTPUT_PATH = Path("workspace/output/final_video.mp4")
PACKAGE_CANDIDATES_PATH = Path("workspace/package_candidates.json")

AI_DISCLOSURE_FOOTER = (
    "\n\n⚠️ This video uses AI-generated voiceover and AI-assisted script writing.\n"
    "⚠️ This is for educational purposes only. Not financial advice."
)


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
    PERFORMANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with PERFORMANCE_FILE.open("w") as f:
        json.dump(data, f, indent=2)


def _load_series_map() -> dict:
    p = Path("data/series_map.json")
    if not p.exists():
        return {}
    with p.open() as f:
        data = json.load(f)
    # Support both {"pillars": {...}} and flat {"pillar": {...}} structures
    return data.get("pillars", data)


def _normalize_candidates(raw: dict, fallback_title: str) -> tuple[dict, list[str], int]:
    candidates = raw if isinstance(raw, dict) else {}
    titles = candidates.get("titles", [fallback_title])
    if not isinstance(titles, list) or not titles:
        titles = [fallback_title]

    cleaned_titles = [str(t) for t in titles if t]
    if not cleaned_titles:
        cleaned_titles = [fallback_title]

    try:
        default_idx = int(candidates.get("default_index", 0) or 0)
    except (TypeError, ValueError):
        default_idx = 0

    if default_idx < 0 or default_idx >= len(cleaned_titles):
        default_idx = 0

    normalized = {
        "default_index": default_idx,
        "titles": cleaned_titles,
        "thumbnail_texts": candidates.get("thumbnail_texts", []),
        "description_hook": str(candidates.get("description_hook", "") or ""),
    }
    return normalized, cleaned_titles, default_idx


def _normalize_content_type(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value in {"short", "long"}:
        return value
    return "long"


def upload(pipeline_json: dict, video_path: Path = OUTPUT_PATH) -> str:
    """
    Upload final_video.mp4 to YouTube using the default packaging candidate.
    Returns the YouTube video ID (or "dry-run" in DRY_RUN mode).
    """
    raw_candidates: dict = {}
    if PACKAGE_CANDIDATES_PATH.exists():
        with PACKAGE_CANDIDATES_PATH.open() as f:
            raw_candidates = json.load(f)

    fallback_title = str(pipeline_json.get("title", "Untitled"))
    candidates, titles, default_idx = _normalize_candidates(raw_candidates, fallback_title)

    title = titles[default_idx]
    description_hook = candidates.get("description_hook", "")
    description = str(pipeline_json.get("description", ""))
    full_description = ((description_hook + "\n\n") if description_hook else "") + description + AI_DISCLOSURE_FOOTER
    tags = pipeline_json.get("tags", [])
    pillar = pipeline_json.get("pillar", "")
    video_privacy = os.environ.get("VIDEO_PRIVACY_STATUS", "public")

    series_map = _load_series_map()
    playlist_id = series_map.get(pillar, {}).get("playlist_id", "")

    payload = {
        "title": title,
        "description": full_description,
        "tags": tags,
        "pillar": pillar,
        "playlist_id": playlist_id,
        "slug": pipeline_json.get("slug", ""),
        "video_path": str(video_path),
    }

    if DRY_RUN:
        dry_path = Path("workspace/output/upload_payload.json")
        dry_path.parent.mkdir(parents=True, exist_ok=True)
        with dry_path.open("w") as f:
            json.dump(payload, f, indent=2)
        logger.info("DRY_RUN: upload payload written to %s", dry_path)
        _record_upload("dry-run", pipeline_json, title, candidates, default_idx)
        return "dry-run"

    # Real upload
    quota_guard.assert_budget("videos.insert")

    yt = _youtube_service()

    body = {
        "snippet": {
            "title": title,
            "description": full_description,
            "tags": tags,
            "categoryId": "27",  # Education
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": video_privacy,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=4 * 1024 * 1024,  # 4MB chunks
    )

    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info("Upload progress: %.1f%%", status.progress() * 100)

    video_id = response["id"]
    quota_guard.charge("videos.insert")
    logger.info("Uploaded video: https://youtu.be/%s", video_id)

    # Add to playlist
    if playlist_id:
        try:
            quota_guard.assert_budget("playlistItems.insert")
            yt.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
            ).execute()
            quota_guard.charge("playlistItems.insert")
            logger.info("Added to playlist %s", playlist_id)
        except Exception as exc:
            logger.warning("Playlist add failed (non-fatal): %s", exc)

    _record_upload(video_id, pipeline_json, title, candidates, default_idx)
    return video_id


def _record_upload(
    video_id: str,
    pipeline_json: dict,
    title: str,
    candidates: dict,
    default_variant_index: int,
) -> None:
    perf = _load_performance()
    content_type = _normalize_content_type(os.environ.get("SOURCE_CONTENT_TYPE", "long"))
    source_run_id = str(os.environ.get("SOURCE_RUN_ID", "") or "").strip()
    parent_video_id = str(os.environ.get("PARENT_VIDEO_ID", "") or "").strip()

    entry = {
        "video_id": video_id,
        "slug": pipeline_json.get("slug", ""),
        "pillar": pipeline_json.get("pillar", ""),
        "title": title,
        "upload_time": datetime.now(timezone.utc).isoformat(),
        "content_type": content_type,
        "source_run_id": source_run_id,
        "parent_video_id": parent_video_id,
        "native_test_started": False,
        "current_variant_index": default_variant_index,
        "packaging_candidates": candidates,
        "metrics_24h": {},
        "metrics_48h": {},
    }
    perf["videos"].append(entry)
    _save_performance(perf)
    logger.info("Recorded upload entry for %s", video_id)
