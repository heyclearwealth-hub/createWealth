"""Unit tests for scripts/run_shorts_original.py."""
import scripts.run_shorts_original as rso


def test_normalize_tags_strips_hashes_and_dedupes():
    raw = ["#Shorts", "#IndexFunds", "PersonalFinance", "#IndexFunds", "", "   ", "#VeryLongTagNameExceedingThirtyCharacters123"]
    tags = rso._normalize_tags(raw)
    assert tags[:3] == ["Shorts", "IndexFunds", "PersonalFinance"]
    assert len(tags) == 4
    assert all(not tag.startswith("#") for tag in tags)
    assert all(len(tag) <= 30 for tag in tags)


def test_normalize_tags_handles_string_input():
    tags = rso._normalize_tags("#Shorts")
    assert tags == ["Shorts"]
