"""On-demand only: capture never invokes speech recognition."""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from .store import assert_library_writable


@contextmanager
def asr_lock(root: Path):
    descriptor = os.open(root / ".asr.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("已有本地转写任务运行，请等它完成后重试。") from exc
        try:
            assert_library_writable(root)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _persist(store, result: dict) -> dict:
    with store._lock(exclusive=True):
        _prune_transcripts_locked(store)
        folder = store.root / "transcripts"
        if folder.is_symlink():
            raise ValueError("转写目录不能是符号链接。")
        folder.mkdir(mode=0o700, exist_ok=True)
        result = dict(result, id=uuid.uuid4().hex, created_at=time.time())
        path = folder / f"{result['id']}.json"
        temp = path.with_suffix(".tmp")
        try:
            with temp.open("x", encoding="utf-8") as handle:
                temp.chmod(0o600)
                json.dump(result, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return result


def prune_transcripts(store, retention_hours: float | None = None) -> int:
    with store._lock(exclusive=True):
        return _prune_transcripts_locked(store, retention_hours)


def _prune_transcripts_locked(store, retention_hours: float | None = None) -> int:
    count = 0
    retention_hours = store.retention_hours if retention_hours is None else retention_hours
    cutoff = time.time() - retention_hours * 3600
    for path in (store.root / "transcripts").glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                count += 1
        except FileNotFoundError:
            continue
    return count


def transcribe_clip(store, clip_id: str, *, model_path=None, language="auto") -> dict:
    from .asr import transcribe
    work = store.root / "work"
    work.mkdir(mode=0o700, exist_ok=True)
    with asr_lock(store.root), tempfile.TemporaryDirectory(dir=work) as temp:
        snapshot = Path(temp) / "clip.wav"
        clip = store.snapshot_clip(clip_id, output_path=snapshot)
        result = transcribe(snapshot, model_path=model_path, language=language)
        # The ASR lock covers persistence as well: a migration must not copy a
        # complete WAV while its already-finished transcript is still unsaved.
        metadata = {key: clip.get(key) for key in ("start_time", "end_time", "original_name")}
        name = clip.get("display_name") or clip.get("original_name")
        if not name:
            try:
                stamp = datetime.fromtimestamp(clip["start_time"]).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OverflowError, OSError):
                stamp = clip_id[:8]
            kind = "麦克风" if clip.get("source", "").startswith(("avfoundation:", "live:")) else "录音"
            name = f"{kind} {stamp}"
        return _persist(store, dict(result, clip_id=clip_id, source=clip.get("source"),
                                    display_name=name, **metadata))


def transcribe_recent(store, seconds: float = 30, *, last_utterance=False,
                      source=None, model_path=None, language="auto", now=None) -> dict:
    from .asr import transcribe
    if not 0 < seconds <= 600:
        raise ValueError("按需转写窗口必须大于 0 且不超过 600 秒。")
    work = store.root / "work"
    work.mkdir(mode=0o700, exist_ok=True)
    with asr_lock(store.root), tempfile.TemporaryDirectory(dir=work) as temp:
        output = Path(temp) / "request.wav"
        if last_utterance:
            utterance = store.last_utterance(lookback_seconds=seconds, now=now, source=source)
            if not utterance:
                raise ValueError("最近窗口内没有检测到讲话。可以改用固定秒数转写。")
            exported = store.export_range(utterance["start_time"], utterance["end_time"],
                                          output_path=output, source=source)
        else:
            exported = store.export_recent(seconds, output_path=output, now=now, source=source)
        result = transcribe(output, model_path=model_path, language=language)
        audio = {k: v for k, v in exported.items() if k != "path"}
        return _persist(store, dict(result, audio=audio, source=source,
                                    selection="last_utterance" if last_utterance else "recent"))


def recent_transcripts(store, limit: int = 10) -> list[dict]:
    if not 1 <= limit <= 100:
        raise ValueError("limit 必须在 1–100 之间。")
    cutoff = time.time() - store.retention_hours * 3600
    candidates = []
    for path in (store.root / "transcripts").glob("*.json"):
        try:
            modified = path.stat().st_mtime
            if modified >= cutoff:
                candidates.append((modified, path))
        except FileNotFoundError:
            continue
    results = []
    for _, path in sorted(candidates, reverse=True):
        try:
            results.append(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            continue
        if len(results) == limit:
            break
    return results


def copy_text(text: str) -> None:
    if not text.strip():
        raise ValueError("转写结果为空，未替换剪贴板。")
    subprocess.run(["/usr/bin/pbcopy"], input=text.encode("utf-8"), check=True, timeout=5)
