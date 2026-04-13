"""
run_shorts_original.py — Entrypoint for standalone Shorts generation.

Generates a YouTube Short (45–55s) on a finance topic.
Uses a Pexels video background (darkened, brand-blended) when PEXELS_API_KEY is set,
falling back to a static gradient background if the key is absent or the download fails.

Usage:
  python scripts/run_shorts_original.py                  # auto-pick topic
  python scripts/run_shorts_original.py --topic "compound interest"
  python scripts/run_shorts_original.py --dry-run        # skip upload

Environment variables required:
  ANTHROPIC_API_KEY
  ELEVENLABS_API_KEY
  YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN  (unless --dry-run)
"""
import argparse
import json
import logging
import os
import shutil
import sys
from pathlib import Path

# Make sure repo root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.shorts_scriptwriter import FINANCE_TOPICS, generate as generate_script
from pipeline.shorts_renderer import OUTPUT_SHORT, render as render_short
from pipeline.voiceover import generate_with_timestamps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("__main__")

WORKSPACE = Path("workspace")
SHORT_VO_PATH = WORKSPACE / "short_voiceover.mp3"
USED_TOPICS_FILE = Path("data/short_topics_used.json")
RETENTION_FEEDBACK_FILE = Path("data/retention_feedback.json")


def _load_used_topics() -> list[str]:
    if not USED_TOPICS_FILE.exists():
        return []
    with USED_TOPICS_FILE.open() as f:
        return json.load(f).get("topics", [])


def _save_used_topic(topic: str) -> None:
    used = _load_used_topics()
    if topic not in used:
        used.append(topic)
    if len(used) > 30:
        used = used[-30:]
    USED_TOPICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with USED_TOPICS_FILE.open("w") as f:
        json.dump({"topics": used}, f)


def _load_retention_feedback() -> dict | None:
    if not RETENTION_FEEDBACK_FILE.exists():
        return None
    try:
        with RETENTION_FEEDBACK_FILE.open() as f:
            return json.load(f)
    except Exception:
        return None


def _clean_workspace() -> None:
    short_work = WORKSPACE / "short_work"
    if short_work.exists():
        shutil.rmtree(short_work)
    SHORT_VO_PATH.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and upload a standalone YouTube Short")
    parser.add_argument("--topic", default=None, help="Override topic (must match FINANCE_TOPICS list)")
    parser.add_argument("--dry-run", action="store_true", help="Skip YouTube upload, save payload locally")
    parser.add_argument("--output", default=None, help="Override output path for the Short video")
    args = parser.parse_args()

    dry_run = args.dry_run or os.environ.get("DRY_RUN") == "1"
    output_path = Path(args.output) if args.output else OUTPUT_SHORT

    _clean_workspace()
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    # ── 1. Pick / validate topic ─────────────────────────────────────────────
    if args.topic:
        topic_match = next(
            (t for t in FINANCE_TOPICS if t["topic"].lower() == args.topic.lower()),
            {"topic": args.topic, "pillar": "investing", "angle": args.topic},
        )
    else:
        topic_match = None  # shorts_scriptwriter will pick automatically

    # ── 2. Generate script ───────────────────────────────────────────────────
    logger.info("Generating Short script...")
    script_data = generate_script(
        topic=topic_match,
        used_topics=_load_used_topics(),
        retention_feedback=_load_retention_feedback(),
    )
    logger.info("Script generated: topic='%s' words=%d overlays=%d",
                script_data["topic"],
                len(script_data.get("voiceover_script", "").split()),
                len(script_data.get("overlays", [])))

    # ── 3. Generate voiceover (with timestamps when available) ───────────────
    logger.info("Generating voiceover...")
    vo_path, word_times = generate_with_timestamps(
        script_data["voiceover_script"],
        output_path=SHORT_VO_PATH,
    )
    script_data["word_timestamps"] = word_times
    logger.info("Voiceover saved: %s", vo_path)

    # ── 4. Render Short ──────────────────────────────────────────────────────
    logger.info("Rendering Short video...")
    video_path = render_short(
        voiceover_path=SHORT_VO_PATH,
        script_data=script_data,
        output_path=output_path,
    )
    logger.info("Short rendered: %s (%.1f MB)", video_path,
                video_path.stat().st_size / 1_048_576)

    # ── 5. Build upload payload ──────────────────────────────────────────────
    titles = script_data.get("title_options", [script_data["topic"]])
    title = titles[0] if titles else script_data["topic"]

    hashtags = " ".join(script_data.get("hashtags", ["#Shorts", "#PersonalFinance"]))
    description = script_data.get("description", "") + f"\n\n{hashtags}"

    upload_payload = {
        "title": title,
        "description": description,
        "tags": script_data.get("hashtags", []),
        "pillar": script_data.get("pillar", "investing"),
        "topic": script_data["topic"],
        "script": script_data.get("voiceover_script", ""),
        "content_type": "short",
        # Dummy fields expected by uploader.py
        "slug": script_data["topic"].lower().replace(" ", "-"),
        "hook_summary": title,
        "thumbnail_concept": title[:50],
        "stat_citations": [],
        "pillar_playlist_bridge": "",
    }

    # ── 6. Upload or dry-run ─────────────────────────────────────────────────
    if dry_run:
        payload_path = output_path.parent / "short_upload_payload.json"
        with payload_path.open("w") as f:
            json.dump(upload_payload, f, indent=2)
        logger.info("DRY RUN — payload saved to %s", payload_path)
        logger.info("Video ready at: %s", video_path)
    else:
        from pipeline.uploader import upload
        logger.info("Uploading Short to YouTube...")
        video_url = upload(upload_payload, video_path=video_path)
        logger.info("Short uploaded: %s", video_url)

    # ── 7. Save topic to used list ───────────────────────────────────────────
    _save_used_topic(script_data["topic"])
    logger.info("Done.")


if __name__ == "__main__":
    main()
