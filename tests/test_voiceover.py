"""Unit tests for voiceover timestamp parsing."""

from pipeline.voiceover import _extract_word_start_times


def test_extract_word_start_times_handles_contractions():
    text = "Don't waste money"
    chars = list("Don't waste money")
    starts = [i * 0.05 for i in range(len(chars))]
    alignment = {
        "characters": chars,
        "character_start_times_seconds": starts,
    }

    word_times = _extract_word_start_times(alignment, text)
    assert len(word_times) == 3
    assert word_times[0] == 0.0
