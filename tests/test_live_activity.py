"""Activity capture -> store -> live ASR, with a real pipe and synthetic PCM.

No audio hardware, HTTP socket, real model or wall-clock-length recording is
used. PCM representing minutes is fed as fast as the capture loop can read it.
"""
from pathlib import Path
import subprocess
import sys
import time
import wave

import pytest

from audio_agent import capture
from audio_agent.live import LiveManager
from audio_agent.pipeline import _persist
from audio_agent.store import AudioStore


def wait_for(predicate, *, timeout=5, description="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(f"Activity integration did not reach {description}")


@pytest.fixture
def activity_session(tmp_path, monkeypatch):
    managers = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(capture.sys, "platform", "darwin")

    def setup(patterns):
        # Capture reads real stdout bytes; only the ffmpeg producer is replaced.
        script = "\n".join([
            "import os, struct, time",
            f"patterns = {patterns!r}",
            "for seconds, value in patterns:",
            "    remaining = memoryview(struct.pack('<h', value) * round(seconds * 16000))",
            "    while remaining:",
            "        remaining = remaining[os.write(1, remaining):]",
            "time.sleep(60)",
        ])

        def fake_popen(*args, **kwargs):
            return real_popen([sys.executable, "-u", "-c", script],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, bufsize=0)

        monkeypatch.setattr(capture.subprocess, "Popen", fake_popen)
        store = AudioStore(tmp_path / "data")
        recorders, asr_calls = [], []

        def capture_factory(store, config):
            assert config.activity_triggered is True, "Web live must opt into pre-storage activity gating"
            recorder = capture.AudioCapture(store, config)
            recorders.append(recorder)
            return recorder

        def fake_transcribe(store, clip_id, *, language):
            clip = store.get_clip(clip_id)
            with wave.open(clip["path"], "rb") as wav:
                audio = wav.readframes(wav.getnframes())
            asr_calls.append({"clip_id": clip_id, "duration": clip["duration"], "audio": audio})
            return _persist(store, {"clip_id": clip_id, "source": clip["source"],
                                    "text": "合成音频的测试结果", "language": language,
                                    "backend": "fake-local-asr", "segments": []})

        manager = LiveManager(store, capture_factory=capture_factory, transcriber=fake_transcribe,
                              poll_interval=0.01, busy_retry_seconds=0.01)
        managers.append(manager)
        manager.start("Synthetic PCM microphone", language="zh")
        total = sum(seconds for seconds, _ in patterns)
        wait_for(lambda: recorders and recorders[0].status().get("observed_seconds", 0) >= total - 1 / 16000,
                 description=f"all {total:g} seconds of synthetic PCM consumed")
        return manager, store, asr_calls

    yield setup
    for manager in managers:
        manager.close()


def stop_and_drain(manager):
    manager.stop()
    wait_for(lambda: not manager.status()["active"], description="owned capture and ASR stopped")
    assert manager.status()["state"] == "finished", manager.status()
    assert manager.status()["pending_count"] == 0


def test_two_minutes_quiet_create_no_clips_and_never_run_asr(activity_session):
    manager, store, calls = activity_session([(120.0, 0)])
    assert manager.status()["active"] is True
    assert manager.status()["mode"] == "activity"
    assert store.status()["clip_count"] == 0
    assert calls == []
    stop_and_drain(manager)
    assert store.status()["clip_count"] == 0
    assert store.status()["duration"] == 0
    assert list((store.root / "clips").glob("*.wav")) == []
    assert list((store.root / "incoming").iterdir()) == []
    assert calls == []
    assert manager.status()["observed_seconds"] == pytest.approx(120)


def test_sound_then_quiet_commits_one_variable_length_clip(activity_session):
    manager, store, calls = activity_session([(0.5, 0), (1.5, 4096), (1.0, 0)])
    wait_for(lambda: len(calls) == 1, description="single activity transcription")
    clips = store.list_clips()
    assert len(clips) == 1
    assert 1.5 <= clips[0]["duration"] <= 2.3
    assert calls[0]["clip_id"] == clips[0]["id"]
    stop_and_drain(manager)
    assert store.status()["clip_count"] == 1  # quiet stop adds no trailing file
    assert len(calls) == 1


def test_separate_sounds_create_two_clips_and_one_asr_per_clip(activity_session):
    manager, store, calls = activity_session([
        (0.5, 0), (1.2, 4096), (1.2, 0), (2.2, 8192), (1.0, 0),
    ])
    wait_for(lambda: len(calls) == 2, description="two activity transcriptions")
    stop_and_drain(manager)
    clips = store.list_clips()
    assert len(clips) == len(calls) == 2
    assert len({call["clip_id"] for call in calls}) == 2
    durations = sorted(clip["duration"] for clip in clips)
    assert 1.2 <= durations[0] <= 2.0
    assert 2.2 <= durations[1] <= 3.0
    assert manager.status()["transcribed_count"] == 2


def test_continuous_45_seconds_is_not_split_at_five_or_thirty_seconds(activity_session):
    manager, store, calls = activity_session([(0.5, 0), (45.0, 4096)])
    # The active utterance has not reached its quiet boundary yet.
    assert store.status()["clip_count"] == 0
    assert calls == []
    stop_and_drain(manager)
    clips = store.list_clips()
    assert len(clips) == len(calls) == 1
    assert 45 <= clips[0]["duration"] <= 45.5
    assert calls[0]["clip_id"] == clips[0]["id"]
    assert Path(clips[0]["path"]).is_file()


def test_stopping_during_initial_quiet_never_creates_empty_clip(activity_session):
    manager, store, calls = activity_session([(0.4, 0)])
    stop_and_drain(manager)
    manager.stop()  # repeat is idempotent and creates no new audio
    assert store.status()["clip_count"] == 0
    assert calls == []
