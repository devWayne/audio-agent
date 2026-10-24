"""Prepare a private library copy while fencing every cooperating writer.

This module never changes a global setting or starts/stops a service. The caller
must first make its own managers idle and serialize HTTP requests. It may build
new managers inside the yielded context, call ``mark_committed()``, then commit
its configuration and runtime. An exception rolls back our marker and copy;
the original library is retained. Existing CLI/MCP clients must reconnect.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid

from .store import AudioStore, assert_library_writable


_ID = re.compile(r"[0-9a-f]{32}\Z")
_PRIVATE_FILES = ("voiceprint.json", "speaker-profiles.json", "live-settings.json", "speaker-settings.json")
_DIRECTORIES = ("clips", "exports", "transcripts", "speakers", "incoming")


class MigrationBusyError(RuntimeError):
    """A cooperating capture/inference process still owns this library."""


def _identity(info):
    return info.st_dev, info.st_ino


def _signature(info):
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"资料库文件不是普通文件，不能迁移：{path.name}")
    return info


def _safe_directory(path, *, required=True):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise ValueError(f"目录不存在：{path}") from None
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"目录不能是符号链接或其他文件：{path}")
    return info


def _target_path(source, directory):
    if not isinstance(directory, (str, Path)) or not str(directory).strip():
        raise ValueError("请选择一个空目录或新的绝对目录。")
    target = Path(directory).expanduser()
    if not target.is_absolute() or ".." in target.parts:
        raise ValueError("迁移目标必须是绝对路径，且不能包含上级目录跳转。")
    # Resolve only after checking every existing component, so a symlink never
    # silently changes the user's chosen destination or escapes an ancestor.
    for ancestor in reversed((target, *target.parents)):
        if os.path.lexists(ancestor):
            _safe_directory(ancestor)
    target = target.resolve()
    if target == source or source in target.parents or target in source.parents:
        raise ValueError("新旧资料库不能相同，也不能互为父子目录。")
    parent_info = _safe_directory(target.parent)
    target_info = _safe_directory(target, required=False)
    if target_info and any(target.iterdir()):
        raise ValueError("迁移目标必须是空目录；现有文件不会被覆盖。")
    return target, parent_info, target_info


@contextmanager
def _process_lock(root, name, label):
    descriptor = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("资料库锁文件无效。")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationBusyError(f"{label}仍在运行，请停止并等待任务完成后迁移。") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _manifest(store, database):
    files = {}
    for name in _DIRECTORIES:
        _safe_directory(store.root / name, required=name in ("clips", "exports", "incoming"))
    for table in ("clips", "exports"):
        for row in database.execute(f"SELECT id,path FROM {table}"):
            if (not isinstance(row["id"], str) or not _ID.fullmatch(row["id"])
                    or row["path"] != f"{table}/{row['id']}.wav"):
                raise ValueError("数据库包含无效的录音路径，未迁移任何资料。")
            relative = Path(row["path"])
            files[relative] = _regular(store.root / relative)
    for directory in ("transcripts", "speakers"):
        folder = store.root / directory
        if folder.exists():
            for path in folder.iterdir():
                if path.suffix == ".json" and _ID.fullmatch(path.stem):
                    files[Path(directory) / path.name] = _regular(path)
    for name in _PRIVATE_FILES:
        path = store.root / name
        if os.path.lexists(path):
            files[Path(name)] = _regular(path)
    return files


def _copy_file(source, target, expected):
    parent = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as incoming:
            before = os.fstat(incoming.fileno())
            if not stat.S_ISREG(before.st_mode) or _signature(before) != _signature(expected):
                raise RuntimeError("复制期间资料库文件发生变化，请重试。")
            output = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(output, "wb") as outgoing:
                remaining = before.st_size
                while remaining:
                    block = incoming.read(min(remaining, 1024 * 1024))
                    if not block:
                        raise RuntimeError("复制期间资料库文件发生变化，请重试。")
                    outgoing.write(block)
                    remaining -= len(block)
                if incoming.read(1):
                    raise RuntimeError("复制期间资料库文件发生变化，请重试。")
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if (_signature(os.fstat(incoming.fileno())) != _signature(before)
                    or _signature(os.stat(source.name, dir_fd=parent, follow_symlinks=False)) != _signature(before)):
                raise RuntimeError("复制期间资料库文件发生变化，请重试。")
    finally:
        os.close(parent)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns), follow_symlinks=False)


def _backup_database(database, destination, source_info):
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    target = sqlite3.connect(destination)
    try:
        database.backup(target)
        # Keep the store's journal mode so merely constructing AudioStore does
        # not alter the published database. Closing the last connection below
        # checkpoints its *new* WAL; no source WAL/SHM is copied or required.
        target.execute("PRAGMA journal_mode=WAL")
        if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError("迁移后的数据库完整性检查未通过。")
        target.commit()
    finally:
        target.close()
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    os.utime(destination, ns=(source_info.st_atime_ns, source_info.st_mtime_ns))


@dataclass
class PreparedLibraryCopy:
    path: Path
    files: int
    bytes: int
    _source: Path = field(repr=False)
    _operation: str = field(default_factory=lambda: uuid.uuid4().hex, repr=False)
    _marker_identity: tuple | None = field(default=None, repr=False)
    _committed: bool = field(default=False, repr=False)
    _open: bool = field(default=True, repr=False)

    def mark_committed(self):
        """Write the source fence BEFORE the caller commits config/runtime.

        The context must still be open. A later exception removes this marker;
        a normal exit retains it. This method does not update global settings.
        """
        if not self._open:
            raise RuntimeError("迁移事务已经结束。")
        if self._committed:
            return
        assert_library_writable(self._source)
        marker = self._source / ".moved-to.json"
        temporary = self._source / f".moved-to-{self._operation}.tmp"
        payload = {"version": 1, "directory": str(self.path), "moved_at": time.time(),
                   "operation_id": self._operation}
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            # link publishes the complete marker without replacing any marker
            # another process/user may have created in the meantime.
            os.link(temporary, marker, follow_symlinks=False)
            self._marker_identity = _identity(marker.lstat())
            _sync_directory(self._source)
            self._committed = True
        finally:
            temporary.unlink(missing_ok=True)

    def _remove_marker(self):
        marker = self._source / ".moved-to.json"
        try:
            if self._marker_identity == _identity(marker.lstat()):
                marker.unlink()
                _sync_directory(self._source)
        except FileNotFoundError:
            pass


def _rollback_target(target, identity, files, directories, previous_info):
    """Remove only our unchanged files; never recursively delete a user's data."""
    try:
        if _identity(target.lstat()) != identity or target.is_symlink():
            return
    except FileNotFoundError:
        return
    for relative, expected in files.items():
        path = target / relative
        try:
            # A replaced/edited file now belongs to someone else. Leave it, and
            # any added file, in place instead of risking data loss on rollback.
            actual = path.lstat()
            if (_identity(actual), actual.st_size, actual.st_mtime_ns) == expected:
                path.unlink()
        except FileNotFoundError:
            pass
    for relative, expected in sorted(directories.items(), key=lambda item: len(item[0].parts), reverse=True):
        path = target / relative
        try:
            if _identity(path.lstat()) == expected:
                path.rmdir()
        except (FileNotFoundError, OSError):
            pass
    if previous_info is None:
        try:
            target.rmdir()
        except OSError:
            pass
    else:
        target.chmod(stat.S_IMODE(previous_info.st_mode))


