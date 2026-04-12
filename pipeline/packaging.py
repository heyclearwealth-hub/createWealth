"""
packaging.py — Generates title/description/thumbnail candidate variants for A/B testing.
"""
import json
import logging
import os
import re
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)
MODEL = "claude-haiku-4-5"
PACKAGE_CANDIDATES_PATH = Path("workspace/package_candidates.json")

PACKAGING_PROMPT = """\
You are a YouTube packaging expert for ClearWealth, a personal finance channel for young professionals (25-35).

Your job is to write titles and thumbnails that make someone stop scrolling and click — without being misleading.

## Pro YouTuber Title Formula
The best-performing personal finance titles combine: SPECIFIC NUMBER + EMOTIONAL HOOK + COUNTERINTUITIVE ANGLE.

Examples of great titles (use these as a style guide, not templates to copy):
- "I Waited Until 29 to Invest. Here's What It Cost Me."
- "Why Paying Off Debt First Is Wrong (For Most People)"
- "The $47 Monthly Difference Between Rich and Average at 60"
- "3 Signs You're Investing the Wrong Way (I Did All 3)"
- "Your 401k Match Is Leaving You Broke. Here's Why."

## Variant Rules
- Variant 1 = PERSONAL STORY angle: First person "I", regret/win, specific dollar amount or age
  Example: "I Lost $34,000 Not Knowing This About My 401k"
- Variant 2 = COUNTERINTUITIVE angle: Challenge the conventional wisdom, use "Wrong", "Actually", "Myth"
  Example: "Why Saving 20% of Your Income Is Actually Bad Advice"
- Variant 3 = SPECIFIC NUMBER + FEAR/URGENCY angle: A number that makes the stakes real
  Example: "The $180,000 Mistake 73% of People Make in Their 20s"

## Title Rules
- 45-60 characters (longer is fine if powerful, max 70)
- Must accurately reflect the video content — no bait-and-switch
- No vague words: "good", "some", "certain", "various", "better"
- No ALL CAPS beyond 2 words
- No ellipsis abuse ("...")

## Thumbnail Text Rules
- 4-6 words max (must be readable at 320px wide)
- Must feel like a gut punch or "wait, what?" moment
- Pair with a strong emotion word: WRONG, MISTAKE, REAL, HIDDEN, ACTUALLY
- Examples: "You're Doing This Wrong", "$34K Gone. Here's Why.", "The Number Nobody Shows You"

Return ONLY this JSON (no explanation, no markdown):
{
  "default_index": 0,
  "titles": ["<variant1>", "<variant2>", "<variant3>"],
  "thumbnail_texts": ["<thumb1>", "<thumb2>", "<thumb3>"],
  "description_hook": "<First 2 punchy sentences for the YouTube description — hook the reader, include a key number>"
}

SCRIPT DATA:
"""


def _call_claude(prompt: str) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=MODEL,
        max_tokens=512,
        temperature=0.7,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _extract_json(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw.strip())
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("No JSON in packaging response")
    return json.loads(match.group())


def generate(script_data: dict) -> dict:
    """
    Generate packaging candidates for the given script.
    Returns a dict saved to workspace/package_candidates.json.
    """
    summary = {
        "topic": script_data.get("topic"),
        "title": script_data.get("title"),
        "hook_summary": script_data.get("hook_summary"),
        "description": script_data.get("description", "")[:300],
    }
    prompt = PACKAGING_PROMPT + json.dumps(summary, indent=2)

    for attempt in range(3):
        try:
            raw = _call_claude(prompt)
            result = _extract_json(raw)
            break
        except Exception as exc:
            if attempt == 2:
                logger.warning("Packaging generation failed: %s — using script defaults", exc)
                result = {
                    "default_index": 0,
                    "titles": [script_data.get("title", ""), "", ""],
                    "thumbnail_texts": [script_data.get("thumbnail_concept", ""), "", ""],
                    "description_hook": "",
                }
            continue

    result["topic"] = script_data.get("topic")
    result["slug"] = script_data.get("slug")
    result["experiment_state"] = "pending"

    PACKAGE_CANDIDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PACKAGE_CANDIDATES_PATH.open("w") as f:
        json.dump(result, f, indent=2)

    logger.info("Generated %d title variants for '%s'", len(result.get("titles", [])), result.get("topic"))
    return result
