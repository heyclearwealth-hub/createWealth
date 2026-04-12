"""
run_feedback_sync.py — Entrypoint for Workflow 6 (feedback-memory-sync).

Called on REJECT issue comment events and daily.
Reads REJECT_REASON and SLUG from environment variables (set by workflow).
"""
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    reason = os.environ.get("REJECT_REASON", "").strip()
    slug = os.environ.get("PIPELINE_SLUG", "").strip()

    if not reason:
        logger.info("No REJECT_REASON provided — nothing to sync")
        sys.exit(0)

    from pipeline.feedback_memory import ingest
    item = ingest(reason, slug=slug)
    logger.info("Feedback ingested: tag=%s reason='%s'", item["tag"], item["reason"][:80])


if __name__ == "__main__":
    main()
