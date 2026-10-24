"""Cross-module HTTP regressions beyond the server's basic endpoint tests."""
import http.client
import json
import os
from pathlib import Path
import struct
import threading
import time
from urllib.parse import quote

import pytest

from audio_agent.store import AudioStore
from audio_agent.web import create_server


@pytest.fixture
def web_store(tmp_path):
    root = tmp_path / "data"
    store = AudioStore(root)
    server = create_server(root, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def get(path):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            payload = response.read()
            if "application/json" in response.getheader("Content-Type", ""):
                payload = json.loads(payload)
            return response.status, payload
        finally:
            connection.close()

    try:
        yield store, get
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_transcript_search_includes_records_beyond_recent_hundred(web_store):
    store, get = web_store
    folder = store.root / "transcripts"
    folder.mkdir(exist_ok=True)
    now = time.time()
    target_id = None
    for index in range(105):
        transcript_id = f"{index:032x}"
        text = "ordinary recent transcript"
        if index == 104:
            text = "unique older phrase"
            target_id = transcript_id
        record = {"id": transcript_id, "text": text, "created_at": now - index,
                  "source": "test-fixture", "segments": []}
        path = folder / f"{transcript_id}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        os.utime(path, (now - index, now - index))
    status, page = get("/api/transcripts?limit=10")
    assert status == 200
    assert page["total"] == 105
    status, found = get("/api/transcripts?q=" + quote("unique older phrase"))
    assert status == 200
    assert found["total"] == 1
    assert found["items"][0]["id"] == target_id
    status, detail = get(f"/api/transcripts/{target_id}")
    assert status == 200
    assert detail["text"] == "unique older phrase"


def test_equal_timestamp_clip_pages_have_no_duplicates_or_omissions(web_store):
    store, get = web_store
    ids = []
    recorded_at = time.time() - 1
    for value in range(8):
        clip = store.add_pcm(struct.pack("<h", value) * 1600, start_time=recorded_at, source="test-mic")
        ids.append(clip["id"])
    with store._connect() as db:
        db.execute("UPDATE clips SET created_time=?", (time.time(),))
    returned = []
    for offset in (0, 3, 6):
        status, page = get(f"/api/clips?limit=3&offset={offset}")
        assert status == 200
        assert page["total"] == 8
        returned.extend(item["id"] for item in page["items"])
    assert len(returned) == len(set(returned)) == 8
    assert set(returned) == set(ids)


def test_expired_clip_audio_is_404_not_a_file_path_fallback(web_store):
    store, get = web_store
    clip = store.add_pcm(struct.pack("<h", 1000) * 1600, start_time=time.time() - 1, source="test-mic")
    endpoint = f"/api/clips/{clip['id']}/audio"
    status, audio = get(endpoint)
    assert status == 200 and audio[:4] == b"RIFF"
    store.prune(now=time.time() + 2 * 86400)
    assert not Path(clip["path"]).exists()
    status, error = get(endpoint)
    assert status == 404
    assert "error" in error
