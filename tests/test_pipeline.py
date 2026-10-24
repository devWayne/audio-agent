"""Local orchestration regressions using fake ASR and synthetic PCM only."""
from datetime import datetime
from pathlib import Path
import json
import os
import struct
import time
import wave

import pytest

from audio_agent import asr, pipeline
from audio_agent.store import AudioStore


def pcm(value, seconds):
    return struct.pack("<h", value) * round(seconds * 16000)


@pytest.fixture
def store(tmp_path):
    return AudioStore(tmp_path / "data")


@pytest.fixture
def fake_asr(monkeypatch):
    calls = []

    def transcribe(path, model_path=None, language="auto"):
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            data = wav.readframes(frames)
        calls.append({"path": Path(path), "data": data, "frames": frames,
                      "model_path": model_path, "language": language})
        return {"text": "测试文本", "segments": [{"start": 0, "end": frames / 16000,
                                                "text": "测试文本"}],
                "duration_seconds": frames / 16000, "backend": "fake-local"}

    monkeypatch.setattr(asr, "transcribe", transcribe)
    return calls


def test_recent_slices_exact_samples_and_persists_without_temporary_path(store, fake_asr):
    store.add_pcm(pcm(1000, 1) + pcm(2000, 1), start_time=100, source="test-mic")
    result = pipeline.transcribe_recent(store, seconds=1, now=102, source="test-mic",
                                        model_path="existing-model.bin", language="zh")
    assert fake_asr[0]["data"] == pcm(2000, 1)
    assert fake_asr[0]["model_path"] == "existing-model.bin"
    assert fake_asr[0]["language"] == "zh"
    assert result["audio"]["start_time"] == 101
    assert result["audio"]["end_time"] == 102
    assert result["audio"]["duration"] == 1
    assert "path" not in result["audio"]
    assert not fake_asr[0]["path"].exists()
    assert pipeline.recent_transcripts(store)[0]["id"] == result["id"]
    assert not list((store.root / "transcripts").glob("*.tmp"))


def test_recent_retains_mapping_across_recording_gap(store, fake_asr):
    store.add_pcm(pcm(1000, 1), start_time=100, source="test-mic")
    store.add_pcm(pcm(2000, 1), start_time=103, source="test-mic")
    result = pipeline.transcribe_recent(store, seconds=4, now=104, source="test-mic")
    assert fake_asr[0]["data"] == pcm(1000, 1) + pcm(2000, 1)
    assert result["audio"]["duration"] == 2
    assert result["audio"]["gap_policy"] == "omit"
    assert [(item["start_time"], item["end_time"]) for item in result["audio"]["segments"]] == [(100, 101), (103, 104)]


def test_last_utterance_uses_store_api_and_excludes_previous_activity(store, fake_asr):
    # Two separate energy bursts, with a full second of quiet between them.
    data = pcm(0, 0.5) + pcm(4000, 1) + pcm(0, 1) + pcm(8000, 0.5) + pcm(0, 1)
    store.add_pcm(data, start_time=100, source="test-mic")
    result = pipeline.transcribe_recent(store, seconds=4, now=104,
                                        source="test-mic", last_utterance=True)
    values = struct.unpack("<" + "h" * fake_asr[0]["frames"], fake_asr[0]["data"])
    assert 8000 in values and 4000 not in values
    assert result["selection"] == "last_utterance"
    assert result["audio"]["start_time"] == pytest.approx(102.35, abs=0.02)
    assert result["audio"]["end_time"] == pytest.approx(103.15, abs=0.02)


def test_silent_last_utterance_never_invokes_asr(store, fake_asr):
    store.add_pcm(pcm(0, 2), start_time=100, source="test-mic")
    with pytest.raises(ValueError, match="没有检测到讲话"):
        pipeline.transcribe_recent(store, seconds=2, now=102, last_utterance=True)
    assert fake_asr == []
    assert list((store.root / "work").iterdir()) == []
    assert pipeline.recent_transcripts(store) == []


def test_busy_asr_lock_refuses_second_request_then_releases(store, fake_asr):
    store.add_pcm(pcm(1000, 1), start_time=100, source="test-mic")
    with pipeline.asr_lock(store.root):
        with pytest.raises(RuntimeError, match="已有本地转写任务"):
            pipeline.transcribe_recent(store, seconds=1, now=101)
    assert fake_asr == []
    assert pipeline.transcribe_recent(store, seconds=1, now=101)["text"] == "测试文本"


