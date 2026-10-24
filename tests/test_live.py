import json
import threading
import time
from unittest import mock

import pytest

from audio_agent.live import LiveManager, LiveSessionError
from audio_agent.pipeline import _persist
from audio_agent.store import AudioStore


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Live condition was not reached")


class FakeCapture:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.started = False
        self.stop_count = 0
        self.stopped = False
        self.start_error = None
        self.on_start = None
        self.final_pcm = None
        self.running_on_start = True
        self.state = {"active": False, "running": False, "audio_seconds": 0,
                      "audio_level": 0.0, "last_audio_at": None, "last_error": None}

    def start(self):
        if self.start_error:
            raise self.start_error
        self.started = True
        self.state.update(active=True, running=self.running_on_start)
        if self.running_on_start:
            self.state.update(last_audio_at=time.time(), audio_level=0.2)
        if self.on_start:
            self.on_start(self)
        return self.status()

    def emit(self, pcm=None):
        pcm = b"\x00\x20" * 1600 if pcm is None else pcm
        clip = self.store.add_pcm(pcm, start_time=time.time() - 0.1, source=self.config.source)
        self.state["audio_seconds"] += clip["duration"]
        return clip

    def status(self):
        return dict(self.state)

    def stop(self):
        if self.started and not self.stopped:
            self.stop_count += 1
            self.stopped = True
            if self.final_pcm:
                self.emit(self.final_pcm)
        self.state.update(active=False, running=False, audio_level=0.0)
        return self.status()


def persist_transcript(store, clip_id, *, language):
    return _persist(store, {"clip_id": clip_id, "text": "本地识别", "language": language,
                            "source": store.get_clip(clip_id)["source"], "segments": []})


@pytest.fixture
def setup_live(tmp_path):
    managers = []
    def setup(*, transcriber=persist_transcript, configure=None, **kwargs):
        store = AudioStore(tmp_path / str(len(managers)))
        captures = []
        def factory(store, config):
            capture = FakeCapture(store, config)
            if configure:
                configure(capture)
            captures.append(capture)
            return capture
        manager = LiveManager(store, capture_factory=factory, transcriber=transcriber,
                              poll_interval=0.01, busy_retry_seconds=0.01, **kwargs)
        managers.append(manager)
        return manager, store, captures
    yield setup
    for manager in managers:
        manager.close()


def test_read_status_does_not_capture_and_start_requires_explicit_device(setup_live):
    manager, store, captures = setup_live()
    assert manager.status()["active"] is False and captures == []
    with pytest.raises(ValueError):
        manager.start("")
    with pytest.raises(ValueError):
        manager.start("default")
    assert captures == []
    manager.start("Mac builtin", language="zh")
    wait_for(lambda: manager.status()["state"] == "recording")
    with pytest.raises(LiveSessionError):
        manager.start("other")
    assert captures[0].config.device == "Mac builtin"
    assert manager.status()["audio_level"] == 0.2


def test_explicit_vad_mode_and_runtime_state_reach_live_status(setup_live):
    def configure(capture):
        capture.state.update(vad_enabled=True, vad_engine="silero-v4", vad_ready=True, vad_probability=.81)
    manager, store, captures = setup_live(configure=configure)
    manager.start("Test microphone", vad_enabled=True)
    wait_for(lambda: manager.status().get("vad_ready"))
    assert captures[0].config.vad_enabled is True
    assert manager.status()["vad_probability"] == .81
    assert manager.status()["vad_engine"] == "silero-v4"


def test_only_session_clips_transcribe_once_and_stop_drains_partial(setup_live):
    calls = []
    def transcribe(store, clip_id, *, language):
        calls.append(clip_id)
        return persist_transcript(store, clip_id, language=language)
    def configure(capture):
        capture.final_pcm = b"\x00\x20" * 800
    manager, store, captures = setup_live(transcriber=transcribe, configure=configure)
    old = store.add_pcm(b"\x00\x20" * 1600, start_time=time.time() - 1, source="old-session")
    manager.start("0")
    wait_for(lambda: captures[0].started)
    first = captures[0].emit()
    wait_for(lambda: manager.status()["transcribed_count"] == 1)
    time.sleep(0.03)
    assert calls == [first["id"]]
    manager.stop()
    wait_for(lambda: not manager.status()["active"])
    status = manager.status()
    assert status["state"] == "finished"
    assert status["clips_count"] == status["transcribed_count"] == 2
    assert status["audio_seconds"] == pytest.approx(0.15)
    assert status["pending_count"] == 0
    assert len(set(calls)) == 2 and old["id"] not in calls
    assert len(status["recent_transcripts"]) == 2
    assert captures[0].stop_count == 1
    assert len(list((store.root / "transcripts").glob("*.json"))) == 2


def test_silence_keeps_audio_but_does_not_run_model(setup_live):
    transcriber = mock.Mock(side_effect=AssertionError("silent audio must not run ASR"))
    manager, store, captures = setup_live(transcriber=transcriber)
    manager.start("0")
    wait_for(lambda: captures[0].started)
    silent = captures[0].emit(b"\x00\x00" * 1600)
    wait_for(lambda: manager.status()["silent_count"] == 1)
    manager.stop()
    wait_for(lambda: not manager.status()["active"])
    assert store.get_clip(silent["id"]) is not None
    assert manager.status()["transcribed_count"] == 0
    transcriber.assert_not_called()


