"""Threshold persistence and live application with synthetic PCM, no hardware."""
from email.message import Message
import io
import json
import math
import os
import stat
import struct
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from audio_agent import capture, web
from audio_agent.capture import AudioCapture, CaptureConfig
from audio_agent.live import LiveManager
from audio_agent.pipeline import _persist
from audio_agent.store import AudioStore


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Settings condition was not reached")


def pcm(seconds, amplitude):
    return struct.pack("<h", amplitude) * round(seconds * 16000)


class PipeProcess:
    def __init__(self):
        reader, self.writer = os.pipe()
        self.stdout = os.fdopen(reader, "rb", buffering=0)
        self.stderr = io.BytesIO()
        self.returncode = None

    def write(self, data):
        pending = memoryview(data)
        while pending:
            pending = pending[os.write(self.writer, pending):]

    def poll(self):
        return self.returncode

    def terminate(self):
        if self.returncode is None:
            self.returncode = 0
            os.close(self.writer)

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def store(tmp_path):
    return AudioStore(tmp_path / "audio")


def test_settings_default_save_restart_and_private_atomic_file(store):
    factory = mock.Mock(side_effect=AssertionError("settings cannot start microphone"))
    manager = LiveManager(store, capture_factory=factory)
    assert manager.settings()["settings"]["trigger_threshold"] == 0.006
    assert manager.settings()["settings"]["trigger_db"] == pytest.approx(20 * math.log10(0.006))
    result = manager.update_settings(-32)
    assert result["pending"] is False
    assert result["settings"]["trigger_threshold"] == pytest.approx(10 ** (-32 / 20))
    assert result["settings"]["release_threshold"] == pytest.approx(10 ** (-32 / 20) * 2 / 3)
    path = store.root / "live-settings.json"
    assert json.loads(path.read_text()) == {"trigger_db": -32.0}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(store.root.glob(".live-settings-*.json")) == []
    reloaded = LiveManager(store, capture_factory=factory)
    assert reloaded.settings() == result
    assert reloaded.status()["trigger_db"] == pytest.approx(-32)
    assert reloaded.status()["configured_trigger_db"] == -32
    factory.assert_not_called()


@pytest.mark.parametrize("value", [True, False, None, "-40", -60.01, -17.99, float("nan"), float("inf"), float("-inf")])
def test_invalid_setting_does_not_mutate_or_persist(store, value):
    manager = LiveManager(store)
    before = manager.settings()
    with pytest.raises(ValueError):
        manager.update_settings(value)
    assert manager.settings() == before
    assert not (store.root / "live-settings.json").exists()


