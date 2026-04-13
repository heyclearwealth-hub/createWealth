"""
shorts_scriptwriter.py — Generates standalone YouTube Shorts scripts (45–55s)
via Claude Haiku. Text-animation style: no B-roll, bold numbers, quick cuts.

Output JSON includes:
- voiceover_script: full spoken text with [PAUSE] markers
- overlays: timed on-screen text plan (word-index based)
- title_options: 3 title variants
- description: short YouTube description with disclaimer
- hashtags: list of hashtags
"""
import json
import logging
import os
import random
import re
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MODEL = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
LAST_SHORTS_FILE = Path("data/last_shorts.json")
MAX_SHORTS_MEMORY = 10
WPS = 2.5  # words per second at voiceover pace
MIN_WORDS = 110
MAX_WORDS = 140
MIN_OVERLAYS = 6
MAX_OVERLAYS = 10
MAX_TEXT_CHARS = 34
MIN_TITLE_OPTIONS = 5
MAX_TITLE_OPTIONS = 10

HOOK_PAIN_TERMS = {
    "lose", "loss", "waste", "wasted", "miss", "missing", "debt", "trap", "mistake",
    "expensive", "costly", "behind", "penalty", "stuck",
}
HOOK_CONSEQUENCE_TERMS = {
    "cost", "costs", "later", "future", "retirement", "years", "forever", "overpay",
    "wealth", "freedom", "growth", "compound", "thousands", "millions",
}
TITLE_POWER_TERMS = {
    "why", "how", "mistake", "truth", "secret", "stop", "before", "after", "lose", "save",
}

OVERLAY_TYPES = {"hook_number", "label", "comparison", "cta"}
DEFAULT_DURATIONS = {
    "hook_number": 4.0,
    "label": 2.2,
    "comparison": 4.0,
    "cta": 4.5,
}

FINANCE_TOPICS = [
    {"topic": "compound interest", "pillar": "investing", "angle": "small monthly amount becomes huge over time"},
    {"topic": "Roth IRA basics", "pillar": "investing", "angle": "why young people are missing free tax savings"},
    {"topic": "emergency fund rule", "pillar": "budgeting", "angle": "the exact number you need, not a vague range"},
    {"topic": "credit score quick wins", "pillar": "debt", "angle": "3 moves that raise your score in 30 days"},
    {"topic": "401k employer match", "pillar": "investing", "angle": "you're leaving free money on the table"},
    {"topic": "debt avalanche vs snowball", "pillar": "debt", "angle": "which method saves you the most money"},
    {"topic": "salary negotiation", "pillar": "career_income", "angle": "the exact phrase that gets you more money"},
    {"topic": "index funds explained", "pillar": "investing", "angle": "why boring beats exciting every time"},
    {"topic": "budget 50/30/20 rule", "pillar": "budgeting", "angle": "the only budget system that actually sticks"},
    {"topic": "high yield savings account", "pillar": "budgeting", "angle": "your savings account is robbing you"},
    {"topic": "tax brackets explained", "pillar": "tax", "angle": "you don't lose money on a raise — the myth busted"},
    {"topic": "student loan payoff strategy", "pillar": "debt", "angle": "the order of payments that saves thousands"},
    {"topic": "investing in your 20s vs 30s", "pillar": "investing", "angle": "starting 10 years late costs 4x more"},
    {"topic": "car loan trap", "pillar": "debt", "angle": "how a $400/month car payment kills wealth"},
    {"topic": "net worth calculation", "pillar": "budgeting", "angle": "the one number that actually tells you how you're doing"},
]

_CLIENT = None
_CLIENT_API_KEY = None


def _load_last_shorts() -> list[dict]:
    if not LAST_SHORTS_FILE.exists():
        return []
    with LAST_SHORTS_FILE.open() as f:
        raw = json.load(f)
    # Migrate legacy plain-string entries written by older code.
    return [r if isinstance(r, dict) else {"topic": "", "script": r} for r in raw]


