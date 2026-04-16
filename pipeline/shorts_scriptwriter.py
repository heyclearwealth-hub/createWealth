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
from __future__ import annotations

import json
import logging
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MODEL = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
LAST_SHORTS_FILE = Path("data/last_shorts.json")
MAX_SHORTS_MEMORY = 120
WPS = 2.5  # words per second at voiceover pace
MIN_WORDS = 110
MAX_WORDS = 125
MIN_OVERLAYS = 6
MAX_OVERLAYS = 10
MAX_TEXT_CHARS = 34
MIN_TITLE_OPTIONS = 5
MAX_TITLE_OPTIONS = 10

HOOK_PAIN_TERMS = {
    "lose", "loss", "waste", "wasted", "miss", "missing", "debt", "trap", "mistake",
    "expensive", "costly", "behind", "penalty", "stuck", "leave", "leaving", "left",
}
HOOK_CONSEQUENCE_TERMS = {
    "cost", "costs", "later", "future", "retirement", "years", "forever", "overpay",
    "wealth", "freedom", "growth", "compound", "thousands", "millions",
    "worth", "net worth", "broke", "zero",
}
TITLE_POWER_TERMS = {
    "why", "how", "mistake", "truth", "secret", "stop", "before", "after", "lose", "save",
}
# Finance-specific high-CTR terms — outperform generic "secret/truth" for money content
TITLE_POWER_TERMS_FINANCE = {
    "mistake", "waste", "lose", "trap", "avoid", "save", "free", "rich", "compound",
    "tax", "debt", "retire", "income", "earn", "invest", "raise", "salary",
}

OVERLAY_TYPES = {"hook_number", "label", "comparison", "cta"}

# Pillar-specific CTA copy — specific CTAs convert better than generic "money tips"
PILLAR_CTA_TEXT = {
    "investing": "Follow for investing wins",
    "budgeting": "Follow for money-saving tips",
    "debt": "Follow for debt freedom steps",
    "tax": "Follow for tax hacks",
    "career_income": "Follow for income growth tips",
}
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
    {"topic": "expense ratio drag", "pillar": "investing", "angle": "a 1 percent fee can cost six figures over decades"},
    {"topic": "dollar cost averaging", "pillar": "investing", "angle": "how consistency beats market timing stress"},
    {"topic": "S&P 500 vs total market", "pillar": "investing", "angle": "when broad diversification quietly wins"},
    {"topic": "HSA triple tax advantage", "pillar": "tax", "angle": "the one account with three tax benefits"},
    {"topic": "backdoor Roth IRA", "pillar": "tax", "angle": "high earners can still access Roth growth"},
    {"topic": "529 plan basics", "pillar": "investing", "angle": "small monthly deposits can reduce future student debt"},
    {"topic": "401k vesting schedule", "pillar": "career_income", "angle": "how leaving a job too early can forfeit money"},
    {"topic": "ESPP discount math", "pillar": "career_income", "angle": "when employee stock discounts are worth taking"},
    {"topic": "job hopping salary growth", "pillar": "career_income", "angle": "why switching roles can outpace annual raises"},
    {"topic": "raise vs bonus tax impact", "pillar": "tax", "angle": "why your bonus feels smaller than expected"},
    {"topic": "W-4 withholding reset", "pillar": "tax", "angle": "the payroll setting that prevents surprise tax bills"},
    {"topic": "capital gains tax basics", "pillar": "tax", "angle": "holding period can change what you owe"},
    {"topic": "RMD rules explained", "pillar": "tax", "angle": "missing required withdrawals triggers costly penalties"},
    {"topic": "credit utilization rule", "pillar": "debt", "angle": "keeping balances low can lift your score faster"},
    {"topic": "balance transfer card math", "pillar": "debt", "angle": "when a 0 percent transfer actually saves money"},
    {"topic": "APR vs APY", "pillar": "debt", "angle": "the hidden difference that changes total interest paid"},
    {"topic": "sinking funds method", "pillar": "budgeting", "angle": "prevent big expenses from becoming new debt"},
    {"topic": "zero based budgeting", "pillar": "budgeting", "angle": "give every dollar a job before the month starts"},
    {"topic": "pay yourself first", "pillar": "budgeting", "angle": "automate savings before spending can steal it"},
    {"topic": "biweekly payment strategy", "pillar": "debt", "angle": "extra payments can cut years off repayment"},
    {"topic": "rent vs buy break-even", "pillar": "budgeting", "angle": "the timeline that decides which is cheaper"},
    {"topic": "insurance deductible strategy", "pillar": "budgeting", "angle": "matching deductible to emergency fund lowers risk"},
    {"topic": "financial independence number", "pillar": "investing", "angle": "the simple math to estimate your freedom target"},
    {"topic": "cash flow review habit", "pillar": "budgeting", "angle": "a 10-minute weekly check that prevents money leaks"},
    {"topic": "401k loan risks", "pillar": "debt", "angle": "borrowing from retirement can cost more than it looks"},
]