def test_busy_model_retries_same_clip_and_succeeds(setup_live):
    calls = []
    def transcribe(store, clip_id, *, language):
        calls.append(clip_id)
        if len(calls) < 3:
            raise RuntimeError("已有本地转写任务运行，请等它完成后重试。")
        return persist_transcript(store, clip_id, language=language)
    manager, store, captures = setup_live(transcriber=transcribe, busy_timeout=0.5)
    manager.start("0")
    wait_for(lambda: captures[0].started)
    clip = captures[0].emit()
    wait_for(lambda: manager.status()["transcribed_count"] == 1)
    assert calls == [clip["id"]] * 3
    assert manager.status()["last_error"] is None


def test_existing_manual_result_prevents_duplicate_asr(setup_live):
    def configure(capture):
        def on_start(owner):
            clip = owner.emit()
            persist_transcript(owner.store, clip["id"], language="zh")
        capture.on_start = on_start
    transcriber = mock.Mock(side_effect=AssertionError("already transcribed"))
    manager, _, _ = setup_live(configure=configure, transcriber=transcriber)
    manager.start("0")
    wait_for(lambda: manager.status()["transcribed_count"] == 1)
    transcriber.assert_not_called()


def test_startup_timeout_stops_only_owned_capture(setup_live):
    def configure(capture):
        capture.running_on_start = False
    manager, _, captures = setup_live(configure=configure, startup_timeout=0.03)
    manager.start("0")
    wait_for(lambda: not manager.status()["active"])
    assert manager.status()["state"] == "error"
    assert "没有收到麦克风数据" in manager.status()["last_error"]
    assert captures[0].stop_count == 1


def test_capture_lock_failure_does_not_touch_external_recorder(setup_live):
    def configure(capture):
        capture.start_error = RuntimeError("Another recorder is already using this audio store")
    manager, store, captures = setup_live(configure=configure)
    external = {"pid": 12345, "running": True, "device": "CLI device"}
    (store.root / "capture.json").write_text(json.dumps(external))
    manager.start("0")
    wait_for(lambda: not manager.status()["active"])
    assert manager.status()["state"] == "error"
    assert captures[0].stop_count == 0
    assert json.loads((store.root / "capture.json").read_text()) == external


def test_asr_error_stops_capture_without_retry_storm(setup_live):
    transcriber = mock.Mock(side_effect=RuntimeError("Local model unavailable"))
    manager, store, captures = setup_live(transcriber=transcriber)
    manager.start("0")
    wait_for(lambda: captures[0].started)
    clip = captures[0].emit()
    wait_for(lambda: not manager.status()["active"])
    assert manager.status()["state"] == "error"
    assert manager.status()["failed_count"] == 1
    assert manager.status()["pending_count"] == 0
    assert captures[0].stop_count == 1
    assert store.get_clip(clip["id"]) is not None
    assert transcriber.call_count == 1


def test_busy_timeout_is_bounded_and_stops_capture(setup_live):
    transcriber = mock.Mock(side_effect=RuntimeError("transcription busy"))
    manager, _, captures = setup_live(transcriber=transcriber, busy_timeout=0.04)
    manager.start("0")
    wait_for(lambda: captures[0].started)
    captures[0].emit()
    wait_for(lambda: not manager.status()["active"])
    assert manager.status()["state"] == "error"
    assert "持续忙碌" in manager.status()["last_error"]
    assert captures[0].stop_count == 1
    assert 1 < transcriber.call_count < 20


def test_stop_remains_responsive_while_asr_runs_and_final_chunk_waits(setup_live):
    entered, release = threading.Event(), threading.Event()
    def transcribe(store, clip_id, *, language):
        entered.set()
        assert release.wait(3)
        return persist_transcript(store, clip_id, language=language)
    def configure(capture):
        capture.final_pcm = b"\x00\x20" * 800
    manager, _, captures = setup_live(transcriber=transcribe, configure=configure)
    manager.start("0")
    wait_for(lambda: captures[0].started)
    captures[0].emit()
    assert entered.wait(1)
    try:
        manager.stop()
        wait_for(lambda: captures[0].stopped)
        assert manager.status()["active"] is True
        assert manager.status()["state"] == "stopping"
        assert manager.status()["pending_count"] == 2
    finally:
        release.set()
    wait_for(lambda: not manager.status()["active"])
    assert manager.status()["transcribed_count"] == 2


def test_backlog_limit_stops_capture_then_drains_saved_clips(setup_live):
    entered, release = threading.Event(), threading.Event()
    def transcribe(store, clip_id, *, language):
        entered.set()
        assert release.wait(3)
        return persist_transcript(store, clip_id, language=language)
    manager, _, captures = setup_live(transcriber=transcribe, max_pending=1)
    manager.start("0")
    wait_for(lambda: captures[0].started)
    captures[0].emit()
    assert entered.wait(1)
    try:
        captures[0].emit()
        captures[0].emit()
        wait_for(lambda: captures[0].stopped)
        assert "跟不上" in manager.status()["last_error"]
        assert manager.status()["pending_count"] == 3
    finally:
        release.set()
    wait_for(lambda: not manager.status()["active"])
    assert manager.status()["state"] == "error"
    assert manager.status()["transcribed_count"] == 3
    assert manager.status()["pending_count"] == 0
