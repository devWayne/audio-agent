"""Offline ASR using an existing Handy Whisper GGML model.

Importing this module never imports a model runtime or downloads anything.
The runtime is imported only after an existing local model is validated.
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave


BACKEND = "whisper.cpp/pywhispercpp"
_MODEL_LOCK = threading.Lock()
_cached_model = None
_cached_key = None


class ASRError(RuntimeError):
    """An actionable local ASR setup, decoding, or inference error."""


def _handy_models_dir() -> Path:
    override = os.environ.get("HANDY_MODELS_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/com.pais.handy/models"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "com.pais.handy/models"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "com.pais.handy/models"


def _is_ggml(path: Path) -> bool:
    try:
        with path.open("rb") as file:
            return file.read(4) == b"lmgg"
    except OSError:
        return False


def discover_models() -> list[dict]:
    """Find compatible model files without loading them or reading Handy history.

    AUDIO_AGENT_MODEL is considered first; HANDY_MODELS_DIR overrides the default
    Handy model directory. Only Whisper GGML .bin files are supported currently.
    """
    candidates: list[tuple[Path, str]] = []
    explicit = os.environ.get("AUDIO_AGENT_MODEL")
    if explicit:
        candidates.append((Path(explicit).expanduser(), "AUDIO_AGENT_MODEL"))
    model_dir = _handy_models_dir()
    if model_dir.is_dir():
        candidates.extend((path, "Handy") for path in sorted(model_dir.glob("*.bin")))
    result = []
    seen = set()
    for path, source in candidates:
        path = path.resolve()
        if path in seen or not path.is_file() or not _is_ggml(path):
            continue
        seen.add(path)
        result.append({"name": path.name, "path": str(path),
                       "size_bytes": path.stat().st_size, "format": "ggml",
                       "backend": BACKEND, "source": source})
    return result


def _resolve_model(model_path: str | None) -> Path:
    supplied = model_path or os.environ.get("AUDIO_AGENT_MODEL")
    if supplied:
        path = Path(supplied).expanduser().resolve()
        if not path.is_file():
            raise ASRError(f"Local model file not found: {path}. Provide a local GGML .bin file; model downloads are disabled.")
        if not _is_ggml(path):
            raise ASRError(f"Not a Whisper GGML model: {path}. This backend cannot load ONNX, GGUF, or a model name.")
        return path
    models = discover_models()
    if not models:
        raise ASRError("No local Whisper GGML model found. Set AUDIO_AGENT_MODEL to an existing .bin file or HANDY_MODELS_DIR to its directory. No model was downloaded.")
    # Prefer the accurate multilingual model already used by Handy on this Mac.
    preferred = next((model for model in models if model["name"] == "ggml-large-v3-q5_0.bin"), models[0])
    return Path(preferred["path"])


def _wav_info(path: Path) -> tuple[bool, float]:
    try:
        with wave.open(str(path), "rb") as wav:
            compatible = (wav.getframerate() == 16000 and wav.getnchannels() == 1
                          and wav.getsampwidth() == 2 and wav.getcomptype() == "NONE")
            return compatible, wav.getnframes() / wav.getframerate()
    except (wave.Error, EOFError, OSError):
        return False, 0.0


@contextmanager
def _normalized_audio(path: Path):
    """Yield a 16 kHz mono PCM16 WAV, leaving the input unchanged."""
    compatible, duration = _wav_info(path)
    if compatible and path.suffix.lower() == ".wav":
        if duration <= 0:
            raise ASRError("Audio contains no samples.")
        yield path, duration
        return
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ASRError("ffmpeg is required for this audio format. Install ffmpeg or supply a 16 kHz mono PCM16 WAV.")
    with tempfile.TemporaryDirectory(prefix="audio-agent-asr-") as temp:
        target = Path(temp) / "normalized.wav"
        command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                   "-protocol_whitelist", "file,pipe", "-i", str(path),
                   "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                   "-c:a", "pcm_s16le", "-y", str(target)]
        try:
            process = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ASRError(f"Local audio conversion failed: {exc}") from exc
        if process.returncode:
            raise ASRError(f"ffmpeg could not decode the local audio: {process.stderr.strip()[-2000:]}")
        compatible, duration = _wav_info(target)
        if not compatible or duration <= 0:
            raise ASRError("Audio conversion produced no valid samples.")
        yield target, duration


def _inference_settings() -> tuple[int, bool]:
    try:
        threads = int(os.environ.get("AUDIO_AGENT_THREADS", min(4, os.cpu_count() or 1)))
    except ValueError as exc:
        raise ASRError("AUDIO_AGENT_THREADS must be a positive integer.") from exc
    if threads <= 0:
        raise ASRError("AUDIO_AGENT_THREADS must be a positive integer.")
    setting = os.environ.get("AUDIO_AGENT_USE_GPU", "true").lower()
    if setting not in {"1", "0", "true", "false", "yes", "no"}:
        raise ASRError("AUDIO_AGENT_USE_GPU must be true or false.")
    return threads, setting in {"1", "true", "yes"}


def _transcribe_inprocess(path: Path, model_path: str | None = None, language: str = "auto") -> dict:
    """Transcribe an existing local file, reusing one loaded model per process.

    Supports Whisper language codes such as auto, zh, en and region forms such
    as zh-CN. Results contain seconds-based segment times. Model loading and
    inference are serialized because whisper.cpp contexts are not thread-safe.
    No cloud fallback or model download is performed.
    """
    global _cached_model, _cached_key
    started = time.monotonic()
    audio = Path(path).expanduser().resolve()
    if not audio.is_file():
        raise ASRError(f"Local audio file not found: {audio}")
    model_file = _resolve_model(model_path)
    threads, use_gpu = _inference_settings()
    language = (language or "auto").strip().lower().replace("_", "-").split("-")[0]
    try:
        from pywhispercpp.model import Model
    except ImportError as exc:
        raise ASRError("Local ASR runtime missing. Install the project's asr extra (uv sync --extra asr); the existing model file will be reused.") from exc
    if language != "auto" and language not in Model.available_languages():
        raise ASRError(f"Unsupported Whisper language code: {language}")
    with _normalized_audio(audio) as (normalized, duration):
        # Plain PCM arrays bypass pywhispercpp's case-sensitive WAV extension
        # decoder and guarantee the samples/format passed to whisper.cpp.
        import numpy as np
        with wave.open(str(normalized), "rb") as wav:
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        key = (str(model_file), model_file.stat().st_mtime_ns, threads, use_gpu)
        with _MODEL_LOCK, redirect_stdout(sys.stderr):
            try:
                if _cached_key != key:
                    _cached_model = None
                    _cached_key = None
                    _cached_model = Model(str(model_file), n_threads=threads,
                                          context_params={"use_gpu": use_gpu},
                                          print_progress=False, print_realtime=False,
                                          print_timestamps=False, no_context=True,
                                          redirect_whispercpp_logs_to=sys.stderr)
                    if getattr(_cached_model, "_ctx", None) is None:
                        _cached_model = None
                        raise ASRError("whisper.cpp could not load the local model; check model integrity and available memory.")
                    _cached_key = key
                segments = _cached_model.transcribe(samples, language=language,
                                                   translate=False, print_progress=False,
                                                   print_realtime=False, print_timestamps=False)
            except ASRError:
                raise
            except Exception as exc:
                _cached_model = None
                _cached_key = None
                raise ASRError(f"Local Whisper transcription failed: {exc}") from exc
    parts = [{"start": round(segment.t0 / 100.0, 3),
              "end": round(segment.t1 / 100.0, 3), "text": segment.text.strip()}
             for segment in segments if segment.text.strip()]
    return {"text": " ".join(part["text"] for part in parts), "model": str(model_file),
            "backend": BACKEND, "language": language, "segments": parts,
            "duration_seconds": round(duration, 3),
            "elapsed_seconds": round(time.monotonic() - started, 3)}


def transcribe(path: Path, model_path: str | None = None, language: str = "auto") -> dict:
    """Run native inference in a disposable child process.

    A Metal/runtime crash cannot terminate capture or corrupt MCP stdout.
    Weights and GPU buffers are released after each request. No cloud fallback.
    """
    started = time.monotonic()
    audio = Path(path).expanduser().resolve()
    if not audio.is_file():
        raise ASRError(f"Local audio file not found: {audio}")
    model = _resolve_model(model_path)
    _inference_settings()
    try:
        timeout = float(os.environ.get("AUDIO_AGENT_ASR_TIMEOUT", "300"))
    except ValueError as exc:
        raise ASRError("AUDIO_AGENT_ASR_TIMEOUT must be 1–1800 seconds.") from exc
    if not 1 <= timeout <= 1800:
        raise ASRError("AUDIO_AGENT_ASR_TIMEOUT must be 1–1800 seconds.")
    with tempfile.TemporaryDirectory(prefix="audio-agent-worker-") as temp:
        output = Path(temp) / "result.json"
        command = [sys.executable, "-m", "audio_agent.asr_worker", "--audio", str(audio),
                   "--model", str(model), "--language", language, "--output", str(output)]
        with tempfile.TemporaryFile() as log:
            try:
                process = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=log, timeout=timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                raise ASRError(f"Local ASR exceeded {timeout:g} seconds and was stopped. Try a shorter clip or smaller existing model.") from exc
            except OSError as exc:
                raise ASRError(f"Could not start local ASR worker: {exc}") from exc
            log.seek(0, os.SEEK_END)
            size = log.tell()
            log.seek(max(0, size - 4000))
            detail = log.read().decode("utf-8", errors="replace").strip()
        if process.returncode:
            raise ASRError(f"Local ASR worker failed (exit {process.returncode}). "
                           "If Metal is unavailable, set AUDIO_AGENT_USE_GPU=false. " + detail)
        try:
            result = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ASRError("Local ASR worker returned no valid result.") from exc
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise ASRError("Local ASR worker returned an invalid transcript.")
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["execution"] = "isolated-local-process"
        return result
