"""Chronological full-text document tests: no audio read or model invocation."""
import json
import os
from pathlib import Path
import time
from unittest import mock
import uuid

import pytest

from audio_agent.document import document_page
from audio_agent.store import AudioStore


@pytest.fixture
def library(tmp_path, monkeypatch):
    now = time.time() + 10
    monkeypatch.setattr("audio_agent.document.time.time", lambda: now)
    store = AudioStore(tmp_path / "data")
    (store.root / "transcripts").mkdir()
    return store, now


def transcript(store, now, *, id=None, clip_id=None, created_at=None, mtime=None, **values):
    transcript_id = id or uuid.uuid4().hex
    item = {"id": transcript_id, "text": "完整文本", "segments": [],
            "created_at": now - 5 if created_at is None else created_at, **values}
    if clip_id is not None:
        item["clip_id"] = clip_id
    path = store.root / "transcripts" / f"{transcript_id}.json"
    path.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    modified = now - 1 if mtime is None else mtime
    os.utime(path, (modified, modified))
    return item, path


def test_empty_document_and_read_only_side_effects(library):
    store, now = library
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in store.root.rglob("*") if p.is_file()}
    with mock.patch("wave.open", side_effect=AssertionError("No WAV access")), \
            mock.patch("audio_agent.pipeline.transcribe_clip", side_effect=AssertionError("No inference")):
        page = document_page(store)
    assert page["items"] == [] and page["total"] == 0 and page["next_cursor"] is None
    assert page["snapshot"] == now and len(page["revision"]) == 64
    after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in store.root.rglob("*") if p.is_file()}
    assert before == after


def test_audio_time_orders_document_instead_of_transcription_completion(library):
    store, now = library
    early = store.add_pcm(bytes(32000), start_time=now - 100, source="live:session")
    late = store.add_pcm(bytes(32000), start_time=now - 50, source="live:session")
    late_text, _ = transcript(store, now, clip_id=late["id"], created_at=now - 4, text="后说先转写")
    early_text, _ = transcript(store, now, clip_id=early["id"], created_at=now - 2, text="先说后转写")
    with mock.patch("wave.open", side_effect=AssertionError("No WAV reads")):
        page = document_page(store)
    assert [item["id"] for item in page["items"]] == [early_text["id"], late_text["id"]]
    assert [item["start_time"] for item in page["items"]] == [now - 100, now - 50]
    assert all(item["source_kind"] == "live" and item["recording_available"] for item in page["items"])


def test_latest_valid_transcript_per_clip_and_tie_break_are_deterministic(library):
    store, now = library
    clip_id = uuid.uuid4().hex
    transcript(store, now, id="1" * 32, clip_id=clip_id, created_at=now - 10, text="old")
    transcript(store, now, id="2" * 32, clip_id=clip_id, created_at=now - 5, text="tie-old")
    chosen, _ = transcript(store, now, id="3" * 32, clip_id=clip_id, created_at=now - 5, text="latest")
    transcript(store, now, clip_id=clip_id, created_at=now - 1, text={"invalid": True})
    page = document_page(store)
    assert page["total"] == 1
    assert page["items"][0]["id"] == chosen["id"]
    assert page["items"][0]["text"] == "latest"


def test_range_exports_keep_own_identity_and_original_audio_times(library):
    store, now = library
    first, _ = transcript(store, now, text="one", audio={"start_time": 100, "end_time": 105}, selection="recent")
    second, _ = transcript(store, now, text="two", audio={"start_time": 100, "end_time": 105}, selection="last_utterance")
    page = document_page(store)
    assert page["total"] == 2
    assert {item["id"] for item in page["items"]} == {first["id"], second["id"]}
    assert all(item["source_kind"] == "recent" and item["clip_id"] is None for item in page["items"])
    assert all((item["start_time"], item["end_time"]) == (100, 105) for item in page["items"])
    assert all(item["recording_available"] is False for item in page["items"])


