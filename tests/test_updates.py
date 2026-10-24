"""Incremental full-text cursor semantics without audio/model side effects."""
import asyncio
import json
import time
from unittest import mock
import uuid

import pytest

from audio_agent.store import AudioStore
from audio_agent.updates import UpdatesFeed


def put_transcript(store, *, clip_id=None, text="完整转写", created_at=None, id=None, **extra):
    folder = store.root / "transcripts"
    folder.mkdir(exist_ok=True)
    now = time.time() - .01
    value = {"id": id or uuid.uuid4().hex, "clip_id": clip_id, "text": text,
             "created_at": now if created_at is None else created_at,
             "start_time": now - 10, "end_time": now - 1, "segments": [], **extra}
    path = folder / f"{value['id']}.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return value, path


def put_analysis(store, clip, *, two=False):
    folder = store.root / "speakers"
    folder.mkdir(exist_ok=True)
    value = {"id": uuid.uuid4().hex, "clip_id": clip["id"], "created_at": time.time(),
             "embedding_model_id": "fake-model", "backend": "fake", "elapsed_seconds": .1,
             "speaker_count": 2 if two else 1, "notes": [],
             "speakers": [{"id": "speaker_1", "speech_seconds": 3 if two else 6,
                           "reference_seconds": 0, "embedding": None}],
             "turns": [{"start": 0, "end": 3 if two else 6, "speaker": "speaker_1"}]}
    if two:
        value["speakers"].append({"id": "speaker_2", "speech_seconds": 3,
                                  "reference_seconds": 0, "embedding": None})
        value["turns"].append({"start": 3, "end": 6, "speaker": "speaker_2"})
    (folder / f"{clip['id']}.json").write_text(json.dumps(value))
    return value


@pytest.fixture
def library(tmp_path):
    store = AudioStore(tmp_path / "data")
    return store, UpdatesFeed(store, poll_interval=.01)


@pytest.mark.asyncio
async def test_initial_recent_subset_preserves_full_text_and_does_not_consume_for_other_clients(library):
    store, feed = library
    values = [put_transcript(store, id=f"{i:032x}", start_time=100+i, end_time=101+i,
                             text="全文" * 10000)[0] for i in range(5)]
    first = await feed.get_updates(limit=2)
    assert [item["id"] for item in first["items"]] == [value["id"] for value in values[-2:]]
    assert all(len(item["text"]) == 20000 for item in first["items"])
    assert first["initial"] is True and not first["has_more"]
    baseline = first["next_cursor"]
    assert (await feed.get_updates(baseline))["items"] == []
    new, _ = put_transcript(store, start_time=99, end_time=100)  # Backdated recording, new arrival.
    a = await feed.get_updates(baseline)
    b = await feed.get_updates(baseline)
    assert a == b and [item["id"] for item in a["items"]] == [new["id"]]
    assert (await feed.get_updates(a["next_cursor"]))["items"] == []


