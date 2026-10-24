"""Bounded PCM activity segmentation before any audio reaches disk.

An optional local VAD supplies speech probabilities. Without it this is explicitly
RMS-only sound detection. Neither mode identifies speakers. Quiet ends a segment;
elapsed time never creates periodic segments.
"""
from __future__ import annotations

import array
from collections import deque
from dataclasses import dataclass
import math
import sys

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320  # 20 milliseconds
SAMPLE_WIDTH = 2


@dataclass(frozen=True)
class ActivitySegment:
    pcm: bytes
    start_sample: int

    @property
    def end_sample(self) -> int:
        return self.start_sample + len(self.pcm) // SAMPLE_WIDTH

    @property
    def start_offset(self) -> float:
        return self.start_sample / SAMPLE_RATE


class ActivityLimitError(RuntimeError):
    """Stop capture, saving these complete active segments exactly once."""
    def __init__(self, segments: list[ActivitySegment]):
        super().__init__("连续声音超过内存保护时长，已保存当前完整片段并停止麦克风；没有按固定时间切片。")
        self.segments = segments


class ActivityGate:
    def __init__(self, *, sound_threshold=0.006, release_threshold=0.004,
                 pre_roll_seconds=0.3, end_silence_seconds=0.8,
                 min_sound_seconds=0.2, max_segment_seconds=None,
                 max_active_seconds=600, tail_seconds=0.2, vad=None,
                 vad_threshold=0.5, vad_release_threshold=0.35):
        values = (sound_threshold, release_threshold, pre_roll_seconds,
                  end_silence_seconds, min_sound_seconds, max_active_seconds, tail_seconds)
        if not all(isinstance(value, (float, int)) and math.isfinite(value) for value in values):
            raise ValueError("Sound activity settings must be finite numbers")
        if not 0 < release_threshold <= sound_threshold < 1:
            raise ValueError("Require 0 < release_threshold <= sound_threshold < 1")
        if not 0 <= pre_roll_seconds <= 5 or not 0 < min_sound_seconds <= 5:
            raise ValueError("Pre-roll must be 0–5 seconds and minimum sound must be >0–5 seconds")
        if not 0 < end_silence_seconds <= 10 or not 0 <= tail_seconds < end_silence_seconds:
            raise ValueError("Trailing silence must be shorter than the sound-end timeout (max 10 seconds)")
        if max_segment_seconds is not None and (not isinstance(max_segment_seconds, (int, float))
                or not math.isfinite(max_segment_seconds) or max_segment_seconds <= 0):
            raise ValueError("max_segment_seconds must be positive or None")
        if not 0 < max_active_seconds <= 600:
            raise ValueError("max_active_seconds must be >0 and <=600 for bounded memory")
        # The optional max_segment field is a safety stop, never a split timer.
        guard = min(max_active_seconds, max_segment_seconds) if max_segment_seconds is not None else max_active_seconds
        if guard <= pre_roll_seconds + min_sound_seconds:
            raise ValueError("The active memory guard must exceed pre-roll plus minimum sound")
        if (not isinstance(vad_threshold, (int, float)) or isinstance(vad_threshold, bool)
                or not isinstance(vad_release_threshold, (int, float)) or isinstance(vad_release_threshold, bool)
                or not math.isfinite(vad_threshold) or not math.isfinite(vad_release_threshold)
                or not 0 < vad_release_threshold <= vad_threshold < 1):
            raise ValueError("Require finite 0 < vad_release_threshold <= vad_threshold < 1")
        self.vad = vad
        self.vad_threshold = vad_threshold
        self.vad_release_threshold = vad_release_threshold
        self.vad_probability = None
        self.frame_samples = getattr(vad, "frame_samples", FRAME_SAMPLES)
        if not isinstance(self.frame_samples, int) or not 1 <= self.frame_samples <= 1536:
            raise ValueError("Invalid VAD frame size")
        self.sound_threshold, self.release_threshold = sound_threshold, release_threshold
        self.pre_roll_samples = round(pre_roll_seconds * SAMPLE_RATE)
        self.end_silence_samples = max(1, round(end_silence_seconds * SAMPLE_RATE))
        self.min_sound_samples = max(1, round(min_sound_seconds * SAMPLE_RATE))
        self.tail_samples = round(tail_seconds * SAMPLE_RATE)
        self.max_active_samples = round(guard * SAMPLE_RATE)
        # VAD confirmation tolerates brief weak phonemes instead of requiring
        # every 20 ms frame to clear the user-selected onset loudness.
        self._candidate_gap_samples = round(0.096 * SAMPLE_RATE)
        self._candidate_limit_samples = 2 * self.min_sound_samples + self._candidate_gap_samples
        confirmation = self._candidate_limit_samples if vad else self.min_sound_samples
        self._ring_capacity = self.pre_roll_samples + confirmation + self.frame_samples
        self._ring = deque()
        self._ring_samples = 0
        self._pending = bytearray()
        self._position = 0
        self._candidate_start = 0
        self._candidate_samples = 0
        self._candidate_last_good = 0
        self._active: bytearray | None = None
        self._active_start = 0
        self._last_sound_end = 0
        self._finished = False

    def update_thresholds(self, sound_threshold: float, release_threshold: float):
        """Change thresholds between events without resetting sample timestamps."""
        if (isinstance(sound_threshold, bool) or isinstance(release_threshold, bool)
                or not isinstance(sound_threshold, (int, float)) or not isinstance(release_threshold, (int, float))
                or not math.isfinite(sound_threshold) or not math.isfinite(release_threshold)
                or not 0 < release_threshold <= sound_threshold < 1):
            raise ValueError("Require finite 0 < release_threshold <= sound_threshold < 1")
        if self._active is not None:
            raise RuntimeError("Activity thresholds can change only between sound segments")
        self.sound_threshold, self.release_threshold = sound_threshold, release_threshold
        # Samples below the old setting must not count toward confirmation at
        # the new setting. Keep pre-roll and absolute timeline intact.
        self._candidate_samples = 0

    @property
    def activity_state(self):
        return "recording" if self._active is not None else "listening"

    @property
    def buffered_bytes(self):
        return len(self._pending) + self._ring_samples * SAMPLE_WIDTH + (len(self._active) if self._active is not None else 0)

    def _remember(self, start: int, frame: bytes):
        self._ring.append((start, frame))
        self._ring_samples += len(frame) // SAMPLE_WIDTH
        while self._ring_samples > self._ring_capacity:
            begin, oldest = self._ring.popleft()
            samples = len(oldest) // SAMPLE_WIDTH
            excess = self._ring_samples - self._ring_capacity
            if samples > excess:
                self._ring.appendleft((begin + excess, oldest[excess * SAMPLE_WIDTH:]))
                self._ring_samples -= excess
            else:
                self._ring_samples -= samples

    def _emit(self, end_sample: int) -> ActivitySegment | None:
        if self._active is None:
            return None
        count = min(len(self._active) // SAMPLE_WIDTH, max(0, end_sample - self._active_start))
        result = ActivitySegment(bytes(self._active[:count * SAMPLE_WIDTH]), self._active_start) if count else None
        self._active = None
        self._candidate_samples = 0
        return result

    def _process(self, frame: bytes) -> ActivitySegment | None:
        if self.vad is not None:
            probability = self.vad.probability(frame)
            if not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
                raise RuntimeError("VAD returned an invalid speech probability")
            self.vad_probability = probability
        start = self._position
        self._position += len(frame) // SAMPLE_WIDTH
        self._remember(start, frame)
        samples = array.array("h", frame)
        if sys.byteorder != "little":
            samples.byteswap()
        rms = math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768
        if self._active is None:
            if self.vad is not None:
                if self._candidate_samples and (
                        self._position - self._candidate_last_good > self._candidate_gap_samples + len(samples)
                        or self._position - self._candidate_start > self._candidate_limit_samples):
                    self._candidate_samples = 0
                if self._candidate_samples:
                    confirmed = probability >= self.vad_release_threshold and rms >= self.release_threshold
                else:
                    confirmed = probability >= self.vad_threshold and rms >= self.sound_threshold
                if confirmed:
                    if self._candidate_samples == 0:
                        self._candidate_start = start
                    self._candidate_samples += len(samples)
                    self._candidate_last_good = self._position
            else:
                if rms >= self.sound_threshold:
                    if self._candidate_samples == 0:
                        self._candidate_start = start
                    self._candidate_samples += len(samples)
                else:
                    self._candidate_samples = 0
            if self._candidate_samples >= self.min_sound_samples:
                self._active_start = max(0, self._candidate_start - self.pre_roll_samples)
                self._active = bytearray()
                for frame_start, retained in self._ring:
                    skip = max(0, self._active_start - frame_start) * SAMPLE_WIDTH
                    if skip < len(retained):
                        self._active.extend(retained[skip:])
                self._last_sound_end = self._position
                self._candidate_samples = 0
        else:
            self._active.extend(frame)
            speech = self.vad is None or probability >= self.vad_release_threshold
            if speech and rms >= self.release_threshold:
                self._last_sound_end = self._position
            if self._position - self._last_sound_end >= self.end_silence_samples:
                return self._emit(self._last_sound_end + self.tail_samples)
        if self._active is not None and len(self._active) // SAMPLE_WIDTH >= self.max_active_samples:
            segment = self._emit(self._position)
            self._finished = True
            raise ActivityLimitError([segment] if segment else [])
        return None

    def feed(self, pcm: bytes) -> list[ActivitySegment]:
        """Accept arbitrary byte boundaries and return completed sound events."""
        if self._finished:
            raise ValueError("The activity gate has already finished")
        results = []
        cursor = 0
        frame_bytes = self.frame_samples * SAMPLE_WIDTH
        try:
            # Copy at most one frame at a time; an oversized input does not
            # expand the internal silent buffer to the size of that input.
            while cursor < len(pcm):
                take = min(frame_bytes - len(self._pending), len(pcm) - cursor)
                self._pending.extend(pcm[cursor:cursor + take])
                cursor += take
                if len(self._pending) == frame_bytes:
                    frame = bytes(self._pending)
                    self._pending.clear()
                    segment = self._process(frame)
                    if segment is not None:
                        results.append(segment)
        except ActivityLimitError as exc:
            exc.segments[:0] = results
            raise
        return results

    def finish(self) -> list[ActivitySegment]:
        """Flush a confirmed active event; discard idle/candidate-only audio."""
        if self._finished:
            return []
        results = []
        usable = len(self._pending) - len(self._pending) % SAMPLE_WIDTH
        try:
            if usable:
                segment = self._process(bytes(self._pending[:usable]))
                if segment is not None:
                    results.append(segment)
            segment = self._emit(min(self._position, self._last_sound_end + self.tail_samples))
            if segment is not None:
                results.append(segment)
        except ActivityLimitError as exc:
            exc.segments[:0] = results
            raise
        finally:
            self._pending.clear()
            self._finished = True
        return results
