"""
footage.py — Downloads stock footage from Pexels API with ffprobe validation.

NOTE: This module is used for LONG-FORM video B-roll only.
For YouTube Shorts, B-roll is NOT used — the Shorts renderer uses gradient
backgrounds + text overlays, which load faster and look cleaner at 9:16.
The Pexels queries here are intentionally visual/scene-based (not thematic)
since Pexels searches what a viewer would see on screen, not concepts.
"""
from __future__ import annotations

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
MAX_CLIPS_PER_QUERY = 2
MAX_CLIPS_PER_BUCKET = 4


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
        "counting cash money hands closeup",
        "person frustrated looking at bills",
        "grocery store checkout shopping",
        "phone banking transfer notification",
        "piggy bank coins saving money",
        "budget spreadsheet laptop stressed",
        "wallet empty no money",
    ],
    "debt": [
        "credit card debt stress anxiety",
        "person tearing up credit card",
        "debt free celebration happy person",
        "loan documents signing bank",
        "person paying bills relief",
        "envelope bills mailbox overdue",
        "financial freedom person arms raised",
    ],
    "investing": [
        "stock market chart green rising",
        "young woman smiling phone investment",
        "compound interest growth visualization",
        "brokerage account mobile phone hands",
        "retirement savings nest egg",
        "real estate property investment",
        "downtown city financial district aerial",
    ],
    "tax": [
        "tax refund check money excited",
        "person doing taxes frustrated paperwork",
        "IRS tax return filing laptop",
        "paycheck stub income earnings",
        "tax savings receipt documents",
        "accountant reviewing documents office",
        "money back tax return happy",
    ],
    "career_income": [
        "job offer letter excited person",
        "salary raise negotiation handshake",
        "promotion celebration office coworkers",
        "resume job application laptop",
        "side hustle freelance working home",
        "paycheck direct deposit notification phone",
        "confident professional presentation meeting",
    ],
}

# Job-specific B-roll — matched to common case study personas
JOB_VISUAL_QUERIES = {
    "nurse": ["nurse hospital scrubs corridor", "medical professional caring patient"],
    "teacher": ["teacher classroom students whiteboard", "school hallway education"],
    "software engineer": ["developer coding dual monitors dark", "tech office modern open space"],
    "engineer": ["engineer blueprint technical drawing", "construction site hard hat"],
    "marketing manager": ["marketing team brainstorm office", "social media analytics laptop"],
    "accountant": ["accountant reviewing financial statements", "office desk numbers spreadsheet"],
    "graphic designer": ["graphic designer creative studio tablet", "designer color palette screen"],
    "project manager": ["project manager whiteboard planning", "team meeting agile scrum"],
    "sales": ["sales person phone call smiling office", "deal closed handshake business"],
    "real estate": ["real estate agent showing house", "house keys new home excited"],
    "doctor": ["doctor hospital white coat stethoscope", "medical office professional"],
    "lawyer": ["lawyer law office books professional", "courtroom legal professional"],
    "dentist": ["dentist office professional medical", "healthcare worker clinic"],
}

# Generic fallback queries — cinematic, motion-rich, emotionally resonant
GENERIC_VISUAL_QUERIES = [
    "young professional city morning commute",
    "person opening laptop at window sunrise",
    "hands counting dollar bills closeup",
    "person smiling looking at phone good news",
    "aerial city skyline sunset timelapse",
    "coffee shop laptop focused working",
]


def _job_queries(job_title: str) -> list[str]:
    """Return 2 job-specific visual queries for the case study persona."""
    if not job_title:
        return []
    job_lower = job_title.lower()
    for key, queries in JOB_VISUAL_QUERIES.items():
        if key in job_lower:
            return queries
    # Generic professional fallback
    return [f"{job_title} professional working office", f"young {job_title} smiling success"]


def _query_bucket(query: str) -> str:
    """
    Coarse visual bucket used to prevent over-concentration on near-identical scenes.
    """
    tokens = re.findall(r"[a-z0-9]+", str(query or "").lower())
    if not tokens:
        return "misc"
    stop_words = {
        "person", "young", "professional", "working", "office", "money", "financial",
        "closeup", "smiling", "laptop", "phone", "bank", "documents", "desk",
        "couple", "family", "business", "man", "woman", "happy", "success",
        "city", "excited", "confident", "modern", "hands", "people",
    }
    core = [tok for tok in tokens if tok not in stop_words][:2]
    if not core:
        core = tokens[:1]
    return "_".join(core)


def _clear_old_clips() -> None:
    """Remove previous run clip files so deterministic names never overwrite silently."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUTPUT_DIR.glob("clip_*.mp4"):
        old.unlink(missing_ok=True)


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

        # Add job/persona-specific visuals if case study metadata is available.
        case_study = script_data.get("case_study", {})
        if isinstance(case_study, dict):
            job_title = str(case_study.get("job", "")).strip()
            if job_title:
                queries.extend(_job_queries(job_title))

    # Add pillar-specific visual queries
    pillar_visuals = PILLAR_VISUAL_QUERIES.get(pillar, [])
    queries.extend(pillar_visuals)

    # Add generic fallbacks
    queries.extend(GENERIC_VISUAL_QUERIES)

    # Keep clip naming deterministic by clearing stale clips from prior runs.
    _clear_old_clips()
    # Preserve query ordering but drop duplicates.
    queries = list(dict.fromkeys(queries))
    valid_clips: list[Path] = []
    seen_ids: set[int] = set()
    query_hits: dict[str, int] = {}
    bucket_hits: dict[str, int] = {}
    clip_attempt_idx = 0  # tracks download attempts independently of validated count

    for query in queries:
        if len(valid_clips) >= target_count:
            break
        query_hits.setdefault(query, 0)
        bucket = _query_bucket(query)
        if bucket_hits.get(bucket, 0) >= MAX_CLIPS_PER_BUCKET:
            logger.info("Skipping query '%s' (bucket '%s' already saturated)", query, bucket)
            continue

        for page in range(1, 4):
            if len(valid_clips) >= target_count or query_hits[query] >= MAX_CLIPS_PER_QUERY:
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
                if query_hits[query] >= MAX_CLIPS_PER_QUERY:
                    break
                if bucket_hits.get(bucket, 0) >= MAX_CLIPS_PER_BUCKET:
                    break

                vid_id = video.get("id")
                if vid_id in seen_ids:
                    continue
                seen_ids.add(vid_id)

                file_info = _best_video_file(video)
                if not file_info:
                    continue

                # Use independent counter so failed/deleted downloads don't create name collisions.
                dest = OUTPUT_DIR / f"clip_{clip_attempt_idx:02d}.mp4"
                clip_attempt_idx += 1
                if not _download_clip(file_info["link"], dest):
                    continue

                meta = _ffprobe_validate(dest)
                if not meta:
                    logger.debug("Clip %s failed validation, discarding", dest.name)
                    dest.unlink(missing_ok=True)
                    continue

                valid_clips.append(dest)
                query_hits[query] += 1
                bucket_hits[bucket] = bucket_hits.get(bucket, 0) + 1
                logger.info("Downloaded valid clip %s (%.1fs %dx%d)",
                            dest.name, meta["duration"], meta["width"], meta["height"])
                time.sleep(0.2)  # polite rate limiting

    if not valid_clips:
        raise RuntimeError("No valid clips downloaded from Pexels")

    logger.info("Downloaded %d valid clips across %d visual buckets", len(valid_clips), len(bucket_hits))
    return valid_clips
