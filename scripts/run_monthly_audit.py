"""
run_monthly_audit.py — Entrypoint for Workflow 5 (monthly-ypp-audit).
"""
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    from pipeline.audit import run_and_post
    url = run_and_post()
    logger.info("Monthly audit issue created: %s", url)


if __name__ == "__main__":
    main()