_CLIENT = None
_CLIENT_API_KEY = None

HOOK_STATS_FILE = Path("data/hook_stats.json")
_HOOK_STATS_CACHE: dict | None = None


def _load_stat_bank() -> dict:
    global _HOOK_STATS_CACHE
    if _HOOK_STATS_CACHE is not None:
        return _HOOK_STATS_CACHE
    if not HOOK_STATS_FILE.exists():
        logger.warning("hook_stats.json not found — hook stats will be LLM-generated (unverified)")
        _HOOK_STATS_CACHE = {}
        return _HOOK_STATS_CACHE
    try:
        with HOOK_STATS_FILE.open() as f:
            raw = json.load(f)
        # Strip the _comment key — it's documentation only
        _HOOK_STATS_CACHE = {k: v for k, v in raw.items() if not k.startswith("_")}
    except Exception as exc:
        logger.error("Failed to load hook_stats.json: %s — falling back to LLM stat", exc)
        _HOOK_STATS_CACHE = {}
    return _HOOK_STATS_CACHE


def _lookup_stat(topic_name: str) -> dict | None:
    """Return the verified stat entry for this topic, or None if not in bank."""
    bank = _load_stat_bank()
    return bank.get(topic_name.strip())


def _load_last_shorts() -> list[dict]:
    if not LAST_SHORTS_FILE.exists():
        return []
    with LAST_SHORTS_FILE.open() as f:
        raw = json.load(f)
    # Migrate legacy plain-string entries written by older code.
    return [r if isinstance(r, dict) else {"topic": "", "script": r} for r in raw]


TOPIC_COOLDOWN_DAYS = int(os.environ.get("TOPIC_COOLDOWN_DAYS", "7"))


def _save_short_to_memory(topic_name: str, script_text: str) -> None:
    """
    Atomic read-modify-write using an advisory lock file to prevent two concurrent
    batch processes from picking the same topic before either has written to disk.
    Falls back to a plain write on platforms that don't support fcntl.
    """
    import tempfile

    LAST_SHORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = LAST_SHORTS_FILE.with_suffix(".lock")

    try:
        import fcntl
        with lock_path.open("w") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                entries = _load_last_shorts()
                entries.append({
                    "topic": topic_name,
                    "script": script_text,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                })
                if len(entries) > MAX_SHORTS_MEMORY:
                    entries = entries[-MAX_SHORTS_MEMORY:]
                # Write to a temp file then rename for atomicity.
                fd, tmp_path = tempfile.mkstemp(dir=LAST_SHORTS_FILE.parent, suffix=".tmp")
                try:
                    with os.fdopen(fd, "w") as fh:
                        json.dump(entries, fh)
                    os.replace(tmp_path, LAST_SHORTS_FILE)
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
    except ImportError:
        # Windows or restricted environment — plain write without locking.
        entries = _load_last_shorts()
        entries.append({
            "topic": topic_name,
            "script": script_text,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        })
        if len(entries) > MAX_SHORTS_MEMORY:
            entries = entries[-MAX_SHORTS_MEMORY:]
        with LAST_SHORTS_FILE.open("w") as f:
            json.dump(entries, f)


def _pick_topic(used_topics: list[str] | None = None) -> dict:
    """
    Pick a topic not recently used.
    Topics generated within TOPIC_COOLDOWN_DAYS are excluded first; if that
    leaves no candidates, the hard-exclude list (used_topics) is tried; if still
    empty, all topics are eligible (full reset).
    """
    now = datetime.now(timezone.utc)
    # Build a set of topics used within the cooldown window from persisted memory.
    recent_entries = _load_last_shorts()
    cooling_down: set[str] = set()
    for entry in recent_entries:
        saved_at_str = entry.get("saved_at", "")
        if not saved_at_str:
            continue
        try:
            saved_at = datetime.fromisoformat(saved_at_str.replace("Z", "+00:00"))
            days_ago = (now - saved_at).total_seconds() / 86400
            if days_ago < TOPIC_COOLDOWN_DAYS:
                cooling_down.add(entry.get("topic", ""))
        except ValueError:
            pass

    used = set(used_topics or []) | cooling_down
    available = [t for t in FINANCE_TOPICS if t["topic"] not in used]
    if not available:
        # Cooldown blocked everything — fall back to hard-exclude only
        available = [t for t in FINANCE_TOPICS if t["topic"] not in set(used_topics or [])]
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
    return len(re.findall(r"[A-Za-z0-9$%']+", _clean_script_text(script)))


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


