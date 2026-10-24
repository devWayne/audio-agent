"""Private, local WAV ring buffer shared by capture and on-demand readers.

All stored clips use 16 kHz mono PCM16. Database connections are short-lived and
WAL-backed; a filesystem lock also keeps pruning from racing an export's reads.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import array
import fcntl
import hashlib
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import wave

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1


class LibraryMovedError(RuntimeError):
    """A retained source library may be read, but must never accept new writes."""


def assert_library_writable(root: Path) -> None:
    # Any marker, including a damaged file or symlink, is a fail-closed fence.
    # Do not follow its contents to choose a new location for an old client.
    if os.path.lexists(root / ".moved-to.json"):
        raise LibraryMovedError("资料库已迁移，旧目录只保留备份。请重新连接或重启客户端以使用新目录。")


class AudioStore:
    def __init__(self, root: str | Path, retention_hours: float = 24, *, ffmpeg_path: str = "ffmpeg",
                 min_free_bytes: int = 256 * 1024 * 1024):
        if not math.isfinite(retention_hours) or retention_hours <= 0:
            raise ValueError("retention_hours must be positive")
        self.root = Path(root).expanduser().resolve()
        assert_library_writable(self.root)
        self.retention_hours = float(retention_hours)
        self.ffmpeg_path = ffmpeg_path
        if not isinstance(min_free_bytes, int) or min_free_bytes < 0:
            raise ValueError("min_free_bytes must be a non-negative integer")
        self.min_free_bytes = min_free_bytes
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("clips", "exports", "incoming"):
            directory = self.root / name
            if directory.is_symlink():
                raise ValueError(f"Data directory must not be a symlink: {directory}")
            directory.mkdir(exist_ok=True, mode=0o700)
        self.db_path = self.root / "audio.sqlite3"
        if self.db_path.is_symlink():
            raise ValueError("Audio database must not be a symlink")
        fd = os.open(self.db_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self._lock(exclusive=True), self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS clips (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                    start_time REAL NOT NULL, end_time REAL NOT NULL,
                    duration REAL NOT NULL, frames INTEGER NOT NULL,
                    sample_rate INTEGER NOT NULL, channels INTEGER NOT NULL,
                    source TEXT NOT NULL, sha256 TEXT NOT NULL,
                    import_sha256 TEXT UNIQUE, created_time REAL NOT NULL,
                    size_bytes INTEGER NOT NULL, original_name TEXT
                );
                CREATE INDEX IF NOT EXISTS clip_time ON clips(start_time, end_time);
                CREATE INDEX IF NOT EXISTS clip_source ON clips(source, start_time);
                CREATE TABLE IF NOT EXISTS exports (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                    created_time REAL NOT NULL
                );
            """)
            if "original_name" not in {row["name"] for row in db.execute("PRAGMA table_info(clips)")}:
                db.execute("ALTER TABLE clips ADD COLUMN original_name TEXT")
        self.db_path.chmod(0o600)

    @contextmanager
    def _lock(self, *, exclusive: bool):
        fd = os.open(self.root / ".store.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            if exclusive:
                assert_library_writable(self.root)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _owned_path(self, relative: str, directory: str) -> Path:
        path = self.root / relative
        if path.parent != self.root / directory or path.is_symlink():
            raise ValueError("Invalid stored audio path")
        resolved = path.resolve()
        if resolved.parent != self.root / directory:
            raise ValueError("Stored audio path leaves data directory")
        return resolved

    def _clip_dict(self, row) -> dict:
        result = dict(row)
        result["path"] = str(self._owned_path(result["path"], "clips"))
        return result

    def ensure_space(self, bytes_needed: int = 0) -> int:
        """Keep a disk reserve and fail before writing the machine's last space."""
        assert_library_writable(self.root)
        free = shutil.disk_usage(self.root).free
        if free < self.min_free_bytes + max(0, bytes_needed):
            raise RuntimeError(
                f"Insufficient disk space for audio: {free // (1024 * 1024)} MiB free; "
                f"keeping a {self.min_free_bytes // (1024 * 1024)} MiB reserve")
        return free

    @staticmethod
    def _new_private_file(path: Path):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        return os.fdopen(fd, "wb")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as audio:
            for block in iter(lambda: audio.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _wav_info(path: Path) -> dict:
        try:
            with wave.open(str(path), "rb") as audio:
                if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH, "NONE"):
                    raise ValueError("WAV needs normalization to mono 16 kHz PCM16")
                frames = audio.getnframes()
                if frames <= 0:
                    raise ValueError("WAV contains no audio frames")
                # Inspect actual bytes too: a truncated header must not create a
                # clip that claims time which was never recorded.
                actual = 0
                while block := audio.readframes(65536):
                    actual += len(block)
                if actual != frames * SAMPLE_WIDTH:
                    raise ValueError("WAV is truncated or has incomplete samples")
                return {"frames": frames, "duration": frames / SAMPLE_RATE,
                        "sample_rate": SAMPLE_RATE, "channels": CHANNELS}
        except (wave.Error, EOFError) as exc:
            raise ValueError(f"Unsupported or invalid WAV: {exc}") from exc

    def _insert_file(self, temp: Path, *, start_time: float, source: str, import_sha256: str | None = None,
                     original_name: str | None = None) -> dict:
        if not math.isfinite(start_time):
            raise ValueError("start_time must be finite")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be non-empty")
        info = self._wav_info(temp)
        clip_id = uuid.uuid4().hex
        relative = f"clips/{clip_id}.wav"
        path = self.root / relative
        with self._lock(exclusive=True), self._connect() as db:
            if import_sha256:
                existing = db.execute("SELECT * FROM clips WHERE import_sha256=?", (import_sha256,)).fetchone()
                if existing:
                    result = self._clip_dict(existing)
                    result["deduplicated"] = True
                    return result
            temp.chmod(0o600)
            digest = self._sha256(temp)
            size = temp.stat().st_size
            os.replace(temp, path)
            try:
                db.execute("""INSERT INTO clips
                    (id,path,start_time,end_time,duration,frames,sample_rate,channels,
                     source,sha256,import_sha256,created_time,size_bytes,original_name)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (clip_id, relative, start_time, start_time + info["duration"],
                     info["duration"], info["frames"], SAMPLE_RATE, CHANNELS, source,
                     digest, import_sha256, time.time(), size, original_name))
                db.commit()
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            result = self._clip_dict(db.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone())
        result["deduplicated"] = False
        return result

    def add_pcm(self, pcm: bytes, *, start_time: float, source: str,
                sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS) -> dict:
        """Commit complete little-endian signed PCM16 samples as one clip."""
        if sample_rate != SAMPLE_RATE or channels != CHANNELS:
            raise ValueError("Stored PCM must be mono 16000 Hz PCM16")
        if not pcm or len(pcm) % SAMPLE_WIDTH:
            raise ValueError("PCM must contain whole, non-empty 16-bit samples")
        self.ensure_space(len(pcm) + 44)
        temp = self.root / "incoming" / f"{uuid.uuid4().hex}.wav"
        try:
            with self._new_private_file(temp) as raw:
                os.chmod(temp, 0o600)
                with wave.open(raw, "wb") as audio:
                    audio.setnchannels(CHANNELS)
                    audio.setsampwidth(SAMPLE_WIDTH)
                    audio.setframerate(SAMPLE_RATE)
                    audio.writeframes(pcm)
                raw.flush()
                os.fsync(raw.fileno())
            return self._insert_file(temp, start_time=float(start_time), source=source)
        finally:
            temp.unlink(missing_ok=True)

    def import_wav(self, path: str | Path, source: str = "import", *, start_time: float | None = None,
                   original_name: str | None = None) -> dict:
        """Import a stable WAV snapshot, normalizing locally when necessary.

        File modification time estimates the recording end unless start_time is
        supplied. Imported files remain for retention_hours after arrival, even
        when their original recording timestamps are old.
        """
        source_path = Path(path).expanduser().resolve(strict=True)
        name = Path(original_name or source_path.name).name[:255]
        if any(char in name for char in "\x00\r\n"):
            raise ValueError("Invalid original WAV filename")
        if not source_path.is_file():
            raise ValueError("WAV input must be a file")
        snapshot = self.root / "incoming" / f"{uuid.uuid4().hex}.source.wav"
        temp = self.root / "incoming" / f"{uuid.uuid4().hex}.wav"
        def identity(stat):
            return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        try:
            with source_path.open("rb") as src:
                initial = os.fstat(src.fileno())
                signature = src.read(12)
                if len(signature) < 12 or signature[:4] not in (b"RIFF", b"RF64", b"BW64") or signature[8:12] != b"WAVE":
                    raise ValueError("Input must be a WAV file")
                self.ensure_space(initial.st_size * 2)
                src.seek(0)
                with self._new_private_file(snapshot) as target:
                    shutil.copyfileobj(src, target)
                    target.flush()
                    os.fsync(target.fileno())
                if identity(initial) != identity(os.fstat(src.fileno())) or identity(initial) != identity(source_path.stat()):
                    raise ValueError("Input WAV changed while reading; retry once it is stable")
            original_hash = self._sha256(snapshot)
            with self._lock(exclusive=False), self._connect() as db:
                existing = db.execute("SELECT * FROM clips WHERE import_sha256=?", (original_hash,)).fetchone()
                if existing:
                    result = self._clip_dict(existing)
                    result["deduplicated"] = True
                    return result
            try:
                info = self._wav_info(snapshot)
                os.replace(snapshot, temp)
            except ValueError:
                # Decode the immutable snapshot, never the mutable source file.
                try:
                    result = subprocess.run(
                        [self.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin",
                         "-i", str(snapshot), "-vn", "-ac", "1", "-ar", "16000",
                         "-c:a", "pcm_s16le", "-f", "wav", "-n", str(temp)],
                        capture_output=True, text=True, timeout=600, check=False)
                except FileNotFoundError as exc:
                    raise RuntimeError("Install ffmpeg to import this WAV format") from exc
                if result.returncode:
                    raise ValueError(f"Cannot normalize WAV: {result.stderr.strip()[-2000:]}")
                temp.chmod(0o600)
                info = self._wav_info(temp)
            if identity(initial) != identity(source_path.stat()):
                raise ValueError("Input WAV changed while importing; retry once it is stable")
            start = float(start_time) if start_time is not None else initial.st_mtime - info["duration"]
            return self._insert_file(temp, start_time=start, source=source, import_sha256=original_hash,
                                     original_name=name)
        finally:
            snapshot.unlink(missing_ok=True)
            temp.unlink(missing_ok=True)

    def snapshot_clip(self, clip_id: str, output_path: str | Path | None = None) -> dict:
        """Copy one canonical clip while holding the prune lock for safe ASR."""
        export_id = uuid.uuid4().hex
        managed = output_path is None
        output = self.root / "exports" / f"{export_id}.wav" if managed else Path(output_path).expanduser().absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        created = False
        with self._lock(exclusive=True):
            clip = self.get_clip(clip_id)
            if clip is None:
                raise ValueError(f"Audio clip was not found or has expired: {clip_id}")
            self.ensure_space(clip["size_bytes"])
            try:
                with open(clip["path"], "rb") as source, self._new_private_file(output) as target:
                    created = True
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                if managed:
                    with self._connect() as db:
                        db.execute("INSERT INTO exports VALUES (?,?,?)", (export_id, f"exports/{export_id}.wav", time.time()))
            except BaseException:
                if created:
                    output.unlink(missing_ok=True)
                raise
        return dict(clip, original_path=clip["path"], path=str(output))

    def get_clip(self, clip_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        return self._clip_dict(row) if row else None

    def list_clips(self, limit: int = 100, source: str | None = None) -> list[dict]:
        if not 1 <= limit <= 10000:
            raise ValueError("limit must be between 1 and 10000")
        where = " WHERE source=?" if source is not None else ""
        args = (source, limit) if source is not None else (limit,)
        with self._connect() as db:
            rows = db.execute(f"SELECT * FROM clips{where} ORDER BY start_time DESC,id LIMIT ?", args).fetchall()
        return [self._clip_dict(row) for row in rows]

    def recent(self, seconds: float = 300, now: float | None = None, source: str | None = None) -> list[dict]:
        end = time.time() if now is None else float(now)
        if not math.isfinite(seconds) or seconds <= 0 or not math.isfinite(end):
            raise ValueError("seconds must be positive and timestamps finite")
        query = "SELECT * FROM clips WHERE end_time>? AND start_time<?"
        args: list = [end - seconds, end]
        if source is not None:
            query += " AND source=?"
            args.append(source)
        with self._connect() as db:
            rows = db.execute(query + " ORDER BY start_time,id", args).fetchall()
        return [self._clip_dict(row) for row in rows]

    def export_recent(self, seconds: float, *, output_path: str | Path | None = None,
                      now: float | None = None, source: str | None = None) -> dict:
        end = time.time() if now is None else float(now)
        return self.export_range(end - seconds, end, output_path=output_path, source=source)

    def export_range(self, start_time: float, end_time: float, *,
                     output_path: str | Path | None = None, source: str | None = None) -> dict:
        """Export a wall-clock interval, concatenating audio without silent gaps.

        Returned ``segments`` map output offsets to original wall-clock times.
        For overlapping clips the earlier clip wins. Select ``source`` when
        multiple microphones/imports overlap; audio is deliberately never mixed.
        """
        start, end = float(start_time), float(end_time)
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ValueError("end_time must be after start_time, both finite")
        export_id = uuid.uuid4().hex
        managed = output_path is None
        output = self.root / "exports" / f"{export_id}.wav" if managed else Path(output_path).expanduser().absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        segments: list[dict] = []
        gaps: list[dict] = []
        cursor = start
        total_frames = 0
        created = False
        # An exclusive lock permits registering exports without an upgrade
        # deadlock and ensures clip files stay alive for the whole read.
        with self._lock(exclusive=True):
            clips = self.recent(end - start, now=end, source=source)
            if not clips:
                raise ValueError("No recorded audio overlaps the requested interval")
            self.ensure_space(sum(clip["size_bytes"] for clip in clips))
            try:
                with self._new_private_file(output) as raw:
                    created = True
                    os.chmod(output, 0o600)
                    with wave.open(raw, "wb") as target:
                        target.setnchannels(CHANNELS)
                        target.setsampwidth(SAMPLE_WIDTH)
                        target.setframerate(SAMPLE_RATE)
                        for clip in clips:
                            slice_start = max(start, cursor, clip["start_time"])
                            slice_end = min(end, clip["end_time"])
                            if slice_end <= slice_start:
                                continue
                            # Round frame boundaries to nearest sample to avoid
                            # floating-point epoch arithmetic dropping samples.
                            first = max(0, round((slice_start - clip["start_time"]) * SAMPLE_RATE))
                            last = min(clip["frames"], round((slice_end - clip["start_time"]) * SAMPLE_RATE))
                            if last <= first:
                                continue
                            actual_start = clip["start_time"] + first / SAMPLE_RATE
                            actual_end = clip["start_time"] + last / SAMPLE_RATE
                            if actual_start - cursor > 1 / SAMPLE_RATE:
                                gaps.append({"start_time": cursor, "end_time": actual_start, "duration": actual_start - cursor})
                            with wave.open(clip["path"], "rb") as audio:
                                audio.setpos(first)
                                remaining = last - first
                                while remaining:
                                    requested = min(remaining, 65536)
                                    data = audio.readframes(requested)
                                    if len(data) != requested * SAMPLE_WIDTH:
                                        raise ValueError(f"Stored clip is truncated: {clip['id']}")
                                    target.writeframesraw(data)
                                    remaining -= requested
                            count = last - first
                            segments.append({"clip_id": clip["id"], "source": clip["source"],
                                             "start_time": actual_start, "end_time": actual_end,
                                             "output_start": total_frames / SAMPLE_RATE,
                                             "output_end": (total_frames + count) / SAMPLE_RATE})
                            total_frames += count
                            cursor = actual_end
                        if not total_frames:
                            raise ValueError("No complete audio samples overlap the requested interval")
                        if end - cursor > 1 / SAMPLE_RATE:
                            gaps.append({"start_time": cursor, "end_time": end, "duration": end - cursor})
                    raw.flush()
                    os.fsync(raw.fileno())
                if managed:
                    with self._connect() as db:
                        db.execute("INSERT INTO exports VALUES (?,?,?)", (export_id, f"exports/{export_id}.wav", time.time()))
            except BaseException:
                if created:
                    output.unlink(missing_ok=True)
                raise
        return {"path": str(output), "start_time": segments[0]["start_time"],
                "end_time": segments[-1]["end_time"], "requested_start_time": start,
                "requested_end_time": end, "duration": total_frames / SAMPLE_RATE,
                "sample_rate": SAMPLE_RATE, "channels": CHANNELS,
                "gaps": gaps, "segments": segments, "gap_policy": "omit"}

    def last_utterance(self, *, lookback_seconds: float = 60, now: float | None = None,
                       source: str | None = None, threshold: float = 0.015,
                       silence_seconds: float = 0.6, padding_seconds: float = 0.15) -> dict | None:
        """Find the latest energy activity; this is not a speech classifier.

        RMS is measured in 20 ms frames and separated by quiet intervals or
        recording gaps. This only suggests a time range; it never transcribes.
        """
        if not 0 < threshold < 1 or silence_seconds <= 0 or padding_seconds < 0:
            raise ValueError("Invalid energy activity parameters")
        end = time.time() if now is None else float(now)
        start = end - lookback_seconds
        events: list[tuple[float, float]] = []
        cursor = start
        with self._lock(exclusive=False):
            for clip in self.recent(lookback_seconds, now=end, source=source):
                first = max(0, round((max(start, cursor) - clip["start_time"]) * SAMPLE_RATE))
                last = min(clip["frames"], round((end - clip["start_time"]) * SAMPLE_RATE))
                if last <= first:
                    continue
                with wave.open(clip["path"], "rb") as audio:
                    audio.setpos(first)
                    offset = first
                    while offset < last:
                        pcm = audio.readframes(min(320, last - offset))
                        samples = array.array("h", pcm)
                        if sys.byteorder != "little":
                            samples.byteswap()
                        if not samples:
                            break
                        rms = math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768
                        begin = clip["start_time"] + offset / SAMPLE_RATE
                        finish = begin + len(samples) / SAMPLE_RATE
                        if rms >= threshold:
                            events.append((begin, finish))
                        offset += len(samples)
                cursor = min(end, clip["end_time"])
        if not events:
            return None
        utterance_start, utterance_end = events[-1]
        for begin, finish in reversed(events[:-1]):
            if utterance_start - finish >= silence_seconds:
                break
            utterance_start = begin
        return {"start_time": max(start, utterance_start - padding_seconds),
                "end_time": min(end, utterance_end + padding_seconds),
                "detector": "energy", "threshold": threshold,
                "note": "Energy activity may include music and noise; this is not speech recognition."}

    def prune(self, now: float | None = None) -> dict:
        current = time.time() if now is None else float(now)
        if not math.isfinite(current):
            raise ValueError("now must be finite")
        cutoff = current - self.retention_hours * 3600
        removed = []
        errors = []
        with self._lock(exclusive=True), self._connect() as db:
            rows = [(row["id"], row["path"], "clips") for row in db.execute("SELECT id,path FROM clips WHERE (import_sha256 IS NULL AND end_time<=?) OR (import_sha256 IS NOT NULL AND created_time<=?)", (cutoff, cutoff))]
            rows += [(row["id"], row["path"], "exports") for row in db.execute("SELECT id,path FROM exports WHERE created_time<=?", (cutoff,))]
            # Validate *all* paths before any destructive mutation.
            paths = [(item_id, self._owned_path(path, table), table) for item_id, path, table in rows]
            speaker_paths = {item_id: self._owned_path(f"speakers/{item_id}.json", "speakers")
                             for item_id, _, table in rows if table == "clips"}
            for item_id, path, table in paths:
                try:
                    path.unlink(missing_ok=True)
                    if table == "clips":
                        speaker_paths[item_id].unlink(missing_ok=True)
                except OSError as exc:
                    errors.append({"path": str(path), "error": str(exc)})
                    continue
                db.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
                removed.append(str(path))
        return {"removed": len(removed), "paths": removed, "errors": errors, "cutoff": cutoff}

    def status(self) -> dict:
        with self._connect() as db:
            row = db.execute("""SELECT COUNT(*) AS clip_count, COALESCE(SUM(duration),0) AS duration,
                                COALESCE(SUM(size_bytes),0) AS size_bytes,
                                MIN(start_time) AS oldest_start_time, MAX(end_time) AS latest_end_time
                                FROM clips""").fetchone()
        return {"root": str(self.root), "retention_hours": self.retention_hours,
                "min_free_bytes": self.min_free_bytes, "free_bytes": shutil.disk_usage(self.root).free,
                "format": "16000 Hz mono PCM16 WAV", **dict(row)}
