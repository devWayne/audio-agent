"""Offline setup and audio-format checks; no large model is loaded by tests."""
import builtins
import hashlib
import math
from pathlib import Path
import shutil
import struct
import subprocess
import wave

import pytest

from audio_agent import asr


def make_wav(path: Path, rate=16000, channels=1, frames=1600):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        samples = [int(5000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(frames)]
        audio.writeframes(b"".join(struct.pack("<h", sample) * channels for sample in samples))
    return path


def test_model_discovery_ignores_other_formats_and_deduplicates(tmp_path, monkeypatch):
    model = tmp_path / "ggml-model.bin"
    model.write_bytes(b"lmgg" + b"\0" * 16)
    (tmp_path / "unrelated.bin").write_bytes(b"GGUF" + b"\0" * 16)
    (tmp_path / "silero.onnx").write_bytes(b"vad model")
    monkeypatch.setenv("HANDY_MODELS_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIO_AGENT_MODEL", str(model))
    models = asr.discover_models()
    assert len(models) == 1
    assert models[0]["path"] == str(model)
    assert models[0]["source"] == "AUDIO_AGENT_MODEL"
    assert models[0]["size_bytes"] == 20


def test_missing_explicit_model_never_falls_back_or_imports_runtime(tmp_path, monkeypatch):
    audio = make_wav(tmp_path / "test.wav")
    (tmp_path / "ggml-installed.bin").write_bytes(b"lmgg")
    monkeypatch.setenv("HANDY_MODELS_DIR", str(tmp_path))
    original_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if name.startswith("pywhispercpp"):
            pytest.fail("Missing model must be rejected before importing ASR runtime")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    with pytest.raises(asr.ASRError, match="downloads are disabled"):
        asr.transcribe(audio, str(tmp_path / "missing.bin"))


def test_no_installed_model_has_actionable_error(tmp_path, monkeypatch):
    monkeypatch.delenv("AUDIO_AGENT_MODEL", raising=False)
    monkeypatch.setenv("HANDY_MODELS_DIR", str(tmp_path))
    with pytest.raises(asr.ASRError, match="No local Whisper GGML model"):
        asr._resolve_model(None)


def test_non_whisper_model_rejected_before_native_load(tmp_path):
    model = tmp_path / "llm.gguf"
    model.write_bytes(b"GGUF")
    with pytest.raises(asr.ASRError, match="Not a Whisper GGML model"):
        asr._resolve_model(str(model))


def test_compatible_audio_needs_no_ffmpeg(tmp_path, monkeypatch):
    audio = make_wav(tmp_path / "audio.WAV")
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with asr._normalized_audio(audio) as (normalized, seconds):
        assert normalized == audio
        assert seconds == pytest.approx(0.1)
    assert audio.exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_stereo_48k_audio_normalized_without_mutation_and_temp_cleaned(tmp_path):
    audio = make_wav(tmp_path / "stereo.wav", rate=48000, channels=2, frames=4800)
    original_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
    with asr._normalized_audio(audio) as (normalized, seconds):
        assert normalized != audio
        assert seconds == pytest.approx(0.1)
        with wave.open(str(normalized), "rb") as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
    assert not normalized.exists()
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == original_hash


def test_empty_audio_rejected(tmp_path):
    audio = make_wav(tmp_path / "empty.wav", frames=0)
    with pytest.raises(asr.ASRError, match="no samples"):
        with asr._normalized_audio(audio):
            pass


def test_conversion_missing_ffmpeg_is_actionable(tmp_path, monkeypatch):
    audio = make_wav(tmp_path / "stereo.wav", channels=2)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(asr.ASRError, match="ffmpeg is required"):
        with asr._normalized_audio(audio):
            pass


@pytest.mark.parametrize("value", ["zero", "0", "-1"])
def test_invalid_thread_configuration_is_rejected(value, monkeypatch):
    monkeypatch.setenv("AUDIO_AGENT_THREADS", value)
    with pytest.raises(asr.ASRError, match="positive integer"):
        asr._inference_settings()


def test_cpu_override(monkeypatch):
    monkeypatch.setenv("AUDIO_AGENT_THREADS", "2")
    monkeypatch.setenv("AUDIO_AGENT_USE_GPU", "false")
    assert asr._inference_settings() == (2, False)


@pytest.mark.parametrize("failure", ["native_crash", "timeout", "missing_output", "invalid_json"])
def test_worker_failure_is_reported_and_temporary_files_removed(tmp_path, monkeypatch, failure):
    audio = make_wav(tmp_path / "audio.wav")
    model = tmp_path / "ggml-model.bin"
    model.write_bytes(b"lmgg" + b"\0" * 16)
    temporary_directories = []
    log_handles = []

    def worker(command, **kwargs):
        output = Path(command[command.index("--output") + 1])
        temporary_directories.append(output.parent)
        log_handles.append(kwargs["stderr"])
        assert output.parent.exists()
        # The parent must not expose native stdout to MCP's JSON-RPC stream.
        assert kwargs["stdout"] == subprocess.DEVNULL
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if failure == "native_crash":
            return subprocess.CompletedProcess(command, -11)
        if failure == "invalid_json":
            output.write_text("not-json", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", worker)
    with pytest.raises(asr.ASRError):
        asr.transcribe(audio, str(model), "en")
    assert temporary_directories
    assert all(not directory.exists() for directory in temporary_directories)
    assert all(log.closed for log in log_handles)
    assert audio.exists()
    assert model.exists()
