from pathlib import Path
import sqlite3
import wave

from audio_agent.store import AudioStore


def test_import_preserves_user_filename_and_explicit_upload_name(tmp_path):
    source = tmp_path / "真实录音.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", ""))
        audio.writeframes(b"\x01\x00" * 160)
    store = AudioStore(tmp_path / "data")
    clip = store.import_wav(source)
    assert clip["original_name"] == "真实录音.wav"
    other = AudioStore(tmp_path / "uploaded")
    upload = other.import_wav(source, original_name="会议记录.wav")
    assert upload["original_name"] == "会议记录.wav"
    assert other.get_clip(upload["id"])["original_name"] == "会议记录.wav"


def test_original_name_migration_preserves_existing_recordings(tmp_path):
    root = tmp_path / "data"
    store = AudioStore(root)
    clip = store.add_pcm(b"\x01\x00" * 160, start_time=100, source="mic")
    before = Path(clip["path"]).read_bytes()
    with sqlite3.connect(store.db_path) as db:
        db.execute("ALTER TABLE clips DROP COLUMN original_name")
    reopened = AudioStore(root)
    result = reopened.get_clip(clip["id"])
    assert result["original_name"] is None
    assert result["start_time"] == 100
    assert Path(result["path"]).read_bytes() == before
