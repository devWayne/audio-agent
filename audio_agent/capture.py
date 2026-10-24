"""Explicit, single-device macOS microphone recording through local ffmpeg."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
import array
import fcntl
import json
import math
import os
import re
import selectors
import subprocess
import sys
import threading
import time
import uuid

from .store import AudioStore, SAMPLE_RATE, SAMPLE_WIDTH
from .activity import ActivityGate, ActivityLimitError
from .vad import SileroVad, DEFAULT_VAD_MODEL


@dataclass(frozen=True)
class CaptureConfig:
    device: str
    chunk_seconds: float = 5
    sample_rate: int = SAMPLE_RATE
    ffmpeg_path: str = "ffmpeg"
    source: str | None = None
    activity_triggered: bool = False
    sound_threshold: float = 0.006
    release_threshold: float = 0.004
    pre_roll_seconds: float = 0.3
    end_silence_seconds: float = 0.8
    min_sound_seconds: float = 0.2
    max_segment_seconds: float | None = None
    max_active_seconds: float = 600
    vad_enabled: bool = False
    vad_model_path: str | None = None

    @property
    def activity_options(self):
        return {key: getattr(self, key) for key in (
            "sound_threshold", "release_threshold", "pre_roll_seconds",
            "end_silence_seconds", "min_sound_seconds", "max_segment_seconds", "max_active_seconds")}

    def __post_init__(self):
        if not isinstance(self.device, str) or not self.device.strip() or any(c in self.device for c in "\x00\r\n"):
            raise ValueError("An explicit AVFoundation audio device index or name is required")
        if self.device.lower() in ("default", "none"):
            raise ValueError("Choose an explicit audio device; 'default' is not supported")
        if self.sample_rate != SAMPLE_RATE:
            raise ValueError("Capture sample_rate must be 16000")
        if not math.isfinite(self.chunk_seconds) or not 0.1 <= self.chunk_seconds <= 300:
            raise ValueError("chunk_seconds must be between 0.1 and 300")
        if not isinstance(self.activity_triggered, bool):
            raise ValueError("activity_triggered must be a boolean")
        if not isinstance(self.vad_enabled, bool):
            raise ValueError("vad_enabled must be a boolean")
        if self.vad_enabled and not self.activity_triggered:
            raise ValueError("VAD requires activity_triggered capture")
        if self.vad_model_path is not None and (not isinstance(self.vad_model_path, str)
                or not self.vad_model_path.strip() or "\x00" in self.vad_model_path):
            raise ValueError("vad_model_path must be a non-empty local path")
        ActivityGate(**self.activity_options)  # Validate without processing audio.


def list_audio_devices(ffmpeg_path: str = "ffmpeg") -> list[dict]:
    """List AVFoundation audio devices. A nonzero ffmpeg exit is expected."""
    if sys.platform != "darwin":
        raise RuntimeError("AVFoundation device listing is available only on macOS")
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required; install it before recording") from exc
    devices = []
    in_audio = False
    for line in result.stderr.splitlines():
        if "AVFoundation audio devices:" in line:
            in_audio = True
            continue
        if "AVFoundation video devices:" in line:
            in_audio = False
            continue
        if in_audio:
            match = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
            if match:
                devices.append({"index": int(match.group(1)), "name": match.group(2)})
    if not devices:
        raise RuntimeError("No AVFoundation audio devices were found. " + result.stderr.strip()[-1500:])
    return devices


def read_capture_status(root: str | Path) -> dict:
    """Read status from the recorder process, marking stale PIDs as inactive."""
    path = Path(root) / "capture.json"
    try:
        result = json.loads(path.read_text())
    except FileNotFoundError:
        return {"state": "stopped", "active": False, "running": False, "last_error": None}
    except (OSError, ValueError) as exc:
        return {"state": "error", "active": False, "running": False, "last_error": f"Cannot read capture status: {exc}"}
    if result.get("active"):
        pid = result.get("pid")
        try:
            if not isinstance(pid, int) or pid <= 0:
                raise ProcessLookupError("Invalid capture PID")
            os.kill(pid, 0)
        except ProcessLookupError:
            result.update(state="stopped", active=False, running=False,
                          last_error="Recorder exited without updating its status")
        except PermissionError:
            pass
    return result


class AudioCapture:
    """Record until explicitly stopped; no transcription or alternate-device fallback.

    The status becomes ``recording`` only after actual PCM arrives. Timestamps
    estimate the first sample's wall clock from its first arrival; hardware input
    latency is not measured. Only completed chunks are visible to readers.
    """
    def __init__(self, store: AudioStore, config: CaptureConfig):
        self.store = store
        self.config = config
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state_lock = threading.RLock()
        self._capture_fd: int | None = None
        self._stderr = deque(maxlen=30)
        self._pending_activity_thresholds = None
        self._gate = None
        self._state = {
            "pid": os.getpid(), "device": config.device,
            "source": config.source or f"avfoundation:{config.device}",
            "state": "stopped", "active": False, "running": False,
            "last_error": None, "updated_at": time.time(), "started_at": None,
            "last_chunk_at": None, "clips_written": 0, "audio_seconds": 0.0,
            "audio_level": 0.0, "audio_peak_level": 0.0, "last_audio_at": None, "observed_seconds": 0.0,
            "settings_pending": False,
            "vad_enabled": config.vad_enabled, "vad_engine": "silero-v4" if config.vad_enabled else "rms",
            "vad_ready": False, "vad_probability": None,
            "vad_model_path": str(config.vad_model_path or DEFAULT_VAD_MODEL) if config.vad_enabled else None,
            "mode": "activity" if config.activity_triggered else "continuous",
            "activity_state": "listening",
            "sound_threshold": config.sound_threshold,
            "release_threshold": config.release_threshold,
            "pre_roll_seconds": config.pre_roll_seconds,
            "end_silence_seconds": config.end_silence_seconds,
            "min_sound_seconds": config.min_sound_seconds,
            "max_segment_seconds": config.max_segment_seconds,
            "max_active_seconds": config.max_active_seconds,
            "chunk_seconds": None if config.activity_triggered else config.chunk_seconds, "sample_rate": SAMPLE_RATE,
            "timing": "first PCM arrival; hardware latency not measured",
        }

    def _publish(self, **changes):
        with self._state_lock:
            self._state.update(changes, updated_at=time.time())
            payload = json.dumps(self._state, ensure_ascii=False, indent=2)
        temp = self.store.root / f".capture-{uuid.uuid4().hex}.json"
        try:
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as target:
                target.write(payload)
                target.flush()
            os.replace(temp, self.store.root / "capture.json")
        finally:
            temp.unlink(missing_ok=True)

    def status(self) -> dict:
        with self._state_lock:
            return dict(self._state)

    def update_activity_threshold(self, trigger_threshold: float, release_threshold: float):
        """Queue a validated threshold change for the next listening frame."""
        if not self.config.activity_triggered:
            raise ValueError("Threshold adjustment requires sound-activity capture")
        # Validate before mutating the recorder, without touching its active gate.
        validator = ActivityGate(**self.config.activity_options)
        validator.update_thresholds(trigger_threshold, release_threshold)
        with self._state_lock:
            self._pending_activity_thresholds = (float(trigger_threshold), float(release_threshold))
            self._state["settings_pending"] = True
        return self.status()

    def _apply_activity_threshold(self, gate):
        with self._state_lock:
            if self._pending_activity_thresholds is None or gate.activity_state != "listening":
                return
            trigger, release = self._pending_activity_thresholds
            gate.update_thresholds(trigger, release)
            self._state.update(sound_threshold=trigger, release_threshold=release, settings_pending=False)
            self._pending_activity_thresholds = None

    def _release_capture_lock(self):
        if self._capture_fd is not None:
            fcntl.flock(self._capture_fd, fcntl.LOCK_UN)
            os.close(self._capture_fd)
            self._capture_fd = None

    def start(self) -> dict:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("This recorder is already active")
        if sys.platform != "darwin":
            raise RuntimeError("Microphone capture currently requires macOS AVFoundation")
        fd = os.open(self.store.root / ".capture.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise RuntimeError("Another recorder is already using this audio store") from exc
        self._capture_fd = fd
        self._stop.clear()
        self._stderr.clear()
        try:
            self.store.ensure_space()
            self._publish(state="starting", active=True, running=False, last_error=None,
                          started_at=time.time(), clips_written=0, audio_seconds=0.0,
                          last_chunk_at=None, audio_level=0.0, audio_peak_level=0.0, last_audio_at=None,
                          activity_state="listening", observed_seconds=0.0, vad_ready=False, vad_probability=None)
            # Fail before opening audio hardware if an explicitly enabled VAD
            # cannot perform local inference. Never silently fall back to RMS.
            vad = SileroVad(self.config.vad_model_path) if self.config.vad_enabled else None
            activity_options = self.config.activity_options | {
                "sound_threshold": self._state["sound_threshold"], "release_threshold": self._state["release_threshold"]}
            self._gate = ActivityGate(**activity_options, vad=vad) if self.config.activity_triggered else None
            if vad is not None:
                self._publish(vad_ready=True)
            self._process = subprocess.Popen(
                [self.config.ffmpeg_path, "-hide_banner", "-loglevel", "warning", "-nostdin",
                 "-f", "avfoundation", "-i", f":{self.config.device}", "-vn",
                 "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0)
            self._stderr_thread = threading.Thread(target=self._drain_stderr, name="audio-ffmpeg-stderr", daemon=True)
            self._stderr_thread.start()
            self._thread = threading.Thread(target=self._consume, name="audio-capture", daemon=False)
            self._thread.start()
        except BaseException as exc:
            if self._process and self._process.poll() is None:
                self._process.kill()
                self._process.wait(timeout=5)
            try:
                self._publish(state="error", active=False, running=False, last_error=str(exc))
            finally:
                self._release_capture_lock()
            raise
        return self.status()

    def run(self) -> dict:
        """Start and block until EOF/stop. The caller installs signal handlers."""
        self.start()
        return self.wait()

    def wait(self) -> dict:
        """Wait for an already-started recorder; useful for arming timers safely."""
        if self._thread is None:
            raise RuntimeError("Recorder has not been started")
        self._thread.join()
        return self.status()

    def _drain_stderr(self):
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in iter(process.stderr.readline, b""):
            self._stderr.append(line.decode("utf-8", errors="replace").strip()[-2000:])

    def _save_chunk(self, chunk: bytes, start_time: float):
        self.store.add_pcm(chunk, start_time=start_time, source=self._state["source"])
        self.store.prune()
        state = self.status()
        self._publish(last_chunk_at=start_time + len(chunk) / (SAMPLE_RATE * SAMPLE_WIDTH),
                      clips_written=state["clips_written"] + 1,
                      audio_seconds=state["audio_seconds"] + len(chunk) / (SAMPLE_RATE * SAMPLE_WIDTH))

    def _consume(self):
        process = self._process
        assert process is not None and process.stdout is not None
        chunk_bytes = round(self.config.chunk_seconds * SAMPLE_RATE) * SAMPLE_WIDTH
        pending = bytearray()
        gate = self._gate
        observed_bytes = 0
        stream_start = None
        committed_frames = 0
        failure = None
        stop_requested_at = None
        level_squared, level_samples, last_level_update = 0, 0, 0.0
        level_tail = b""
        peak_tail = b""
        peak_window = deque()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    if self._stop.is_set() and stop_requested_at is None:
                        stop_requested_at = time.monotonic()
                        if process.poll() is None:
                            process.terminate()
                    if stop_requested_at is not None and time.monotonic() - stop_requested_at > 3 and process.poll() is None:
                        process.kill()
                    ready = selector.select(timeout=0.25)
                    if not ready:
                        if process.poll() is not None:
                            break
                        continue
                    data = os.read(process.stdout.fileno(), 65536)
                    if not data:
                        break
                    if stream_start is None:
                        stream_start = time.time() - len(data) / (SAMPLE_RATE * SAMPLE_WIDTH)
                        self._publish(state="recording", active=True, running=True,
                                      activity_state="listening" if gate else "recording")
                    observed_bytes += len(data)
                    # Real PCM signal feedback, averaged over at most one
                    # second; keep this in memory instead of writing per read.
                    measurement = level_tail + data
                    complete_bytes = len(measurement) - len(measurement) % SAMPLE_WIDTH
                    measured = array.array("h", measurement[:complete_bytes])
                    level_tail = measurement[complete_bytes:]
                    if sys.byteorder != "little":
                        measured.byteswap()
                    level_squared += sum(value * value for value in measured)
                    level_samples += len(measured)
                    measured_at = time.monotonic()
                    # Max RMS of complete 20 ms frames in the recent one-second
                    # window, distinct from both average RMS and sample peaks.
                    peak_data = peak_tail + data
                    peak_bytes = len(peak_data) // 640 * 640
                    block_peak = 0.0
                    for offset in range(0, peak_bytes, 640):
                        frame = array.array("h", peak_data[offset:offset + 640])
                        if sys.byteorder != "little":
                            frame.byteswap()
                        block_peak = max(block_peak, math.sqrt(sum(value * value for value in frame) / len(frame)) / 32768)
                    peak_tail = peak_data[peak_bytes:]
                    if peak_bytes:
                        peak_window.append((measured_at, block_peak))
                    while peak_window and measured_at - peak_window[0][0] > 1:
                        peak_window.popleft()
                    with self._state_lock:
                        self._state["audio_peak_level"] = max((level for _, level in peak_window), default=0.0)
                        self._state["last_audio_at"] = time.time()
                        self._state["observed_seconds"] = (observed_bytes // SAMPLE_WIDTH) / SAMPLE_RATE
                        if level_samples and measured_at - last_level_update >= 1:
                            self._state["audio_level"] = math.sqrt(level_squared / level_samples) / 32768
                            level_squared, level_samples, last_level_update = 0, 0, measured_at
                    if gate is not None:
                        try:
                            segments = []
                            # At most one full detector frame per call, so a
                            # pending update applies before the next event even
                            # when a single pipe read contains multiple events.
                            for offset in range(0, len(data), 640):
                                self._apply_activity_threshold(gate)
                                segments.extend(gate.feed(data[offset:offset + 640]))
                        except ActivityLimitError as exc:
                            for segment in segments + exc.segments:
                                self._save_chunk(segment.pcm, stream_start + segment.start_offset)
                            raise
                        for segment in segments:
                            self._save_chunk(segment.pcm, stream_start + segment.start_offset)
                        self._apply_activity_threshold(gate)
                        with self._state_lock:
                            self._state["vad_probability"] = gate.vad_probability
                        if self.status()["activity_state"] != gate.activity_state:
                            self._publish(activity_state=gate.activity_state)
                    else:
                        pending.extend(data)
                        while len(pending) >= chunk_bytes:
                            chunk = bytes(pending[:chunk_bytes])
                            del pending[:chunk_bytes]
                            self._save_chunk(chunk, stream_start + committed_frames / SAMPLE_RATE)
                            committed_frames += len(chunk) // SAMPLE_WIDTH
            # A partial final clip is committed on both orderly stop and an
            # input-device failure. An incomplete last sample is discarded.
            usable = len(pending) - len(pending) % SAMPLE_WIDTH
            if gate is not None and stream_start is not None:
                try:
                    segments = gate.finish()
                except ActivityLimitError as exc:
                    for segment in exc.segments:
                        self._save_chunk(segment.pcm, stream_start + segment.start_offset)
                    raise
                for segment in segments:
                    self._save_chunk(segment.pcm, stream_start + segment.start_offset)
                self._apply_activity_threshold(gate)
            elif usable and stream_start is not None:
                self._save_chunk(bytes(pending[:usable]), stream_start + committed_frames / SAMPLE_RATE)
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait(timeout=5)
            if self._stderr_thread:
                self._stderr_thread.join(timeout=1)
            if not self._stop.is_set():
                detail = "\n".join(self._stderr)[-4000:]
                failure = f"Audio input ended unexpectedly (ffmpeg exit {returncode})."
                if detail:
                    failure += " " + detail
        except BaseException as exc:
            failure = str(exc)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            if self._stderr_thread:
                self._stderr_thread.join(timeout=1)
            if process.stderr:
                process.stderr.close()
            try:
                self._publish(state="error" if failure else "stopped", active=False,
                              running=False, last_error=failure, audio_level=0.0, audio_peak_level=0.0,
                              activity_state="listening")
            finally:
                self._release_capture_lock()

    def stop(self) -> dict:
        """Request termination, drain available PCM, and flush the final clip."""
        # Preserve a stop request delivered during Popen/startup, before the
        # consumer thread exists. It observes the flag as soon as it starts.
        self._stop.set()
        if not self._thread or not self._thread.is_alive():
            return self.status()
        self._publish(state="stopping", active=True, running=False)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=12)
            if self._thread.is_alive():
                raise RuntimeError("Recorder did not stop in time; inspect capture.json")
        return self.status()
