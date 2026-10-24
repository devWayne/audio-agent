from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock

import pytest

from audio_agent.capture import AudioCapture, CaptureConfig, list_audio_devices, read_capture_status
from audio_agent.store import AudioStore


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def test_requires_explicit_device_and_supported_format():
    for device in ("", "default", "none"):
        with pytest.raises(ValueError, match="device"):
            CaptureConfig(device=device)
    with pytest.raises(ValueError, match="16000"):
        CaptureConfig(device="0", sample_rate=48000)
    with pytest.raises(ValueError, match="chunk_seconds"):
        CaptureConfig(device="0", chunk_seconds=-5)


def test_device_listing_only_returns_audio_devices():
    diagnostic = """[AVFoundation indev @ abc] AVFoundation video devices:
[AVFoundation indev @ abc] [0] FaceTime HD Camera
[AVFoundation indev @ abc] [1] Capture screen 0
[AVFoundation indev @ abc] AVFoundation audio devices:
[AVFoundation indev @ abc] [0] MacBook Pro Microphone
[AVFoundation indev @ abc] [1] DJI MIC RX
"""
    with mock.patch("audio_agent.capture.sys.platform", "darwin"), \
            mock.patch("audio_agent.capture.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", diagnostic)):
        assert list_audio_devices() == [
            {"index": 0, "name": "MacBook Pro Microphone"},
            {"index": 1, "name": "DJI MIC RX"}]


def spawn_fake_process(script):
    # Use a real pipe/subprocess for the capture loop without accessing audio
    # hardware. Keep a reference before patching the shared subprocess module.
    real_popen = subprocess.Popen
    return lambda *args, **kwargs: real_popen(
        [sys.executable, "-u", "-c", script], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)


def test_stop_flushes_partial_audio_and_prevents_second_recorder(tmp_path):
    store = AudioStore(tmp_path / "audio")
    recorder = AudioCapture(store, CaptureConfig(device="Built-in Microphone", chunk_seconds=5))
    fake = spawn_fake_process("import os,time; os.write(1,b'\\x01\\x00'*8000); time.sleep(30)")
    with mock.patch("audio_agent.capture.sys.platform", "darwin"), \
            mock.patch("audio_agent.capture.subprocess.Popen", side_effect=fake) as popen:
        recorder.start()
        try:
            wait_for(lambda: recorder.status()["running"])
            assert store.status()["clip_count"] == 0
            second = AudioCapture(AudioStore(store.root), CaptureConfig(device="0"))
            with pytest.raises(RuntimeError, match="Another recorder"):
                second.start()
            # No fallback or second ffmpeg is launched.
            assert popen.call_count == 1
            state = recorder.stop()
        finally:
            recorder.stop()
    assert state["state"] == "stopped"
    assert state["running"] is False and state["active"] is False
    assert state["clips_written"] == 1
    assert store.status()["duration"] == 0.5
    assert read_capture_status(store.root)["state"] == "stopped"
    assert ":Built-in Microphone" in popen.call_args.args[0]


def test_spawn_without_pcm_never_claims_recording_and_reports_error(tmp_path):
    recorder = AudioCapture(AudioStore(tmp_path / "audio"), CaptureConfig(device="missing-mic"))
    fake = spawn_fake_process("import sys; sys.stderr.write('device unavailable'); sys.exit(1)")
    with mock.patch("audio_agent.capture.sys.platform", "darwin"), \
            mock.patch("audio_agent.capture.subprocess.Popen", side_effect=fake):
        initial = recorder.start()
        assert not initial["running"]
        wait_for(lambda: not recorder.status()["active"])
    state = recorder.status()
    assert state["state"] == "error"
    assert "device unavailable" in state["last_error"]
    assert recorder.store.status()["clip_count"] == 0


def test_unexpected_eof_still_saves_available_audio(tmp_path):
    recorder = AudioCapture(AudioStore(tmp_path / "audio"), CaptureConfig(device="0", chunk_seconds=0.1))
    fake = spawn_fake_process("import os; os.write(1,b'\\x03\\x00'*4000)")
    with mock.patch("audio_agent.capture.sys.platform", "darwin"), \
            mock.patch("audio_agent.capture.subprocess.Popen", side_effect=fake):
        state = recorder.run()
    assert state["state"] == "error"
    assert state["clips_written"] == 3
    assert state["audio_seconds"] == pytest.approx(0.25)
    assert recorder.store.status()["duration"] == pytest.approx(0.25)


def test_stale_process_status_is_not_reported_as_listening(tmp_path):
    (tmp_path / "capture.json").write_text(json.dumps({"pid": 99999999, "active": True, "running": True}))
    state = read_capture_status(tmp_path)
    assert state["running"] is False
    assert state["state"] == "stopped"


def test_startup_error_releases_singleton_lock(tmp_path):
    store = AudioStore(tmp_path / "audio")
    recorder = AudioCapture(store, CaptureConfig(device="0"))
    with mock.patch("audio_agent.capture.sys.platform", "darwin"), \
            mock.patch("audio_agent.capture.subprocess.Popen", side_effect=FileNotFoundError("missing ffmpeg")):
        with pytest.raises(FileNotFoundError):
            recorder.start()
    assert recorder.status()["state"] == "error"
    second = AudioCapture(store, CaptureConfig(device="0"))
    fake = spawn_fake_process("import sys; sys.exit(1)")
    with mock.patch("audio_agent.capture.sys.platform", "darwin"), \
            mock.patch("audio_agent.capture.subprocess.Popen", side_effect=fake):
        state = second.run()
    assert "already" not in (state["last_error"] or "")