def _save_short_to_memory(topic_name: str, script_text: str) -> None:
    entries = _load_last_shorts()
    entries.append({"topic": topic_name, "script": script_text})
    if len(entries) > MAX_SHORTS_MEMORY:
        entries = entries[-MAX_SHORTS_MEMORY:]
    LAST_SHORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LAST_SHORTS_FILE.open("w") as f:
        json.dump(entries, f)


def _pick_topic(used_topics: list[str] | None = None) -> dict:
    """Pick a topic not recently used, chosen randomly among available candidates."""
    used = set(used_topics or [])
    available = [t for t in FINANCE_TOPICS if t["topic"] not in used]
    if not available:
        available = FINANCE_TOPICS  # all topics used — reset cycle
    return random.choice(available)


def _get_client():
    """Create Anthropic client once per API key for connection reuse."""
    global _CLIENT, _CLIENT_API_KEY
    api_key = os.environ["ANTHROPIC_API_KEY"]
    if _CLIENT is None or _CLIENT_API_KEY != api_key:
        _CLIENT = anthropic.Anthropic(api_key=api_key)
        _CLIENT_API_KEY = api_key
    return _CLIENT


def _call_claude(prompt: str, temperature: float = 0.8) -> str:
    client = _get_client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        temperature=temperature,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _extract_json(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw.strip())
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in response")
    return json.loads(match.group())


def _clean_script_text(script: str) -> str:
    """Return spoken text only, without stage markers."""
    return re.sub(r"\[PAUSE\]", " ", script or "").strip()


def _word_count(script: str) -> int:
    return len(re.findall(r"[A-Za-z0-9$%]+", _clean_script_text(script)))


def _first_token(script: str) -> str:
    words = _clean_script_text(script).split()
    return words[0] if words else ""


def _extract_first_number(script: str) -> str:
    match = re.search(r"\$?\d[\d,]*(?:\.\d+)?%?", script or "")
    return match.group(0) if match else "$100"


def _normalize_text(value: str, fallback: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        text = fallback
    if len(text) > MAX_TEXT_CHARS:
        text = text[: MAX_TEXT_CHARS - 3].rsplit(" ", 1)[0].rstrip() + "..."
    return text


def _normalize_label_text(value: str, fallback: str = "THIS IS KEY") -> str:
    """
    Labels should stay punchy (2-5 words) and never trail with ellipses.
    """
    raw = " ".join(str(value or "").split()).strip().upper()
    words = re.findall(r"[A-Z0-9$%]+", raw)
    if not words:
        words = fallback.split()
    if len(words) < 2:
        words.append("NOW")
    return " ".join(words[:5])


def _default_stat_citations(topic: dict) -> list[str]:
    pillar = str(topic.get("pillar", "")).strip()
    defaults = {
        "investing": "SPIVA U.S. Scorecard 2025",
        "budgeting": "Federal Reserve SHED 2025",
        "debt": "Federal Reserve G.19 Consumer Credit",
        "tax": "IRS Publication 17",
        "career_income": "BLS Employment Cost Index",
    }
    return [defaults.get(pillar, "Federal Reserve Economic Data")]


def _normalize_stat_citations(raw, topic: dict) -> list[str]:
    out: list[str] = []
    for item in (raw or []):
        text = " ".join(str(item or "").split()).strip()
        if 4 <= len(text) <= 90 and text not in out:
            out.append(text)
    if not out:
        out = _default_stat_citations(topic)
    return out[:2]


def _split_sentences(script: str) -> list[str]:
    cleaned = " ".join(str(script or "").split()).strip()
    if not cleaned:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]


def _loop_ending_line(script: str) -> str:
    first_number = _extract_first_number(script)
    return f"Save this, and if {first_number} surprised you, replay the first 3 seconds."


def _enforce_loop_ending(script: str) -> str:
    """
    Professional Shorts benefit from an ending that references the opening hook.
    """
    sentences = _split_sentences(script)
    if not sentences:
        return script
    loop_line = _loop_ending_line(script)
    last = sentences[-1].lower()
    if "follow" in last or "save this" in last:
        sentences[-1] = loop_line
    else:
        sentences.append(loop_line)
    return " ".join(sentences)


