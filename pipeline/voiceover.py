"""
voiceover.py — ElevenLabs TTS with optional word-level timestamps.
"""
import base64
import json
import logging
import os
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
ELEVENLABS_TTS_TIMESTAMPS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"
DEFAULT_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")  # "Sarah" — professional female
OUTPUT_PATH = Path("workspace/voiceover.mp3")
ALIGNMENT_PATH = Path("workspace/voiceover_alignment.json")

TTS_SETTINGS = {
    "model_id": "eleven_turbo_v2_5",
    "voice_settings": {
        "stability": 0.42,
        "similarity_boost": 0.82,
        "style": 0.12,
        "use_speaker_boost": True,
    },
    "output_format": "mp3_44100_128",
}


def _clean_script(script: str) -> str:
    """Convert stage markers into natural spoken pauses and remove non-spoken tags."""
    import re
    text = str(script or "")
    # Keep audible pacing at hook beats by converting [PAUSE] into a natural pause.
    text = re.sub(r"\[PAUSE\]", " ... ", text)
    text = re.sub(r"\[STAT:[^\]]*\]", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip()


def _extract_word_start_times(alignment: dict, text: str) -> list[float]:
    """
    Convert ElevenLabs character-level alignment into per-word start times.
    Words are sequences of [A-Za-z0-9$%] to match overlay indexing logic.
    """
    import re

    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    if len(chars) != len(starts):
        return []

    word_start_times: list[float] = []
    prev_is_word = False
    for idx, (ch, t) in enumerate(zip(chars, starts)):
        next_ch = chars[idx + 1] if idx + 1 < len(chars) else ""
        # Keep apostrophes inside words so contractions ("don't", "you're")
        # map to one spoken token instead of splitting into 2 tokens.
        is_apostrophe_connector = (
            ch == "'"
            and prev_is_word
            and bool(re.match(r"[A-Za-z0-9$%]", next_ch))
        )
        is_word = bool(re.match(r"[A-Za-z0-9$%]", ch)) or is_apostrophe_connector
        if is_word and not prev_is_word:
            word_start_times.append(float(t))
        prev_is_word = is_word

    # If we got fewer word starts than actual words, the alignment is incomplete.
    # Return [] so callers get a hard fail (missing captions) rather than drifted captions.
    expected_words = len(re.findall(r"[A-Za-z0-9$%']+", text))
    if len(word_start_times) < expected_words:
        logger.error(
            "Timestamp alignment incomplete: got %d word timestamps, expected %d "
            "— captions will use WPS fallback (expect ±0.5s drift). "
            "Check ElevenLabs character alignment response.",
            len(word_start_times), expected_words,
        )
        return []
    return word_start_times[:expected_words]


def generate(script: str, output_path: Path = OUTPUT_PATH, voice_id: str = DEFAULT_VOICE_ID) -> Path:
    """
    Generate voiceover MP3 from script text via ElevenLabs streaming API.
    Returns the path to the saved MP3 file.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise EnvironmentError("ELEVENLABS_API_KEY not set")

    clean_text = _clean_script(script)
    if not clean_text:
        raise ValueError("Script text is empty after cleaning")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    url = ELEVENLABS_API_URL.format(voice_id=voice_id)
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": clean_text,
        **TTS_SETTINGS,
    }

    logger.info("Requesting TTS for %.0f chars via ElevenLabs", len(clean_text))
    response = requests.post(url, json=payload, headers=headers, stream=True, timeout=120)

    if response.status_code != 200:
        raise RuntimeError(
            f"ElevenLabs API error {response.status_code}: {response.text[:200]}"
        )

    bytes_written = 0
    with output_path.open("wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
                bytes_written += len(chunk)

    if bytes_written < 10_000:
        raise RuntimeError(f"Voiceover file suspiciously small: {bytes_written} bytes")

    logger.info("Voiceover saved to %s (%.1f KB)", output_path, bytes_written / 1024)
    return output_path


def generate_with_timestamps(
    script: str,
    output_path: Path = OUTPUT_PATH,
    alignment_path: Path = ALIGNMENT_PATH,
    voice_id: str = DEFAULT_VOICE_ID,
) -> tuple[Path, list[float]]:
    """
    Generate voiceover and return word-level start times.
    Falls back to basic generate() if timestamps request fails.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise EnvironmentError("ELEVENLABS_API_KEY not set")

    clean_text = _clean_script(script)
    if not clean_text:
        raise ValueError("Script text is empty after cleaning")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    alignment_path.parent.mkdir(parents=True, exist_ok=True)

    url = ELEVENLABS_TTS_TIMESTAMPS_URL.format(voice_id=voice_id)
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "text": clean_text,
        **TTS_SETTINGS,
    }

    import time as _time

    logger.info("Requesting TTS+timestamps for %.0f chars via ElevenLabs", len(clean_text))
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=180)
            if response.status_code == 429:
                wait = 10 * (attempt + 1)
                logger.warning("ElevenLabs rate-limited, waiting %ds before retry", wait)
                _time.sleep(wait)
                last_exc = RuntimeError(f"ElevenLabs 429 rate-limit (attempt {attempt + 1})")
                continue
            if response.status_code != 200:
                raise RuntimeError(f"ElevenLabs API {response.status_code}: {response.text[:200]}")
            data = response.json()
            audio_b64 = data.get("audio_base64", "")
            if not audio_b64:
                raise RuntimeError("No audio_base64 in timestamps response")
            audio_bytes = base64.b64decode(audio_b64)
            with output_path.open("wb") as f:
                f.write(audio_bytes)

            alignment = data.get("alignment") or data.get("normalized_alignment") or {}
            word_times = _extract_word_start_times(alignment, clean_text)

            alignment_payload = {
                "alignment": alignment,
                "word_start_times": word_times,
                "text": clean_text,
            }
            alignment_path.write_text(json.dumps(alignment_payload))

            logger.info("Voiceover+timestamps saved (%d words)", len(word_times))
            return output_path, word_times
        except Exception as exc:
            last_exc = exc
            if attempt < 1:
                logger.warning(
                    "TTS+timestamps attempt %d failed: %s — retrying in 3s", attempt + 1, exc
                )
                _time.sleep(3)

    logger.error(
        "Timestamps failed after 2 attempts (%s) — falling back to basic TTS. "
        "Word-synced captions will NOT be available for this render.",
        last_exc,
    )
    path = generate(script, output_path=output_path, voice_id=voice_id)
    return path, []
