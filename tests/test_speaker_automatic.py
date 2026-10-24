"""Automatic diarization policy/queue with fake inference and no microphone."""
import json
import stat
import threading
import time
from unittest import mock

import pytest

from audio_agent.live import LiveManager
from audio_agent.pipeline import asr_lock
from audio_agent.speaker_service import SpeakerManager, _atomic_json
from audio_agent.store import AudioStore
from test_live import FakeCapture, persist_transcript, wait_for
from test_speaker_service import clip, model_status, result, wait_job, assert_no_embeddings


@pytest.fixture
def managers(tmp_path):
    values = []
    def create(**kwargs):
        store = AudioStore(tmp_path / str(len(values)))
        manager = SpeakerManager(store, analyzer=kwargs.pop("analyzer", lambda *a, **k: result()),
                                 model_status=kwargs.pop("model_status", model_status),
                                 busy_retry_seconds=.005, **kwargs)
        values.append(manager)
        return store, manager
    yield create
    for manager in values:
        manager.close()


def test_settings_default_off_persist_privately_and_never_analyze_history(managers):
    analyzer = mock.Mock(side_effect=AssertionError("settings/GET must not analyze"))
    store, manager = managers(analyzer=analyzer)
    source = clip(store)
    assert manager.status()["automatic_enabled"] is False
    assert manager.submit_automatic(source["id"]) is None
    assert manager.update_settings(True)["automatic_enabled"] is True
    assert manager.items() == []
    assert stat.S_IMODE(manager.settings_path.stat().st_mode) == 0o600
    manager.close()
    restored = SpeakerManager(store, analyzer=analyzer, model_status=model_status)
    try:
        assert restored.status()["automatic_enabled"] is True
        assert restored.items() == []
        analyzer.assert_not_called()
    finally:
        restored.close()


@pytest.mark.parametrize("invalid", [None, 1, 0, "true", {}, []])
def test_invalid_settings_do_not_change_policy(managers, invalid):
    _, manager = managers()
    with pytest.raises(ValueError):
        manager.update_settings(invalid)
    assert manager.status()["automatic_enabled"] is False
    assert not manager.settings_path.exists()


