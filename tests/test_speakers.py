"""Speaker-analysis lifecycle and overlap rules; synthetic PCM, no model load."""
import builtins
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from audio_agent import speakers


def wav(path, seconds=4, rate=16000, channels=1, width=2):
    with wave.open(str(path), "wb") as audio:
        audio.setframerate(rate)
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.writeframes(b"\x01" * (round(seconds * rate) * channels * width))
    return path


@pytest.fixture
def models(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_AGENT_SPEAKER_MODELS_DIR", str(tmp_path))
    for name in ("segmentation", "embedding"):
        monkeypatch.delenv(f"AUDIO_AGENT_SPEAKER_{name.upper()}_MODEL", raising=False)
        (tmp_path / f"{name}.onnx").write_bytes(name.encode())
    monkeypatch.delenv("AUDIO_AGENT_SPEAKER_TIMEOUT", raising=False)
    monkeypatch.delenv("AUDIO_AGENT_SPEAKER_THREADS", raising=False)
    return tmp_path


def test_status_checks_only_file_metadata_and_runtime_presence(models, monkeypatch):
    original_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if name in ("sherpa_onnx", "numpy", "onnxruntime"):
            pytest.fail("Status must not import an inference runtime")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    monkeypatch.setattr(speakers.importlib.util, "find_spec", lambda _: object())
    status = speakers.speaker_model_status()
    assert status["ready"]
    assert status["models"]["embedding"]["size_bytes"] == len(b"embedding")
    (models / "embedding.onnx").unlink()
    assert not speakers.speaker_model_status()["ready"]


def test_missing_models_fail_before_starting_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_AGENT_SPEAKER_MODELS_DIR", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Must not run without models"))
    with pytest.raises(speakers.SpeakerError, match="不会自动下载"):
        speakers.analyze_audio(wav(tmp_path / "a.wav"))


@pytest.mark.parametrize("count", [0, 11, True, "2", 2.0])
def test_invalid_speaker_count_rejected_before_reading_audio(count):
    with pytest.raises(speakers.SpeakerError, match="1–10"):
        speakers.analyze_audio("does-not-exist.wav", count)


@pytest.mark.parametrize("options", [dict(rate=8000), dict(channels=2), dict(width=1), dict(seconds=0), dict(seconds=600.001)])
def test_only_bounded_normalized_audio_accepted(tmp_path, monkeypatch, options):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Must not run invalid audio"))
    with pytest.raises(speakers.SpeakerError):
        speakers.analyze_audio(wav(tmp_path / "a.wav", **options))


@pytest.mark.parametrize("timeout", ["nan", "inf", "0", "601", "broken"])
def test_timeout_cannot_exceed_bound(timeout, monkeypatch):
    monkeypatch.setenv("AUDIO_AGENT_SPEAKER_TIMEOUT", timeout)
    with pytest.raises(speakers.SpeakerError):
        speakers._settings()


@pytest.mark.parametrize("failure", ["crash", "timeout", "missing", "json", "invalid_embedding"])
def test_failed_worker_isolated_and_temp_results_cleaned(models, monkeypatch, failure):
    monkeypatch.setattr(speakers.importlib.util, "find_spec", lambda _: object())
    directories, logs = [], []

    def run(command, **kwargs):
        assert kwargs["stdin"] == kwargs["stdout"] == subprocess.DEVNULL
        assert 0 < kwargs["timeout"] <= 600
        output = Path(command[command.index("--output") + 1])
        directories.append(output.parent)
        logs.append(kwargs["stderr"])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if failure == "crash":
            return subprocess.CompletedProcess(command, -11)
        if failure == "json":
            output.write_text("not JSON")
        if failure == "invalid_embedding":
            output.write_text(json.dumps({"turns": [], "speakers": [{"id": "speaker_1", "embedding": [float("nan")]}],
                                          "speaker_count": 1, "backend": speakers.BACKEND, "embedding_model_id": "sha256:test"}))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    audio = wav(models / "a.wav")
    original = audio.read_bytes()
    with pytest.raises(speakers.SpeakerError):
        speakers.analyze_audio(audio)
    assert directories and all(not directory.exists() for directory in directories)
    assert all(log.closed for log in logs)
    assert audio.read_bytes() == original


def test_exclusive_ranges_exclude_all_overlaps_and_cap_at_thirty_seconds():
    turns = [{"start": 0, "end": 60, "speaker": "a"},
             {"start": 4, "end": 10, "speaker": "b"},
             {"start": 8, "end": 20, "speaker": "c"}]
    ranges = speakers._exclusive_ranges(turns, "a", 60 * 16000)
    assert ranges == [(20 * 16000, 50 * 16000)]
    assert speakers._exclusive_ranges(turns, "b", 60 * 16000) == []


