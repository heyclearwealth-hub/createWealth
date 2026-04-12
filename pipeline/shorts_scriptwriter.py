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
import re
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
LAST_SHORTS_FILE = Path("data/last_shorts.json")
MAX_SHORTS_MEMORY = 10
WPS = 2.5  # words per second at voiceover pace

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


def _load_last_shorts() -> list[str]:
    if not LAST_SHORTS_FILE.exists():
        return []
    with LAST_SHORTS_FILE.open() as f:
        return json.load(f)


def _save_short_to_memory(script_text: str) -> None:
    scripts = _load_last_shorts()
    scripts.append(script_text)
    if len(scripts) > MAX_SHORTS_MEMORY:
        scripts = scripts[-MAX_SHORTS_MEMORY:]
    with LAST_SHORTS_FILE.open("w") as f:
        json.dump(scripts, f)


def _pick_topic(used_topics: list[str] | None = None) -> dict:
    """Pick a topic not recently used."""
    used = set(used_topics or [])
    for t in FINANCE_TOPICS:
        if t["topic"] not in used:
            return t
    return FINANCE_TOPICS[0]  # fallback: cycle back


def _call_claude(prompt: str, temperature: float = 0.8) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
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


def _build_prompt(topic: dict) -> str:
    return f"""You are writing a YouTube Shorts script for a personal finance channel called ClearWealth.

TOPIC: {topic["topic"]}
ANGLE: {topic["angle"]}
PILLAR: {topic["pillar"]}

FORMAT RULES:
- Total voiceover: 45–55 seconds (110–140 words at 2.5 words/sec)
- Opens with ONE shocking number or stat in the first 2 seconds — no intro, no greeting
- Uses simple language — explain like the viewer is smart but has never heard this before
- One concrete worked example with real dollar amounts and specific numbers
- Emotional arc: hook with urgency → simple explanation → empowering takeaway
- Ends with direct CTA: "Follow for more" or "Save this" — 1 sentence max
- No earnings guarantees, no "you will make X", educational only
- Include one [PAUSE] after the hook number for impact

OVERLAY RULES:
- on_screen text appears at specific word indexes in the voiceover
- start_word: 0-indexed position of word in voiceover where overlay appears
- duration_s: how long it stays on screen
- types: "hook_number" (big stat, 4s), "label" (short phrase, 2.5s), "comparison" (before/after side by side, 4s), "cta" (final call to action, 5s)
- Plan 6–9 overlays total. Every key number must have an overlay.

Return ONLY this JSON, no explanation:
{{
  "title_options": [
    "<title option 1, max 60 chars, starts with number or power word>",
    "<title option 2>",
    "<title option 3>"
  ],
  "voiceover_script": "<full 45-55s spoken script with [PAUSE] markers. First word must be a number or shocking fact.>",
  "overlays": [
    {{"type": "hook_number", "text": "<big stat>", "start_word": 0, "duration_s": 4.0}},
    {{"type": "label", "text": "<short phrase>", "start_word": 15, "duration_s": 2.5}},
    {{"type": "comparison", "left": "<before>", "right": "<after>", "start_word": 60, "duration_s": 4.0}},
    {{"type": "cta", "text": "Follow for more money tips", "start_word": 120, "duration_s": 5.0}}
  ],
  "description": "<2-3 sentence YouTube description. End with: '⚠️ Educational only. Not financial advice.'> #Shorts",
  "hashtags": ["#Shorts", "#PersonalFinance", "#MoneyTips", "<2 more relevant tags>"]
}}"""


def generate(topic: dict | None = None, used_topics: list[str] | None = None) -> dict:
    """
    Generate a standalone Short script.
    Returns the script data dict.
    Raises RuntimeError if generation fails after retries.
    """
    if topic is None:
        topic = _pick_topic(used_topics)

    logger.info("Generating Short for topic: %s", topic["topic"])

    for attempt in range(3):
        try:
            temperature = 0.8 + attempt * 0.1
            raw = _call_claude(_build_prompt(topic), temperature=temperature)
            data = _extract_json(raw)
        except Exception as exc:
            if attempt == 2:
                raise RuntimeError(f"Short script generation failed: {exc}") from exc
            logger.warning("Attempt %d failed: %s — retrying", attempt + 1, exc)
            continue

        script = data.get("voiceover_script", "")
        if not script or len(script.split()) < 80:
            if attempt == 2:
                raise RuntimeError("Short script too short after all attempts")
            logger.warning("Script too short (attempt %d), retrying", attempt + 1)
            continue

        data["topic"] = topic["topic"]
        data["pillar"] = topic["pillar"]
        _save_short_to_memory(script)
        logger.info("Short script generated: %d words, %d overlays",
                    len(script.split()), len(data.get("overlays", [])))
        return data

    raise RuntimeError("Short script generation failed — all attempts exhausted")