def test_keyset_pagination_equal_audio_timestamps_has_no_duplicates(library):
    store, now = library
    expected = []
    for index in range(7):
        item, _ = transcript(store, now, id=f"{index:032x}", audio={"start_time": 100, "end_time": 110})
        expected.append(item["id"])
    items, cursor, revisions = [], None, set()
    while True:
        page = document_page(store, limit=2, cursor=cursor)
        assert page["total"] == 7
        items.extend(item["id"] for item in page["items"])
        revisions.add(page["revision"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert items == expected and len(set(items)) == 7
    assert len(revisions) == 1


def test_snapshot_excludes_new_append_and_retranscription_until_refresh(library, monkeypatch):
    store, now = library
    first_clip, later_clip = uuid.uuid4().hex, uuid.uuid4().hex
    original, _ = transcript(store, now, clip_id=first_clip, audio={"start_time": 100, "end_time": 101})
    later, _ = transcript(store, now, clip_id=later_clip, audio={"start_time": 200, "end_time": 201})
    first = document_page(store, limit=1)
    assert first["items"][0]["id"] == original["id"]
    monkeypatch.setattr("audio_agent.document.time.time", lambda: now + 10)
    new, _ = transcript(store, now + 10, clip_id=later_clip, created_at=now + 2,
                        audio={"start_time": 200, "end_time": 201}, text="rewritten")
    extra, _ = transcript(store, now + 10, audio={"start_time": 150, "end_time": 151})
    second = document_page(store, limit=1, cursor=first["next_cursor"])
    assert second["snapshot"] == first["snapshot"]
    assert second["total"] == 2 and second["next_cursor"] is None
    assert second["items"][0]["id"] == later["id"]
    assert second["revision"] == first["revision"]
    refreshed = document_page(store)
    assert [item["id"] for item in refreshed["items"]] == [original["id"], extra["id"], new["id"]]
    assert refreshed["revision"] != first["revision"]


def test_snapshot_excludes_new_backdated_file_by_modification_time(library, monkeypatch):
    store, now = library
    for index in range(2):
        transcript(store, now, id=f"{index:032x}")
    first = document_page(store, limit=1)
    monkeypatch.setattr("audio_agent.document.time.time", lambda: now + 10)
    transcript(store, now + 10, created_at=now - 100)
    second = document_page(store, limit=1, cursor=first["next_cursor"])
    assert second["total"] == 2


def test_exact_source_filter_and_cursor_query_binding(library):
    store, now = library
    for source in ("live:abc", "live:abcd", "live:abc", "import"):
        transcript(store, now, source=source)
    first = document_page(store, limit=1, source="live:abc")
    assert first["total"] == 2 and first["next_cursor"]
    assert first["items"][0]["source"] == "live:abc"
    for options in ({"source": None, "limit": 1}, {"source": "live:abcd", "limit": 1},
                    {"source": "live:abc", "limit": 2}):
        with pytest.raises(ValueError, match="游标"):
            document_page(store, cursor=first["next_cursor"], **options)


def test_expired_or_deleted_audio_preserves_retained_text_and_saved_time(library):
    store, now = library
    source = store.add_pcm(bytes(32000), start_time=now - 90000, source="live:old")
    item, _ = transcript(store, now, clip_id=source["id"], created_at=now - 90000,
                         audio={"start_time": source["start_time"], "end_time": source["end_time"],
                                "display_name": "旧录音"}, source="live:old")
    old = document_page(store)["items"][0]
    assert old["recording_available"] is False and old["id"] == item["id"]
    store.prune(now=now)
    deleted = document_page(store)["items"][0]
    assert deleted["recording_available"] is False
    assert deleted["start_time"] == source["start_time"]
    assert deleted["display_name"] == "旧录音"


def test_old_transcript_modification_time_is_excluded_even_with_recent_created_at(library):
    store, now = library
    transcript(store, now, mtime=now - 90000)
    kept, _ = transcript(store, now, created_at=1)
    page = document_page(store)
    assert page["total"] == 1 and page["items"][0]["id"] == kept["id"]


def test_missing_legacy_audio_timestamp_falls_back_to_creation_and_empty_text_is_kept(library):
    store, now = library
    empty, _ = transcript(store, now, clip_id=uuid.uuid4().hex, text="", created_at=now - 20)
    item = document_page(store)["items"][0]
    assert item["text"] == "" and item["id"] == empty["id"]
    assert item["start_time"] == item["end_time"] == now - 20
    assert item["recording_available"] is False


def test_full_text_segments_and_more_than_two_hundred_records_are_not_truncated(library):
    store, now = library
    text = "完整长段落。" * 10000
    segments = [{"start": index, "end": index + 1, "text": f"段落 {index}"} for index in range(400)]
    special, _ = transcript(store, now, id="0" * 32, text=text, segments=segments)
    for index in range(1, 205):
        transcript(store, now, id=f"{index:032x}")
    first = document_page(store)
    assert first["total"] == 205 and len(first["items"]) == 200
    assert first["items"][0]["id"] == special["id"]
    assert first["items"][0]["text"] == text and first["items"][0]["segments"] == segments
    assert len(document_page(store, cursor=first["next_cursor"])["items"]) == 5


@pytest.mark.parametrize("values", [
    {"created_at": float("nan")}, {"created_at": True}, {"created_at": 10**400},
    {"text": 1}, {"segments": {"bad": True}}, {"segments": [1]},
    {"segments": [{"start": float("inf")}]}, {"audio": {"start_time": float("nan")}},
    {"clip_id": "../outside"},
])
def test_invalid_or_nonfinite_transcripts_are_skipped(library, values):
    store, now = library
    transcript(store, now, **values)
    assert document_page(store)["total"] == 0


def test_invalid_json_ids_and_symlinks_are_skipped_without_outside_reads(library, tmp_path):
    store, now = library
    folder = store.root / "transcripts"
    (folder / f"{'1'*32}.json").write_text("{broken")
    transcript(store, now, id="not-a-valid-id")
    valid, path = transcript(store, now)
    moved = folder / f"{'2'*32}.json"
    path.rename(moved)  # filename no longer agrees with record id
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"id": "3" * 32, "text": "secret", "created_at": now - 1}))
    (folder / f"{'3'*32}.json").symlink_to(outside)
    assert document_page(store)["total"] == 0


