"""
run_upload.py — Entrypoint for Workflow 2 (approve-and-upload).

Called after human approves via GitHub Issue comment "APPROVE".
Artifact has already been downloaded by the workflow into workspace/.

Steps:
1. Load workspace/pipeline.json
2. Upload to YouTube via uploader.py
3. Post video URL as comment + close issue
"""
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

WORKSPACE = Path("workspace")


def main() -> None:
    pipeline_path = WORKSPACE / "pipeline.json"
    if not pipeline_path.exists():
        logger.error("pipeline.json not found in workspace/")
        sys.exit(1)

    with pipeline_path.open() as f:
        pipeline_json = json.load(f)

    video_path = WORKSPACE / "output" / "final_video.mp4"
    if not video_path.exists():
        logger.error("final_video.mp4 not found in workspace/output/")
        sys.exit(1)

    from pipeline.uploader import upload
    video_id = upload(pipeline_json, video_path)
    logger.info("Upload complete: video_id=%s", video_id)

    # Close issue via issue_manager (issue_number passed as env var by workflow)
    issue_number = os.environ.get("ISSUE_NUMBER")
    if issue_number:
        from pipeline.issue_manager import close_issue
        url = f"https://youtu.be/{video_id}"
        close_issue(
            int(issue_number),
            f"✅ Uploaded successfully!\n\n**Video URL:** {url}\n\nClosing this issue.",
        )
        logger.info("Issue #%s closed", issue_number)


if __name__ == "__main__":
    main()
