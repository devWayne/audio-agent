"""Completed local WAV ingestion; no device or model is used by these tests."""
import os
from pathlib import Path
import struct
import time
import wave

import pytest

from audio_agent.store import AudioStore
from audio_agent.watcher import FolderWatcher


def write_wav(path, value=1000, frames=1600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(struct.pack("<h", value) * frames)
    return path


@pytest.fixture
def watched(tmp_path):
    folder = tmp_path / "drop"
    folder.mkdir()
    store = AudioStore(tmp_path / "data")
    return folder, store


def test_waits_for_stable_version_and_imports_once(watched):
    folder, store = watched
    path = write_wav(folder / "clip.wav")
    watcher = FolderWatcher(store, folder, stable_seconds=3)
    assert watcher.scan(now=0) == []
    assert watcher.scan(now=2) == []
    # A growing WAV restarts the stable interval.
    write_wav(path, frames=3200)
    assert watcher.scan(now=3) == []
    assert watcher.scan(now=5) == []
    result = watcher.scan(now=6)
    assert len(result) == 1 and "clip" in result[0]
    assert result[0]["clip"]["duration"] == pytest.approx(0.2)
    assert watcher.scan(now=20) == []
    assert len(store.list_clips()) == 1


def test_skips_dji_edited_variant_and_non_wav_inputs(watched):
    folder, store = watched
    original = write_wav(folder / "TX_MIC001_20261024_120000" / "TX01_MIC001_20261024_120000_orig.wav", 1000)
    write_wav(original.with_name("TX01_MIC001_20261024_120000_edit.wav"), 2000)
    write_wav(folder / "not-finished.wav.part", 3000)
    watcher = FolderWatcher(store, folder, stable_seconds=0)
    watcher.scan(now=0)
    result = watcher.scan(now=1)
    assert [item["file"] for item in result] == [str(original)]
    assert len(store.list_clips()) == 1


def test_edits_can_be_explicitly_included(watched):
    folder, store = watched
    write_wav(folder / "TX01_MIC001_20261024_120000_edit.wav")
    watcher = FolderWatcher(store, folder, stable_seconds=0, include_edits=True)
    watcher.scan(now=0)
    assert len(watcher.scan(now=1)) == 1


def test_does_not_follow_file_symlink_outside_selected_folder(watched, tmp_path):
    folder, store = watched
    outside = write_wav(tmp_path / "outside.wav")
    (folder / "link.wav").symlink_to(outside)
    watcher = FolderWatcher(store, folder, stable_seconds=0)
    assert watcher.scan(now=0) == []
    assert watcher.scan(now=1) == []
    assert store.list_clips() == []


def test_error_is_reported_and_replaced_valid_file_is_retried(watched):
    folder, store = watched
    path = folder / "bad.wav"
    path.write_bytes(b"not a WAV")
    watcher = FolderWatcher(store, folder, stable_seconds=1)
    assert watcher.scan(now=0) == []
    errors = watcher.scan(now=1)
    assert len(errors) == 1 and "error" in errors[0]
    assert store.list_clips() == []
    write_wav(path)
    assert watcher.scan(now=2) == []
    completed = watcher.scan(now=3)
    assert len(completed) == 1 and "clip" in completed[0]


def test_old_recording_is_available_after_watch_loop_prunes(watched):
    folder, store = watched
    path = write_wav(folder / "archive.wav")
    old = time.time() - 7 * 86400
    os.utime(path, (old, old))
    watcher = FolderWatcher(store, folder, stable_seconds=0)
    watcher.scan(now=0)
    result = watcher.scan(now=1)
    assert len(result) == 1 and "clip" in result[0]
    store.prune()
    assert store.get_clip(result[0]["clip"]["id"]) is not None


@pytest.mark.parametrize("relationship", ["same", "inside", "parent"])
def test_rejects_store_and_watched_folder_overlap(tmp_path, relationship):
    store = AudioStore(tmp_path / "data")
    folder = {"same": store.root, "inside": store.root / "incoming", "parent": tmp_path}[relationship]
    with pytest.raises(ValueError, match="不能互相包含"):
        FolderWatcher(store, folder)