def test_settings_write_failure_is_atomic_and_corrupt_load_stays_disabled(managers):
    store, manager = managers()
    manager.update_settings(True)
    original = manager.settings_path.read_bytes()
    with mock.patch("audio_agent.speaker_service.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            manager.update_settings(False)
    assert manager.status()["automatic_enabled"] is True
    assert manager.settings_path.read_bytes() == original
    manager.settings_path.write_text('{"automatic_enabled": 1}')
    other = SpeakerManager(store, model_status=model_status)
    try:
        assert other.status()["automatic_enabled"] is False
        assert other.status()["automatic_error"]
    finally:
        other.close()


def test_manual_and_automatic_jobs_queue_serially_and_automatic_deduplicates(managers):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def analyze(*args, **kwargs):
        calls.append(kwargs["num_speakers"])
        entered.set()
        assert release.wait(3)
        return result()
    store, manager = managers(analyzer=analyze)
    first, second = clip(store), clip(store)
    manager.update_settings(True)
    manual = manager.submit(first["id"], 2)
    try:
        assert entered.wait(3)
        assert manager.submit_automatic(first["id"])["id"] == manual["id"]
        automatic = manager.submit_automatic(second["id"])
        assert automatic["automatic"] is True and automatic["status"] == "queued"
        assert manager.submit_automatic(second["id"]) is None
        assert calls == [2]
        assert manager.status()["queued_count"] == 1
        assert manager.status()["running_count"] == 1
        manager.update_settings(False)
    finally:
        release.set()
    assert wait_job(manager, manual["id"])["status"] == "completed"
    assert wait_job(manager, automatic["id"])["status"] == "completed"
    assert calls == [2, None]  # Previously queued work finishes after disabling.
    manager.update_settings(True)
    assert manager.submit_automatic(second["id"]) is None
    assert_no_embeddings(manager.items())
    assert manager.voiceprint()["enrolled"] is False


def test_busy_lock_retries_without_immediate_failure_and_has_finite_limit(managers):
    store, manager = managers(busy_timeout=.04)
    first = clip(store)
    with asr_lock(store.root):
        job = manager.submit(first["id"])
        wait_for(lambda: manager.items()[0]["waiting_reason"] is not None)
        assert manager.items()[0]["status"] == "queued"
    assert wait_job(manager, job["id"])["status"] == "completed"
    second = clip(store)
    manager.update_settings(True)
    with asr_lock(store.root):
        blocked = manager.submit_automatic(second["id"])
        failed = wait_job(manager, blocked["id"])
    assert failed["status"] == "failed" and "忙碌" in failed["error"]
    assert manager.status()["automatic_error"] == failed["error"]


def test_queue_capacity_failure_is_visible_without_throwing_from_live_callback(managers):
    pending = threading.Event()
    pending.set()
    store, manager = managers(max_pending=1, asr_pending=pending.is_set)
    first, second = clip(store), clip(store)
    manager.update_settings(True)
    accepted = manager.submit_automatic(first["id"])
    assert manager.submit_automatic(second["id"]) is None
    assert "队列已满" in manager.status()["automatic_error"]
    assert len(manager.items()) == 1 and manager.items()[0]["status"] == "queued"
    with pytest.raises(RuntimeError, match="队列已满"):
        manager.submit(second["id"])
    pending.clear()
    assert wait_job(manager, accepted["id"])["status"] == "completed"


def test_close_during_model_acquisition_cannot_restart_a_cancelled_job(managers):
    consulted, release = threading.Event(), threading.Event()
    def pending():
        consulted.set()
        assert release.wait(3)
        return False
    analyzer = mock.Mock(return_value=result())
    store, manager = managers(analyzer=analyzer, asr_pending=pending)
    job = manager.submit(clip(store)["id"])
    assert consulted.wait(3)
    closer = threading.Thread(target=manager.close)
    closer.start()
    try:
        wait_for(lambda: manager._closed)
    finally:
        release.set()
        closer.join(3)
    assert not closer.is_alive()
    assert wait_job(manager, job["id"])["status"] == "failed"
    analyzer.assert_not_called()


def test_save_and_prune_are_atomic_so_no_orphan_embedding_survives(managers):
    store, manager = managers()
    source = clip(store)
    saving, continue_save, pruned = threading.Event(), threading.Event(), threading.Event()
    def delayed_write(path, value):
        saving.set()
        assert continue_save.wait(3)
        _atomic_json(path, value)
    def prune():
        store.prune(now=time.time() + 90000)
        pruned.set()
    with mock.patch("audio_agent.speaker_service._atomic_json", side_effect=delayed_write):
        job = manager.submit(source["id"])
        assert saving.wait(3)
        pruner = threading.Thread(target=prune)
        pruner.start()
        try:
            assert not pruned.wait(.05)
        finally:
            continue_save.set()
            pruner.join(3)
    assert pruned.is_set()
    assert wait_job(manager, job["id"])["status"] == "completed"
    assert not (manager.folder / f"{source['id']}.json").exists()
    assert store.get_clip(source["id"]) is None


def make_live(store, **kwargs):
    captures = []
    def factory(store, config):
        capture = FakeCapture(store, config)
        captures.append(capture)
        return capture
    manager = LiveManager(store, capture_factory=factory, transcriber=kwargs.pop("transcriber", persist_transcript),
                          poll_interval=.005, busy_retry_seconds=.005, **kwargs)
    manager.start("Fake microphone")
    wait_for(lambda: bool(captures) and captures[0].started)
    return manager, captures[0]


def test_live_hot_setting_only_processes_new_successful_asr_not_historical_results(managers):
    analyzer = mock.Mock(side_effect=lambda *a, **k: result())
    store, speakers = managers(analyzer=analyzer)
    live, capture = make_live(store, on_transcript=speakers.submit_automatic)
    try:
        old = capture.emit(b"\x00\x20" * (12 * 16000))
        wait_for(lambda: live.status()["transcribed_count"] == 1)
        speakers.update_settings(True)
        assert speakers.items() == []
        fresh = capture.emit(b"\x00\x20" * (12 * 16000))
        wait_for(lambda: len(speakers.items()) == 1)
        assert wait_job(speakers, speakers.items()[0]["id"])["status"] == "completed"
        assert speakers.items()[0]["clip_id"] == fresh["id"]
        assert speakers.analysis(old["id"]) is None
        speakers.update_settings(False)
        capture.emit(b"\x00\x20" * (12 * 16000))
        wait_for(lambda: live.status()["transcribed_count"] == 3)
        assert analyzer.call_count == 1
    finally:
        live.close()


def test_callback_failure_does_not_stop_capture_or_discard_successful_text(managers):
    store, _ = managers()
    callback = mock.Mock(side_effect=RuntimeError("fake speaker unavailable"))
    live, capture = make_live(store, on_transcript=callback)
    try:
        first = capture.emit()
        wait_for(lambda: live.status()["speaker_error"] is not None)
        assert live.status()["active"] and live.status()["last_error"] is None
        assert live.status()["latest_text"] == "本地识别"
        assert live.status()["failed_count"] == 0
        callback.assert_called_once_with(first["id"])
    finally:
        live.close()


def test_automatic_inference_failure_is_only_a_speaker_job_failure(managers):
    analyzer = mock.Mock(side_effect=RuntimeError("fake diarization failed"))
    store, speakers = managers(analyzer=analyzer)
    speakers.update_settings(True)
    live, capture = make_live(store, on_transcript=speakers.submit_automatic)
    try:
        capture.emit(b"\x00\x20" * (12 * 16000))
        wait_for(lambda: len(speakers.items()) == 1)
        failed = wait_job(speakers, speakers.items()[0]["id"])
        assert failed["status"] == "failed"
        assert speakers.status()["automatic_error"] == "fake diarization failed"
        assert live.status()["active"] and live.status()["last_error"] is None
        assert live.status()["transcribed_count"] == 1
        assert live.status()["latest_text"] == "本地识别"
        assert live.status()["failed_count"] == 0
    finally:
        live.close()


def test_known_speaker_lock_wait_and_backlog_do_not_fail_live_and_stop_drains_disk(managers):
    entered, release = threading.Event(), threading.Event()
    def analyze(*a, **k):
        entered.set()
        assert release.wait(3)
        return result()
    store, speakers = managers(analyzer=analyze)
    def transcribe(store, clip_id, *, language):
        with asr_lock(store.root):
            return persist_transcript(store, clip_id, language=language)
    live, capture = make_live(store, transcriber=transcribe, speaker_model_busy=speakers.model_busy,
                              busy_timeout=.03, max_pending=2)
    try:
        speakers.submit(clip(store)["id"])
        assert entered.wait(3)
        for _ in range(6):
            capture.emit()
        wait_for(lambda: live.status()["pending_count"] == 6)
        assert live.status()["deferred_count"] > 0
        assert live.status()["waiting_for_speaker"] is True
        assert live.asr_pending() is True
        time.sleep(.06)  # Greater than the ordinary external-model busy limit.
        assert live.status()["active"] and live.status()["last_error"] is None
        assert len(live._queue) <= 2
        live.stop()
        wait_for(lambda: live._capture_done)
        release.set()
        wait_for(lambda: not live.status()["active"])
        assert live.status()["state"] == "finished"
        assert live.status()["transcribed_count"] == 6
        assert live.status()["pending_count"] == 0
    finally:
        release.set()
        live.close()


def test_existing_manual_transcript_during_busy_wait_does_not_trigger_auto_callback(managers):
    store, _ = managers()
    callback = mock.Mock()
    def transcribe(store, clip_id, *, language):
        persist_transcript(store, clip_id, language=language)
        raise RuntimeError("transcription busy")
    live, capture = make_live(store, transcriber=transcribe, on_transcript=callback)
    try:
        capture.emit()
        wait_for(lambda: live.status()["transcribed_count"] == 1)
        callback.assert_not_called()
    finally:
        live.close()
