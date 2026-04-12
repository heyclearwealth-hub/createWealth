"""
footage.py — Downloads stock footage from Pexels API with ffprobe validation.
"""
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

PEXELS_API_URL = "https://api.pexels.com/videos/search"
OUTPUT_DIR = Path("workspace/clips")
TARGET_CLIP_COUNT = 10
MIN_CLIP_DURATION_SEC = 5
MIN_WIDTH = 1280


def _search_pexels(query: str, per_page: int = 20, page: int = 1) -> list[dict]:
    api_key = os.environ.get("PEXELS_API_KEY")
    if not api_key:
        raise EnvironmentError("PEXELS_API_KEY not set")

    headers = {"Authorization": api_key}
    params = {
        "query": query,
        "per_page": per_page,
        "page": page,
        "orientation": "landscape",
        "size": "medium",
    }
    resp = requests.get(PEXELS_API_URL, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("videos", [])


def _best_video_file(video: dict) -> dict | None:
    """Pick the best video file: HD preferred, at least MIN_WIDTH wide."""
    files = video.get("video_files", [])
    # Sort by width descending, then pick first that meets minimum
    files_sorted = sorted(files, key=lambda f: f.get("width", 0), reverse=True)
    for f in files_sorted:
        if f.get("width", 0) >= MIN_WIDTH and f.get("file_type", "").startswith("video/"):
            return f
    return None


def _download_clip(url: str, dest: Path) -> bool:
    """Download a video file. Returns True on success."""
    try:
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        return True
    except Exception as exc:
        logger.warning("Failed to download %s: %s", url, exc)
        if dest.exists():
            dest.unlink()
        return False


def _ffprobe_validate(path: Path) -> dict | None:
    """
    Run ffprobe on a clip. Returns a dict with {duration, width, height, has_video}
    or None if validation fails.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if not video:
            return None

        width = int(video.get("width", 0))
        height = int(video.get("height", 0))
        duration = float(video.get("duration", 0))

        # Check landscape
        if width <= height:
            logger.debug("Clip %s is not landscape (%dx%d)", path.name, width, height)
            return None
        if duration < MIN_CLIP_DURATION_SEC:
            logger.debug("Clip %s too short (%.1fs)", path.name, duration)
            return None

        return {"duration": duration, "width": width, "height": height, "has_video": True}
    except Exception as exc:
        logger.warning("ffprobe failed for %s: %s", path, exc)
        return None


# Scene-based pillar queries — describe what a viewer would see on screen,
# not the topic keyword. Pexels searches visual scenes, not concepts.
PILLAR_VISUAL_QUERIES = {
    "budgeting": [
        "person looking at bills on laptop",
        "grocery shopping cart supermarket",
        "person writing in notebook at desk",
        "smartphone banking app hands",
        "jar of coins savings",
    ],
    "debt": [
        "person stressed looking at credit card bills",
        "scissors cutting credit card",
        "person paying with credit card store",
        "calculator pen financial documents desk",
        "person relieved happy at computer",
    ],
    "investing": [
        "person using smartphone stock app",
        "graphs rising chart screen closeup",
        "young professional laptop coffee shop",
        "hands typing on laptop finance",
        "city skyline buildings business",
    ],
    "tax": [
        "person filling out tax forms paperwork",
        "calculator documents desk pen",
        "person on laptop tax filing",
        "folders documents organized office",
        "person looking at pay stub salary",
    ],
    "career_income": [
        "job interview handshake office",
        "business meeting professional team",
        "person celebrating success office",
        "salary negotiation two people talking",
        "young professional working laptop office",
    ],
}

# Generic fallback queries — always cinematic and visually engaging
GENERIC_VISUAL_QUERIES = [
    "person walking city urban lifestyle",
    "coffee shop working morning",
    "sunrise cityscape motivation",
]


def download(topic: dict, target_count: int = TARGET_CLIP_COUNT, script_data: dict | None = None) -> list[Path]:
    """
    Search and download stock footage clips relevant to the topic.
    Returns a list of validated clip paths.

    If script_data is provided, extracts 2-3 scene-specific queries from the
    hook_summary and worked example for more relevant B-roll.
    """
    pillar = topic.get("pillar", "")

    # Build query list: script-aware queries first, then pillar visuals, then generic
    queries: list[str] = []

    # Script-aware queries from hook and worked example context
    if script_data:
        hook = script_data.get("hook_summary", "")
        # Extract the core visual scene from hook — look for dollar amounts, people, actions
        if hook:
            # Derive a short visual search phrase from the hook
            # If hook mentions a person scenario, make it visual
            hook_lower = hook.lower()
            if any(w in hook_lower for w in ["savings account", "save", "saving"]):
                queries.append("person checking savings account mobile phone")
            elif any(w in hook_lower for w in ["debt", "pay off", "loan"]):
                queries.append("person stressed bills financial paperwork")
            elif any(w in hook_lower for w in ["invest", "roth", "401k", "ira", "stock"]):
                queries.append("young person investing smartphone app")
            elif any(w in hook_lower for w in ["tax", "deduct", "refund"]):
                queries.append("person filing taxes documents computer")
            elif any(w in hook_lower for w in ["salary", "raise", "income", "earn"]):
                queries.append("professional salary negotiation office")

    # Add pillar-specific visual queries
    pillar_visuals = PILLAR_VISUAL_QUERIES.get(pillar, [])
    queries.extend(pillar_visuals)

    # Add generic fallbacks
    queries.extend(GENERIC_VISUAL_QUERIES)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    valid_clips: list[Path] = []
    seen_ids: set[int] = set()

    for query in queries:
        if len(valid_clips) >= target_count:
            break

        for page in range(1, 4):
            if len(valid_clips) >= target_count:
                break

            logger.info("Searching Pexels: query='%s' page=%d", query, page)
            try:
                videos = _search_pexels(query, per_page=15, page=page)
            except Exception as exc:
                logger.warning("Pexels search failed: %s", exc)
                break

            for video in videos:
                if len(valid_clips) >= target_count:
                    break

                vid_id = video.get("id")
                if vid_id in seen_ids:
                    continue
                seen_ids.add(vid_id)

                file_info = _best_video_file(video)
                if not file_info:
                    continue

                dest = OUTPUT_DIR / f"clip_{len(valid_clips):02d}.mp4"
                if not _download_clip(file_info["link"], dest):
                    continue

                meta = _ffprobe_validate(dest)
                if not meta:
                    logger.debug("Clip %s failed validation, discarding", dest.name)
                    dest.unlink(missing_ok=True)
                    continue

                valid_clips.append(dest)
                logger.info("Downloaded valid clip %s (%.1fs %dx%d)",
                            dest.name, meta["duration"], meta["width"], meta["height"])
                time.sleep(0.2)  # polite rate limiting

    if not valid_clips:
        raise RuntimeError("No valid clips downloaded from Pexels")

    logger.info("Downloaded %d valid clips", len(valid_clips))
    return valid_clips
