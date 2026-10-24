import http.client
import io
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import quote
import wave
from unittest import mock

import pytest

from audio_agent import web


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * 1600)
    return output.getvalue()


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "discover_models", lambda: [])
    server = web.create_server(tmp_path / "data", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(server, path, *, method="GET", body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    raw = response.read()
    values = dict(response.getheaders())
    status = response.status
    connection.close()
    if values.get("Content-Type", "").startswith("application/json"):
        raw = json.loads(raw)
    return status, raw, values


def mutation_headers(server, content_type="application/json"):
    return {"X-Audio-Agent-Token": server.token, "Content-Type": content_type,
            "Origin": f"http://127.0.0.1:{server.server_port}"}


def add_clip(server):
    return server.store.add_pcm(b"\x01\x00" * 1600, start_time=time.time() - 1, source="test")


def test_read_only_api_never_transcribes_or_opens_capture(server, monkeypatch):
    asr = mock.Mock(side_effect=AssertionError("must not transcribe"))
    monkeypatch.setattr(web, "transcribe_clip", asr)
    clip = add_clip(server)
    status, response, _ = request(server, "/api/status")
    assert status == 200
    assert response["storage"]["clip_count"] == 1
    assert response["capture"]["running"] is False
    assert response["models"] == []
    assert response["transcript_count"] == 0
    status, response, _ = request(server, "/api/clips")
    assert response["total"] == 1
    assert response["items"][0]["id"] == clip["id"]
    assert response["items"][0]["transcribed"] is False
    assert "path" not in response["items"][0]
    status, response, headers = request(server, f"/api/clips/{clip['id']}/audio")
    assert status == 200 and response.startswith(b"RIFF")
    assert headers["Content-Type"] == "audio/wav"
    asr.assert_not_called()


def test_session_and_all_origins_are_local_only(server):
    status, response, headers = request(server, "/api/session")
    assert status == 200 and response["token"] == server.token
    assert "Access-Control-Allow-Origin" not in headers
    for path in ("/api/session", "/api/status", "/api/clips", "/api/document", "/"):
        status, response, _ = request(server, path, headers={"Origin": "https://hostile.example"})
        assert status == 403 and response["error"]["code"] == "invalid_origin"
        status, response, _ = request(server, path, headers={"Host": "hostile.example"})
        assert status == 403 and response["error"]["code"] == "invalid_host"
    status, _, _ = request(server, "/api/status", headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403
    with pytest.raises(ValueError, match="loopback"):
        web.create_server(server.store.root, host="0.0.0.0")


def test_writes_need_token_and_import_keeps_filename(server):
    body = wav_bytes()
    status, response, _ = request(server, "/api/import", method="POST", body=body,
                                  headers={"Content-Type": "audio/wav"})
    assert status == 403 and response["error"]["code"] == "invalid_token"
    headers = mutation_headers(server, "audio/wav") | {"X-Filename": quote("测试录音.wav")}
    status, response, _ = request(server, "/api/import", method="POST", body=body, headers=headers)
    assert status == 201
    clip = response["clip"]
    assert clip["display_name"] == "测试录音.wav"
    assert clip["original_name"] == "测试录音.wav"
    assert clip["transcribed"] is False
    status, second, _ = request(server, "/api/import", method="POST", body=body, headers=headers)
    assert second["clip"]["id"] == clip["id"]
    assert second["clip"]["deduplicated"] is True
    assert list((server.store.root / "incoming").iterdir()) == []
    status, response, _ = request(server, "/api/clips?q=" + quote("测试录音"))
    assert response["total"] == 1


def test_invalid_upload_and_filename_leave_no_staged_audio(server, monkeypatch):
    headers = mutation_headers(server, "audio/wav")
    status, response, _ = request(server, "/api/import", method="POST", body=b"not audio" * 8, headers=headers)
    assert status == 400 and "Traceback" not in str(response)
    status, _, _ = request(server, "/api/import", method="POST", body=wav_bytes(),
                           headers=headers | {"X-Filename": quote("../outside.wav")})
    assert status == 400
    monkeypatch.setattr(web, "MAX_UPLOAD_BYTES", 32)
    status, _, _ = request(server, "/api/import", method="POST", body=wav_bytes(), headers=headers)
    assert status == 413
    assert server.store.status()["clip_count"] == 0
    assert list((server.store.root / "incoming").iterdir()) == []


def test_wav_ranges_include_suffix_and_unsatisfiable(server):
    clip = add_clip(server)
    original = Path(clip["path"]).read_bytes()
    endpoint = f"/api/clips/{clip['id']}/audio"
    status, body, headers = request(server, endpoint, headers={"Range": "bytes=4-11"})
    assert status == 206 and body == original[4:12]
    assert headers["Content-Range"] == f"bytes 4-11/{len(original)}"
    status, body, _ = request(server, endpoint, headers={"Range": "bytes=-6"})
    assert status == 206 and body == original[-6:]
    for invalid in ("bytes=999999-", "bytes=-0", "bytes=10-4", "bytes=0-1,4-8", "bytes=-"):
        status, _, headers = request(server, endpoint, headers={"Range": invalid})
        assert status == 416
        assert headers["Content-Range"] == f"bytes */{len(original)}"
    status, body, headers = request(server, endpoint, method="HEAD", headers={"Range": "bytes=0-3"})
    assert status == 206 and body == b"" and headers["Content-Length"] == "4"


def test_clip_filters_join_retained_transcript_content(server):
    first = add_clip(server)
    second = add_clip(server)
    folder = server.store.root / "transcripts"
    folder.mkdir()
    transcript = {"id": "a" * 32, "clip_id": first["id"], "created_at": time.time(),
                  "text": "独特的本地转写内容", "segments": [{"start": 0, "end": 0.1, "text": "本地内容"}]}
    (folder / (transcript["id"] + ".json")).write_text(json.dumps(transcript))
    status, response, _ = request(server, "/api/clips?status=transcribed")
    assert response["total"] == 1 and response["items"][0]["id"] == first["id"]
    status, response, _ = request(server, "/api/clips?status=pending")
    assert response["total"] == 1 and response["items"][0]["id"] == second["id"]
    status, response, _ = request(server, "/api/clips?q=" + quote("独特"))
    assert response["total"] == 1
    assert response["items"][0]["latest_transcript"]["text"] == transcript["text"]
    status, response, _ = request(server, "/api/transcripts?clip_id=" + first["id"])
    assert response["total"] == 1
    status, response, _ = request(server, "/api/transcripts/" + transcript["id"])
    assert response == transcript


def test_bad_transcript_metadata_does_not_break_library(server):
    folder = server.store.root / "transcripts"
    folder.mkdir()
    for digit, value in (("a", "yesterday"), ("b", float("nan")), ("c", float("inf"))):
        (folder / (digit * 32 + ".json")).write_text(json.dumps({"id": digit * 32, "created_at": value, "text": "bad"}))
    status, response, _ = request(server, "/api/transcripts")
    assert status == 200 and response["total"] == 0
    assert request(server, "/api/status")[0] == 200


def test_explicit_transcription_is_async_and_rejects_second_job(server, monkeypatch):
    clip = add_clip(server)
    started, release = threading.Event(), threading.Event()
    def transcribe(store, clip_id, *, language):
        started.set()
        assert release.wait(timeout=5)
        return {"id": "b" * 32, "clip_id": clip_id, "text": "本地转写", "language": language}
    monkeypatch.setattr(web, "transcribe_clip", transcribe)
    body = json.dumps({"clip_id": clip["id"], "language": "zh"})
    try:
        status, response, _ = request(server, "/api/transcribe", method="POST", body=body, headers=mutation_headers(server))
        assert status == 202 and response["job"]["status"] == "queued"
        job_id = response["job"]["id"]
        assert started.wait(timeout=2)
        status, response, _ = request(server, "/api/transcribe", method="POST", body=body, headers=mutation_headers(server))
        assert status == 409 and response["error"]["code"] == "transcription_busy"
        status, response, _ = request(server, "/api/jobs/" + job_id)
        assert response["job"]["status"] == "running"
    finally:
        release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status, response, _ = request(server, "/api/jobs/" + job_id)
        if response["job"]["status"] == "completed":
            break
        time.sleep(0.01)
    assert response["job"]["result"]["text"] == "本地转写"


def test_failed_job_exposes_error_without_http_traceback(server, monkeypatch):
    clip = add_clip(server)
    def fail(*args, **kwargs):
        raise RuntimeError("Local model unavailable")
    monkeypatch.setattr(web, "transcribe_clip", fail)
    status, response, _ = request(server, "/api/transcribe", method="POST",
                                  body=json.dumps({"clip_id": clip["id"]}), headers=mutation_headers(server))
    assert status == 202
    job_id = response["job"]["id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        _, response, _ = request(server, "/api/jobs/" + job_id)
        if response["job"]["status"] == "failed":
            break
        time.sleep(0.01)
    assert response["job"]["error"] == "Local model unavailable"
    assert response["job"]["result"] is None


def test_static_files_cannot_read_outside_asset_folder(server, tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<title>Audio Agent</title>")
    (tmp_path / "secret.txt").write_text("private")
    (assets / "outside.html").symlink_to(tmp_path / "secret.txt")
    monkeypatch.setattr(web, "ASSETS_DIR", assets)
    assert request(server, "/")[0] == 200
    assert request(server, "/%2e%2e/secret.txt")[0] == 404
    assert request(server, "/outside.html")[0] == 404
    assert request(server, "/api/clips/../../secret.txt/audio")[0] == 404


def document_transcript(server, index, **values):
    folder = server.store.root / "transcripts"
    folder.mkdir(exist_ok=True)
    item = {"id": f"{index:032x}", "text": f"完整段落 {index}", "created_at": time.time() - 2,
            "segments": [], "audio": {"start_time": 100 + index, "end_time": 101 + index}, **values}
    (folder / f"{item['id']}.json").write_text(json.dumps(item, ensure_ascii=False))
    return item


def test_document_get_is_read_only_and_preserves_chronological_metadata(server, monkeypatch):
    text = "这是完整的未截断正文。" * 2000
    segments = [{"start": index, "end": index + 1, "text": "完整分段"} for index in range(300)]
    original = document_transcript(server, 1, text=text, segments=segments, source="live:session")
    with mock.patch("audio_agent.capture.AudioCapture.start", side_effect=AssertionError("No microphone")) as capture, \
            mock.patch("audio_agent.vad.SileroVad.__init__", side_effect=AssertionError("No VAD load")) as vad, \
            mock.patch("audio_agent.speakers.analyze_audio", side_effect=AssertionError("No speaker inference")) as speakers, \
            mock.patch("audio_agent.asr.transcribe", side_effect=AssertionError("No ASR load")) as asr:
        status, result, _ = request(server, "/api/document")
        assert status == 200
        assert result["total"] == 1 and result["next_cursor"] is None
        assert isinstance(result["snapshot"], (int, float)) and len(result["revision"]) == 64
        item = result["items"][0]
        assert item["id"] == original["id"] and item["text"] == text and item["segments"] == segments
        assert item["start_time"] == 101 and item["end_time"] == 102
        assert item["source_kind"] == "live" and item["source"] == "live:session"
        assert item["recording_available"] is False and item["display_name"]
        capture.assert_not_called()
        vad.assert_not_called()
        speakers.assert_not_called()
        asr.assert_not_called()


def test_document_default_two_hundred_pagination_returns_all_full_entries(server):
    for index in range(205):
        document_transcript(server, index)
    status, first, _ = request(server, "/api/document")
    assert status == 200 and first["total"] == 205 and len(first["items"]) == 200
    status, second, _ = request(server, "/api/document?cursor=" + quote(first["next_cursor"]))
    assert status == 200 and second["total"] == 205 and len(second["items"]) == 5
    assert second["next_cursor"] is None
    assert first["snapshot"] == second["snapshot"] and first["revision"] == second["revision"]
    ids = [item["id"] for item in first["items"] + second["items"]]
    assert ids == [f"{index:032x}" for index in range(205)]


@pytest.mark.parametrize("query", [
    "cursor=not-valid", "cursor=", "limit=0", "limit=201", "limit=NaN", "limit=1.5",
    "source=", "source=%00", "limit=1&limit=2", "unknown=value",
])
def test_document_invalid_queries_return_specific_clean_error(server, query):
    status, result, _ = request(server, "/api/document?" + query)
    assert status == 400 and result["error"]["code"] == "invalid_document_query"
    assert "Traceback" not in json.dumps(result)


def test_document_source_query_is_exact_and_bound_to_cursor(server):
    document_transcript(server, 1, source="live:one")
    document_transcript(server, 2, source="live:one")
    document_transcript(server, 3, source="live:one-extra")
    status, first, _ = request(server, "/api/document?limit=1&source=live%3Aone")
    assert status == 200 and first["total"] == 2
    cursor = quote(first["next_cursor"])
    assert request(server, "/api/document?limit=1&source=live%3Aone&cursor=" + cursor)[0] == 200
    status, error, _ = request(server, "/api/document?limit=1&cursor=" + cursor)
    assert status == 400 and error["error"]["code"] == "invalid_document_query"


def test_document_existing_speaker_annotations_preserve_metadata_and_update_revision(server):
    from audio_agent.speaker_service import _atomic_json
    source = server.store.add_pcm(bytes(6 * 32000), start_time=time.time() - 10, source="live:annotated")
    original = document_transcript(server, 1, clip_id=source["id"], text="完整可复制文本",
                                   segments=[{"start": 0, "end": 6, "text": "完整可复制文本"}])
    status, anonymous, _ = request(server, "/api/document")
    analysis = {"id": "a" * 32, "clip_id": source["id"], "created_at": time.time(),
                "speaker_count": 1, "backend": "fake-local", "embedding_model_id": "model-a", "elapsed_seconds": .1,
                "turns": [{"start": 0, "end": 6, "speaker": "speaker_1"}], "notes": [],
                "speakers": [{"id": "speaker_1", "embedding": [1., 0.], "speech_seconds": 6, "reference_seconds": 6}]}
    _atomic_json(server.speakers.folder / f"{source['id']}.json", analysis)
    status, labeled, _ = request(server, "/api/document")
    assert status == 200 and labeled["revision"] != anonymous["revision"]
    item = labeled["items"][0]
    assert item["segments"][0]["speaker_labels"] == ["说话人 1"]
    assert item["id"] == original["id"] and item["text"] == original["text"]
    assert item["start_time"] == source["start_time"] and item["source_kind"] == "live"
    assert item["recording_available"] is True and item["display_name"]
    assert "embedding" not in json.dumps(labeled)
    server.speakers.enroll(source["id"], "speaker_1", True, analysis_id=analysis["id"])
    status, enrolled, _ = request(server, "/api/document")
    assert status == 200 and enrolled["revision"] != labeled["revision"]
    assert enrolled["items"][0]["segments"][0]["speaker_labels"] == ["我"]
    server.speakers.clear()
    status, cleared, _ = request(server, "/api/document")
    assert cleared["revision"] != enrolled["revision"]
    assert cleared["items"][0]["segments"][0]["speaker_labels"] == ["说话人 1"]
    persisted = json.loads((server.store.root / "transcripts" / f"{original['id']}.json").read_text())
    assert "speaker_labels" not in persisted["segments"][0]


def test_document_revision_uses_private_metadata_only_and_changes_on_update_or_removal(server):
    path = server.store.root / "voiceprint.json"
    initial = web._document_revision(server.store, "base")
    path.write_text("private contents do not need to be valid JSON for a metadata fingerprint")
    with mock.patch.object(Path, "read_text", side_effect=AssertionError("No private read")), \
            mock.patch.object(Path, "read_bytes", side_effect=AssertionError("No private read")):
        first = web._document_revision(server.store, "base")
    assert first != initial and len(first) == 64
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
    second = web._document_revision(server.store, "base")
    assert first != second
    path.unlink()
    assert web._document_revision(server.store, "base") == initial
    sidecar = server.speakers.folder / f"{'b'*32}.json"
    sidecar.write_text("private speaker vector")
    assert web._document_revision(server.store, "base") != initial
    sidecar.unlink()
    assert web._document_revision(server.store, "base") == initial
    named = server.store.root / "speaker-profiles.json"
    named.write_text("private named reference metadata")
    with mock.patch.object(Path, "read_text", side_effect=AssertionError("No private read")), \
            mock.patch.object(Path, "read_bytes", side_effect=AssertionError("No private read")):
        named_revision = web._document_revision(server.store, "base")
    assert named_revision != initial
    named.unlink()
    assert web._document_revision(server.store, "base") == initial