# Pillar → keywords that should appear in citations for that pillar.
# If a citation contains none of these, it's likely from the wrong pillar.
_PILLAR_CITATION_KEYWORDS = {
    "investing": {"spiva", "vanguard", "s&p", "index", "morningstar", "etf", "fund", "return", "401k", "roth"},
    "budgeting": {"fed", "federal reserve", "shed", "consumer", "budget", "savings", "income", "spending"},
    "debt": {"federal reserve", "g.19", "credit", "debt", "loan", "card", "interest", "balance"},
    "tax": {"irs", "pub", "publication", "tax", "fica", "bracket", "deduction", "w-2"},
    "career_income": {"bls", "employment", "wage", "salary", "labor", "cost index", "income"},
}


def _validate_citations(citations: list[str], pillar: str) -> list[str]:
    """
    Warn if any citation looks mismatched for the pillar. Returns citations unchanged
    (warn-only — never block generation over a citation mismatch).
    """
    keywords = _PILLAR_CITATION_KEYWORDS.get(pillar, set())
    if not keywords:
        return citations
    for cite in citations:
        lower = cite.lower()
        if not any(kw in lower for kw in keywords):
            logger.warning(
                "Citation may be off-pillar (pillar='%s'): '%s' — "
                "ensure it matches the script's claims",
                pillar, cite,
            )
    return citations


def _normalize_stat_citations(raw, topic: dict) -> list[str]:
    out: list[str] = []
    for item in (raw or []):
        text = " ".join(str(item or "").split()).strip()
        if 4 <= len(text) <= 90 and text not in out:
            out.append(text)
    if not out:
        out = _default_stat_citations(topic)
    pillar = str(topic.get("pillar", "")).strip()
    return _validate_citations(out[:2], pillar)


def _split_sentences(script: str) -> list[str]:
    cleaned = " ".join(str(script or "").split()).strip()
    if not cleaned:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]


_LOOP_ACTION_VERBS = {
    "open", "start", "move", "switch", "put", "set", "pay", "cut", "invest",
    "save", "earn", "build", "apply", "use", "stop", "avoid", "check",
}
_LOOP_FINANCE_WORDS = {
    "compound", "interest", "return", "debt", "tax", "salary", "income",
    "budget", "credit", "fund", "retirement", "savings", "ira", "401k",
    "percent", "%", "$", "rate", "fee", "match",
}


def _sentence_action_score(sent: str) -> int:
    """Score a sentence for loop-ending quality: action verbs + finance keywords."""
    lower = sent.lower()
    words = set(re.findall(r"[a-z0-9$%]+", lower))
    return (
        sum(1 for v in _LOOP_ACTION_VERBS if v in words) * 2
        + sum(1 for f in _LOOP_FINANCE_WORDS if f in lower)
    )


