"""Unit tests for issue_manager.py"""
import pytest
from unittest.mock import patch, MagicMock
import pipeline.issue_manager as im

VALID_BODY = """
Some issue text.

<!-- artifact-run-id: 12345678 -->

More text.
"""

TAMPERED_BODY = """
<!-- artifact-run-id: ../../../etc/passwd -->
"""

MISSING_MARKER_BODY = """
No marker here at all.
"""

SHORT_ID_BODY = """
<!-- artifact-run-id: 123 -->
"""


# ── extract_run_id tests ───────────────────────────────────────────────────────

def test_extract_run_id_valid():
    assert im.extract_run_id(VALID_BODY) == "12345678"


def test_extract_run_id_missing_raises():
    with pytest.raises(ValueError, match="No valid artifact-run-id"):
        im.extract_run_id(MISSING_MARKER_BODY)


def test_extract_run_id_tampered_raises():
    with pytest.raises(ValueError, match="No valid artifact-run-id"):
        im.extract_run_id(TAMPERED_BODY)


def test_extract_run_id_too_short_raises():
    with pytest.raises(ValueError, match="No valid artifact-run-id"):
        im.extract_run_id(SHORT_ID_BODY)


# ── parse_comment tests ────────────────────────────────────────────────────────

def test_parse_approve():
    assert im.parse_comment("APPROVE") == {"action": "approve"}


def test_parse_approve_with_whitespace():
    assert im.parse_comment("  APPROVE\n") == {"action": "approve"}


def test_parse_reject():
    result = im.parse_comment("REJECT: hook is weak")
    assert result["action"] == "reject"
    assert result["reason"] == "hook is weak"


def test_parse_ignore():
    assert im.parse_comment("looks good") == {"action": "ignore"}


def test_parse_lowercase_approve_ignored():
    # Must be exact uppercase APPROVE
    assert im.parse_comment("approve") == {"action": "ignore"}


# ── validate_run_provenance tests ──────────────────────────────────────────────

def _mock_run_response(
    name="research-and-render",
    branch="main",
    conclusion="success",
    path=".github/workflows/research-and-render.yml",
    repo="heyclearwealth-hub/youtube-autopilot",
    event="schedule",
):
    return {
        "name": name,
        "head_branch": branch,
        "conclusion": conclusion,
        "path": path,
        "repository": {"full_name": repo},
        "event": event,
    }


def _mock_artifacts_response(run_id):
    return {"artifacts": [{"name": f"pipeline-{run_id}"}]}


def test_provenance_valid():
    run_id = "12345678"
    with patch("pipeline.issue_manager.REPO", "heyclearwealth-hub/youtube-autopilot"):
        with patch("requests.get") as mock_get:
            mock_run = MagicMock()
            mock_run.status_code = 200
            mock_run.json.return_value = _mock_run_response()
            mock_artifacts = MagicMock()
            mock_artifacts.status_code = 200
            mock_artifacts.json.return_value = _mock_artifacts_response(run_id)
            mock_get.side_effect = [mock_run, mock_artifacts]

            assert im.validate_run_provenance(run_id) is True


def test_provenance_wrong_workflow():
    run_id = "12345678"
    with patch("pipeline.issue_manager.REPO", "heyclearwealth-hub/youtube-autopilot"):
        with patch("requests.get") as mock_get:
            mock_run = MagicMock()
            mock_run.status_code = 200
            mock_run.json.return_value = _mock_run_response(name="some-other-workflow")
            mock_get.return_value = mock_run

            assert im.validate_run_provenance(run_id) is False


def test_provenance_wrong_branch():
    run_id = "12345678"
    with patch("pipeline.issue_manager.REPO", "heyclearwealth-hub/youtube-autopilot"):
        with patch("requests.get") as mock_get:
            mock_run = MagicMock()
            mock_run.status_code = 200
            mock_run.json.return_value = _mock_run_response(branch="feature/test")
            mock_get.return_value = mock_run

            assert im.validate_run_provenance(run_id) is False


def test_provenance_wrong_event():
    run_id = "12345678"
    with patch("pipeline.issue_manager.REPO", "heyclearwealth-hub/youtube-autopilot"):
        with patch("requests.get") as mock_get:
            mock_run = MagicMock()
            mock_run.status_code = 200
            mock_run.json.return_value = _mock_run_response(event="push")
            mock_get.return_value = mock_run

            assert im.validate_run_provenance(run_id) is False


def test_provenance_run_not_found():
    run_id = "99999999"
    with patch("pipeline.issue_manager.REPO", "heyclearwealth-hub/youtube-autopilot"):
        with patch("requests.get") as mock_get:
            mock_run = MagicMock()
            mock_run.status_code = 404
            mock_get.return_value = mock_run

            assert im.validate_run_provenance(run_id) is False


def test_provenance_invalid_run_id_format():
    assert im.validate_run_provenance("123") is False  # too short
    assert im.validate_run_provenance("abc123") is False  # non-digit
