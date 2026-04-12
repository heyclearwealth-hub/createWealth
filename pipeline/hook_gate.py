"""
hook_gate.py — Scores the first 30 seconds of a script and enforces a go/no-go threshold.
Uses Claude Haiku for scoring to keep cost minimal (~$0.002/call).
"""
import json
import logging
import os
import re
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
DEFAULT_THRESHOLD = 0.75
MAX_REGEN_ATTEMPTS = 2


SCORING_PROMPT = """\
You are a YouTube content quality reviewer specializing in personal finance videos.
Score the following hook (opening 30 seconds of a script) on a scale from 0.0 to 1.0.

Scoring criteria:
- 0.9–1.0: Excellent. Specific outcome promise with a concrete number in the first 10 seconds. Immediately relevant to the target audience (young professionals, 25–35). No filler opener.
- 0.7–0.89: Good. Has an outcome promise and a relevant hook but slightly generic or missing a specific number.
- 0.5–0.69: Weak. Vague promise, starts with filler phrases ("Hey guys", "Welcome back", "In today's video"), or no clear outcome for the viewer.
- 0.0–0.49: Poor. No promise, just a topic introduction. Viewer has no reason to keep watching.

Also check:
- Does it contain a specific dollar figure or statistic? (required for score ≥ 0.75)
- Does it avoid generic openers like "Hey guys", "Welcome back", "In today's video"? (required for score ≥ 0.75)
- Is there a clear outcome the viewer will gain? (required for score ≥ 0.75)

Return ONLY this JSON:
{"score": <float 0.0–1.0>, "pass": <true|false>, "reason": "<one sentence explanation>", "issues": ["<issue1>", ...]}

HOOK TEXT:
"""


def _extract_hook(script: str, word_limit: int = 75) -> str:
    """Extract approximately the first 30 seconds (75 words at 150 wpm)."""
    words = script.split()
    return " ".join(words[:word_limit])


def _call_claude(prompt: str) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=MODEL,
        max_tokens=256,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _parse_score_response(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw.strip())
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("No JSON in hook scoring response")
    return json.loads(match.group())


def score_hook(script: str, threshold: float = DEFAULT_THRESHOLD) -> dict:
    """
    Score the hook of a script.
    Returns: {score, pass, reason, issues, hook_text}
    """
    hook_text = _extract_hook(script)
    raw = _call_claude(SCORING_PROMPT + hook_text)

    try:
        result = _parse_score_response(raw)
    except Exception as exc:
        logger.warning("Hook scoring parse failed: %s — defaulting to fail", exc)
        return {"score": 0.0, "pass": False, "reason": "Parse error", "issues": [str(exc)], "hook_text": hook_text}

    result["hook_text"] = hook_text
    result["pass"] = result.get("score", 0.0) >= threshold
    logger.info("Hook score: %.2f (threshold %.2f) — %s", result["score"], threshold, "PASS" if result["pass"] else "FAIL")
    return result


def gate(script_data: dict, threshold: float = DEFAULT_THRESHOLD) -> dict:
    """
    Run the hook gate. Returns the score result dict.
    Raises RuntimeError if hook fails and no regen is possible (caller handles regen).
    """
    script = script_data.get("script", "")
    result = score_hook(script, threshold)
    if not result["pass"]:
        logger.warning("Hook gate FAILED: score=%.2f issues=%s", result["score"], result["issues"])
    return result
