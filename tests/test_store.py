import array
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import sys
import time
import wave

import pytest

from audio_agent.store import AudioStore, SAMPLE_RATE


def pcm(value, seconds):
    samples = array.array("h", [value] * round(seconds * SAMPLE_RATE))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def samples(path):
    with wave.open(str(path), "rb") as audio:
        result = array.array("h", audio.readframes(audio.getnframes()))
    if sys.byteorder != "little":
        result.byteswap()
    return result


@pytest.fixture
def store(tmp_path):
    return AudioStore(tmp_path / "data")


def test_export_slices_real_samples_and_reports_missing_time(store):
    now = 1_700_000_000.0
    a = store.add_pcm(pcm(111, 2), start_time=now, source="mic")
    b = store.add_pcm(pcm(222, 2), start_time=now + 3, source="mic")
    out = store.export_range(now + 0.5, now + 5.5)
    data = samples(out["path"])
    assert len(data) == int(3.5 * SAMPLE_RATE)
    assert data[:int(1.5 * SAMPLE_RATE)] == array.array("h", [111] * int(1.5 * SAMPLE_RATE))
    assert set(data[int(1.5 * SAMPLE_RATE):]) == {222}
    assert out["duration"] == 3.5
    assert [gap["duration"] for gap in out["gaps"]] == [1, 0.5]
    assert [segment["clip_id"] for segment in out["segments"]] == [a["id"], b["id"]]
    assert out["segments"][1]["output_start"] == 1.5


def test_export_trims_overlapping_clips_and_source_filter(store):
    store.add_pcm(pcm(11, 2), start_time=100, source="mic")
    store.add_pcm(pcm(22, 2), start_time=101, source="import")
    out = store.export_range(100, 103)
    data = samples(out["path"])
    assert len(data) == 3 * SAMPLE_RATE
    assert set(data[:2 * SAMPLE_RATE]) == {11}
    assert set(data[2 * SAMPLE_RATE:]) == {22}
    assert out["gaps"] == []
    only_import = store.export_range(100, 103, source="import")
    assert set(samples(only_import["path"])) == {22}
    assert only_import["gaps"][0]["duration"] == 1


