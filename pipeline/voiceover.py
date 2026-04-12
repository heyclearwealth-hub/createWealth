"""
voiceover.py — ElevenLabs streaming TTS using eleven_turbo_v2_5.
"""
import logging
import os
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
DEFAULT_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")  # "Sarah" — professional female
OUTPUT_PATH = Path("workspace/voiceover.mp3")

TTS_SETTINGS = {
    "model_id": "eleven_turbo_v2_5",
    "voice_settings": {
        "stability": 0.50,
        "similarity_boost": 0.75,
        "style": 0.20,
        "use_speaker_boost": True,
    },
    "output_format": "mp3_44100_128",
}


def _clean_script(script: str) -> str:
    """Strip stage directions like [PAUSE], [STAT: ...] from spoken text."""
    import re
    text = re.sub(r"\[PAUSE\]", " ", script)
    text = re.sub(r"\[STAT:[^\]]*\]", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


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
