"""
scriptwriter.py — Generates YouTube scripts via Claude Haiku with JSON retry,
uniqueness check (cosine similarity), and finance compliance validation.
"""
import json
import logging
import math
import os
import re
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

PROMPT_FILE = Path("prompts/finance_script.md")
LAST_SCRIPTS_FILE = Path("data/last_scripts.json")
FEEDBACK_FILE = Path("data/review_feedback.json")
MAX_SCRIPTS_MEMORY = 5
SIMILARITY_THRESHOLD = 0.85
MAX_RETRIES = 2
MODEL = "claude-haiku-4-5"


def _load_prompt() -> str:
    return PROMPT_FILE.read_text()


def _load_last_scripts() -> list[str]:
    if not LAST_SCRIPTS_FILE.exists():
        return []
    with LAST_SCRIPTS_FILE.open() as f:
        return json.load(f)


def _save_script_to_memory(script_text: str) -> None:
    scripts = _load_last_scripts()
    scripts.append(script_text)
    if len(scripts) > MAX_SCRIPTS_MEMORY:
        scripts = scripts[-MAX_SCRIPTS_MEMORY:]
    with LAST_SCRIPTS_FILE.open("w") as f:
        json.dump(scripts, f)


def _load_feedback_constraints() -> str:
    if not FEEDBACK_FILE.exists():
        return ""
    with FEEDBACK_FILE.open() as f:
        data = json.load(f)
    items = data.get("items", [])
    unresolved = [i for i in items if not i.get("resolved")]
    if not unresolved:
        return ""
    tags = list({i["tag"] for i in unresolved})
    reasons = [i["reason"] for i in unresolved[-5:]]
    lines = [
        "\n## Reviewer Feedback Constraints (apply to this script):",
        f"Avoid these patterns flagged in recent reviews: {', '.join(tags)}",
    ]
    lines += [f"- {r}" for r in reasons]
    return "\n".join(lines)


def _word_freq(text: str) -> dict[str, float]:
    words = re.findall(r"[a-z]+", text.lower())
    freq: dict[str, float] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    total = sum(freq.values()) or 1
    return {w: c / total for w, c in freq.items()}


def _cosine_similarity(a: str, b: str) -> float:
    fa, fb = _word_freq(a), _word_freq(b)
    keys = set(fa) | set(fb)
    dot = sum(fa.get(k, 0) * fb.get(k, 0) for k in keys)
    mag_a = math.sqrt(sum(v ** 2 for v in fa.values()))
    mag_b = math.sqrt(sum(v ** 2 for v in fb.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _is_too_similar(new_script: str) -> bool:
    past = _load_last_scripts()
    for old in past:
        sim = _cosine_similarity(new_script, old)
        if sim > SIMILARITY_THRESHOLD:
            logger.warning("Script too similar to a past script (cosine=%.2f)", sim)
            return True
    return False


def _call_claude(messages: list[dict], temperature: float = 0.8) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        temperature=temperature,
        messages=messages,
    )
    return response.content[0].text


def _extract_json(raw: str) -> dict:
    """Extract the first JSON object from raw text, stripping markdown fences."""
    # Strip ```json ... ``` fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw.strip())
    # Find first { ... } block
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in Claude response")
    return json.loads(match.group())


def _generate_script(topic: dict, extra_instructions: str = "", temperature: float = 0.8) -> dict:
    """Single generation attempt. Raises on bad JSON after 2 fix retries."""
    system_prompt = _load_prompt()
    feedback = _load_feedback_constraints()
    user_msg = (
        f"Generate a script for this topic: **{topic['keyword']}** (pillar: {topic['pillar']})\n"
        f"Slug: {topic['slug']}\n"
        f"{extra_instructions}"
        f"{feedback}"
    )
    messages = [{"role": "user", "content": user_msg}]

    raw = _call_claude(
        [{"role": "user", "content": system_prompt + "\n\n---\n\n" + user_msg}],
        temperature=temperature,
    )

    # JSON retry loop
    for fix_attempt in range(3):
        try:
            return _extract_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            if fix_attempt == 2:
                raise RuntimeError(f"Claude returned invalid JSON after 3 attempts: {exc}") from exc
            logger.warning("JSON parse failed (attempt %d): %s — asking Claude to fix", fix_attempt + 1, exc)
            fix_messages = [
                {"role": "user", "content": system_prompt + "\n\n---\n\n" + user_msg},
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "Your response was not valid JSON. Return ONLY the JSON object with no other text, no markdown fences."},
            ]
            raw = _call_claude(fix_messages, temperature=0.3)

    raise RuntimeError("Unreachable")  # pragma: no cover


