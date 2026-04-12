"""
feedback_memory.py — Ingest REJECT reasons into data/review_feedback.json.

Tags: hook | compliance | pacing | visuals | packaging | other
"""
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

FEEDBACK_FILE = Path("data/review_feedback.json")

# Keyword → tag mapping (checked in order; first match wins)
TAG_RULES: list[tuple[str, str]] = [
    (r"hook|intro|opening|first.*(second|second|10|fifteen|30)\s*sec", "hook"),
    (r"compli|disclaim|advice|claim|guarantee|mislead|mislead|promise|illegal", "compliance"),
    (r"pac(e|ing)|slow|fast|too long|too short|rush|drag", "pacing"),
    (r"visual|clip|footage|b.roll|thumbnail|image|blurr|quality|resolution", "visuals"),
    (r"title|packag|variant|description|desc|thumbnail text|cta|call.to.action", "packaging"),
]


def _tag_reason(reason: str) -> str:
    lower = reason.lower()
    for pattern, tag in TAG_RULES:
        if re.search(pattern, lower):
            return tag
    return "other"


def _load() -> dict:
    if not FEEDBACK_FILE.exists():
        return {"items": []}
    with FEEDBACK_FILE.open() as f:
        return json.load(f)


def _save(data: dict) -> None:
    FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with FEEDBACK_FILE.open("w") as f:
        json.dump(data, f, indent=2)


def ingest(reason: str, slug: str = "") -> dict:
    """
    Parse a REJECT reason string and persist it.
    Returns the created feedback item dict.
    """
    tag = _tag_reason(reason)
    item = {
        "slug": slug,
        "reason": reason,
        "tag": tag,
        "resolved": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    data = _load()
    data["items"].append(item)
    _save(data)
    logger.info("Feedback ingested [%s]: %s", tag, reason[:80])
    return item


def get_constraints() -> str:
    """
    Return unresolved feedback items as prompt-injection text.
    Loaded by scriptwriter.py and packaging.py to avoid repeated patterns.
    """
    data = _load()
    unresolved = [i for i in data.get("items", []) if not i.get("resolved")]
    if not unresolved:
        return ""

    lines = ["PREVIOUS REVIEWER FEEDBACK (avoid repeating these patterns):"]
    by_tag: dict[str, list[str]] = {}
    for item in unresolved:
        by_tag.setdefault(item["tag"], []).append(item["reason"])

    for tag, reasons in by_tag.items():
        lines.append(f"\n[{tag.upper()}]")
        for r in reasons[-3:]:  # cap at last 3 per tag to avoid prompt bloat
            lines.append(f"  - {r}")

    return "\n".join(lines)


def mark_resolved(tag: str) -> int:
    """
    Mark all unresolved items with a given tag as resolved.
    Returns count of items resolved.
    """
    data = _load()
    count = 0
    for item in data["items"]:
        if item.get("tag") == tag and not item.get("resolved"):
            item["resolved"] = True
            count += 1
    if count:
        _save(data)
    return count