def fake_runtime(monkeypatch, turns):
    observed = {"configs": [], "samples": []}

    class Config:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
        def validate(self):
            return True

    class Diarizer:
        sample_rate = 16000
        def __init__(self, config):
            observed["configs"].append(config)
        def process(self, samples):
            assert samples.dtype == np.float32
            return SimpleNamespace(sort_by_start_time=lambda: [SimpleNamespace(start=a, end=b, speaker=c) for a, b, c in turns])

    class Stream:
        def accept_waveform(self, sample_rate, waveform):
            assert sample_rate == 16000
            observed["samples"].append(waveform.copy())
        def input_finished(self):
            pass

    class Extractor:
        def __init__(self, config):
            assert config.provider == "cpu"
        def create_stream(self):
            return Stream()
        def is_ready(self, stream):
            return True
        def compute(self, stream):
            return [3.0, 4.0]

    module = SimpleNamespace(OfflineSpeakerDiarization= Diarizer, SpeakerEmbeddingExtractor=Extractor)
    for name in ("OfflineSpeakerDiarizationConfig", "OfflineSpeakerSegmentationModelConfig", "OfflineSpeakerSegmentationPyannoteModelConfig", "SpeakerEmbeddingExtractorConfig", "FastClusteringConfig"):
        setattr(module, name, Config)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", module)
    return observed


def test_analysis_has_local_labels_normalized_embeddings_and_overlap_exclusion(models, monkeypatch):
    observed = fake_runtime(monkeypatch, [(0, 4, 7), (3, 4, 2), (4, 8, 2), (9, 9.5, 10)])
    audio = wav(models / "a.wav", seconds=10)
    result = speakers._analyze_inprocess(audio, models / "segmentation.onnx", models / "embedding.onnx")
    assert [row["id"] for row in result["speakers"]] == ["speaker_1", "speaker_2", "speaker_3"]
    assert [row["speech_seconds"] for row in result["speakers"]] == [4, 5, 0.5]
    assert [row["embedding_seconds"] for row in result["speakers"]] == [3, 4, 0.5]
    assert [row["reference_seconds"] for row in result["speakers"]] == [3, 4, 0.5]
    assert result["speakers"][0]["embedding"] == pytest.approx([0.6, 0.8])
    assert result["speakers"][2]["embedding"] == pytest.approx([0.6, 0.8])
    assert [len(x) for x in observed["samples"]] == [3 * 16000, 4 * 16000, 8000]
    assert result["embedding_model_id"] == "sha256:" + hashlib.sha256(b"embedding").hexdigest()
    assert observed["configs"][0].clustering.num_clusters == -1
    assert observed["configs"][0].clustering.threshold == 0.9
    assert result["cluster_threshold"] == 0.9
    assert "自动估计" in result["notes"][1]


@pytest.mark.parametrize("threshold", ["nan", "inf", "0", "1", "broken"])
def test_invalid_cluster_threshold_does_not_start_inference(threshold, monkeypatch):
    monkeypatch.setenv("AUDIO_AGENT_SPEAKER_CLUSTER_THRESHOLD", threshold)
    with pytest.raises(speakers.SpeakerError):
        speakers._cluster_threshold()


def test_no_detected_speech_does_not_create_fictitious_speaker(models, monkeypatch):
    observed = fake_runtime(monkeypatch, [])
    result = speakers._analyze_inprocess(wav(models / "a.wav"), models / "segmentation.onnx", models / "embedding.onnx", 2)
    assert result["speaker_count"] == 0
    assert result["turns"] == result["speakers"] == []
    assert observed["samples"] == []
    assert observed["configs"][0].clustering.num_clusters == 2


def test_truncated_pcm_is_rejected_before_model_construction(models, monkeypatch):
    audio = wav(models / "a.wav")
    audio.write_bytes(audio.read_bytes()[:-200])
    observed = fake_runtime(monkeypatch, [])
    with pytest.raises(speakers.SpeakerError, match="不完整"):
        speakers._analyze_inprocess(audio, models / "segmentation.onnx", models / "embedding.onnx")
    assert observed["configs"] == []


def test_successful_child_json_is_returned_without_native_stdout(models, monkeypatch):
    monkeypatch.setattr(speakers.importlib.util, "find_spec", lambda _: object())
    expected = {"turns": [], "speakers": [], "speaker_count": 0,
                "backend": speakers.BACKEND, "embedding_model_id": "sha256:test", "elapsed_seconds": 1}

    def run(command, **kwargs):
        assert command[command.index("--num-speakers") + 1] == "2"
        assert kwargs["stdout"] == subprocess.DEVNULL
        Path(command[command.index("--output") + 1]).write_text(json.dumps(expected))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert speakers.analyze_audio(wav(models / "a.wav"), 2) == expected