def _validate_compliance(script_data: dict) -> bool:
    """Second Claude call to validate finance compliance. Returns True if pass."""
    script_text = script_data.get("script", "")
    description = script_data.get("description", "")

    compliance_prompt = (
        "Review the following YouTube script for a personal finance channel. Check for:\n"
        "1. Any earnings guarantees or 'you will make X' language → flag as FAIL\n"
        "2. Any 'get rich quick' or misleading return promises → flag as FAIL\n"
        "3. Any specific stock, ETF ticker, or crypto buy/sell recommendations → flag as FAIL\n"
        "4. Any claim that sounds like personalized financial advice rather than education → flag as FAIL\n"
        "5. Does the description end with the required AI and finance disclaimers? → flag as FAIL if missing\n\n"
        "If ALL checks pass, respond: {\"compliance\": \"pass\"}\n"
        "If any check fails, respond: {\"compliance\": \"fail\", \"reason\": \"<specific issue>\"}\n\n"
        f"SCRIPT:\n{script_text}\n\nDESCRIPTION:\n{description}"
    )

    raw = _call_claude([{"role": "user", "content": compliance_prompt}], temperature=0.0)
    try:
        result = _extract_json(raw)
        if result.get("compliance") == "pass":
            return True
        logger.warning("Compliance check failed: %s", result.get("reason"))
        return False
    except Exception as exc:
        logger.warning("Compliance response parse failed: %s — treating as fail", exc)
        return False


def _generate_text_overlays(script_data: dict) -> list[dict]:
    """
    Ask Claude to identify key moments in the script for text overlays.
    Returns a list of overlay dicts with type, timing, and text content.
    Falls back to [] on any error.
    """
    script = script_data.get("script", "")
    if not script:
        return []

    words = script.split()
    total_words = len(words)

    prompt = (
        f"You are a video editor. Given this YouTube script ({total_words} words total), "
        "identify key moments for on-screen text overlays.\n\n"
        "Extract exactly:\n"
        "- 1 title_card at start: person name/situation + core problem number\n"
        "- 3–5 stat callouts: specific dollar amounts or percentages spoken in script\n"
        "- 2–3 section headers at story beat transitions: e.g. 'THE PROBLEM', 'THE TURNING POINT', 'THE RESULT'\n"
        "- 1 before_after card near the end: before/after comparison (newline-separated lines)\n\n"
        "Rules:\n"
        "- start_word = word index in the script where overlay appears (0-indexed)\n"
        "- duration_s = seconds the text stays on screen\n"
        "- Keep stat text short: just the number/amount (e.g. '$38,000' not 'she had $38,000 in debt')\n\n"
        "Return ONLY this JSON (no explanation):\n"
        '{"overlays": [\n'
        '  {"type": "title_card", "lines": ["Name", "Job | Age | Salary", "Core problem"], "start_word": 0, "duration_s": 4},\n'
        '  {"type": "stat", "text": "$38,000", "start_word": 45, "duration_s": 3},\n'
        '  {"type": "section", "text": "THE TURNING POINT", "start_word": 180, "duration_s": 2.5},\n'
        '  {"type": "before_after", "before": "Line1\\nLine2", "after": "Line1\\nLine2", "start_word": 420, "duration_s": 5}\n'
        "]}\n\n"
        f"SCRIPT:\n{script[:4000]}"
    )

    try:
        raw = _call_claude([{"role": "user", "content": prompt}], temperature=0.2)
        data = _extract_json(raw)
        overlays = data.get("overlays", [])
        logger.info("Generated %d text overlays", len(overlays))
        return overlays
    except Exception as exc:
        logger.warning("Text overlay generation failed: %s — skipping overlays", exc)
        return []


def generate(topic: dict) -> dict:
    """
    Generate a compliant, unique script for the given topic.
    Returns the script data dict.
    Raises RuntimeError if all attempts fail.
    """
    for attempt in range(MAX_RETRIES + 1):
        temperature = 0.8 + (attempt * 0.1)  # increase temperature on retry for more variety
        extra = ""
        if attempt > 0:
            extra = (
                f"\nIMPORTANT: This is attempt {attempt + 1}. "
                "The previous attempt was either too similar to a past script or failed compliance. "
                "Use a distinctly different structural angle, different statistics, and a different worked example. "
            )

        try:
            data = _generate_script(topic, extra_instructions=extra, temperature=temperature)
        except RuntimeError as exc:
            if attempt == MAX_RETRIES:
                raise
            logger.warning("Script generation attempt %d failed: %s", attempt + 1, exc)
            continue

        # Uniqueness check
        script_text = data.get("script", "")
        if _is_too_similar(script_text):
            if attempt == MAX_RETRIES:
                raise RuntimeError("Script too similar to past scripts after all retries")
            logger.warning("Retrying due to similarity (attempt %d)", attempt + 1)
            continue

        # Compliance check
        if not _validate_compliance(data):
            if attempt == MAX_RETRIES:
                raise RuntimeError("Script failed compliance check after all retries")
            logger.warning("Retrying due to compliance failure (attempt %d)", attempt + 1)
            continue

        # All checks passed
        _save_script_to_memory(script_text)
        data["text_overlays"] = _generate_text_overlays(data)
        logger.info("Script generated successfully for topic: %s", topic["keyword"])
        return data

    raise RuntimeError("Script generation failed — all attempts exhausted")  # pragma: no cover