def _loop_ending_line(script: str) -> str:
    """
    Build a context-aware rewatch prompt by extracting the script's core insight.

    Picks the highest-scoring sentence (action verbs + finance keywords) from the
    second half of the script, skipping CTA-type lines. Falls back to a mid-script
    number if no substantive sentence is found.
    """
    sentences = _split_sentences(_clean_script_text(script))
    cta_words = {"follow", "save this", "subscribe", "replay", "comment", "share"}

    # Restrict to second half of script — first half is hook/proof, second half has the takeaway.
    second_half = sentences[len(sentences) // 2:] if len(sentences) >= 4 else sentences

    candidates = [
        s for s in second_half
        if not any(w in s.lower() for w in cta_words) and len(s.split()) >= 4
    ]

    core_insight = ""
    if candidates:
        # Pick highest action-verb + finance-keyword density sentence.
        best = max(candidates, key=_sentence_action_score)
        words = best.split()
        core_insight = " ".join(words[:8]).rstrip(".,!?")

    if core_insight:
        short_core = " ".join(core_insight.split()[:5]).rstrip(".,!?")
        return f"Replay and apply this: {short_core}."
    # Fallback: find a mid-script number (skip the first one, which is the hook).
    # Referencing the hook number again in the loop ending provides no new curiosity trigger.
    all_numbers = re.findall(r"\$?\d[\d,]*(?:\.\d+)?%?", _clean_script_text(script))
    mid_number = all_numbers[1] if len(all_numbers) >= 2 else (all_numbers[0] if all_numbers else "this")
    return f"Seriously, watch this again: the {mid_number} number hits different on second watch."


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
    Hook quality gate:
    1. First word (index 0-2) must contain a number — viewer must see the stat before 1.2s.
    2. First 14 words must include a pain term and a consequence term.
    """
    tokens = re.findall(r"[A-Za-z0-9$%']+", _clean_script_text(script))
    hook_tokens = tokens[:14]
    hook_text = " ".join(hook_tokens).lower()

    # Rule 1: number must appear in the first 3 tokens (≤1.2s at 2.5 WPS)
    first_three = tokens[:3]
    if not any(re.search(r"\d", t) for t in first_three):
        return False, (
            "hook number not in first 3 words — first word must be a stat "
            f"(found: '{' '.join(first_three)}')"
        )

    has_pain = any(term in hook_text for term in HOOK_PAIN_TERMS)
    has_consequence = any(term in hook_text for term in HOOK_CONSEQUENCE_TERMS)

    if not has_pain:
        return False, "hook missing pain framing in opening beat"
    if not has_consequence:
        return False, "hook missing consequence framing in opening beat"
    return True, "ok"


def _repair_hook_opening(script: str, reason: str) -> str:
    """
    Best-effort hook repair used only when LLM output narrowly misses hook gate wording.
    Keeps the first token intact (numeric requirement) and injects compact signal words
    into the opening so validation can pass without a full regeneration cycle.
    """
    cleaned = str(script or "").strip()
    if not cleaned:
        return cleaned
    missing_pain = "missing pain framing" in reason
    missing_consequence = "missing consequence framing" in reason
    if not (missing_pain or missing_consequence):
        return cleaned

    sentences = _split_sentences(cleaned)
    if not sentences:
        return cleaned

    first_num = _extract_first_number(cleaned)
    is_percent = first_num.endswith("%")
    is_dollar = first_num.startswith("$")
    # Keep additions short and natural to avoid obvious AI-repair artifacts.
    if missing_pain and missing_consequence:
        if is_dollar:
            repaired_hook = f"{first_num} you lose early can cost you for years."
        elif is_percent:
            repaired_hook = f"{first_num} of people lose money and pay for it over years."
        else:
            repaired_hook = f"{first_num} people lose money and pay for it over years."
    elif missing_pain:
        if is_dollar:
            repaired_hook = f"{first_num} you lose early is hard to recover."
        elif is_percent:
            repaired_hook = f"{first_num} of people lose free money fast."
        else:
            repaired_hook = f"{first_num} people lose free money fast."
    else:
        if is_dollar:
            repaired_hook = f"{first_num} left unclaimed costs you for years."
        elif is_percent:
            repaired_hook = f"{first_num} of people leave money unclaimed, and that costs them over years."
        else:
            repaired_hook = f"{first_num} people leave money unclaimed, and that costs them over years."

    remainder = " ".join(sentences[1:]).strip()
    return f"{repaired_hook} {remainder}".strip()


def _trim_script_to_max_words(script: str, max_words: int = MAX_WORDS) -> str:
    """
    Deterministic trim when LLM output exceeds max word budget.
    Preserves the opening hook token by truncating from the tail.
    """
    text = str(script or "").strip()
    if not text:
        return text
    if _word_count(text) <= max_words:
        return text

    out_tokens: list[str] = []
    running_words = 0
    for tok in text.split():
        token_words = len(re.findall(r"[A-Za-z0-9$%']+", tok))
        if running_words + token_words > max_words:
            break
        out_tokens.append(tok)
        running_words += token_words
    out = " ".join(out_tokens).strip()
    if out and out[-1] not in ".!?":
        out += "."
    return out


def _pad_script_to_min_words(script: str, min_words: int = MIN_WORDS) -> str:
    """Append concise, varied filler until script meets minimum word count."""
    text = str(script or "").strip()
    if not text:
        return text
    if _word_count(text) >= min_words:
        return text

    if text[-1] not in ".!?":
        text += "."

    lower = _clean_script_text(text).lower()
    focus = "money plan"
    focus_hints = [
        ("debt payoff", ("debt", "interest", "credit card", "loan", "apr")),
        ("tax setup", ("tax", "irs", "withholding", "deduction", "refund")),
        ("investing plan", ("invest", "index", "ira", "401k", "portfolio", "returns")),
        ("budget system", ("budget", "spending", "expenses", "savings", "cash flow")),
        ("income strategy", ("salary", "raise", "career", "income", "job")),
    ]
    for focus_name, terms in focus_hints:
        if any(term in lower for term in terms):
            focus = focus_name
            break

    short_options = [
        "Start today and check it next week.",
        "Do this now and review your numbers weekly.",
        "Set this up today and stay consistent.",
        "Take one step today and keep it simple.",
    ]
    medium_options = [
        "Start today, automate one move, and review it every week.",
        "Set this up now, then track progress every payday.",
        "Do one action today and keep your plan on autopilot each week.",
        "Make this move now and check your progress every Friday.",
    ]
    long_options = [
        "Start today, automate one transfer, and protect your {focus} before extra spending takes over.",
        "Do this now, lock in one recurring action, and review your {focus} every week without skipping.",
        "Make this move today, repeat it weekly, and let your {focus} compound instead of drifting.",
        "Set this up right now, keep it automatic, and track your {focus} every single week.",
    ]

    for _ in range(4):
        current_words = _word_count(text)
        if current_words >= min_words:
            break
        needed = min_words - current_words
        if needed <= 4:
            pool = short_options
        elif needed <= 9:
            pool = medium_options
        else:
            pool = long_options

        seed = sum(ord(ch) for ch in _clean_script_text(text)) + needed * 17
        idx = seed % len(pool)
        extra = pool[idx].format(focus=focus)
        if extra.lower() in text.lower():
            extra = pool[(idx + 1) % len(pool)].format(focus=focus)
        text = f"{text} {extra}".strip()

    return text


def _fit_script_word_budget(script: str, min_words: int = MIN_WORDS, max_words: int = MAX_WORDS) -> str:
    """Clamp script length into accepted [min_words, max_words] range."""
    out = str(script or "").strip()
    if not out:
        return out
    wc = _word_count(out)
    if wc > max_words:
        out = _trim_script_to_max_words(out, max_words)
        wc = _word_count(out)
    if wc < min_words:
        out = _pad_script_to_min_words(out, min_words)
        if _word_count(out) > max_words:
            out = _trim_script_to_max_words(out, max_words)
    return out


def _ensure_numeric_opening(script: str, topic: dict | None = None) -> str:
    """
    Ensure the first spoken token contains a numeric value for hook compliance.
    Rewrites the first sentence if needed while preserving the rest.
    """
    text = str(script or "").strip()
    if not text:
        return text
    if re.search(r"[\d$%]", _first_token(text)):
        return text

    sentences = _split_sentences(text)
    if not sentences:
        return text
    first_num = _extract_first_number(text)
    if not re.search(r"\d", first_num):
        first_num = "3"
    topic_phrase = str((topic or {}).get("topic", "money rule")).strip().lower()
    topic_words = re.findall(r"[a-z0-9]+", topic_phrase)[:3]
    topic_stub = " ".join(topic_words) if topic_words else "money"
    new_hook = f"{first_num} people ignore this {topic_stub} rule and lose money over years."
    remainder = " ".join(sentences[1:]).strip()
    return f"{new_hook} {remainder}".strip()


def _retime_overlays_for_script_edit(data: dict, old_script: str, new_script: str) -> None:
    """
    Keep overlay timing roughly aligned when fallback logic rewrites script text.
    Maps old word indexes to new indexes by relative position.
    """
    overlays = data.get("overlays")
    if not isinstance(overlays, list) or not overlays:
        return

    old_words = max(1, _word_count(old_script))
    new_words = max(1, _word_count(new_script))
    if old_words == new_words:
        return

    retimed: list[dict] = []
    for ov in overlays:
        if not isinstance(ov, dict):
            continue
        kind = str(ov.get("type", "")).strip()
        cloned = dict(ov)
        try:
            old_start = int(cloned.get("start_word", 0))
        except (TypeError, ValueError):
            old_start = 0

        if kind == "hook_number":
            new_start = 0
            first_num = _extract_first_number(new_script)
            if first_num:
                cloned["text"] = first_num
        elif kind == "cta":
            new_start = max(new_words - int(3.0 * WPS), 0)
        else:
            ratio_pos = max(0.0, min(1.0, old_start / old_words))
            new_start = int(round(ratio_pos * new_words))

        cloned["start_word"] = max(0, min(new_start, max(0, new_words - 1)))
        retimed.append(cloned)

    data["overlays"] = retimed


def _apply_stat_bank(data: dict, topic: dict) -> dict:
    """
    Override hook_number overlay and stat_citations with verified bank values.
    Called after normalization so LLM output can never replace a verified stat.
    No-op when the topic has no bank entry.
    """
    stat_entry = _lookup_stat(topic.get("topic", ""))
    if not stat_entry:
        return data

    # Force hook_number overlay to use verified number + short claim subtitle
    overlays = data.get("overlays", [])
    hook_set = False
    for ov in overlays:
        if ov.get("type") == "hook_number" and ov.get("start_word", 999) <= 2:
            ov["text"] = stat_entry["overlay_number"]
            ov["subtitle"] = stat_entry["overlay_subtitle"]
            hook_set = True
            break
    if not hook_set:
        # Fallback: prepend a hook overlay if normalization dropped it
        overlays.insert(0, {
            "type": "hook_number",
            "text": stat_entry["overlay_number"],
            "subtitle": stat_entry["overlay_subtitle"],
            "start_word": 0,
            "duration_s": 4.0,
        })
    data["overlays"] = overlays

    # Force stat_citations to the verified source — overrides anything LLM wrote
    data["stat_citations"] = [stat_entry["source_short"]]
    logger.info(
        "Stat bank applied for '%s': overlay=%s | source=%s",
        topic.get("topic"), stat_entry["overlay_number"], stat_entry["source_short"],
    )
    return data


def _finalize_short_payload(data: dict, topic: dict) -> dict:
    """Normalize and stamp common metadata for a successful short payload."""
    data = _normalize_short_data(data, topic)
    data = _apply_stat_bank(data, topic)
    data["topic"] = topic["topic"]
    data["pillar"] = topic["pillar"]

    description = str(data.get("description", "")).strip()
    # Keep mobile preview readable: remove leading hashtag spam if model emits it.
    description = re.sub(r"^(?:\s*#[A-Za-z][A-Za-z0-9_]+\s*)+", "", description).strip()
    if "Educational only. Not financial advice." not in description:
        description = (description + " ⚠️ Educational only. Not financial advice.").strip()
    # Append hashtags to description text so YouTube indexes them for hashtag-feed discovery.
    # YouTube's `tags` API field is invisible metadata; only in-description hashtags appear
    # in the hashtag feed (#Shorts, #PersonalFinance, etc.).
    hashtags = data.get("hashtags") or []
    if isinstance(hashtags, list) and hashtags:
        ht_parts: list[str] = []
        for h in hashtags[:15]:
            tag = " ".join(str(h or "").split()).strip()
            if not tag:
                continue
            if not tag.startswith("#"):
                tag = "#" + tag
            if tag not in ht_parts:
                ht_parts.append(tag)
        if ht_parts:
            description = description + "\n\n" + " ".join(ht_parts)
    data["description"] = description
    return data


def _title_score(title: str, topic: dict) -> float:
    cleaned = " ".join(str(title or "").split()).strip()
    if not cleaned:
        return -999.0
    lower = cleaned.lower()
    score = 0.0
    length = len(cleaned)

    if 42 <= length <= 55:   # optimal SERP range — visible on mobile without truncation
        score += 2.2
    elif 32 <= length < 42:
        score += 1.0
    elif 55 < length <= 70:
        score -= 0.5   # truncated on mobile SERP, wastes tail keywords
    else:
        score -= 1.5   # too short or too long

    if re.search(r"[\d$%]", cleaned):
        score += 2.0
    # Use finance-specific power terms for finance pillars (higher signal for money CTR)
    pillar = str(topic.get("pillar", "")).lower()
    _power_terms = (
        TITLE_POWER_TERMS_FINANCE
        if pillar in {"investing", "budgeting", "debt", "tax", "career_income"}
        else TITLE_POWER_TERMS
    )
    if any(term in lower for term in _power_terms):
        score += 1.5
    # Extra boost for the proven number-verb-noun pattern ("5 Mistakes With...")
    if re.search(r"^\d+\s+\w+\s+\w+\s", lower):
        score += 2.0
    elif re.search(r"^\d+\s+", lower):
        score += 1.0
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
        parts.append(f"- Viewers have dropped off around {stamp_text}.")
        # Map each drop-off range to a concrete structural intervention.
        for s in dropoffs[:5]:
            t = float(s)
            if t <= 5:
                parts.append(
                    "  → The hook (first 5s) is losing viewers. Lead with a bigger, more specific number. "
                    "Cut any introductory words before the stat."
                )
            elif t <= 15:
                parts.append(
                    "  → Viewers leave right after the hook. The 'simple math proof' step (5–15s) is too slow. "
                    "Replace explanation with one concrete comparison (e.g., BEFORE vs AFTER numbers)."
                )
            elif t <= 32:
                parts.append(
                    "  → Mid-video drop at the explanation segment. Keep the worked example to 2 sentences max — "
                    "one specific dollar amount, one surprising outcome. Cut filler phrases."
                )
            else:
                parts.append(
                    "  → Late drop-off: the action step or CTA isn't landing. Make the takeaway a single, "
                    "specific action ('open a Roth IRA today') rather than general advice."
                )
    if notes:
        parts.append(f"- Recent retention notes: {notes}")
    if not parts:
        return ""
    return "\nRETENTION FEEDBACK (apply these structural fixes):\n" + "\n".join(parts) + "\n"


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
        normalized["text"] = _normalize_text(cta, "Save this and follow for more")

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
        stat_entry = _lookup_stat(topic.get("topic", ""))
        hook_text = stat_entry["overlay_number"] if stat_entry else _extract_first_number(script)
        hook_overlay: dict = {
            "type": "hook_number",
            "text": hook_text,
            "start_word": 0,
            "duration_s": 4.0,
        }
        if stat_entry:
            hook_overlay["subtitle"] = stat_entry["overlay_subtitle"]
        normalized.insert(0, hook_overlay)

    # Ensure CTA overlay near the end — use pillar-specific copy.
    cta_start = max(words - int(3.0 * WPS), 0)
    if not any(o["type"] == "cta" for o in normalized):
        pillar = str(topic.get("pillar", "")).lower()
        cta_copy = PILLAR_CTA_TEXT.get(pillar, "Follow for more money tips")
        normalized.append(
            {
                "type": "cta",
                "text": cta_copy,
                "start_word": cta_start,
                "duration_s": 3.8,
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

    # Hook exclusion zone: no other overlay within ±1.25s (≈3 words) of the hook_number.
    # Two overlays at time-zero split viewer attention during the most critical moment.
    HOOK_EXCL_WORDS = int(1.25 * WPS)  # ≈3 words
    hook_ovs = [o for o in normalized if o["type"] == "hook_number"]
    hook_start = hook_ovs[0]["start_word"] if hook_ovs else 0
    hook_end = hook_start + int((hook_ovs[0]["duration_s"] if hook_ovs else 4.0) * WPS)
    # Remove any non-hook overlay that falls inside the exclusion zone.
    normalized = [
        o for o in normalized
        if o["type"] == "hook_number"
        or not (hook_start - HOOK_EXCL_WORDS <= o["start_word"] < hook_end + HOOK_EXCL_WORDS)
    ]

    # Enforce 2.5s minimum gap between consecutive label overlays.
    MIN_LABEL_GAP_WORDS = int(2.5 * WPS)  # ~6 words at 2.5 WPS
    label_ends: list[int] = sorted(
        ov["start_word"] + int(ov["duration_s"] * WPS)
        for ov in normalized if ov.get("type") == "label"
    )

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
            # Enforce minimum gap from the previous label end.
            if any(candidate < end + MIN_LABEL_GAP_WORDS for end in label_ends):
                continue
            label_end = candidate + int(2.0 * WPS)
            label_ends.append(label_end)
            label_ends.sort()
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

    stat_entry = _lookup_stat(topic.get("topic", ""))
    if stat_entry:
        stat_block = (
            f"\nVERIFIED HOOK STAT (use exactly as written — do NOT invent a different number):\n"
            f"  Opening sentence: \"{stat_entry['hook_statement']}\"\n"
            f"  Source: {stat_entry['source']}\n"
            f"  RULE: The script MUST open with the sentence above, verbatim. "
            f"Do not replace the number, do not round it, do not paraphrase it.\n"
        )
    else:
        stat_block = (
            "\nWARNING: No verified stat found for this topic. "
            "You MUST include a real, specific statistic with a named source in stat_citations. "
            "Do not use a vague percentage without a citation.\n"
        )

    return f"""You are writing a YouTube Shorts script for a personal finance channel called ClearWealth.
{feedback_block}
TOPIC: {topic["topic"]}
ANGLE: {topic["angle"]}
PILLAR: {topic["pillar"]}
{stat_block}
{retention_block}

FORMAT RULES:
- Total voiceover: 45–55 seconds (MINIMUM {MIN_WORDS} words, TARGET {MIN_WORDS + 5}–{MAX_WORDS - 2} words, MAXIMUM {MAX_WORDS} words at ~2.5 words/sec)
- Count your words before finalising — scripts under {MIN_WORDS} words will be rejected
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
  "voiceover_script": "<full spoken script, {MIN_WORDS}–{MAX_WORDS} words, with [PAUSE] markers. First word must be a number or shocking fact.>",
  "overlays": [
    {{"type": "hook_number", "text": "<big stat>", "start_word": 0, "duration_s": 4.0}},
    {{"type": "label", "text": "<short phrase>", "start_word": 15, "duration_s": 2.5}},
    {{"type": "comparison", "left": "<before>", "right": "<after>", "start_word": 60, "duration_s": 4.0}},
    {{"type": "cta", "text": "Follow for more money tips", "start_word": 100, "duration_s": 5.0}}
  ],
  "stat_citations": ["<short source label 1>", "<short source label 2 optional>"],
  "description": "<2-3 sentence YouTube description — no disclaimer, no hashtags>",
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

    def _apply_script_update(data: dict, new_script: str) -> str:
        old_script = str(data.get("voiceover_script", "") or "")
        fitted = _fit_script_word_budget(new_script, MIN_WORDS, MAX_WORDS)
        if fitted != old_script:
            _retime_overlays_for_script_edit(data, old_script, fitted)
        data["voiceover_script"] = fitted
        return fitted

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
        data["voiceover_script"] = script
        script = _apply_script_update(data, _enforce_loop_ending(script))

        valid, reason = _is_valid_short_shape(data, topic)
        if not valid:
            if "hook does not start with a numeric token" in reason:
                numeric_script = _ensure_numeric_opening(data.get("voiceover_script", ""), topic=topic)
                if numeric_script != data.get("voiceover_script", ""):
                    _apply_script_update(data, numeric_script)
                    valid, reason = _is_valid_short_shape(data, topic)
                    if valid:
                        logger.info("Applied numeric-opening fallback for topic '%s'", topic["topic"])

            # Self-heal slight over-length scripts instead of failing the run.
            if not valid and "word-count out of range" in reason:
                fitted_script = _fit_script_word_budget(data.get("voiceover_script", ""), MIN_WORDS, MAX_WORDS)
                if fitted_script != data.get("voiceover_script", ""):
                    _apply_script_update(data, fitted_script)
                    valid, reason = _is_valid_short_shape(data, topic)
                    if valid:
                        logger.info("Applied word-count fallback for topic '%s'", topic["topic"])

            if (
                not valid
                and (
                    "hook missing pain framing" in reason
                    or "hook missing consequence framing" in reason
                    or "hook number not in first 3 words" in reason
                )
            ):
                repaired_script = _repair_hook_opening(data.get("voiceover_script", ""), reason)
                if repaired_script != data.get("voiceover_script", ""):
                    _apply_script_update(data, repaired_script)
                    valid, reason = _is_valid_short_shape(data, topic)
                    if valid:
                        logger.info("Applied hook auto-repair for topic '%s'", topic["topic"])
                    else:
                        logger.warning("Hook auto-repair attempted but still invalid: %s", reason)
            if valid:
                data = _finalize_short_payload(data, topic)
                _save_short_to_memory(topic["topic"], data["voiceover_script"])
                logger.info("Short script generated: %d words, %d overlays",
                            _word_count(data["voiceover_script"]), len(data.get("overlays", [])))
                return data
            if attempt == 2:
                raise RuntimeError(f"Short script validation failed: {reason}")
            last_failure = reason
            logger.warning("Short validation failed (attempt %d): %s", attempt + 1, reason)
            continue

        data = _finalize_short_payload(data, topic)

        _save_short_to_memory(topic["topic"], data["voiceover_script"])
        logger.info("Short script generated: %d words, %d overlays",
                    _word_count(data["voiceover_script"]), len(data.get("overlays", [])))
        return data

    raise RuntimeError("Short script generation failed — all attempts exhausted")