def test_file_import_deduplicates_original_bytes(store, tmp_path):
    original = tmp_path / "incoming.wav"
    with wave.open(str(original), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(pcm(123, 0.2))
    first = store.import_wav(original, source="DJI", start_time=100)
    second = store.import_wav(original, source="again", start_time=200)
    assert first["id"] == second["id"]
    assert not first["deduplicated"] and second["deduplicated"]
    assert first["start_time"] == 100
    assert Path(first["path"]).read_bytes() == original.read_bytes()
    assert store.status()["clip_count"] == 1


def test_retention_removes_only_expired_owned_audio(store, tmp_path):
    expired = store.add_pcm(pcm(1, 1), start_time=1, source="mic")
    fresh = store.add_pcm(pcm(2, 1), start_time=86400, source="mic")
    original = tmp_path / "original.wav"
    original.write_bytes(Path(expired["path"]).read_bytes())
    report = store.prune(now=86402)
    assert report["removed"] == 1
    assert not Path(expired["path"]).exists()
    assert Path(fresh["path"]).exists()
    assert original.exists()
    assert store.get_clip(expired["id"]) is None


def test_export_never_overwrites_existing_output(store, tmp_path):
    store.add_pcm(pcm(9, 1), start_time=0, source="mic")
    output = tmp_path / "important.wav"
    output.write_bytes(b"must survive")
    with pytest.raises(FileExistsError):
        store.export_range(0, 1, output_path=output)
    assert output.read_bytes() == b"must survive"


def test_no_audio_and_invalid_pcm_have_actionable_errors(store):
    with pytest.raises(ValueError, match="No recorded audio"):
        store.export_recent(30)
    with pytest.raises(ValueError, match="whole"):
        store.add_pcm(b"x", start_time=0, source="mic")
    with pytest.raises(ValueError, match="16000"):
        store.add_pcm(b"xx", start_time=0, source="mic", sample_rate=48000)
    with pytest.raises(ValueError, match="positive"):
        store.recent(-1)
    with pytest.raises(ValueError, match="finite"):
        store.export_range(0, float("inf"))


def test_concurrent_store_instances_serialize_writer_and_exports(tmp_path):
    root = tmp_path / "shared"
    writer = AudioStore(root)
    reader = AudioStore(root)
    writer.add_pcm(pcm(100, 1), start_time=100, source="mic")

    def write_more():
        for index in range(1, 6):
            writer.add_pcm(pcm(index, 1), start_time=100 + index, source="mic")

    def read_many():
        for _ in range(6):
            exported = reader.export_range(100, 106)
            assert len(samples(exported["path"])) >= SAMPLE_RATE

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write_more), pool.submit(read_many)]
        for future in futures:
            future.result()
    assert reader.status()["clip_count"] == 6
    with sqlite3.connect(writer.db_path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_last_utterance_energy_range_is_separated_by_silence(store):
    store.add_pcm(pcm(6000, 0.4) + pcm(0, 1) + pcm(6000, 0.3) + pcm(0, 0.3),
                  start_time=100, source="mic")
    found = store.last_utterance(lookback_seconds=3, now=102, padding_seconds=0)
    assert found["start_time"] == pytest.approx(101.4)
    assert found["end_time"] == pytest.approx(101.7)
    assert found["detector"] == "energy"


def test_invalid_database_path_cannot_delete_unrelated_file(store, tmp_path):
    clip = store.add_pcm(pcm(1, 1), start_time=0, source="mic")
    protected = tmp_path / "protected.wav"
    protected.write_bytes(b"protected")
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE clips SET path=? WHERE id=?", (str(protected), clip["id"]))
    with pytest.raises(ValueError, match="Invalid stored audio path"):
        store.prune(now=90000)
    assert protected.read_bytes() == b"protected"


def test_imported_old_recording_retained_from_arrival(store, tmp_path):
    original = tmp_path / "old.wav"
    with wave.open(str(original), "wb") as audio:
        audio.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        audio.writeframes(pcm(500, 0.1))
    clip = store.import_wav(original, start_time=100)
    store.prune(now=clip["created_time"] + 86399)
    assert store.get_clip(clip["id"]) is not None
    store.prune(now=clip["created_time"] + 86401)
    assert store.get_clip(clip["id"]) is None
    assert original.exists()


def test_import_rejects_file_modified_during_snapshot(store, tmp_path, monkeypatch):
    import shutil
    original = tmp_path / "growing.wav"
    with wave.open(str(original), "wb") as audio:
        audio.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        audio.writeframes(pcm(20, 0.1))
    copy = shutil.copyfileobj
    def copy_then_mutate(source, target, *args, **kwargs):
        copy(source, target, *args, **kwargs)
        with original.open("ab") as writer:
            writer.write(b"more data")
    monkeypatch.setattr("audio_agent.store.shutil.copyfileobj", copy_then_mutate)
    with pytest.raises(ValueError, match="changed while reading"):
        store.import_wav(original)
    assert store.status()["clip_count"] == 0
    assert list((store.root / "incoming").iterdir()) == []


def test_snapshot_survives_original_clip_expiration(store, tmp_path):
    clip = store.add_pcm(pcm(420, 0.1), start_time=0, source="mic")
    output = tmp_path / "asr-input.wav"
    snapshot = store.snapshot_clip(clip["id"], output_path=output)
    store.prune(now=90000)
    assert not Path(clip["path"]).exists()
    assert snapshot["source"] == "mic"
    assert set(samples(output)) == {420}
    with pytest.raises(ValueError, match="not found or has expired"):
        store.snapshot_clip(clip["id"])


def test_disk_space_reserve_fails_before_writing(store, monkeypatch):
    from collections import namedtuple
    disk_usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("audio_agent.store.shutil.disk_usage", lambda _: disk_usage(100, 90, 10))
    with pytest.raises(RuntimeError, match="Insufficient disk space"):
        store.add_pcm(pcm(10, 0.1), start_time=100, source="mic")
    assert store.status()["clip_count"] == 0
    assert list((store.root / "incoming").iterdir()) == []


def test_store_rejects_symlinked_clip_directory(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "clips").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        AudioStore(root)
