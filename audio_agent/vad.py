"""Offline Silero VAD v4 inference using an existing, read-only ONNX model.

The state/input contract follows the upstream v4 OnnxWrapper. No torch, model
download, network call or microphone access occurs here. One instance belongs
to one PCM stream; recurrent state must never be shared between recordings.
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

DEFAULT_VAD_MODEL = Path("/Applications/Handy.app/Contents/Resources/resources/models/silero_vad_v4.onnx")
SAMPLE_RATE = 16000
FRAME_SAMPLES = 512


def vad_availability(model_path: str | Path | None = None) -> dict:
    """Report installed prerequisites without loading a model or downloading it."""
    path = Path(model_path) if model_path is not None else DEFAULT_VAD_MODEL
    runtime = importlib.util.find_spec("onnxruntime") is not None
    exists = path.is_file()
    return {"engine": "silero-v4", "model_path": str(path), "model_exists": exists,
            "runtime_installed": runtime, "available": exists and runtime,
            "frame_samples": FRAME_SAMPLES, "sample_rate": SAMPLE_RATE}


def vad_model_status(model_path: str | Path | None = None) -> dict:
    state = vad_availability(model_path)
    reason = ("Local Silero VAD model is missing" if not state["model_exists"] else
              "onnxruntime is not installed" if not state["runtime_installed"] else None)
    return state | {"backend": "silero-v4", "reason": reason}


class SileroVad:
    """Stateful, CPU-only 16 kHz mono PCM16 speech-probability estimator."""

    frame_samples = FRAME_SAMPLES
    engine = "silero-v4"

    def __init__(self, model_path: str | Path | None = None):
        self.model_path = Path(model_path) if model_path is not None else DEFAULT_VAD_MODEL
        if not self.model_path.is_file():
            raise RuntimeError(f"Silero VAD model is missing: {self.model_path}. No model was downloaded.")
        try:
            import numpy as np
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError("Local Silero VAD requires the installed onnxruntime package; VAD was not started.") from exc
        self._np = np
        ort.disable_telemetry_events()
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        try:
            self._session = ort.InferenceSession(str(self.model_path), sess_options=options,
                                                 providers=["CPUExecutionProvider"])
            inputs = {item.name for item in self._session.get_inputs()}
            outputs = [item.name for item in self._session.get_outputs()]
            if inputs != {"input", "h", "c", "sr"} or len(outputs) != 3:
                raise ValueError("Expected Silero v4 input/h/c/sr recurrent-state interface")
            self._outputs = outputs
            self.reset()
            # Validate a real inference before the caller opens a microphone.
            self.probability(bytes(FRAME_SAMPLES * 2))
            self.reset()
        except Exception as exc:
            raise RuntimeError(f"Cannot initialize local Silero VAD v4: {exc}") from exc

    def reset(self):
        self._h = self._np.zeros((2, 1, 64), dtype=self._np.float32)
        self._c = self._np.zeros((2, 1, 64), dtype=self._np.float32)
        self.last_probability = None

    def probability(self, pcm: bytes) -> float:
        """Process exactly 512 samples, or zero-pad a final partial frame.

        Padding is for inference only; the gate retains only original samples.
        Feeding the full stream, including silence, preserves recurrent state.
        """
        if not pcm or len(pcm) % 2 or len(pcm) > FRAME_SAMPLES * 2:
            raise ValueError("Silero VAD expects 1–512 complete PCM16 samples")
        np = self._np
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if audio.size < FRAME_SAMPLES:
            audio = np.pad(audio, (0, FRAME_SAMPLES - audio.size))
        try:
            output, h, c = self._session.run(self._outputs, {
                "input": audio.reshape(1, FRAME_SAMPLES), "h": self._h, "c": self._c,
                "sr": np.array(SAMPLE_RATE, dtype=np.int64)})
            probability = float(np.asarray(output).reshape(-1)[0])
            if (not math.isfinite(probability) or not 0 <= probability <= 1
                    or h.shape != (2, 1, 64) or c.shape != (2, 1, 64)
                    or not np.isfinite(h).all() or not np.isfinite(c).all()):
                raise ValueError("Invalid VAD probability or recurrent state")
        except Exception as exc:
            raise RuntimeError(f"Local Silero VAD inference failed: {exc}") from exc
        self._h, self._c = h, c
        self.last_probability = probability
        return probability
