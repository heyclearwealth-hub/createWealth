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
import re
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

FINANCE_SAFE_FALLBACK_TITLE = "Personal Finance: Practical Steps That Work"
_RISKY_PACKAGING_PATTERNS = [
    re.compile(r"\b(get rich quick|overnight wealth|overnight success)\b", re.IGNORECASE),
    re.compile(r"\b(guarantee(?:d)?|risk[- ]?free|surefire)\b", re.IGNORECASE),
    re.compile(r"\b(double|triple)\s+your\s+money\b", re.IGNORECASE),
    re.compile(r"\b\d{2,}%\s*(?:daily|weekly|monthly|return|profit)\b", re.IGNORECASE),
    re.compile(r"\byou\s+will\s+(?:make|earn)\b", re.IGNORECASE),
]
_UPLOAD_TITLE_BLOCK_PATTERNS = [
    re.compile(r"\bguarantee(?:d)?\b", re.IGNORECASE),
    re.compile(r"\brisk[- ]?free\b", re.IGNORECASE),
    re.compile(r"\bget\s+rich(?:\s+quick)?\b", re.IGNORECASE),
    re.compile(r"\bmake\s*\$?\s*\d[\d,]*(?:\.\d+)?\s*(?:/|per)?\s*(?:a\s+)?(?:day|daily)\b", re.IGNORECASE),
]


def _clean_text(value) -> str:
    return " ".join(str(value or "").split()).strip()


def _is_risky_packaging_text(text: str) -> bool:
    candidate = _clean_text(text)
    if not candidate:
        return False
    return any(pattern.search(candidate) for pattern in _RISKY_PACKAGING_PATTERNS)


def _sanitize_title_candidates(raw_titles: list, fallback_title: str) -> list[str]:
    seen: set[str] = set()
    safe_titles: list[str] = []
    for raw in raw_titles or []:
        title = _clean_text(raw)
        if not title:
            continue
        if _is_risky_packaging_text(title):
            logger.warning("Dropping risky title candidate: '%s'", title[:80])
            continue
        if len(title) > 100:
            logger.warning("Title candidate truncated to 100 chars: '%s'", title[:80])
            title = title[:100].rstrip()
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        safe_titles.append(title)

    if safe_titles:
        return safe_titles

    fallback = _clean_text(fallback_title) or FINANCE_SAFE_FALLBACK_TITLE
    if _is_risky_packaging_text(fallback):
        logger.warning("Fallback title was risky. Using neutral fallback title instead.")
        fallback = FINANCE_SAFE_FALLBACK_TITLE
    return [fallback[:100]]


def _sanitize_thumbnail_texts(raw_values) -> list[str]:
    values = raw_values if isinstance(raw_values, list) else []
    seen: set[str] = set()
    safe: list[str] = []
    for raw in values:
        text = _clean_text(raw)
        if not text:
            continue
        if _is_risky_packaging_text(text):
            logger.warning("Dropping risky thumbnail text candidate: '%s'", text[:80])
            continue
        if len(text) > 40:
            text = text[:40].rstrip()
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        safe.append(text)
    return safe


def _sanitize_description_hook(raw_hook) -> str:
    hook = _clean_text(raw_hook)
    if not hook:
        return ""
    if _is_risky_packaging_text(hook):
        logger.warning("Dropping risky description hook.")
        return ""
    if len(hook) > 350:
        logger.warning("Description hook truncated to 350 chars")
        hook = hook[:350].rstrip()
    return hook


def _is_blocked_upload_title(title: str) -> bool:
    text = _clean_text(title)
    if not text:
        return True
    return any(pattern.search(text) for pattern in _UPLOAD_TITLE_BLOCK_PATTERNS)