def assess_hook_strength(script: str) -> tuple[bool, str]:
    """
    Hook quality gate: first beat should include number + pain + consequence framing.
    """
    tokens = re.findall(r"[A-Za-z0-9$%']+", _clean_script_text(script))
    hook_tokens = tokens[:14]
    hook_text = " ".join(hook_tokens).lower()
    has_number = any(re.search(r"\d", t) for t in hook_tokens)
    has_pain = any(term in hook_text for term in HOOK_PAIN_TERMS)
    has_consequence = any(term in hook_text for term in HOOK_CONSEQUENCE_TERMS)

    if not has_number:
        return False, "hook missing number in opening beat"
    if not has_pain:
        return False, "hook missing pain framing in opening beat"
    if not has_consequence:
        return False, "hook missing consequence framing in opening beat"
    return True, "ok"


def _title_score(title: str, topic: dict) -> float:
    cleaned = " ".join(str(title or "").split()).strip()
    if not cleaned:
        return -999.0
    lower = cleaned.lower()
    score = 0.0
    length = len(cleaned)

    if 42 <= length <= 60:
        score += 2.2
    elif 32 <= length <= 68:
        score += 1.0
    else:
        score -= 1.0

    if re.search(r"[\d$%]", cleaned):
        score += 2.0
    if any(term in lower for term in TITLE_POWER_TERMS):
        score += 1.2
    if any(tok in lower for tok in str(topic.get("topic", "")).lower().split()[:2]):
        score += 0.8
    if "?" in cleaned:
        score += 0.4
    if cleaned.isupper():
        score -= 0.8
    if "guaranteed" in lower or "get rich" in lower:
        score -= 3.0
    return score


def _normalize_title_options(raw_titles, topic: dict, script: str) -> tuple[list[str], list[dict]]:
    titles: list[str] = []
    for t in (raw_titles or []):
        candidate = " ".join(str(t or "").split()).strip()
        if candidate and candidate not in titles:
            titles.append(candidate)

    first_num = _extract_first_number(script)
    base_topic = str(topic.get("topic", "money")).title()
    fallbacks = [
        f"{first_num} Mistake Most People Make With {base_topic}",
        f"Why {base_topic} Feels Hard (And the Simple Fix)",
        f"Stop Losing Money on {base_topic}: 1 Rule",
        f"How to Use {base_topic} Without Guessing",
        f"The Boring {base_topic} Move That Wins Long-Term",
    ]
    for fb in fallbacks:
        if fb not in titles:
            titles.append(fb)
        if len(titles) >= MIN_TITLE_OPTIONS:
            break

    scored = [{"title": t, "score": round(_title_score(t, topic), 3)} for t in titles[:MAX_TITLE_OPTIONS]]
    scored.sort(key=lambda item: item["score"], reverse=True)
    ranked = [item["title"] for item in scored]
    return ranked, scored


def _retention_prompt_block(retention_feedback: dict | None) -> str:
    if not retention_feedback:
        return ""
    dropoffs = retention_feedback.get("dropoff_seconds") or []
    notes = " ".join(str(retention_feedback.get("notes", "")).split()).strip()
    parts: list[str] = []
    if dropoffs:
        stamp_text = ", ".join(f"{float(s):.0f}s" for s in dropoffs[:5])
        parts.append(
            f"- Retention dips have appeared around {stamp_text}; add novelty or a new number before those beats."
        )
    if notes:
        parts.append(f"- Recent retention notes: {notes}")
    if not parts:
        return ""
    return "\nRETENTION FEEDBACK (use this to improve watch-through):\n" + "\n".join(parts) + "\n"


def _coerce_start_word(value, max_words: int, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0, min(max(parsed, 0), max(0, max_words - 1)))


