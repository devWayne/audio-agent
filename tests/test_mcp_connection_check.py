"""Exercise the public read-only check against a real isolated stdio server."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from audio_agent import storage_config


def test_connection_check_is_bounded_and_does_not_record_or_claim_client_setup(tmp_path):
    project = Path(__file__).resolve().parents[1]
    library = tmp_path / "空资料库"
    result = subprocess.run([sys.executable, str(project / "scripts/check-mcp-connection"),
                             "--data-dir", str(library), "--timeout", "15"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report["status"] == "stdio_probe_passed"
    assert report["client_connection"] == report["skill_discovery"] == "not_checked"
    assert report["data_dir"] == str(library)
    assert report["clip_count"] == 0 and report["capture_active"] is False
    assert len(report["tools"]) == 6
    assert not (library / "capture.json").exists()
    assert not list(library.rglob("*.wav"))
    assert not list((library / "transcripts").glob("*.json"))


@pytest.mark.parametrize("explicit", [False, True])
def test_probe_forwards_library_environment_with_explicit_argument_precedence(tmp_path, monkeypatch, explicit):
    project = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    safe_fallback = tmp_path / "saved-library"
    storage_config.write_storage_config(safe_fallback)
    environment_library = tmp_path / "environment-library"
    monkeypatch.setenv("AUDIO_AGENT_DATA_DIR", str(environment_library))
    monkeypatch.setenv("AUDIO_AGENT_FFMPEG", "/usr/bin/false")
    expected = tmp_path / "explicit-library" if explicit else environment_library
    args = [sys.executable, str(project / "scripts/check-mcp-connection"), "--timeout", "15"]
    if explicit:
        args += ["--data-dir", str(expected)]
    result = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report["data_dir"] == str(expected)
    assert report["capture_active"] is False and report["clip_count"] == 0
    assert not safe_fallback.exists()
    assert not (expected / "capture.json").exists()