@contextmanager
def prepared_library_copy(store: AudioStore, directory: str | Path):
    """Yield a complete copy, holding capture/ASR/store locks until commit.

    The destination's parent must already exist. The destination must be new or
    empty, with no symlink components. No microphone, inference or cleanup of
    retained source material is performed by this operation.
    """
    target, parent_info, previous_info = _target_path(store.root, directory)
    stage = None
    prepared = None
    published = False
    completed = False
    target_identity = None
    copied = {}
    directories = {}
    with ExitStack() as locks:
        locks.enter_context(_process_lock(store.root, ".capture.lock", "麦克风采集"))
        locks.enter_context(_process_lock(store.root, ".asr.lock", "本地模型任务"))
        locks.enter_context(store._lock(exclusive=True))
        try:
            source_info = _regular(store.db_path)
            with store._connect() as database:
                files = _manifest(store, database)
                database_bytes = database.execute("PRAGMA page_count").fetchone()[0] * database.execute("PRAGMA page_size").fetchone()[0]
                required = sum(info.st_size for info in files.values()) + database_bytes
                if shutil.disk_usage(target.parent).free < required + store.min_free_bytes:
                    raise RuntimeError("目标磁盘可用空间不足，无法复制资料库并保留安全余量。")
                stage = Path(tempfile.mkdtemp(prefix=".audio-agent-migration-", dir=target.parent))
                stage.chmod(0o700)
                for name in _DIRECTORIES:
                    (stage / name).mkdir(mode=0o700)
                # Fresh, unlocked files belong to this transaction. Never copy
                # source locks. Runtime initialization can open these files
                # without leaving untracked artifacts after a failed commit.
                for name in (".store.lock", ".capture.lock", ".asr.lock"):
                    descriptor = os.open(stage / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(descriptor)
                _backup_database(database, stage / "audio.sqlite3", source_info)
                for relative, info in files.items():
                    _copy_file(store.root / relative, stage / relative, info)
            for path in stage.rglob("*"):
                info = path.lstat()
                relative = path.relative_to(stage)
                if stat.S_ISREG(info.st_mode):
                    copied[relative] = (_identity(info), info.st_size, info.st_mtime_ns)
                elif stat.S_ISDIR(info.st_mode):
                    directories[relative] = _identity(info)
            for relative in directories:
                _sync_directory(stage / relative)
            _sync_directory(stage)
            # Recheck the parent/empty destination immediately before publish.
            checked, checked_parent, checked_target = _target_path(store.root, target)
            if checked != target or _identity(checked_parent) != _identity(parent_info):
                raise RuntimeError("迁移目标目录发生变化，请重新选择。")
            if ((previous_info is None) != (checked_target is None)
                    or previous_info and _identity(previous_info) != _identity(checked_target)):
                raise RuntimeError("迁移目标目录发生变化，请重新选择。")
            os.replace(stage, target)
            stage = None
            published = True
            target_identity = _identity(target.lstat())
            _sync_directory(target.parent)
            prepared = PreparedLibraryCopy(target, len(files) + 1, sum(item[1] for item in copied.values()), store.root)
            yield prepared
            completed = prepared._committed
        finally:
            if prepared:
                prepared._open = False
            if not completed:
                if prepared:
                    prepared._remove_marker()
                if published:
                    _rollback_target(target, target_identity, copied, directories, previous_info)
            if stage is not None:
                shutil.rmtree(stage)
