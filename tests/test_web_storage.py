"""Storage HTTP contract using isolated libraries and synthetic PCM only.

No real home config, microphone, ASR or diarization model is used. The fake
speaker references below belong only to these temporary test libraries.
"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import threading
import time
from unittest import mock
import uuid

import pytest

from audio_agent import storage_config, web
from audio_agent.live import LiveManager
from audio_agent.speaker_service import SpeakerManager, _atomic_json


@pytest.fixture
def storage_server(tmp_path, monkeypatch):
    config_path = tmp_path / "settings" / "storage.json"
    forbidden = mock.Mock(side_effect=AssertionError("No hardware or model inference in storage tests"))
    monkeypatch.setattr(web, "discover_models", lambda: [])
    monkeypatch.setattr(web, "list_audio_devices", forbidden)
    monkeypatch.setattr(web, "transcribe_clip", forbidden)
    monkeypatch.setattr(web, "vad_model_status", lambda: {"available": False, "reason": "test"})
    real_resolve = storage_config.resolve_storage_location

    def resolve(root, *, config_path):
        return real_resolve(root, config_path=config_path, home=tmp_path / "fake-home",
                            env={}, platform="darwin", project_root=tmp_path / "fake-project")

    def speaker_manager(store, **kwargs):
        return SpeakerManager(store, analyzer=forbidden,
                              model_status=lambda: {"ready": True, "backend": "test-fake", "reason": None},
                              **kwargs)

    def live_manager(store, **kwargs):
        return LiveManager(store, capture_factory=forbidden, transcriber=forbidden, **kwargs)

    monkeypatch.setattr(web, "resolve_storage_location", resolve)
    monkeypatch.setattr(web, "SpeakerManager", speaker_manager)
    monkeypatch.setattr(web, "LiveManager", live_manager)
    server = web.create_server(tmp_path / "original", port=0, storage_config_path=config_path)
    server.test_config_path = config_path
    server.test_inference = forbidden
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()
        forbidden.assert_not_called()


def request(server, path, *, payload=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    method = "POST" if payload is not None else "GET"
    body = json.dumps(payload).encode() if payload is not None else None
    supplied = {"Content-Type": "application/json"} if payload is not None else {}
    supplied.update(headers or {})
    try:
        connection.request(method, path, body=body, headers=supplied)
        response = connection.getresponse()
        raw = response.read()
        result = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
        return response.status, result
    finally:
        connection.close()


def post(server, payload, *, token=True):
    headers = {"Origin": f"http://127.0.0.1:{server.server_port}"}
    if token:
        headers["X-Audio-Agent-Token"] = server.token
    return request(server, "/api/storage", payload=payload, headers=headers)


def destination(tmp_path):
    return tmp_path / "target"


def assert_unmoved(server, original, target):
    assert server.store is original
    assert server.store.root == original.root
    assert not (original.root / ".moved-to.json").exists()
    assert not target.exists() or not any(target.iterdir())


def add_clip(server):
    return server.store.add_pcm(b"\x10\x00" * 32_000, start_time=time.time() - 2,
                                source="test-synthetic-storage")


def seed_library(server):
    clip = add_clip(server)
    analysis_id = uuid.uuid4().hex
    analysis = {
        "id": analysis_id, "clip_id": clip["id"], "created_at": time.time(),
        "embedding_model_id": "storage-test-fake-model", "backend": "test-fake",
        "elapsed_seconds": .001, "speaker_count": 2, "notes": [],
        "speakers": [
            {"id": "speaker_1", "embedding": [1., 0.], "speech_seconds": 1., "reference_seconds": 1.},
            {"id": "speaker_2", "embedding": [0., 1.], "speech_seconds": 1., "reference_seconds": 1.},
        ],
        "turns": [{"start": 0., "end": 1., "speaker": "speaker_1"},
                  {"start": 1., "end": 2., "speaker": "speaker_2"}],
    }
    _atomic_json(server.speakers.folder / f"{clip['id']}.json", analysis)
    server.speakers.enroll(clip["id"], "speaker_1", confirm=True, analysis_id=analysis_id)
    profile = server.speakers.enroll_profile(
        clip_id=clip["id"], speaker_id="speaker_2", analysis_id=analysis_id,
        confirm=True, name="合成人物", note="仅测试虚拟声纹")["profile"]
    server.speakers.update_settings(True)
    server.live.update_settings(-32.)
    transcript = {
        "id": uuid.uuid4().hex, "clip_id": clip["id"], "created_at": time.time(),
        "start_time": clip["start_time"], "end_time": clip["end_time"],
        "source": clip["source"], "text": "第一段。第二段。", "language": "zh", "duration_seconds": 2.,
        "segments": [{"start": 0., "end": 1., "text": "第一段。"},
                     {"start": 1., "end": 2., "text": "第二段。"}],
    }
    folder = server.store.root / "transcripts"
    folder.mkdir(exist_ok=True)
    _atomic_json(folder / f"{transcript['id']}.json", transcript)
    return clip, transcript, profile


def test_storage_get_is_read_only_and_reports_actual_paths_and_txt_behavior(storage_server, tmp_path):
    server = storage_server
    before = {p.relative_to(server.store.root) for p in server.store.root.rglob("*")}
    assert not server.test_config_path.exists()
    status, result = request(server, "/api/storage")
    assert status == 200
    assert result["current_dir"] == str(server.store.root)
    assert result["paths"] == {"recordings": str(server.store.root / "clips"),
                               "transcripts": str(server.store.root / "transcripts")}
    assert result["default_dir"] == str(tmp_path / "fake-project" / ".data")
    assert result["config_path"] == str(server.test_config_path)
    assert result["configured_dir"] is None
    assert result["retention_hours"] == 24
    assert result["can_change"] is True and result["busy_reasons"] == []
    assert not server.test_config_path.exists()
    assert not Path(result["default_dir"]).exists()
    assert {p.relative_to(server.store.root) for p in server.store.root.rglob("*")} == before
    status, html = request(server, "/")
    assert status == 200
    assert "TXT 仅在点击" in html.decode()
    assert "不会在资料目录自动生成 TXT" in html.decode()
    assert list(server.store.root.rglob("*.txt")) == []


def test_storage_post_requires_session_token_before_creating_a_target(storage_server, tmp_path):
    server, target = storage_server, destination(tmp_path)
    original = server.store
    payload = {"directory": str(target), "copy_existing": True}
    status, result = post(server, payload, token=False)
    assert status == 403 and result["error"]["code"] == "invalid_token"
    status, result = request(server, "/api/storage", payload=payload,
                             headers={"X-Audio-Agent-Token": "incorrect"})
    assert status == 403 and result["error"]["code"] == "invalid_token"
    assert_unmoved(server, original, target)
    assert not server.test_config_path.exists()


@pytest.mark.parametrize("extra", [{}, {"copy_existing": False}, {"copy_existing": 1},
                                    {"copy_existing": "true"}, {"copy_existing": True, "move": True}])
def test_storage_post_requires_explicit_boolean_copy(storage_server, tmp_path, extra):
    server, target = storage_server, destination(tmp_path)
    original = server.store
    status, result = post(server, {"directory": str(target), **extra})
    assert status == 400 and result["error"]["code"] == "invalid_storage_settings"
    assert_unmoved(server, original, target)
    assert not server.test_config_path.exists()


@pytest.mark.parametrize("busy", ["live", "live-pending", "asr-queued", "speaker-queued"])
def test_active_capture_and_queued_work_block_migration(storage_server, tmp_path, monkeypatch, busy):
    server, target = storage_server, destination(tmp_path)
    original = server.store
    if busy.startswith("live"):
        status = server.live.status() | {"active": busy == "live", "pending_count": 1 if busy == "live-pending" else 0}
        monkeypatch.setattr(server.live, "status", lambda: status)
    elif busy == "asr-queued":
        monkeypatch.setattr(server.jobs, "items", lambda: [{"id": "test", "status": "queued"}])
    else:
        monkeypatch.setattr(server.speakers, "items", lambda: [{"id": "test", "status": "queued"}])
    code, result = request(server, "/api/storage")
    assert code == 200 and result["can_change"] is False and result["busy_reasons"]
    code, result = post(server, {"directory": str(target), "copy_existing": True})
    assert code == 409 and result["error"]["code"] == "storage_busy"
    assert_unmoved(server, original, target)
    assert not server.test_config_path.exists()


def test_real_copy_switches_runtime_preserving_ids_text_profiles_and_old_wav(storage_server, tmp_path):
    server, target = storage_server, destination(tmp_path)
    original = server.store
    old_live, old_jobs, old_speakers = server.live, server.jobs, server.speakers
    clip, transcript, profile = seed_library(server)
    original_wav = Path(clip["path"]).read_bytes()
    preserved = {name: (original.root / name).read_bytes() for name in (
        "voiceprint.json", "speaker-profiles.json", "speaker-settings.json", "live-settings.json",
        f"speakers/{clip['id']}.json", f"transcripts/{transcript['id']}.json")}
    status, response = post(server, {"directory": str(target), "copy_existing": True})
    assert status == 200, response
    assert response["previous_dir"] == str(original.root)
    assert response["copied"]["files"] >= len(preserved) + 2
    assert response["copied"]["bytes"] >= len(original_wav)
    assert response["storage"]["current_dir"] == str(target)
    assert response["storage"]["configured_dir"] == str(target)
    assert response["storage"]["source"] == "saved"
    assert json.loads(server.test_config_path.read_text()) == {"data_dir": str(target)}
    assert server.store.root == target and server.store is not original
    for manager in (server.live, server.jobs, server.speakers):
        assert manager.store is server.store
    assert server.live is not old_live and server.jobs is not old_jobs and server.speakers is not old_speakers
    assert Path(clip["path"]).read_bytes() == original_wav
    assert Path(server.store.get_clip(clip["id"])["path"]).is_relative_to(target)
    for name, content in preserved.items():
        assert (target / name).read_bytes() == content
        assert (original.root / name).read_bytes() == content
    status, result = request(server, "/api/storage")
    assert status == 200 and result["current_dir"] == str(target)
    status, audio = request(server, f"/api/clips/{clip['id']}/audio")
    assert status == 200 and audio == original_wav
    status, records = request(server, "/api/document")
    assert status == 200 and records["total"] == 1
    assert records["items"][0]["id"] == transcript["id"]
    assert records["items"][0]["text"] == transcript["text"]
    assert [segment["speaker_labels"] for segment in records["items"][0]["segments"]] == [["我"], ["合成人物 · 仅测试虚拟声纹"]]
    status, profiles = request(server, "/api/speaker-profiles")
    assert status == 200 and profiles["items"] == [profile] and profiles["voiceprint"]["enrolled"] is True
    assert "embedding" not in json.dumps(profiles)
    assert server.speakers.status()["automatic_enabled"] is True
    assert server.live.settings()["settings"]["trigger_db"] == pytest.approx(-32.)
    added = server.store.add_pcm(b"\x20\x00" * 1600, start_time=time.time() - .1, source="test-new-library")
    assert Path(added["path"]).is_relative_to(target)
    assert not (original.root / "clips" / f"{added['id']}.wav").exists()
    assert list(target.rglob("*.txt")) == []


def test_nonempty_target_is_never_overwritten(storage_server, tmp_path):
    server, target = storage_server, destination(tmp_path)
    original = server.store
    clip = add_clip(server)
    target.mkdir()
    sentinel = target / "keep-me.txt"
    sentinel.write_text("existing unrelated data")
    status, result = post(server, {"directory": str(target), "copy_existing": True})
    assert status == 400, result
    assert server.store is original and not server.test_config_path.exists()
    assert sentinel.read_text() == "existing unrelated data"
    assert list(target.iterdir()) == [sentinel]
    assert Path(clip["path"]).exists()
    assert not (original.root / ".moved-to.json").exists()


def test_config_write_failure_rolls_back_runtime_config_marker_and_target(storage_server, tmp_path, monkeypatch):
    server, target = storage_server, destination(tmp_path)
    original = server.store
    old_managers = (server.live, server.jobs, server.speakers)
    clip = add_clip(server)
    original_wav = Path(clip["path"]).read_bytes()
    storage_config.write_storage_config(original.root, config_path=server.test_config_path)
    config_before = server.test_config_path.read_bytes()
    failure = mock.Mock(side_effect=OSError("injected settings disk failure"))
    monkeypatch.setattr(web, "write_storage_config", failure)
    status, result = post(server, {"directory": str(target), "copy_existing": True})
    assert status == 500 and result["error"]["code"] == "storage_error"
    failure.assert_called_once()
    assert_unmoved(server, original, target)
    assert (server.live, server.jobs, server.speakers) == old_managers
    assert server.test_config_path.read_bytes() == config_before
    assert Path(clip["path"]).read_bytes() == original_wav
    status, current = request(server, "/api/storage")
    assert status == 200 and current["current_dir"] == str(original.root)
    assert current["configured_dir"] == str(original.root) and current["can_change"] is True
    # A failed switch must not strand the old runtime behind a migration fence.
    new_clip = original.add_pcm(b"\x30\x00" * 1600, start_time=time.time() - .1, source="test-after-failure")
    assert Path(new_clip["path"]).exists()
