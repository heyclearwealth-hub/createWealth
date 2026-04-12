"""
quota_guard.py — Daily YouTube Data API unit budget tracking.

YouTube Data API v3 allows 10,000 units/day.
We cap at DAILY_API_QUOTA_BUDGET (default 9,000) to keep headroom.

Unit costs (approximate):
  videos.insert                   ~1,600
  videos.update                      ~50
  videos.list                         ~1
  channels.list                       ~1
  playlistItems.insert               ~50
  search.list                       ~100
  youtubeAnalytics.reports.query      ~1
"""
import json
import logging
import os
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

BUDGET_FILE = Path("data/api_budget.json")
DAILY_BUDGET = int(os.environ.get("DAILY_API_QUOTA_BUDGET", "9000"))

# Known unit costs
UNIT_COSTS = {
    "videos.insert": 1600,
    "videos.update": 50,
    "videos.list": 1,
    "channels.list": 1,
    "playlistItems.insert": 50,
    "search.list": 100,
    "thumbnails.set": 50,
    "youtubeAnalytics.reports.query": 1,
}


def _load() -> dict:
    if not BUDGET_FILE.exists():
        return {"daily_units_used": 0, "date": "1970-01-01"}
    with BUDGET_FILE.open() as f:
        return json.load(f)


def _save(data: dict) -> None:
    BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    with BUDGET_FILE.open("w") as f:
        json.dump(data, f, indent=2)


def _reset_if_new_day(data: dict) -> dict:
    today = str(date.today())
    if data.get("date") != today:
        data = {"daily_units_used": 0, "date": today}
        _save(data)
        logger.info("New day — API quota reset")
    return data


def remaining() -> int:
    """Return remaining units for today."""
    data = _reset_if_new_day(_load())
    return max(0, DAILY_BUDGET - data["daily_units_used"])


def can_afford(operation: str) -> bool:
    """
    Check if we have enough budget for the given operation.
    Uses UNIT_COSTS lookup; unknown operations cost 1 unit.
    """
    cost = UNIT_COSTS.get(operation, 1)
    budget_left = remaining()
    if budget_left < cost:
        logger.warning(
            "Quota guard: insufficient budget for '%s' (cost=%d, remaining=%d)",
            operation,
            cost,
            budget_left,
        )
        return False
    return True


def charge(operation: str, units: int | None = None) -> None:
    """
    Deduct units from today's budget.
    If units is None, looks up cost in UNIT_COSTS (defaults to 1).
    """
    cost = units if units is not None else UNIT_COSTS.get(operation, 1)
    data = _reset_if_new_day(_load())
    data["daily_units_used"] += cost
    _save(data)
    logger.info(
        "Quota charged: %s (%d units) — used=%d/%d",
        operation,
        cost,
        data["daily_units_used"],
        DAILY_BUDGET,
    )


def assert_budget(operation: str) -> None:
    """
    Raise RuntimeError if budget is insufficient for operation.
    Call this before any YouTube API write/read operation with meaningful quota cost.
    """
    if not can_afford(operation):
        cost = UNIT_COSTS.get(operation, 1)
        raise RuntimeError(
            f"Daily API quota insufficient for '{operation}' "
            f"(need {cost}, remaining {remaining()})"
        )