def _coerce_duration(value, kind: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_DURATIONS[kind]
    return max(1.2, min(parsed, 5.0))


def _normalize_overlay(overlay: dict, word_count: int) -> dict | None:
    kind = str((overlay or {}).get("type", "")).strip()
    if kind not in OVERLAY_TYPES:
        return None

    normalized = {
        "type": kind,
        "start_word": _coerce_start_word(
            (overlay or {}).get("start_word"),
            word_count,
            default=0,
        ),
        "duration_s": _coerce_duration((overlay or {}).get("duration_s"), kind),
    }

    if kind == "hook_number":
        normalized["text"] = _normalize_text((overlay or {}).get("text"), "$100/month")
        subtitle = _normalize_text((overlay or {}).get("subtitle"), "")
        if subtitle:
            normalized["subtitle"] = subtitle
    elif kind == "label":
        normalized["text"] = _normalize_label_text((overlay or {}).get("text"), "THIS IS KEY")
    elif kind == "comparison":
        normalized["left"] = _normalize_text((overlay or {}).get("left"), "Before")
        normalized["right"] = _normalize_text((overlay or {}).get("right"), "After")
    else:  # cta
        cta = " ".join(str((overlay or {}).get("text", "")).split()).strip()
        normalized["text"] = cta or "Save this and follow for more"

    return normalized


def _ensure_overlay_density(overlays: list[dict], script: str, topic: dict) -> list[dict]:
    """Guarantee enough overlay cadence for a fast-paced Short."""
    words = _word_count(script)
    if words <= 0:
        return overlays

    normalized: list[dict] = []
    for ov in overlays:
        parsed = _normalize_overlay(ov, words)
        if parsed:
            normalized.append(parsed)

    normalized.sort(key=lambda o: o["start_word"])

    # Ensure hook overlay at time zero.
    if not any(o["type"] == "hook_number" and o["start_word"] == 0 for o in normalized):
        normalized.insert(
            0,
            {
                "type": "hook_number",
                "text": _extract_first_number(script),
                "start_word": 0,
                "duration_s": 4.0,
            },
        )

    # Ensure CTA overlay near the end.
    cta_start = max(words - 12, 0)
    if not any(o["type"] == "cta" for o in normalized):
        normalized.append(
            {
                "type": "cta",
                "text": "Follow for more money tips",
                "start_word": cta_start,
                "duration_s": 4.5,
            }
        )

    filler_labels = [
        "REAL EXAMPLE",
        "SIMPLE MATH",
        "THIS IS KEY",
        "TIME MATTERS",
        "START SMALL",
        "STAY CONSISTENT",
    ]

    # Fill missing density to reduce static periods.
    if len(normalized) < MIN_OVERLAYS:
        needed = MIN_OVERLAYS - len(normalized)
        step = max(7, words // (needed + 2))
        for i in range(needed):
            candidate = min(words - 2, step * (i + 1))
            # Nudge forward past any occupied overlay window before placing.
            while candidate < words - 1 and any(
                ov["start_word"] <= candidate < ov["start_word"] + int(ov["duration_s"] * WPS)
                for ov in normalized
            ):
                candidate += 1
            # Skip if still occupied — no free window exists in this script.
            if any(
                ov["start_word"] <= candidate < ov["start_word"] + int(ov["duration_s"] * WPS)
                for ov in normalized
            ):
                continue
            normalized.append(
                {
                    "type": "label",
                    "text": filler_labels[i % len(filler_labels)],
                    "start_word": candidate,
                    "duration_s": 2.0,
                }
            )

    # Clamp and sort again.
    clamped = []
    for ov in normalized:
        parsed = _normalize_overlay(ov, words)
        if parsed:
            clamped.append(parsed)
    clamped.sort(key=lambda o: o["start_word"])

    if len(clamped) > MAX_OVERLAYS:
        # Keep hook, CTA, and the earliest useful overlays.
        hook = [o for o in clamped if o["type"] == "hook_number"][:1]
        cta = [o for o in clamped if o["type"] == "cta"][-1:]
        middle = [o for o in clamped if o["type"] not in {"hook_number", "cta"}]
        keep_middle = middle[: max(0, MAX_OVERLAYS - len(hook) - len(cta))]
        clamped = sorted(hook + keep_middle + cta, key=lambda o: o["start_word"])

    return clamped


def _normalize_short_data(data: dict, topic: dict) -> dict:
    """Return a new dict with overlays and title_options normalised. Does not mutate data."""
    script = data.get("voiceover_script", "")
    ranked_titles, scored_titles = _normalize_title_options(data.get("title_options", []), topic, script)
    stat_citations = _normalize_stat_citations(data.get("stat_citations", []), topic)
    return {
        **data,
        "overlays": _ensure_overlay_density(data.get("overlays", []), script, topic),
        "title_options": ranked_titles,
        "title_scores": scored_titles,
        "selected_title": ranked_titles[0] if ranked_titles else str(topic.get("topic", "")).strip(),
        "stat_citations": stat_citations,
    }


def _is_valid_short_shape(data: dict, topic: dict) -> tuple[bool, str]:
    """Validate shape only — does not mutate data."""
    script = data.get("voiceover_script", "")
    words = _word_count(script)
    if words < MIN_WORDS or words > MAX_WORDS:
        return False, f"word-count out of range ({words}, expected {MIN_WORDS}-{MAX_WORDS})"

    first = _first_token(script)
    if not re.search(r"[\d$%]", first):
        return False, "hook does not start with a numeric token"

    titles = data.get("title_options", [])
    if not isinstance(titles, list) or len(titles) < MIN_TITLE_OPTIONS:
        return False, "missing title_options variants"

    hook_ok, hook_reason = assess_hook_strength(script)
    if not hook_ok:
        return False, hook_reason

    # Check raw Claude output — _ensure_overlay_density is called later in _normalize_short_data.
    # We only fail here if Claude returned almost nothing, so normalization has too little to work with.
    raw_overlays = [
        ov for ov in (data.get("overlays") or [])
        if isinstance(ov, dict) and str(ov.get("type", "")).strip() in OVERLAY_TYPES
    ]
    if len(raw_overlays) < 3:
        return False, f"Claude returned too few overlays ({len(raw_overlays)}, expected >= 3)"

    return True, "ok"


def _build_prompt(topic: dict, feedback: str = "", retention_feedback: dict | None = None) -> str:
    feedback_block = (
        f"\n⚠️  PREVIOUS ATTEMPT REJECTED — {feedback}"
        f"\nYou MUST fix this before responding.\n"
        if feedback else ""
    )
    retention_block = _retention_prompt_block(retention_feedback)
    return f"""You are writing a YouTube Shorts script for a personal finance channel called ClearWealth.
{feedback_block}
TOPIC: {topic["topic"]}
ANGLE: {topic["angle"]}
PILLAR: {topic["pillar"]}
{retention_block}

FORMAT RULES:
- Total voiceover: 45–55 seconds (MINIMUM 110 words, TARGET 120–130 words, MAXIMUM 140 words at 2.5 words/sec)
- Count your words before finalising — scripts under 110 words will be rejected
- The VERY FIRST spoken token must contain a number or dollar amount
- The first 1.2 seconds must include: (1) number, (2) pain/problem, (3) consequence if ignored
- Use this 3-beat flow only: SHOCK NUMBER -> SIMPLE MATH PROOF -> ACTION STEP
- Uses simple language — explain like the viewer is smart but has never heard this before
- One concrete worked example with real dollar amounts and specific numbers
- Emotional arc: hook with urgency → simple explanation → empowering takeaway
- Ends with loop-style CTA that references the opening hook so viewers rewatch
- No earnings guarantees, no "you will make X", educational only
- Include one [PAUSE] after the hook number for impact

OVERLAY RULES:
- on_screen text appears at specific word indexes in the voiceover
- start_word: 0-indexed position of spoken word in voiceover (count only real words — do NOT count [PAUSE] markers)
- duration_s: how long it stays on screen
- types: "hook_number" (big stat, 4s), "label" (short phrase, 2.5s), "comparison" (before/after side by side, 4s), "cta" (final call to action, 5s)
- Plan 6–10 overlays total.
- Label overlays must be concise: 2–5 words, no ellipses.
- Every key number must have an overlay.
- There must be no dead visual gap longer than 2.5s.

Return ONLY this JSON, no explanation:
{{
  "title_options": [
    "<title option 1, max 60 chars, starts with number or power word>",
    "<title option 2>",
    "<title option 3>",
    "<title option 4>",
    "<title option 5>",
    "<title option 6>",
    "<title option 7>",
    "<title option 8>"
  ],
  "voiceover_script": "<full spoken script, 110–140 words, with [PAUSE] markers. First word must be a number or shocking fact.>",
  "overlays": [
    {{"type": "hook_number", "text": "<big stat>", "start_word": 0, "duration_s": 4.0}},
    {{"type": "label", "text": "<short phrase>", "start_word": 15, "duration_s": 2.5}},
    {{"type": "comparison", "left": "<before>", "right": "<after>", "start_word": 60, "duration_s": 4.0}},
    {{"type": "cta", "text": "Follow for more money tips", "start_word": 120, "duration_s": 5.0}}
  ],
  "stat_citations": ["<short source label 1>", "<short source label 2 optional>"],
  "description": "<2-3 sentence YouTube description. End with: '⚠️ Educational only. Not financial advice.'> #Shorts",
  "hashtags": ["#Shorts", "#PersonalFinance", "#MoneyTips", "<2 more relevant tags>"]
}}"""


def generate(
    topic: dict | None = None,
    used_topics: list[str] | None = None,
    retention_feedback: dict | None = None,
) -> dict:
    """
    Generate a standalone Short script.
    Returns the script data dict.
    Raises RuntimeError if generation fails after retries.
    """
    if topic is None:
        # Self-populate used_topics from persisted memory so callers don't have to.
        if used_topics is None:
            used_topics = [e["topic"] for e in _load_last_shorts() if e.get("topic")]
        topic = _pick_topic(used_topics)

    logger.info("Generating Short for topic: %s", topic["topic"])

    last_failure = ""
    for attempt in range(3):
        try:
            temperature = 0.8 + attempt * 0.1
            raw = _call_claude(
                _build_prompt(topic, feedback=last_failure, retention_feedback=retention_feedback),
                temperature=temperature,
            )
            data = _extract_json(raw)
        except Exception as exc:
            if attempt == 2:
                raise RuntimeError(f"Short script generation failed: {exc}") from exc
            last_failure = str(exc)
            logger.warning("Attempt %d failed: %s — retrying", attempt + 1, exc)
            continue

        script = data.get("voiceover_script", "")
        if not script:
            if attempt == 2:
                raise RuntimeError("Short script missing after all attempts")
            last_failure = "voiceover_script was empty"
            logger.warning("Script missing (attempt %d), retrying", attempt + 1)
            continue
        data["voiceover_script"] = _enforce_loop_ending(script)
        script = data["voiceover_script"]

        valid, reason = _is_valid_short_shape(data, topic)
        if not valid:
            if attempt == 2:
                raise RuntimeError(f"Short script validation failed: {reason}")
            last_failure = reason
            logger.warning("Short validation failed (attempt %d): %s", attempt + 1, reason)
            continue

        data = _normalize_short_data(data, topic)
        data["topic"] = topic["topic"]
        data["pillar"] = topic["pillar"]

        # Ensure description always ends with educational disclaimer.
        description = str(data.get("description", "")).strip()
        if "Educational only. Not financial advice." not in description:
            description = (description + " ⚠️ Educational only. Not financial advice.").strip()
        data["description"] = description

        _save_short_to_memory(topic["topic"], script)
        logger.info("Short script generated: %d words, %d overlays",
                    _word_count(script), len(data.get("overlays", [])))
        return data

    raise RuntimeError("Short script generation failed — all attempts exhausted")
