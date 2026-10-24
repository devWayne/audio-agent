"""Explicit browser-owned microphone sessions with sequential local ASR.

The capture monitor and ASR worker are separate: startup deadlines and stopping
remain responsive while native inference runs. Every clip is kept in AudioStore;
only clips carrying this session's unique source are eligible for automatic ASR.
"""
from __future__ import annotations

import array
from collections import deque
import copy
import json
import math
import os
import re
import sys
import threading
import time
import uuid
import wave

from .capture import AudioCapture, CaptureConfig
from .pipeline import transcribe_clip
from .store import AudioStore


class LiveSessionError(RuntimeError):
    pass


class LiveManager:
    def __init__(self, store: AudioStore, *, capture_factory=AudioCapture,
                 transcriber=transcribe_clip, chunk_seconds=5, poll_interval=0.2,
                 startup_timeout=10, busy_retry_seconds=0.5, busy_timeout=120,
                 max_pending=12, silence_threshold=0.003,
                 sound_threshold=0.006, release_threshold=0.004,
                 end_silence_seconds=0.8, pre_roll_seconds=0.3,
                 on_transcript=None, speaker_model_busy=None):
        if not 0.1 <= chunk_seconds <= 30 or min(poll_interval, startup_timeout, busy_retry_seconds, busy_timeout) <= 0:
            raise ValueError("Live timing values must be positive; chunks must be 0.1–30 seconds")
        if not isinstance(max_pending, int) or max_pending < 1 or not 0 <= silence_threshold < 1:
            raise ValueError("Invalid live queue or silence settings")
        self.store, self.capture_factory, self.transcriber = store, capture_factory, transcriber
        self.on_transcript = on_transcript
        self.speaker_model_busy = speaker_model_busy or (lambda: False)
        self.chunk_seconds, self.poll_interval = chunk_seconds, poll_interval
        self.startup_timeout, self.busy_retry_seconds, self.busy_timeout = startup_timeout, busy_retry_seconds, busy_timeout
        self.max_pending, self.silence_threshold = max_pending, silence_threshold
        self.sound_threshold, self.release_threshold = sound_threshold, release_threshold
        self.end_silence_seconds, self.pre_roll_seconds = end_silence_seconds, pre_roll_seconds
        self._condition = threading.Condition(threading.RLock())
        self._stop = threading.Event()
        self._monitor_thread = None
        self._worker_thread = None
        self._capture = None
        self._closed = False
        self._capture_done = False
        self._abort_asr = False
        self._queue = deque()
        self._speaker_backlog = False
        self._deferred_count = 0
        self._row_cursor = 0
        self._source = None
        self._settings_path = self.store.root / "live-settings.json"
        self._settings_load_error = None
        self._configured_settings = {"trigger_db": 20 * math.log10(self.sound_threshold),
                                     "trigger_threshold": self.sound_threshold,
                                     "release_threshold": self.release_threshold}
        self._load_settings()
        self._state = self._empty_state()

    def _empty_state(self):
        return {"state": "finished", "active": False, "device": None, "language": "zh",
                "session_id": None, "started_at": None, "audio_seconds": 0.0,
                "clips_count": 0, "transcribed_count": 0, "pending_count": 0,
                "silent_count": 0, "failed_count": 0, "current_clip_id": None,
                "last_error": None, "speaker_error": None, "latest_text": "", "latest_transcript_id": None,
                "recent_transcripts": [], "chunk_seconds": None,
                "mode": "activity", "activity_state": "listening",
                "trigger_threshold": self.sound_threshold, "release_threshold": self.release_threshold,
                "trigger_db": 20 * math.log10(self.sound_threshold),
                "configured_trigger_db": self._configured_settings["trigger_db"],
                "configured_trigger_threshold": self.sound_threshold,
                "settings_pending": False, "settings_error": self._settings_load_error,
                "end_silence_seconds": self.end_silence_seconds, "pre_roll_seconds": self.pre_roll_seconds,
                "observed_seconds": 0.0, "audio_level": 0.0, "audio_peak_level": 0.0, "last_audio_at": None,
                "vad_enabled": False, "vad_engine": "rms", "vad_ready": False, "vad_probability": None}

    @staticmethod
    def _settings_for_db(trigger_db):
        if (not isinstance(trigger_db, (int, float)) or isinstance(trigger_db, bool)
                or not math.isfinite(trigger_db) or not -60 <= trigger_db <= -18):
            raise ValueError("trigger_db 必须是 -60 到 -18 之间的有限数值。")
        threshold = 10 ** (float(trigger_db) / 20)
        return {"trigger_db": float(trigger_db), "trigger_threshold": threshold,
                "release_threshold": threshold * (2 / 3)}

    def _load_settings(self):
        try:
            if self._settings_path.is_symlink() or self._settings_path.stat().st_size > 4096:
                raise ValueError("Invalid settings file")
            value = json.loads(self._settings_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or set(value) != {"trigger_db"}:
                raise ValueError("Invalid settings fields")
            settings = self._settings_for_db(value["trigger_db"])
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeError):
            self._settings_load_error = "已保存的声音阈值设置无法读取，当前使用默认值；重新保存可修复。"
            return
        self._configured_settings = settings
        self.sound_threshold = settings["trigger_threshold"]
        self.release_threshold = settings["release_threshold"]

    def _refresh_settings_locked(self):
        applied = self._state["trigger_threshold"]
        release = self._state["release_threshold"]
        capture_pending = False
        if self._state["active"] and self._capture is not None:
            capture = self._capture.status()
            applied = capture.get("sound_threshold", applied)
            release = capture.get("release_threshold", release)
            capture_pending = capture.get("settings_pending", False)
        else:
            applied, release = self.sound_threshold, self.release_threshold
        self._state.update(trigger_threshold=applied, release_threshold=release,
                           trigger_db=20 * math.log10(applied),
                           configured_trigger_db=self._configured_settings["trigger_db"],
                           configured_trigger_threshold=self.sound_threshold,
                           settings_pending=bool(self._state["active"] and (capture_pending or
                               not math.isclose(applied, self.sound_threshold, rel_tol=1e-12))))

    def settings(self) -> dict:
        with self._condition:
            self._refresh_settings_locked()
            return {"settings": dict(self._configured_settings), "pending": self._state["settings_pending"]}

    def update_settings(self, trigger_db) -> dict:
        settings = self._settings_for_db(trigger_db)
        with self._condition, self.store._lock(exclusive=True):
            if self._closed:
                raise LiveSessionError("本地服务正在关闭。")
            temporary = self.store.root / f".live-settings-{uuid.uuid4().hex}.json"
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as target:
                    json.dump({"trigger_db": settings["trigger_db"]}, target, allow_nan=False)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, self._settings_path)
            finally:
                temporary.unlink(missing_ok=True)
            # Persist first: a failed atomic write must not change the live gate.
            self._configured_settings = settings
            self.sound_threshold = settings["trigger_threshold"]
            self.release_threshold = settings["release_threshold"]
            self._settings_load_error = self._state["settings_error"] = None
            if self._state["active"] and self._capture is not None:
                update = getattr(self._capture, "update_activity_threshold", None)
                if update is not None:
                    try:
                        update(self.sound_threshold, self.release_threshold)
                    except (RuntimeError, ValueError):
                        self._state["settings_error"] = "设置已保存，将在下次录音生效。"
            return self.settings()

    def status(self) -> dict:
        with self._condition:
            self._refresh_settings_locked()
            result = copy.deepcopy(self._state)
            result["pending_count"] = len(self._queue) + bool(self._state["current_clip_id"]) + self._deferred_count
            result["deferred_count"] = self._deferred_count
            result["waiting_for_speaker"] = bool(self.speaker_model_busy() and result["pending_count"])
            return result

    def asr_pending(self) -> bool:
        with self._condition:
            return bool(self._queue or self._state["current_clip_id"] or self._deferred_count)

    def start(self, device: str, language: str = "zh", *, vad_enabled: bool = False) -> dict:
        if language not in ("zh", "auto", "en"):
            raise ValueError("Live language must be zh, auto or en")
        if not isinstance(vad_enabled, bool):
            raise ValueError("vad_enabled 必须为布尔值。")
        session_id = uuid.uuid4().hex
        with self._condition:
            if self._closed:
                raise LiveSessionError("本地服务正在关闭。")
            if self._state["active"]:
                raise LiveSessionError("已有实时录音或待完成的转写，请停止并等待完成后再开始。")
            config = CaptureConfig(device=device, chunk_seconds=self.chunk_seconds,
                                   ffmpeg_path=self.store.ffmpeg_path, source=f"live:{session_id}",
                                   activity_triggered=True, sound_threshold=self.sound_threshold,
                                   release_threshold=self.release_threshold,
                                   end_silence_seconds=self.end_silence_seconds,
                                   pre_roll_seconds=self.pre_roll_seconds,
                                   max_segment_seconds=None, max_active_seconds=600,
                                   vad_enabled=vad_enabled)
            self._capture = self.capture_factory(self.store, config)
            self._source = config.source
            self._stop.clear()
            self._capture_done = self._abort_asr = False
            self._row_cursor = 0
            self._queue.clear()
            self._speaker_backlog = False
            self._deferred_count = 0
            self._state = self._empty_state() | {
                "state": "starting", "active": True, "device": device, "language": language,
                "session_id": session_id, "started_at": time.time(),
                "vad_enabled": vad_enabled, "vad_engine": "silero-v4" if vad_enabled else "rms",
                "vad_ready": False, "vad_probability": None}
            self._monitor_thread = threading.Thread(target=self._monitor, name="live-audio-monitor", daemon=False)
            self._monitor_thread.start()
            return self.status()

    def stop(self) -> dict:
        with self._condition:
            if self._state["active"]:
                self._state["state"] = "stopping"
                self._stop.set()
                self._condition.notify_all()
            return self.status()

    def close(self):
        with self._condition:
            self._closed = True
        self.stop()
        monitor = self._monitor_thread
        if monitor and monitor is not threading.current_thread():
            monitor.join()

    def _set_error(self, message: str):
        with self._condition:
            if self._state["last_error"] is None:
                self._state["last_error"] = message
            self._state["state"] = "stopping"
            self._stop.set()
            self._condition.notify_all()

    def _collect(self):
        with self._condition:
            speaker_busy = self.speaker_model_busy()
            self._speaker_backlog = self._speaker_backlog or speaker_busy
            with self.store._connect() as db:
                rows = db.execute("SELECT rowid AS row_number,* FROM clips WHERE source=? AND rowid>? ORDER BY rowid LIMIT 200",
                                  (self._source, self._row_cursor)).fetchall()
            for row in rows:
                # Speaker inference may temporarily hold the model lock. Keep
                # the RAM queue bounded and leave surplus IDs in AudioStore
                # until ASR catches up, instead of stopping the microphone.
                if self._speaker_backlog and not self._abort_asr and len(self._queue) >= self.max_pending:
                    break
                self._row_cursor = row["row_number"]
                self._state["clips_count"] += 1
                if self._abort_asr:
                    self._state["failed_count"] += 1
                else:
                    self._queue.append(row["id"])
            if self._speaker_backlog:
                with self.store._connect() as db:
                    self._deferred_count = db.execute("SELECT COUNT(*) FROM clips WHERE source=? AND rowid>?",
                                                       (self._source, self._row_cursor)).fetchone()[0]
                if not speaker_busy and not self._deferred_count and not self._queue and not self._state["current_clip_id"]:
                    self._speaker_backlog = False
            if len(self._queue) > self.max_pending:
                self._set_error("本地转写速度暂时跟不上录音，已停止麦克风并处理剩余片段。")
            self._condition.notify_all()

    def _monitor(self):
        capture = self._capture
        try:
            capture.start()
            self._worker_thread = threading.Thread(target=self._transcribe_loop, name="live-local-asr", daemon=False)
            self._worker_thread.start()
            deadline = time.monotonic() + self.startup_timeout
            heard_audio = False
            while True:
                if self._stop.is_set():
                    capture.stop()  # Only the object created by this manager.
                state = capture.status()
                heard_audio = heard_audio or bool(state.get("running")) or bool(state.get("last_audio_at"))
                with self._condition:
                    self._state.update(audio_seconds=state.get("audio_seconds", 0.0),
                                       audio_level=state.get("audio_level", 0.0),
                                       audio_peak_level=state.get("audio_peak_level", 0.0),
                                       last_audio_at=state.get("last_audio_at"),
                                       observed_seconds=state.get("observed_seconds", 0.0),
                                       activity_state=state.get("activity_state", "listening"),
                                       vad_enabled=state.get("vad_enabled", self._state.get("vad_enabled", False)),
                                       vad_engine=state.get("vad_engine", "rms"),
                                       vad_ready=state.get("vad_ready", False),
                                       vad_probability=state.get("vad_probability"))
                    if state.get("running") and not self._stop.is_set():
                        self._state["state"] = "recording"
                self._collect()
                if not state.get("active"):
                    if state.get("last_error"):
                        self._set_error(state["last_error"])
                    elif not self._stop.is_set():
                        self._set_error("麦克风输入意外结束。已保存收到的录音。")
                    break
                if not heard_audio and time.monotonic() >= deadline:
                    self._set_error("10 秒内没有收到麦克风数据，已停止。请检查所选设备和系统麦克风权限。")
                self._stop.wait(self.poll_interval)
        except Exception as exc:
            self._set_error(str(exc))
        finally:
            try:
                # A failed singleton acquisition has no owned recording thread;
                # stop on that unstarted object cannot stop a CLI recorder.
                capture.stop()
                final = capture.status()
                with self._condition:
                    self._state.update(audio_seconds=final.get("audio_seconds", 0.0),
                                       last_audio_at=final.get("last_audio_at"), audio_level=0.0, audio_peak_level=0.0,
                                       observed_seconds=final.get("observed_seconds", 0.0),
                                       activity_state="listening")
                self._collect()  # Includes the partial final chunk from stop.
            except Exception as exc:
                self._set_error(str(exc))
            with self._condition:
                self._capture_done = True
                self._condition.notify_all()
            if self._worker_thread is not None:
                self._worker_thread.join()
            with self._condition:
                self._state.update(active=False, audio_level=0.0, audio_peak_level=0.0,
                                   state="error" if self._state["last_error"] else "finished")
                self._condition.notify_all()

    def _existing_transcript(self, clip_id: str):
        folder = self.store.root / "transcripts"
        if folder.is_symlink():
            raise RuntimeError("转写目录不能是符号链接。")
        candidates = []
        cutoff = time.time() - self.store.retention_hours * 3600
        for path in folder.glob("*.json"):
            try:
                if path.is_symlink() or path.stat().st_mtime < cutoff:
                    continue
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict) or record.get("clip_id") != clip_id or not isinstance(record.get("text"), str):
                    continue
                created = record.get("created_at")
                record_id = record.get("id")
                if (not isinstance(record_id, str) or not re.fullmatch(r"[0-9a-f]{32}", record_id)
                        or not isinstance(created, (int, float)) or isinstance(created, bool) or not math.isfinite(created)):
                    continue
                json.dumps(record, allow_nan=False)
                candidates.append(record)
            except (OSError, ValueError, UnicodeError):
                continue
        return max(candidates, key=lambda record: (record["created_at"], record["id"])) if candidates else None

    def _has_signal(self, clip_id: str) -> bool:
        with self.store._lock(exclusive=False):
            clip = self.store.get_clip(clip_id)
            if clip is None:
                raise RuntimeError("实时录音片段已被清理，未进行转写。")
            handle = open(clip["path"], "rb")
        with handle, wave.open(handle, "rb") as audio:
            while block := audio.readframes(320):
                samples = array.array("h", block)
                if sys.byteorder != "little":
                    samples.byteswap()
                if samples and math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768 >= self.silence_threshold:
                    return True
        return False

    @staticmethod
    def _busy(exc: Exception) -> bool:
        return (isinstance(exc.__cause__, BlockingIOError) or
                "已有本地转写任务" in str(exc) or "transcription busy" in str(exc).lower())

    def _transcribe_one(self, clip_id: str):
        deadline = time.monotonic() + self.busy_timeout
        while True:
            existing = self._existing_transcript(clip_id)
            if existing is not None:
                return existing, False
            try:
                return self.transcriber(self.store, clip_id, language=self._state["language"]), True
            except RuntimeError as exc:
                if not self._busy(exc):
                    raise
                # Our own bounded speaker inference can take longer than the
                # external-ASR busy limit. It must not cause capture to fail.
                known_speaker = self.speaker_model_busy()
                if not known_speaker and time.monotonic() >= deadline:
                    raise RuntimeError("本地模型持续忙碌，已停止实时转写；未处理片段仍在录音库，可稍后手动转写。") from exc
                # A user stop ends capture but deliberately lets queued clips
                # finish. Do not use the stop event here (it would spin).
                waiting_at = time.monotonic()
                with self._condition:
                    self._condition.wait(timeout=self.busy_retry_seconds)
                if known_speaker:
                    deadline += time.monotonic() - waiting_at

    def _transcribe_loop(self):
        while True:
            with self._condition:
                if self._capture_done and self._speaker_backlog:
                    self._collect()  # Drain IDs left on disk after capture stop.
                while not self._queue and not self._capture_done:
                    self._condition.wait()
                if not self._queue:
                    return
                clip_id = self._queue.popleft()
                self._state["current_clip_id"] = clip_id
            try:
                existing = self._existing_transcript(clip_id)
                # Real activity capture has already confirmed the event using
                # the threshold in effect when it occurred. A second fixed RMS
                # cutoff would wrongly reject accepted low-volume events after
                # lowering the slider (or after a later hot threshold change).
                gated = self._capture.status().get("mode") == "activity"
                if existing is None and not gated and not self._has_signal(clip_id):
                    with self._condition:
                        self._state["silent_count"] += 1
                else:
                    result, is_new = (existing, False) if existing is not None else self._transcribe_one(clip_id)
                    with self._condition:
                        self._state["transcribed_count"] += 1
                        self._state["latest_text"] = result.get("text", "")
                        self._state["latest_transcript_id"] = result.get("id")
                        self._state["recent_transcripts"] = (self._state["recent_transcripts"] + [result])[-10:]
                    # Text has already been committed and published. Optional
                    # downstream work cannot turn successful ASR into failure.
                    if is_new and self.on_transcript is not None:
                        try:
                            self.on_transcript(clip_id)
                        except Exception as exc:
                            with self._condition:
                                self._state["speaker_error"] = str(exc)
            except Exception as exc:
                with self._condition:
                    self._abort_asr = True
                    self._state["failed_count"] += 1 + len(self._queue)
                    self._queue.clear()
                    self._deferred_count = 0
                    self._speaker_backlog = False
                self._set_error(str(exc))
                return
            finally:
                with self._condition:
                    self._state["current_clip_id"] = None
                    self._condition.notify_all()
