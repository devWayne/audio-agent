"""Isolated storage transactions; no production library, mic or inference."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct
from threading import Event
import time
import wave

import pytest

from audio_agent import asr, pipeline, storage_migration as migration
from audio_agent.store import AudioStore, LibraryMovedError


@pytest.fixture
def store(tmp_path):
    return AudioStore(tmp_path / "original", min_free_bytes=0)


def pcm(seconds=0.25):
    return struct.pack("<h", 1200) * round(16000 * seconds)


def seed(store):
    clip = store.add_pcm(pcm(), start_time=time.time(), source="live:test")
    exported = store.snapshot_clip(clip["id"])
    transcript = pipeline._persist(store, {"clip_id": clip["id"], "text": "完整的测试文本", "segments": []})
    (store.root / "speakers").mkdir()
    (store.root / "speakers" / f"{clip['id']}.json").write_text('{"private_test_vector": [1, 0]}')
    for name in migration._PRIVATE_FILES:
        (store.root / name).write_text('{"test_setting": true}')
    transcript_path = store.root / "transcripts" / f"{transcript['id']}.json"
    timestamp = time.time_ns() - 100_000_000_000
    os.utime(transcript_path, ns=(timestamp, timestamp))
    return clip, exported, transcript_path


def test_copy_whitelist_preserves_ids_bytes_timestamps_privacy_and_wal(store, tmp_path):
    # Keep a connection open so the later committed clip is represented by a
    # live WAL, not just the on-disk main database that a naive copy would read.
    reader = sqlite3.connect(store.db_path)
    reader.execute("SELECT COUNT(*) FROM clips").fetchone()
    try:
        clip, exported, transcript_path = seed(store)
        assert (store.root / "audio.sqlite3-wal").stat().st_size > 0
        for name in ("models", "test-fixtures", "work", "logs", "web-qa"):
            folder = store.root / name
            folder.mkdir(exist_ok=True)
            (folder / "not-library.txt").write_text("do not copy")
        (store.root / "incoming" / "uncommitted.wav").write_bytes(b"incomplete")
        (store.root / "clips" / ("f" * 32 + ".wav")).write_bytes(b"unreferenced")
        (store.root / "transcripts" / "scratch.json").write_text("{}")
        target = tmp_path / "destination"
        with migration.prepared_library_copy(store, target) as prepared:
            assert prepared.path == target
            assert prepared.files == 9  # DB, two WAV, transcript, analysis, four settings.
            assert prepared.bytes > len(pcm()) * 2
            assert not (store.root / ".moved-to.json").exists()
            copied = AudioStore(target, min_free_bytes=0)
            assert copied.get_clip(clip["id"])["id"] == clip["id"]
            assert Path(copied.get_clip(clip["id"])["path"]).read_bytes() == Path(clip["path"]).read_bytes()
            assert (target / "exports" / Path(exported["path"]).name).read_bytes() == Path(exported["path"]).read_bytes()
            assert (target / "transcripts" / transcript_path.name).stat().st_mtime_ns == transcript_path.stat().st_mtime_ns
            assert not any((target / name).exists() for name in ("models", "test-fixtures", "work", "logs", "web-qa"))
            assert list((target / "incoming").iterdir()) == []
            assert not (target / "clips" / ("f" * 32 + ".wav")).exists()
            assert not (target / "transcripts" / "scratch.json").exists()
            for path in target.rglob("*"):
                assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
            prepared.mark_committed()
            prepared.mark_committed()  # Idempotent within the same transaction.
        assert json.loads((store.root / ".moved-to.json").read_text())["directory"] == str(target)
        assert Path(clip["path"]).exists() and transcript_path.exists()
        assert store.get_clip(clip["id"])["id"] == clip["id"]  # Existing readers can inspect backup.
        with pytest.raises(LibraryMovedError):
            AudioStore(store.root)
        with pytest.raises(LibraryMovedError):
            store.add_pcm(pcm(), start_time=time.time(), source="old-client")
        with pytest.raises(LibraryMovedError):
            store.prune()
        with pytest.raises(LibraryMovedError):
            pipeline._persist(store, {"text": "old client"})
        with pytest.raises(LibraryMovedError):
            pipeline.prune_transcripts(store)
        with pytest.raises(LibraryMovedError):
            with pipeline.asr_lock(store.root):
                pytest.fail("must fail before running a model")
    finally:
        reader.close()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("fail_after_mark", [False, True])
def test_rollback_without_commit_or_after_config_failure(store, tmp_path, existing, fail_after_mark):
    clip, _, _ = seed(store)
    target = tmp_path / "destination"
    if existing:
        target.mkdir(mode=0o750)
    try:
        with migration.prepared_library_copy(store, target) as prepared:
            # Match Web's construction before mark/config/runtime replacement.
            AudioStore(target, min_free_bytes=0)
            if fail_after_mark:
                prepared.mark_committed()
                raise OSError("configuration write failed")
    except OSError as error:
        assert str(error) == "configuration write failed"
    assert not (store.root / ".moved-to.json").exists()
    assert (target.exists() and list(target.iterdir()) == []) if existing else not target.exists()
    if existing:
        assert stat.S_IMODE(target.stat().st_mode) == 0o750
    assert store.get_clip(clip["id"])
    assert store.add_pcm(pcm(), start_time=time.time(), source="after-rollback")
    assert not list(tmp_path.glob(".audio-agent-migration-*"))


def test_rollback_never_deletes_files_added_or_replaced_by_user(store, tmp_path):
    clip, _, _ = seed(store)
    target = tmp_path / "destination"
    with pytest.raises(RuntimeError, match="abort"):
        with migration.prepared_library_copy(store, target):
            (target / "user-notes.txt").write_text("keep me")
            path = target / "clips" / Path(clip["path"]).name
            path.unlink()
            path.write_bytes(b"user replacement")
            raise RuntimeError("abort")
    assert (target / "user-notes.txt").read_text() == "keep me"
    assert path.read_bytes() == b"user replacement"
    assert not (store.root / ".moved-to.json").exists()


@pytest.mark.parametrize("lock", [".capture.lock", ".asr.lock"])
def test_external_recorder_or_model_lock_rejects_without_copy(store, tmp_path, lock):
    target = tmp_path / "destination"
    with (store.root / lock).open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(migration.MigrationBusyError):
            with migration.prepared_library_copy(store, target):
                pytest.fail("busy library must not yield")
    assert not target.exists()
    with migration.prepared_library_copy(store, target):
        assert target.exists()  # All earlier acquired locks were released.


def test_all_locks_remain_held_until_context_exit(store, tmp_path):
    with migration.prepared_library_copy(store, tmp_path / "destination"):
        for name in (".capture.lock", ".asr.lock", ".store.lock"):
            with (store.root / name).open("a") as handle:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("kind", ["same", "child", "parent", "nonempty", "symlink", "symlink-parent", "missing-parent", "relative"])
def test_rejects_unsafe_destinations_without_mutation(store, tmp_path, kind):
    target = tmp_path / "destination"
    if kind == "same":
        target = store.root
    elif kind == "child":
        target = store.root / "nested"
    elif kind == "parent":
        target = store.root.parent
    elif kind == "nonempty":
        target.mkdir()
        (target / "keep.txt").write_text("keep")
    elif kind == "symlink":
        target.symlink_to(store.root, target_is_directory=True)
    elif kind == "symlink-parent":
        target.symlink_to(store.root.parent, target_is_directory=True)
        target = target / "nested"
    elif kind == "missing-parent":
        target = target / "nested"
    elif kind == "relative":
        target = Path("relative-library")
    with pytest.raises(ValueError):
        with migration.prepared_library_copy(store, target):
            pytest.fail("invalid target must not yield")
    assert not (store.root / ".moved-to.json").exists()
    if kind == "nonempty":
        assert (target / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize("kind", ["clip-symlink", "folder-symlink", "profile-symlink", "database-path"])
def test_rejects_invalid_library_paths(store, tmp_path, kind):
    clip, _, _ = seed(store)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside remains intact")
    if kind == "clip-symlink":
        path = Path(clip["path"])
        path.unlink()
        path.symlink_to(outside)
    elif kind == "folder-symlink":
        (store.root / "transcripts").rename(store.root / "transcripts-original")
        (store.root / "transcripts").symlink_to(store.root / "transcripts-original", target_is_directory=True)
    elif kind == "profile-symlink":
        (store.root / "voiceprint.json").unlink()
        (store.root / "voiceprint.json").symlink_to(outside)
    else:
        with store._connect() as database:
            database.execute("UPDATE clips SET path='../outside' WHERE id=?", (clip["id"],))
    with pytest.raises(ValueError):
        with migration.prepared_library_copy(store, tmp_path / "destination"):
            pytest.fail("invalid library must not yield")
    assert outside.read_bytes() == b"outside remains intact"
    assert not (tmp_path / "destination").exists()


def test_insufficient_space_copies_nothing(store, tmp_path, monkeypatch):
    seed(store)
    usage = migration.shutil.disk_usage(tmp_path)
    monkeypatch.setattr(migration.shutil, "disk_usage", lambda path: usage._replace(free=1))
    with pytest.raises(RuntimeError, match="空间不足"):
        with migration.prepared_library_copy(store, tmp_path / "destination"):
            pytest.fail("full disk must not yield")
    assert not (tmp_path / "destination").exists()
    assert not list(tmp_path.glob(".audio-agent-migration-*"))


def test_copy_failure_and_marker_failure_leave_source_writable(store, tmp_path, monkeypatch):
    seed(store)
    copy = migration._copy_file
    def fail_copy(*args):
        raise OSError("copy failed")
    monkeypatch.setattr(migration, "_copy_file", fail_copy)
    with pytest.raises(OSError, match="copy failed"):
        with migration.prepared_library_copy(store, tmp_path / "destination"):
            pytest.fail("copy failure must not yield")
    assert not list(tmp_path.glob(".audio-agent-migration-*"))
    monkeypatch.setattr(migration, "_copy_file", copy)
    def fail_link(*args, **kwargs):
        raise OSError("marker failed")
    monkeypatch.setattr(migration.os, "link", fail_link)
    with pytest.raises(OSError, match="marker failed"):
        with migration.prepared_library_copy(store, tmp_path / "destination") as prepared:
            prepared.mark_committed()
    assert not (tmp_path / "destination").exists()
    assert not list(store.root.glob(".moved-to*"))
    assert store.add_pcm(pcm(), start_time=time.time(), source="rollback")


def test_inflight_watcher_import_cannot_commit_to_old_library(store, tmp_path, monkeypatch):
    incoming = tmp_path / "source.wav"
    with wave.open(str(incoming), "wb") as handle:
        handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        handle.writeframes(pcm())
    original_bytes = incoming.read_bytes()
    staged, release = Event(), Event()
    insert = store._insert_file
    def delayed_insert(*args, **kwargs):
        staged.set()
        assert release.wait(5)
        return insert(*args, **kwargs)
    monkeypatch.setattr(store, "_insert_file", delayed_insert)
    with ThreadPoolExecutor(max_workers=1) as workers:
        job = workers.submit(store.import_wav, incoming)
        try:
            assert staged.wait(5)
            with migration.prepared_library_copy(store, tmp_path / "destination") as prepared:
                assert list((prepared.path / "incoming").iterdir()) == []
                prepared.mark_committed()
        finally:
            release.set()
        with pytest.raises(LibraryMovedError):
            job.result(timeout=5)
    assert incoming.read_bytes() == original_bytes
    assert list((store.root / "incoming").iterdir()) == []
    assert store.list_clips() == []


@pytest.mark.parametrize("recent", [False, True])
def test_pipeline_holds_model_lock_until_transcript_is_committed(store, tmp_path, monkeypatch, recent):
    clip = store.add_pcm(pcm(1), start_time=100, source="test")
    monkeypatch.setattr(asr, "transcribe", lambda *args, **kwargs: {"text": "test", "segments": []})
    persist = pipeline._persist
    checked = []
    def checked_persist(*args, **kwargs):
        with pytest.raises(migration.MigrationBusyError):
            with migration.prepared_library_copy(store, tmp_path / "destination"):
                pytest.fail("migration must wait for transcript persistence")
        checked.append(True)
        return persist(*args, **kwargs)
    monkeypatch.setattr(pipeline, "_persist", checked_persist)
    if recent:
        result = pipeline.transcribe_recent(store, seconds=1, now=101)
    else:
        result = pipeline.transcribe_clip(store, clip["id"])
    assert checked == [True]
    with migration.prepared_library_copy(store, tmp_path / "destination") as prepared:
        assert (prepared.path / "transcripts" / f"{result['id']}.json").exists()


def test_waiting_writer_checks_fence_after_store_lock_is_acquired(store, tmp_path):
    reached = Event()
    def write():
        reached.set()
        return pipeline._persist(store, {"text": "late writer"})
    with ThreadPoolExecutor(max_workers=1) as workers:
        with migration.prepared_library_copy(store, tmp_path / "destination") as prepared:
            job = workers.submit(write)
            assert reached.wait(5)
            prepared.mark_committed()
        with pytest.raises(LibraryMovedError):
            job.result(timeout=5)
    assert list((store.root / "transcripts").glob("*.json")) == []


@pytest.mark.parametrize("action", ["live-settings", "speaker-settings", "self-enroll", "person-enroll",
                                   "person-edit", "person-remove", "self-clear"])
def test_other_managers_cannot_write_old_settings_or_references_after_migration(store, tmp_path, action):
    from audio_agent.live import LiveManager
    from audio_agent.speaker_service import SpeakerManager
    from test_speaker_service import clip, model_status, persisted
    def forbidden(*args, **kwargs):
        pytest.fail("settings migration must not capture or infer")
    live = LiveManager(store, capture_factory=forbidden)
    speaker = SpeakerManager(store, analyzer=forbidden, model_status=model_status)
    source = clip(store)
    analysis = persisted(speaker, source)
    live.update_settings(-44.4)
    speaker.update_settings(False)
    speaker.enroll(source["id"], "speaker_1", True, analysis["id"])
    person = speaker.enroll_profile(source["id"], "speaker_2", analysis["id"], True, "测试人物", "")['profile']
    calls = {
        "live-settings": lambda: live.update_settings(-32),
        "speaker-settings": lambda: speaker.update_settings(True),
        "self-enroll": lambda: speaker.enroll(source["id"], "speaker_1", True, analysis["id"]),
        "person-enroll": lambda: speaker.enroll_profile(source["id"], "speaker_2", analysis["id"], True,
                                                        "改变姓名", "", replace_profile_id=person["id"]),
        "person-edit": lambda: speaker.update_profile(person["id"], "改变姓名", ""),
        "person-remove": lambda: speaker.remove_profile(person["id"]),
        "self-clear": speaker.clear,
    }
    before = {name: (store.root / name).read_bytes() for name in migration._PRIVATE_FILES}
    reached = Event()
    def write():
        reached.set()
        return calls[action]()
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            with migration.prepared_library_copy(store, tmp_path / "destination") as prepared:
                job = workers.submit(write)
                assert reached.wait(5)
                with pytest.raises(FutureTimeout):
                    job.result(timeout=.02)  # Each settings/reference write shares the migration lock.
                prepared.mark_committed()
            with pytest.raises(LibraryMovedError):
                job.result(timeout=5)
        assert {name: (store.root / name).read_bytes() for name in migration._PRIVATE_FILES} == before
        assert live.settings()["settings"]["trigger_db"] == -44.4
        assert speaker.status()["automatic_enabled"] is False
    finally:
        live.close()
        speaker.close()


def test_migration_waits_for_other_managers_atomic_settings_write(store, tmp_path, monkeypatch):
    from audio_agent import speaker_service
    from test_speaker_service import model_status
    speaker = speaker_service.SpeakerManager(store, model_status=model_status)
    entered, release = Event(), Event()
    atomic = speaker_service._atomic_json
    def held_write(path, value):
        entered.set()
        assert release.wait(5)
        return atomic(path, value)
    monkeypatch.setattr(speaker_service, "_atomic_json", held_write)
    def copy():
        with migration.prepared_library_copy(store, tmp_path / "destination") as prepared:
            return json.loads((prepared.path / "speaker-settings.json").read_text())
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            write = workers.submit(speaker.update_settings, True)
            try:
                assert entered.wait(5)
                copied = workers.submit(copy)
                with pytest.raises(FutureTimeout):
                    copied.result(timeout=.03)
            finally:
                release.set()
            assert write.result(timeout=5)["automatic_enabled"] is True
            assert copied.result(timeout=5) == {"automatic_enabled": True}
    finally:
        speaker.close()


def test_speaker_model_lock_covers_sidecar_commit(store, tmp_path, monkeypatch):
    from audio_agent import speaker_service
    from test_speaker_service import clip, model_status, result, wait_job
    speaker = speaker_service.SpeakerManager(store, analyzer=lambda *a, **k: result(), model_status=model_status)
    source = clip(store)
    atomic = speaker_service._atomic_json
    observed = []
    def checked_write(path, value):
        with pytest.raises(migration.MigrationBusyError):
            with migration.prepared_library_copy(store, tmp_path / "destination"):
                pytest.fail("migration must not race completed analysis persistence")
        observed.append(True)
        return atomic(path, value)
    monkeypatch.setattr(speaker_service, "_atomic_json", checked_write)
    try:
        job = speaker.submit(source["id"])
        assert wait_job(speaker, job["id"])["status"] == "completed"
        assert observed == [True]
        with migration.prepared_library_copy(store, tmp_path / "destination") as prepared:
            assert (prepared.path / "speakers" / f"{source['id']}.json").exists()
    finally:
        speaker.close()