def test_atomic_write_failure_leaves_memory_and_saved_setting_unchanged(store, monkeypatch):
    manager = LiveManager(store)
    manager.update_settings(-50)
    before = manager.settings()
    original = (store.root / "live-settings.json").read_bytes()
    monkeypatch.setattr("audio_agent.live.os.replace", mock.Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        manager.update_settings(-32)
    assert manager.settings() == before
    assert (store.root / "live-settings.json").read_bytes() == original
    assert list(store.root.glob(".live-settings-*.json")) == []


@pytest.mark.parametrize("bad", ["broken json", '{"trigger_db":NaN}', '{"trigger_db":true}', '{"trigger_db":-20,"unknown":1}'])
def test_malformed_saved_settings_fall_back_without_starting_capture(store, bad):
    (store.root / "live-settings.json").write_text(bad)
    manager = LiveManager(store)
    assert manager.status()["trigger_threshold"] == 0.006
    assert manager.status()["settings_error"]
    assert manager.status()["active"] is False
    manager.update_settings(-44.4)
    assert manager.status()["settings_error"] is None


def test_capture_hot_threshold_waits_until_end_and_preserves_whole_utterance(store, monkeypatch):
    process = PipeProcess()
    monkeypatch.setattr(capture.sys, "platform", "darwin")
    monkeypatch.setattr(capture.subprocess, "Popen", lambda *args, **kwargs: process)
    recorder = AudioCapture(store, CaptureConfig(device="Synthetic", activity_triggered=True))
    recorder.start()
    try:
        process.write(pcm(0.4, 1000))
        wait_for(lambda: recorder.status()["activity_state"] == "recording")
        recorder.update_activity_threshold(0.1, 0.1 * 2 / 3)
        assert recorder.status()["settings_pending"] is True
        assert recorder.status()["sound_threshold"] == 0.006
        process.write(pcm(0.9, 300))  # Above old release, below new release.
        wait_for(lambda: recorder.status()["observed_seconds"] >= 1.3)
        assert recorder.status()["activity_state"] == "recording"
        assert store.status()["clip_count"] == 0
        process.write(pcm(0.8, 0))
        wait_for(lambda: not recorder.status()["settings_pending"])
        assert recorder.status()["sound_threshold"] == 0.1
        assert store.status()["clip_count"] == 1
        assert store.status()["duration"] == pytest.approx(1.5)
        process.write(pcm(0.4, 300) + pcm(0.8, 0))
        wait_for(lambda: recorder.status()["observed_seconds"] >= 3.3)
        assert store.status()["clip_count"] == 1
    finally:
        recorder.stop()


def test_peak_feedback_is_20ms_rms_not_single_sample_peak(store, monkeypatch):
    process = PipeProcess()
    monkeypatch.setattr(capture.sys, "platform", "darwin")
    monkeypatch.setattr(capture.subprocess, "Popen", lambda *args, **kwargs: process)
    recorder = AudioCapture(store, CaptureConfig(device="Synthetic", activity_triggered=True))
    recorder.start()
    try:
        process.write(struct.pack("<h", 32767) + b"\x00\x00" * 319)
        wait_for(lambda: recorder.status()["observed_seconds"] >= 0.02)
        assert recorder.status()["audio_peak_level"] == pytest.approx((32767 / 32768) / math.sqrt(320))
        assert recorder.status()["audio_peak_level"] < 0.1
    finally:
        recorder.stop()
    assert recorder.status()["audio_peak_level"] == 0
    assert store.status()["clip_count"] == 0


def test_lowered_threshold_event_is_not_rejected_by_old_postfilter(store, monkeypatch):
    process = PipeProcess()
    monkeypatch.setattr(capture.sys, "platform", "darwin")
    monkeypatch.setattr(capture.subprocess, "Popen", lambda *args, **kwargs: process)
    calls = []
    transcription_started, finish_transcription = threading.Event(), threading.Event()
    def transcribe(store, clip_id, *, language):
        calls.append(clip_id)
        transcription_started.set()
        assert finish_transcription.wait(3), "test did not release the fake transcription"
        return _persist(store, {"clip_id": clip_id, "text": "quiet accepted event", "language": language})
    manager = LiveManager(store, transcriber=transcribe, poll_interval=0.01)
    manager.update_settings(-60)  # .001 trigger; signal .00198 < old .003 filter.
    manager.start("Synthetic")
    try:
        wait_for(lambda: manager._capture.status()["active"])
        process.write(pcm(0.6, 65) + pcm(0.8, 0))
        assert transcription_started.wait(3)
        # An invoked transcriber is not a completed/persisted result. Keep this
        # distinction deterministic even when filesystem commits are slower.
        assert manager.status()["transcribed_count"] == 0
        finish_transcription.set()
        wait_for(lambda: manager.status()["transcribed_count"] == 1)
        assert len(calls) == 1
        assert manager.status()["transcribed_count"] == 1
        assert manager.status()["silent_count"] == 0
        assert store.status()["clip_count"] == 1
    finally:
        finish_transcription.set()
        manager.close()
    assert manager.status()["state"] == "finished"


def test_unsupported_capture_adapter_marks_settings_pending_until_next_start(store):
    class Adapter:
        def __init__(self, store, config):
            self.config = config
            self.value = {"active": False, "running": False, "audio_seconds": 0}
        def start(self):
            self.value.update(active=True, running=True)
        def status(self):
            return dict(self.value)
        def stop(self):
            self.value.update(active=False, running=False)
    manager = LiveManager(store, capture_factory=Adapter, poll_interval=0.01)
    manager.start("Fake")
    try:
        wait_for(lambda: manager.status()["state"] == "recording")
        result = manager.update_settings(-50)
        assert result["pending"] is True
        assert manager.status()["trigger_threshold"] == 0.006
        assert manager.status()["configured_trigger_db"] == -50
    finally:
        manager.close()
    assert manager.settings()["pending"] is False
    restarted = LiveManager(store, capture_factory=Adapter)
    assert restarted.settings()["settings"]["trigger_db"] == -50


def http_request(manager, body=None, token=True, method="POST"):
    handler = web.AudioRequestHandler.__new__(web.AudioRequestHandler)
    handler.server = SimpleNamespace(live=manager, store=manager.store, token="session", server_port=8767,
                                     _request_lock=threading.RLock())
    handler.command, handler.path = method, "/api/live/settings"
    handler.client_address = ("127.0.0.1", 50000)
    handler.close_connection = False
    handler.headers = Message()
    handler.headers["Host"] = "127.0.0.1:8767"
    if token:
        handler.headers["X-Audio-Agent-Token"] = "session"
    payload = json.dumps(body).encode() if method == "POST" else b""
    handler.headers["Content-Type"] = "application/json"
    handler.headers["Content-Length"] = str(len(payload))
    handler.rfile, handler.wfile = io.BytesIO(payload), io.BytesIO()
    responses = []
    handler._json = lambda status, result, **kwargs: responses.append((status, result))
    handler._handle()
    return responses[0]


@pytest.mark.parametrize("body", [{}, {"trigger_db": -44, "device": "other"}, {"trigger_db": True}, {"trigger_db": float("nan")}, {"trigger_db": -70}])
def test_http_settings_validation_and_protection(store, body):
    manager = LiveManager(store)
    assert http_request(manager, {"trigger_db": -50}, token=False)[0] == 403
    assert http_request(manager, body)[0] == 400
    assert not (store.root / "live-settings.json").exists()


def test_http_settings_read_write_schema_without_microphone(store):
    factory = mock.Mock(side_effect=AssertionError("no microphone"))
    manager = LiveManager(store, capture_factory=factory)
    status, body = http_request(manager, method="GET", token=False)
    assert status == 200 and body["pending"] is False
    assert body["settings"]["trigger_threshold"] == 0.006
    status, body = http_request(manager, {"trigger_db": -32})
    assert status == 200 and body["settings"]["trigger_db"] == -32
    assert body["pending"] is False
    factory.assert_not_called()