@pytest.mark.parametrize("limit", [0, 201, True, 1.5, "20"])
def test_invalid_page_limit_is_rejected(library, limit):
    with pytest.raises(ValueError):
        document_page(library[0], limit=limit)


@pytest.mark.parametrize("cursor", ["", "not-a-valid-cursor", "!", 123, "x" * 5000])
def test_invalid_cursor_is_rejected(library, cursor):
    with pytest.raises(ValueError, match="游标"):
        document_page(library[0], cursor=cursor)


def test_cursor_rejects_tampering_and_another_store(library, tmp_path):
    store, now = library
    for _ in range(2):
        transcript(store, now)
    cursor = document_page(store, limit=1)["next_cursor"]
    changed = cursor[:10] + ("A" if cursor[10] != "A" else "B") + cursor[11:]
    with pytest.raises(ValueError):
        document_page(store, limit=1, cursor=changed)
    other = AudioStore(tmp_path / "other")
    with pytest.raises(ValueError):
        document_page(other, limit=1, cursor=cursor)


def test_revision_changes_on_effective_text_modification(library, monkeypatch):
    store, now = library
    item, path = transcript(store, now)
    first = document_page(store)
    item["text"] = "已修订"
    path.write_text(json.dumps(item))
    os.utime(path, (now + 1, now + 1))
    monkeypatch.setattr("audio_agent.document.time.time", lambda: now + 2)
    second = document_page(store)
    assert second["revision"] != first["revision"]
    assert second["items"][0]["text"] == "已修订"