def _assert_upload_title_safe(title: str) -> None:
    if _is_blocked_upload_title(title):
        raise ValueError(
            "Upload blocked by title safety gate. "
            "Title contains a restricted monetization-risk pattern."
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
    if not isinstance(titles, list):
        titles = [fallback_title]

    cleaned_titles = _sanitize_title_candidates(titles, fallback_title)

    try:
        default_idx = int(candidates.get("default_index", 0) or 0)
    except (TypeError, ValueError):
        default_idx = 0

    if default_idx < 0 or default_idx >= len(cleaned_titles):
        default_idx = 0

    normalized = {
        "default_index": default_idx,
        "titles": cleaned_titles,
        "thumbnail_texts": _sanitize_thumbnail_texts(candidates.get("thumbnail_texts", [])),
        "description_hook": _sanitize_description_hook(candidates.get("description_hook", "")),
    }
    return normalized, cleaned_titles, default_idx


def _sanitize_tags(raw_tags: list) -> list[str]:
    """
    Enforce YouTube tag limits: each tag ≤30 chars, total ≤15 tags, no duplicates.
    Strips '#' prefix so tags go in the tags field (not description hashtag format).
    Logs a warning if any tag was truncated or dropped.
    """
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in (raw_tags or []):
        tag = str(raw or "").strip().lstrip("#")
        if not tag:
            continue
        if len(tag) > 30:
            logger.warning("Tag truncated to 30 chars: '%s' → '%s'", tag, tag[:30])
            tag = tag[:30]
        if tag.lower() in seen:
            continue
        seen.add(tag.lower())
        cleaned.append(tag)
        if len(cleaned) >= 15:
            logger.warning("Tag list capped at 15 (dropped %d)", len(raw_tags) - 15)
            break
    return cleaned


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
    thumbnail_path_raw = str(pipeline_json.get("thumbnail_path", "") or "").strip()
    thumbnail_path = Path(thumbnail_path_raw) if thumbnail_path_raw else None
    full_description = ((description_hook + "\n\n") if description_hook else "") + description + AI_DISCLOSURE_FOOTER
    # Warn if title and description hook are near-identical (wastes hook's click-through value).
    if description_hook and title:
        title_lower = title.lower().strip()
        hook_lower = description_hook.lower().strip()
        # Simple overlap check: if one is a substring of the other, they're redundant.
        if title_lower in hook_lower or hook_lower in title_lower or title_lower == hook_lower:
            logger.warning(
                "Title and description hook are near-identical — consider a distinct hook "
                "to improve description CTR (title='%s', hook='%s')",
                title[:60], description_hook[:60],
            )

    if len(full_description) > 4800:
        # YouTube limit is 5000 chars; leave headroom for safety.
        logger.warning("Description truncated from %d to 4800 chars", len(full_description))
        full_description = full_description[:4797] + "..."
    tags = _sanitize_tags(pipeline_json.get("tags", []))
    pillar = pipeline_json.get("pillar", "")
    video_privacy = os.environ.get("VIDEO_PRIVACY_STATUS") or "unlisted"
    if not os.environ.get("VIDEO_PRIVACY_STATUS"):
        logger.warning("VIDEO_PRIVACY_STATUS not set — defaulting to 'unlisted'. Set to 'public' when ready.")

    series_map = _load_series_map()
    playlist_id = series_map.get(pillar, {}).get("playlist_id", "")
    if not playlist_id:
        logger.warning(
            "Pillar '%s' not found in series_map — video will not be added to any playlist. "
            "Add it to data/series_map.json to enable playlist membership.",
            pillar,
        )

    payload = {
        "title": title,
        "description": full_description,
        "tags": tags,
        "pillar": pillar,
        "playlist_id": playlist_id,
        "slug": pipeline_json.get("slug", ""),
        "video_path": str(video_path),
        "thumbnail_path": str(thumbnail_path) if thumbnail_path else "",
    }

    if DRY_RUN:
        if not video_path.exists():
            raise FileNotFoundError(
                f"DRY_RUN: video file not found at {video_path} — render must complete before upload."
            )
        dry_path = Path("workspace/output/upload_payload.json")
        dry_path.parent.mkdir(parents=True, exist_ok=True)
        with dry_path.open("w") as f:
            json.dump(payload, f, indent=2)
        logger.info("DRY_RUN: upload payload written to %s", dry_path)
        _record_upload("dry-run", pipeline_json, title, candidates, default_idx)
        return "dry-run"

    # Real upload
    _assert_upload_title_safe(title)
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
            logger.error("Playlist add failed for video %s (playlist %s): %s", video_id, playlist_id, exc)

    # Apply custom thumbnail when available.
    if thumbnail_path and thumbnail_path.exists():
        try:
            quota_guard.assert_budget("thumbnails.set")
            yt.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/png"),
            ).execute()
            quota_guard.charge("thumbnails.set")
            logger.info("Custom thumbnail applied: %s", thumbnail_path)
        except Exception as exc:
            logger.error("Thumbnail set failed for video %s (%s): %s", video_id, thumbnail_path, exc)
    elif thumbnail_path:
        logger.warning("Thumbnail path provided but file not found: %s", thumbnail_path)

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
    existing = perf.get("videos", [])
    deduped = [v for v in existing if v.get("video_id") != video_id]
    if len(deduped) != len(existing):
        logger.warning("Replacing existing performance entry for video_id=%s", video_id)

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
        "current_thumbnail_variant_index": default_variant_index,
        "thumbnail_path": str(pipeline_json.get("thumbnail_path", "") or "").strip(),
        "packaging_candidates": candidates,
        "metrics_24h": {},
        "metrics_48h": {},
    }
    deduped.append(entry)
    perf["videos"] = deduped
    _save_performance(perf)
    logger.info("Recorded upload entry for %s", video_id)
