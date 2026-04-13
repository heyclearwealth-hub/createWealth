"""
run_pipeline.py — Entrypoint for Workflow 1 (research-and-render).

Steps:
1. Random sleep 0–MAX_STARTUP_DELAY_SECONDS (defaults to 900s)
2. Pick trending topic (pytrends + evergreen fallback, 90-day dedup)
3. Generate script with compliance check and similarity guard
4. Score hook — go/no-go gate
5. Generate voiceover (ElevenLabs)
6. Download stock footage (Pexels)
7. Render video (FFmpeg two-pass)
8. Generate packaging candidates (3 title + 3 thumbnail variants)
9. Write workspace/pipeline.json (metadata summary for issue)
10. Create GitHub review issue with artifact run_id
11. Mark topic used in data/topics_used.json and sync to pipeline-data branch
"""
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

WORKSPACE = Path("workspace")
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"


def _parse_hook_threshold() -> float:
    raw = os.environ.get("HOOK_SCORE_THRESHOLD", "0.75")
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid HOOK_SCORE_THRESHOLD='%s' — falling back to 0.75", raw)
        return 0.75


HOOK_SCORE_THRESHOLD = _parse_hook_threshold()
MAX_STARTUP_DELAY_SECONDS = max(
    0,
    int(os.environ.get("MAX_STARTUP_DELAY_SECONDS", "900") or "900"),
)


def _sync_data_branch() -> None:
    """Push data/ directory changes to pipeline-data branch (best-effort)."""
    data_dir = Path("data")
    if not data_dir.exists():
        logger.info("No data directory found; skipping data branch sync")
        return

    files_to_sync = [p for p in data_dir.rglob("*") if p.is_file()]
    if not files_to_sync:
        logger.info("No data files found to sync")
        return

    try:
        subprocess.run(["git", "fetch", "origin", "pipeline-data"], check=False, capture_output=True, text=True)

        with tempfile.TemporaryDirectory(prefix="pipeline-data-sync-") as tmpdir:
            worktree_path = Path(tmpdir) / "worktree"

            add_existing = subprocess.run(
                ["git", "worktree", "add", str(worktree_path), "origin/pipeline-data"],
                capture_output=True,
                text=True,
            )
            if add_existing.returncode != 0:
                add_new = subprocess.run(
                    ["git", "worktree", "add", "-b", "pipeline-data", str(worktree_path)],
                    capture_output=True,
                    text=True,
                )
                if add_new.returncode != 0:
                    raise subprocess.CalledProcessError(
                        add_new.returncode,
                        add_new.args,
                        output=add_new.stdout,
                        stderr=add_new.stderr,
                    )

            try:
                subprocess.run(
                    ["git", "-C", str(worktree_path), "config", "user.email", "github-actions@github.com"],
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", str(worktree_path), "config", "user.name", "github-actions[bot]"],
                    check=True,
                )

                dest_data = worktree_path / "data"
                dest_data.mkdir(parents=True, exist_ok=True)

                for src in files_to_sync:
                    rel = src.relative_to(data_dir)
                    dest = dest_data / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)

                subprocess.run(["git", "-C", str(worktree_path), "add", "data/"], check=True)
                diff = subprocess.run(["git", "-C", str(worktree_path), "diff", "--cached", "--quiet"])

                if diff.returncode == 0:
                    logger.info("No data changes to sync")
                else:
                    subprocess.run(
                        ["git", "-C", str(worktree_path), "commit", "-m", "chore: update pipeline data [skip ci]"],
                        check=True,
                    )
                    subprocess.run(
                        ["git", "-C", str(worktree_path), "push", "origin", "HEAD:pipeline-data"],
                        check=True,
                    )
                    logger.info("Data branch synced to pipeline-data")
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree_path)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
    except subprocess.CalledProcessError as exc:
        logger.warning("Data branch sync failed (non-fatal): %s", exc)


def main() -> None:
    # Step 1: Random delay to avoid rigid automation pattern while staying inside workflow timeout.
    if not DRY_RUN and MAX_STARTUP_DELAY_SECONDS > 0:
        delay = random.randint(0, MAX_STARTUP_DELAY_SECONDS)
        logger.info("Random startup delay: %ds", delay)
        time.sleep(delay)

    # Step 2: Pick topic
    from pipeline.trends import mark_topic_used, pick_topic

    topic = pick_topic()
    logger.info("Topic selected: %s [%s]", topic["keyword"], topic["pillar"])

    # Step 3: Generate script
    from pipeline.scriptwriter import generate as gen_script

    script_data = gen_script(topic)
    logger.info("Script generated: %s", script_data.get("slug"))

    # Step 4: Hook gate
    from pipeline.hook_gate import gate

    hook_result = gate(script_data, threshold=HOOK_SCORE_THRESHOLD)
    if not hook_result["pass"]:
        logger.error("Hook gate failed: %s", hook_result.get("reason"))
        sys.exit(1)
    logger.info(
        "Hook gate passed: score=%.2f (threshold=%.2f)",
        hook_result["score"],
        HOOK_SCORE_THRESHOLD,
    )

    # Step 5: Voiceover
    from pipeline.voiceover import generate as gen_voice

    voiceover_path = gen_voice(script_data["script"])
    logger.info("Voiceover generated: %s", voiceover_path)

    # Step 6: Footage
    from pipeline.footage import download as dl_footage

    clip_paths = dl_footage(topic, script_data=script_data)
    if not clip_paths:
        logger.error("No footage downloaded — aborting")
        sys.exit(1)
    logger.info("Downloaded %d clips", len(clip_paths))

    # Step 7: Render
    from pipeline.renderer import render

    video_path = render(clip_paths, voiceover_path, text_overlays=script_data.get("text_overlays"))
    logger.info("Video rendered: %s", video_path)

    # Step 8: Packaging candidates
    from pipeline.packaging import generate as gen_packaging

    candidates = gen_packaging(script_data)
    logger.info("Packaging generated: %d title variants", len(candidates.get("titles", [])))

    # Step 9: Write pipeline.json
    WORKSPACE.mkdir(exist_ok=True)
    pipeline_json = {
        **script_data,
        "hook_score": hook_result["score"],
        "compliance": "pass",
        "video_path": str(video_path),
        "voiceover_path": str(voiceover_path),
    }
    (WORKSPACE / "pipeline.json").write_text(json.dumps(pipeline_json, indent=2, ensure_ascii=False))
    logger.info("pipeline.json written")

    # Step 10: Create review issue (done by CI after artifact upload — run_id not available here)
    # The workflow YAML creates the issue using GH CLI after artifact upload.

    # Step 11: Mark topic used + sync
    mark_topic_used(topic["slug"])
    _sync_data_branch()

    logger.info("Pipeline complete. Awaiting human review.")


if __name__ == "__main__":
    main()