def test_disk_guard_does_not_start_microphone(tmp_path):
    store = AudioStore(tmp_path / "audio")
    recorder = AudioCapture(store, CaptureConfig(device="0"))
    with mock.patch("audio_agent.capture.sys.platform", "darwin"), \
            mock.patch.object(store, "ensure_space", side_effect=RuntimeError("Insufficient disk space")), \
            mock.patch("audio_agent.capture.subprocess.Popen") as popen:
        with pytest.raises(RuntimeError, match="Insufficient disk space"):
            recorder.start()
        popen.assert_not_called()
    assert recorder.status()["active"] is False


def test_stop_requested_during_startup_is_preserved(tmp_path):
    recorder = AudioCapture(AudioStore(tmp_path / "audio"), CaptureConfig(device="0"))
    real_fake = spawn_fake_process("import time; time.sleep(30)")
    def stop_before_returning_process(*args, **kwargs):
        process = real_fake(*args, **kwargs)
        recorder.stop()
        return process
    with mock.patch("audio_agent.capture.sys.platform", "darwin"), \
            mock.patch("audio_agent.capture.subprocess.Popen", side_effect=stop_before_returning_process):
        recorder.start()
        state = recorder.wait()
    assert state["state"] == "stopped"
    assert state["audio_seconds"] == 0


def test_capture_level_comes_from_pcm_and_clears_when_stopped(tmp_path):
    recorder = AudioCapture(AudioStore(tmp_path / "audio"), CaptureConfig(device="0"))
    fake = spawn_fake_process("import os,time; os.write(1,b'\\x00\\x20'*8000); time.sleep(30)")
    with mock.patch("audio_agent.capture.sys.platform", "darwin"), \
            mock.patch("audio_agent.capture.subprocess.Popen", side_effect=fake):
        recorder.start()
        try:
            wait_for(lambda: recorder.status()["audio_level"] > 0)
            assert recorder.status()["audio_level"] == pytest.approx(0.25)
            assert recorder.status()["last_audio_at"] is not None
        finally:
            recorder.stop()
    assert recorder.status()["audio_level"] == 0


def test_activity_capture_silence_stays_listening_without_wav_files(tmp_path):
    recorder = AudioCapture(AudioStore(tmp_path / "audio"), CaptureConfig(device="0", activity_triggered=True))
    fake = spawn_fake_process("import sys,time; sys.stdout.buffer.write(b'\\x00\\x00'*16000*12); sys.stdout.buffer.flush(); time.sleep(30)")
    with mock.patch("audio_agent.capture.sys.platform", "darwin"), \
            mock.patch("audio_agent.capture.subprocess.Popen", side_effect=fake):
        recorder.start()
        try:
            wait_for(lambda: recorder.status()["observed_seconds"] >= 12)
            assert recorder.status()["running"] is True
            assert recorder.status()["mode"] == "activity"
            assert recorder.status()["activity_state"] == "listening"
            assert recorder.status()["chunk_seconds"] is None
            assert recorder.store.status()["clip_count"] == 0
        finally:
            recorder.stop()
    assert recorder.store.status()["clip_count"] == 0
    assert list((recorder.store.root / "clips").iterdir()) == []


def test_activity_capture_flushes_one_active_event_on_stop(tmp_path):
    recorder = AudioCapture(AudioStore(tmp_path / "audio"), CaptureConfig(device="0", activity_triggered=True))
    fake = spawn_fake_process("import sys,time; sys.stdout.buffer.write(b'\\x00\\x00'*16000+b'\\x00\\x20'*8000); sys.stdout.buffer.flush(); time.sleep(30)")
    with mock.patch("audio_agent.capture.sys.platform", "darwin"), \
            mock.patch("audio_agent.capture.subprocess.Popen", side_effect=fake):
        recorder.start()
        try:
            wait_for(lambda: recorder.status()["observed_seconds"] >= 1.5)
            wait_for(lambda: recorder.status()["activity_state"] == "recording")
            assert recorder.store.status()["clip_count"] == 0
        finally:
            recorder.stop()
    assert recorder.store.status()["clip_count"] == 1
    assert recorder.store.status()["duration"] == pytest.approx(0.8)
    assert recorder.status()["audio_seconds"] == pytest.approx(0.8)
    assert recorder.status()["observed_seconds"] == pytest.approx(1.5)


def test_activity_capture_guard_saves_event_and_reports_stop(tmp_path):
    recorder = AudioCapture(AudioStore(tmp_path / "audio"), CaptureConfig(
        device="0", activity_triggered=True, max_active_seconds=1))
    fake = spawn_fake_process("import sys,time; sys.stdout.buffer.write(b'\\x00\\x20'*32000); sys.stdout.buffer.flush(); time.sleep(30)")
    with mock.patch("audio_agent.capture.sys.platform", "darwin"), \
            mock.patch("audio_agent.capture.subprocess.Popen", side_effect=fake):
        recorder.start()
        state = recorder.wait()
    assert state["state"] == "error"
    assert state["active"] is False
    assert "内存保护" in state["last_error"]
    assert recorder.store.status()["clip_count"] == 1
    assert recorder.store.status()["duration"] == pytest.approx(1)
