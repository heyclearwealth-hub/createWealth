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
You are a YouTube packaging expert for a personal finance channel called ClearWealth targeting young professionals (25-35).

Given the script data below, generate EXACTLY 3 title variants and 3 thumbnail text concepts.

Rules:
- Titles must be 45-60 characters, no clickbait, accurately reflect content
- Thumbnail text must be 4-7 words, punchy, matches video content (no bait-and-switch)
- Each variant must feel meaningfully different (not just minor word changes)
- Variant 1 = benefit-focused ("How to...", "Why your...")
- Variant 2 = curiosity/myth-busting ("The truth about...", "Stop doing...")
- Variant 3 = specificity/number-led ("$X hack:", "5 steps to...")

Return ONLY this JSON:
{
  "default_index": 0,
  "titles": ["<title1>", "<title2>", "<title3>"],
  "thumbnail_texts": ["<thumb1>", "<thumb2>", "<thumb3>"],
  "description_hook": "<First 2 sentences of description — high-CTR version>"
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