def test_asr_failure_cleans_temporary_audio_and_releases_lock(store, monkeypatch):
    store.add_pcm(pcm(1000, 1), start_time=100, source="test-mic")

    def fail(*args, **kwargs):
        raise RuntimeError("fake inference failure")

    monkeypatch.setattr(asr, "transcribe", fail)
    with pytest.raises(RuntimeError, match="fake inference failure"):
        pipeline.transcribe_recent(store, seconds=1, now=101)
    assert list((store.root / "work").iterdir()) == []
    assert pipeline.recent_transcripts(store) == []
    with pipeline.asr_lock(store.root):
        pass


def test_clip_snapshot_survives_pruning_during_inference(store, monkeypatch):
    original = store.add_pcm(pcm(1000, 1), start_time=100, source="live:test-mic")
    observed = []

    def transcribe(path, **kwargs):
        path = Path(path)
        observed.append(path)
        store.prune(now=100 + 2 * 86400)
        assert not Path(original["path"]).exists()
        with wave.open(str(path), "rb") as wav:
            assert wav.readframes(wav.getnframes()) == pcm(1000, 1)
        return {"text": "safe snapshot", "segments": [], "backend": "fake-local"}

    monkeypatch.setattr(asr, "transcribe", transcribe)
    result = pipeline.transcribe_clip(store, original["id"])
    assert result["clip_id"] == original["id"]
    assert result["text"] == "safe snapshot"
    assert result["start_time"] == 100
    assert result["end_time"] == 101
    assert result["created_at"] > result["end_time"]
    assert result["source"] == "live:test-mic"
    assert result["original_name"] is None
    assert result["display_name"] == f"麦克风 {datetime.fromtimestamp(100):%Y-%m-%d %H:%M:%S}"
    assert store.get_clip(original["id"]) is None
    saved = json.loads((store.root / "transcripts" / f"{result['id']}.json").read_text())
    assert saved == result
    assert "path" not in saved and "original_path" not in saved
    assert not observed[0].exists()


def test_import_name_and_recording_times_survive_retranscription_and_audio_cleanup(store, fake_asr, tmp_path):
    original = tmp_path / "upload.wav"
    with wave.open(str(original), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(pcm(2000, 1.25))
    clip = store.import_wav(original, start_time=123.5, original_name="访谈原录音.wav")

    first = pipeline.transcribe_clip(store, clip["id"], language="zh")
    second = pipeline.transcribe_clip(store, clip["id"], language="zh")
    assert first["id"] != second["id"]
    assert second["created_at"] >= first["created_at"]
    assert len(fake_asr) == 2
    assert all(call["data"] == pcm(2000, 1.25) and call["language"] == "zh" for call in fake_asr)
    store.prune(now=clip["created_time"] + 2 * 86400)
    assert store.get_clip(clip["id"]) is None
    for transcript in pipeline.recent_transcripts(store):
        assert transcript["start_time"] == 123.5
        assert transcript["end_time"] == 124.75
        assert transcript["original_name"] == "访谈原录音.wav"
        assert transcript["display_name"] == "访谈原录音.wav"
        assert transcript["text"] == "测试文本"
    assert len(pipeline.recent_transcripts(store)) == 2


@pytest.mark.parametrize("seconds", [0, -1, 601, float("inf"), float("nan")])
def test_invalid_window_never_invokes_asr(store, fake_asr, seconds):
    with pytest.raises(ValueError):
        pipeline.transcribe_recent(store, seconds=seconds)
    assert fake_asr == []


@pytest.mark.parametrize("retention_hours, age_hours, should_exist", [(1, 2, False), (48, 30, True)])
def test_transcript_retention_uses_store_policy(tmp_path, retention_hours, age_hours, should_exist):
    store = AudioStore(tmp_path / "policy-data", retention_hours=retention_hours)
    folder = store.root / "transcripts"
    folder.mkdir()
    old = folder / "previous.json"
    old.write_text('{"text": "previous transcript"}', encoding="utf-8")
    modified = time.time() - age_hours * 3600
    os.utime(old, (modified, modified))
    pipeline.prune_transcripts(store)
    assert old.exists() is should_exist
