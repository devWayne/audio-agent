"""No microphone or downloads: deterministic streaming and installed-model tests."""
import array
import builtins
from pathlib import Path
import random
import sys
import wave
from unittest import mock

import pytest

from audio_agent.activity import ActivityGate
from audio_agent.capture import AudioCapture, CaptureConfig
from audio_agent.store import AudioStore
from audio_agent.vad import DEFAULT_VAD_MODEL, SileroVad, vad_model_status


def frame(amplitude=1000, samples=512):
    values = array.array("h", [amplitude] * samples)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


class ScriptedVad:
    frame_samples = 512

    def __init__(self, probabilities):
        self.values = iter(probabilities)
        self.calls = 0

    def probability(self, pcm):
        assert 0 < len(pcm) <= 1024 and len(pcm) % 2 == 0
        self.calls += 1
        return next(self.values)


def test_vad_rejects_loud_non_speech_before_disk():
    gate = ActivityGate(vad=ScriptedVad([0.01] * 1000))
    assert gate.feed(frame(16000) * 1000) == []
    assert gate.finish() == []
    assert gate.activity_state == "listening"
    assert gate.buffered_bytes < 30000


def test_vad_confirmation_uses_lower_release_energy_and_bridges_weak_phonemes():
    # Only the initial syllable clears the onset threshold. Lower volume frames
    # and a 64 ms dip must not reset every partial confirmation.
    vad = ScriptedVad([0.8] * 2 + [0.2] * 2 + [0.8] * 5 + [0.01] * 26)
    gate = ActivityGate(vad=vad)
    assert gate.feed(frame(300) + frame(160) + frame(0) * 2 + frame(160) * 5) == []
    assert gate.activity_state == "recording"
    segments = gate.feed(frame(0) * 26)
    assert len(segments) == 1
    assert segments[0].start_sample == 0
    assert segments[0].end_sample == 9 * 512 + 3200


def test_vad_still_requires_user_loudness_before_triggering():
    gate = ActivityGate(vad=ScriptedVad([0.99] * 100))
    assert gate.feed(frame(100) * 100) == []
    assert gate.finish() == []


def test_loud_noise_does_not_keep_speech_segment_open():
    gate = ActivityGate(vad=ScriptedVad([0.9] * 10 + [0.02] * 50))
    assert gate.feed(frame(1000) * 10) == []
    segments = gate.feed(frame(1000) * 50)
    assert len(segments) == 1
    assert segments[0].end_sample == 10 * 512 + 3200
    assert gate.activity_state == "listening"
    assert gate.finish() == []


def test_vad_stream_fragmentation_and_source_timestamps_remain_exact():
    probabilities = [0.01] * 32 + [0.9] * 16 + [0.01] * 40 + [0.9] * 20 + [0.01] * 30
    pcm = b"".join(frame(1000 if p > 0.5 else 0) for p in probabilities)
    direct = ActivityGate(vad=ScriptedVad(probabilities))
    expected = direct.feed(pcm) + direct.finish()
    fragmented = ActivityGate(vad=ScriptedVad(probabilities))
    pieces = []
    cursor = 0
    randomizer = random.Random(10)
    while cursor < len(pcm):
        count = randomizer.randint(1, 2048)
        pieces.extend(fragmented.feed(pcm[cursor:cursor + count]))
        cursor += count
    pieces.extend(fragmented.finish())
    assert pieces == expected
    assert len(pieces) == 2
    assert pieces[0].start_sample == 32 * 512 - 4800
    assert pieces[1].start_sample == 88 * 512 - 4800


def test_vad_45_seconds_of_continuous_speech_is_one_segment_on_stop():
    pcm = frame(1000, samples=45 * 16000)
    vad = ScriptedVad([0.9] * 1407)
    gate = ActivityGate(vad=vad)
    assert gate.feed(pcm) == []
    segments = gate.finish()
    assert len(segments) == 1
    assert segments[0].pcm == pcm
    assert gate.finish() == []


def test_vad_invalid_probability_is_an_error_not_rms_fallback():
    gate = ActivityGate(vad=ScriptedVad([float("nan")]))
    with pytest.raises(RuntimeError, match="invalid speech probability"):
        gate.feed(frame())


def test_vad_missing_model_is_explicit_and_model_status_does_not_load(tmp_path):
    path = tmp_path / "missing.onnx"
    with mock.patch("audio_agent.vad.importlib.util.find_spec", return_value=object()):
        status = vad_model_status(path)
    assert status["available"] is False
    assert status["backend"] == "silero-v4"
    assert "missing" in status["reason"]
    with pytest.raises(RuntimeError, match="model is missing"):
        SileroVad(path)


def test_vad_missing_runtime_is_explicit(tmp_path):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"test")
    original_import = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == "onnxruntime":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)
    with mock.patch("builtins.__import__", side_effect=unavailable):
        with pytest.raises(RuntimeError, match="requires the installed onnxruntime"):
            SileroVad(model)


def test_vad_startup_failure_does_not_open_microphone_or_hold_lock(tmp_path):
    recorder = AudioCapture(AudioStore(tmp_path), CaptureConfig(
        device="0", activity_triggered=True, vad_enabled=True))
    with mock.patch("audio_agent.capture.sys.platform", "darwin"), \
            mock.patch("audio_agent.capture.SileroVad", side_effect=RuntimeError("VAD missing")), \
            mock.patch("audio_agent.capture.subprocess.Popen") as popen:
        with pytest.raises(RuntimeError, match="VAD missing"):
            recorder.start()
        popen.assert_not_called()
    assert recorder.status()["state"] == "error"
    assert recorder.status()["vad_ready"] is False
    assert recorder._capture_fd is None
    assert recorder.store.status()["clip_count"] == 0


@pytest.mark.parametrize("options", [{"vad_enabled": "true"}, {"vad_enabled": True}, {"vad_model_path": ""}])
def test_vad_configuration_is_explicit(options):
    with pytest.raises(ValueError):
        CaptureConfig(device="0", **options)


@pytest.fixture
def installed_vad():
    if not vad_model_status()["available"]:
        pytest.skip("Optional local Handy VAD model/runtime is not installed; no download")
    return SileroVad(DEFAULT_VAD_MODEL)


def test_real_installed_vad_silence_and_recurrent_reset(installed_vad):
    vad = installed_vad
    first = [vad.probability(frame(0)) for _ in range(50)]
    assert all(0 <= value < 0.5 for value in first)
    vad.reset()
    assert [vad.probability(frame(0)) for _ in range(50)] == pytest.approx(first)
    assert 0 <= vad.probability(frame(0, 30)) <= 1


def test_real_installed_vad_public_speech_fixture(installed_vad):
    fixture = Path(__file__).resolve().parents[1] / ".data/test-fixtures/jfk.wav"
    if not fixture.is_file():
        pytest.skip("Public JFK fixture is not locally present; no download")
    with wave.open(str(fixture), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        pcm = wav.readframes(wav.getnframes())
    gate = ActivityGate(vad=installed_vad)
    source = frame(0, 16000) + pcm + frame(0, 16000)
    segments = gate.feed(source) + gate.finish()
    assert 1 <= len(segments) <= 5
    assert sum(len(segment.pcm) for segment in segments) > len(pcm) * 0.5
    for segment in segments:
        assert segment.pcm == source[segment.start_sample * 2:segment.end_sample * 2]
    assert gate.activity_state == "listening"
