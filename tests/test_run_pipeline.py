"""Unit tests for scripts/run_pipeline.py."""
import importlib
import subprocess
import pytest
from unittest.mock import patch


def test_run_pipeline_passes_env_hook_threshold(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("MAX_STARTUP_DELAY_SECONDS", "0")
    monkeypatch.setenv("HOOK_SCORE_THRESHOLD", "0.91")

    import scripts.run_pipeline as rp
    rp = importlib.reload(rp)

    topic = {"keyword": "k", "pillar": "investing", "slug": "k"}
    script_data = {"script": "test script", "slug": "k"}

    with patch("pipeline.trends.pick_topic", return_value=topic):
        with patch("pipeline.scriptwriter.generate", return_value=script_data):
            with patch("pipeline.hook_gate.gate", return_value={"pass": False, "reason": "weak", "score": 0.1}) as mock_gate:
                with pytest.raises(SystemExit):
                    rp.main()

    assert mock_gate.call_count == 1
    assert mock_gate.call_args.kwargs["threshold"] == 0.91


def test_run_pipeline_falls_back_on_invalid_hook_threshold(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("MAX_STARTUP_DELAY_SECONDS", "0")
    monkeypatch.setenv("HOOK_SCORE_THRESHOLD", "not-a-number")

    import scripts.run_pipeline as rp
    rp = importlib.reload(rp)

    topic = {"keyword": "k", "pillar": "investing", "slug": "k"}
    script_data = {"script": "test script", "slug": "k"}

    with patch("pipeline.trends.pick_topic", return_value=topic):
        with patch("pipeline.scriptwriter.generate", return_value=script_data):
            with patch("pipeline.hook_gate.gate", return_value={"pass": False, "reason": "weak", "score": 0.1}) as mock_gate:
                with pytest.raises(SystemExit):
                    rp.main()

    assert mock_gate.call_count == 1
    assert mock_gate.call_args.kwargs["threshold"] == 0.75


def test_sync_data_branch_returns_false_on_git_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "topics_used.json").write_text('{"topics":[]}')

    import scripts.run_pipeline as rp
    rp = importlib.reload(rp)

    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(rp.subprocess, "run", fake_run)
    assert rp._sync_data_branch() is False
