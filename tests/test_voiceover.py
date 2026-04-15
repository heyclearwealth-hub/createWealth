"""Unit tests for voiceover cleaning and timestamp parsing."""

from pipeline.voiceover import _clean_script, _extract_word_start_times


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


def test_clean_script_keeps_pause_and_removes_stat_marker():
    script = "Save first [PAUSE] then invest [STAT: source note]."
    cleaned = _clean_script(script)
    assert "[STAT:" not in cleaned
    assert "..." in cleaned
