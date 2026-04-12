"""
run_shorts.py — Entrypoint for Workflow 3 (shorts-from-approved).

Finds the latest eligible long-form video, creates a Short, uploads it,
and links the resulting short back to the long-video entry.
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

PERFORMANCE_FILE = Path("data/video_performance.json")
WORKSPACE = Path("workspace")
SHORTS_PRIVACY = os.environ.get("SHORTS_PRIVACY_STATUS", "public")


def _parse_iso(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _load_performance() -> dict:
    if not PERFORMANCE_FILE.exists():
        return {"videos": []}
    with PERFORMANCE_FILE.open() as f:
        return json.load(f)


def _save_performance(data: dict) -> None:
    PERFORMANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with PERFORMANCE_FILE.open("w") as f:
        json.dump(data, f, indent=2)


def _select_long_video(perf: dict) -> dict | None:
    target_video_id = (os.environ.get("TARGET_LONG_VIDEO_ID", "") or "").strip()

    candidates = []
    for v in perf.get("videos", []):
        video_id = v.get("video_id")
        if not video_id or video_id == "dry-run":
            continue
        if (v.get("content_type") or "long") == "short":
            continue
        if v.get("short_video_id"):
            continue
        if target_video_id and video_id != target_video_id:
            continue
        candidates.append(v)

    if not candidates:
        return None

    return sorted(candidates, key=lambda v: _parse_iso(v.get("upload_time")), reverse=True)[0]


def _link_short_to_long(perf: dict, long_video_id: str, short_video_id: str) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    for entry in perf.get("videos", []):
        if entry.get("video_id") == long_video_id:
            entry["short_video_id"] = short_video_id
            entry["short_created_at"] = now_iso
            break


def main() -> None:
    perf = _load_performance()
    entry = _select_long_video(perf)
    if not entry:
        logger.info("No eligible long video found for Shorts generation")
        return

    video_id = entry["video_id"]
    slug = entry.get("slug", "unknown")
    logger.info("Creating Short from video %s (%s)", video_id, slug)

    # We need the original rendered video + voiceover from artifact workspace.
    video_path = WORKSPACE / "output" / "final_video.mp4"
    audio_path = WORKSPACE / "voiceover.mp3"

    if not video_path.exists():
        logger.error("final_video.mp4 not found — cannot create Short")
        sys.exit(1)
    if not audio_path.exists():
        logger.error("voiceover.mp3 not found — cannot create Short")
        sys.exit(1)

    # Load pipeline.json for metadata
    pipeline_path = WORKSPACE / "pipeline.json"
    if not pipeline_path.exists():
        logger.error("pipeline.json not found")
        sys.exit(1)
    with pipeline_path.open() as f:
        pipeline_json = json.load(f)

    from pipeline.shorts import create_short

    short_path = create_short(
        video_path=video_path,
        audio_path=audio_path,
        pipeline_json=pipeline_json,
    )
    logger.info("Short rendered: %s", short_path)

    # Upload Short
    from pipeline.uploader import upload

    short_pipeline_json = {
        **pipeline_json,
        "title": f"[Short] {pipeline_json.get('title', '')}",
        "description": (
            f"{pipeline_json.get('description', '')}\n\n"
            f"▶ Watch the full video: https://youtu.be/{video_id}\n\n"
            "#Shorts #PersonalFinance #ClearWealth"
        ),
    }

    # Override metadata for short uploads.
    os.environ["VIDEO_PRIVACY_STATUS"] = SHORTS_PRIVACY
    os.environ["SOURCE_CONTENT_TYPE"] = "short"
    os.environ["PARENT_VIDEO_ID"] = video_id

    source_run_id = str(entry.get("source_run_id", "") or "").strip()
    if source_run_id:
        os.environ["SOURCE_RUN_ID"] = source_run_id

    short_id = upload(short_pipeline_json, short_path)
    logger.info("Short uploaded: https://youtu.be/%s", short_id)

    if short_id != "dry-run":
        perf = _load_performance()
        _link_short_to_long(perf, video_id, short_id)
        _save_performance(perf)
        logger.info("Linked short %s -> long video %s", short_id, video_id)


if __name__ == "__main__":
    main()