@pytest.mark.asyncio
async def test_same_timestamp_pagination_is_complete_and_stable_across_append(library):
    store, feed = library
    cursor = (await feed.get_updates())["next_cursor"]
    now = time.time() - 1
    expected = [put_transcript(store, id=f"{i:032x}", created_at=now, start_time=now, end_time=now)[0]["id"]
                for i in range(7)]
    first = await feed.get_updates(cursor, limit=2)
    assert first["has_more"]
    extra = put_transcript(store, id="f"*32, created_at=now, start_time=now-10, end_time=now-5)[0]
    received = list(first["items"])
    cursor = first["next_cursor"]
    while True:
        page = await feed.get_updates(cursor, limit=2)
        received.extend(page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert [item["id"] for item in received] == expected + [extra["id"]]
    assert len({item["update_sequence"] for item in received}) == 8
    assert (await feed.get_updates(cursor))["items"] == []


@pytest.mark.asyncio
async def test_updates_read_all_document_pages_without_recent_result_limit(library):
    store, feed = library
    cursor = (await feed.get_updates())["next_cursor"]
    for index in range(205):
        put_transcript(store, id=f"{index:032x}", start_time=100, end_time=101)
    received = []
    while True:
        page = await feed.get_updates(cursor, limit=100)
        received.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert received == [f"{index:032x}" for index in range(205)]


@pytest.mark.asyncio
async def test_retranscription_and_annotation_upsert_one_document_identity(library):
    store, feed = library
    clip = store.add_pcm(bytes(6 * 32000), start_time=time.time()-10, source="live:test")
    first, _ = put_transcript(store, clip_id=clip["id"], text="先显示文字",
                              segments=[{"start": 0, "end": 6, "text": "完整段落"}])
    initial = await feed.get_updates()
    key = initial["items"][0]["document_key"]
    cursor = initial["next_cursor"]
    put_analysis(store, clip, two=True)
    updated = await feed.get_updates(cursor)
    assert updated["items"][0]["id"] == first["id"]
    assert updated["items"][0]["document_key"] == key
    assert updated["items"][0]["segments"][0]["speaker_labels"] == ["说话人 1", "说话人 2"]
    assert "embedding" not in json.dumps(updated)
    cursor = updated["next_cursor"]
    latest, _ = put_transcript(store, clip_id=clip["id"], text="稍后重新转写的全文", created_at=time.time()-.001)
    replacement = await feed.get_updates(cursor)
    assert len(replacement["items"]) == 1
    assert replacement["items"][0]["id"] == latest["id"]
    assert replacement["items"][0]["document_key"] == key


@pytest.mark.asyncio
async def test_update_before_its_page_is_read_is_coalesced_without_losing_latest(library):
    store, feed = library
    cursor = (await feed.get_updates())["next_cursor"]
    docs = [put_transcript(store, id=f"{i:032x}", text="before", start_time=100+i)[0] for i in range(3)]
    first = await feed.get_updates(cursor, limit=1)
    changed, _ = put_transcript(store, id=docs[1]["id"], text="latest full version", start_time=101)
    rest = await feed.get_updates(first["next_cursor"], limit=10)
    by_id = {item["id"]: item for item in rest["items"]}
    assert set(by_id) == {docs[1]["id"], docs[2]["id"]}
    assert by_id[changed["id"]]["text"] == "latest full version"


@pytest.mark.asyncio
async def test_audio_cleanup_keeps_recent_text_available(library):
    store, feed = library
    clip = store.add_pcm(bytes(32000), start_time=time.time()-2, source="test")
    put_transcript(store, clip_id=clip["id"], text="保留文字", start_time=clip["start_time"], end_time=clip["end_time"])
    before = await feed.get_updates()
    assert before["items"][0]["recording_available"] is True
    store.prune(now=time.time()+90000)
    after = await feed.get_updates(before["next_cursor"])
    assert len(after["items"]) == 1 and after["items"][0]["text"] == "保留文字"
    assert after["items"][0]["recording_available"] is False


@pytest.mark.asyncio
async def test_long_poll_observes_append_and_timeout_does_not_consume_cursor(library):
    store, feed = library
    cursor = (await feed.get_updates())["next_cursor"]
    poll = asyncio.create_task(feed.get_updates(cursor, wait_seconds=.5))
    await asyncio.sleep(.04)
    value, _ = put_transcript(store)
    page = await poll
    assert page["items"][0]["id"] == value["id"] and not page["timed_out"]
    start = time.monotonic()
    timeout = await feed.get_updates(page["next_cursor"], wait_seconds=.05)
    assert .04 <= time.monotonic()-start < .4
    assert timeout["timed_out"] and timeout["items"] == []
    assert timeout["next_cursor"] == page["next_cursor"]


@pytest.mark.asyncio
async def test_get_updates_has_no_file_changes_no_wav_reads_or_model_calls(library):
    store, feed = library
    put_transcript(store)
    before = {str(path): (path.stat().st_mtime_ns, path.stat().st_size)
              for path in store.root.rglob("*") if path.is_file()}
    with mock.patch("wave.open", side_effect=AssertionError("No audio read")), \
            mock.patch("audio_agent.pipeline.transcribe_clip", side_effect=AssertionError("No ASR")), \
            mock.patch("audio_agent.speakers.analyze_audio", side_effect=AssertionError("No speaker model")), \
            mock.patch("audio_agent.capture.AudioCapture.start", side_effect=AssertionError("No microphone")):
        page = await feed.get_updates()
        await feed.get_updates(page["next_cursor"], wait_seconds=.02)
    after = {str(path): (path.stat().st_mtime_ns, path.stat().st_size)
             for path in store.root.rglob("*") if path.is_file()}
    assert before == after
    assert not (store.root / "speakers").exists()
    assert not (store.root / "capture.json").exists()


@pytest.mark.asyncio
async def test_cursors_reject_tampering_other_instance_and_oversize(library):
    store, feed = library
    good = (await feed.get_updates())["next_cursor"]
    other = UpdatesFeed(store)
    with pytest.raises(ValueError, match="重启.*省略 cursor"):
        await other.get_updates(good)
    for bad in ("invalid", "A"*1025, good[:-4]+"AAAA", 12, "../path"):
        with pytest.raises(ValueError, match="游标"):
            await feed.get_updates(bad)


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": 1.5},
                                   {"wait_seconds": -1}, {"wait_seconds": 31}, {"wait_seconds": True},
                                   {"wait_seconds": 10**400},
                                   {"wait_seconds": float("nan")}, {"wait_seconds": float("inf")}])
async def test_invalid_limits_are_rejected_without_reading(library, kwargs):
    _, feed = library
    with mock.patch.object(feed, "_snapshot", side_effect=AssertionError("invalid request must not scan")):
        with pytest.raises(ValueError):
            await feed.get_updates(**kwargs)
