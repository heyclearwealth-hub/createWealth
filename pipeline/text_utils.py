"""
text_utils.py — Shared text normalization utilities for the pipeline.

Single source of truth for finance-domain acronym patterns so that
shorts_scriptwriter.py and shorts_renderer.py never diverge.
"""
from __future__ import annotations

import re

# Finance acronyms that TTS and LLM output often lowercase — normalize to display form.
ACRONYM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bapr\b", re.IGNORECASE), "APR"),
    (re.compile(r"\bapy\b", re.IGNORECASE), "APY"),
    (re.compile(r"\betf\b", re.IGNORECASE), "ETF"),
    (re.compile(r"\bira\b", re.IGNORECASE), "IRA"),
    (re.compile(r"\bhsa\b", re.IGNORECASE), "HSA"),
    (re.compile(r"\brmd\b", re.IGNORECASE), "RMD"),
    (re.compile(r"\b401k\b", re.IGNORECASE), "401k"),
    (re.compile(r"(?<!\w)w-4(?!\w)", re.IGNORECASE), "W-4"),
    (re.compile(r"\bhysa\b", re.IGNORECASE), "HYSA"),
    (re.compile(r"\bfsa\b", re.IGNORECASE), "FSA"),
]


def fix_finance_acronyms(text: str) -> str:
    out = str(text or "")
    for pattern, replacement in ACRONYM_PATTERNS:
        out = pattern.sub(replacement, out)
    return out
