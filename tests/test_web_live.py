"""Live HTTP contract without sockets, real microphones or model loading.

Scheduling, ASR lock retries and end-of-recording flush are tested in test_live.
Here the actual request validation/router runs against an inert live manager.
"""
from email.message import Message
import io
import json
from types import SimpleNamespace
import threading
import time
from unittest import mock

import pytest

from audio_agent import cli, web
from audio_agent.store import AudioStore


class FakeLive:
    def __init__(self):
        self.actions = []
        self.value = {"state": "idle", "active": False, "device": None,
                      "pending_count": 0, "recent_transcripts": [], "chunk_seconds": 5}

    def status(self):
        return dict(self.value)

    def start(self, device, language="zh"):
        self.actions.append(("start", device, language))
        self.value.update(state="starting", active=True, device=device, language=language)
        return self.status()

    def stop(self):
        self.actions.append(("stop",))
        self.value.update(state="idle", active=False)
        return self.status()


@pytest.fixture
def fake_server(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "discover_models", lambda: [])
    monkeypatch.setattr(web, "read_capture_status", lambda root: {"active": False, "running": False})
    forbidden_asr = mock.Mock(side_effect=AssertionError("GET must not start ASR"))
    monkeypatch.setattr(web, "transcribe_clip", forbidden_asr)
    server = SimpleNamespace(store=AudioStore(tmp_path / "data"), live=FakeLive(),
                             token="test-session-token", server_port=8767,
                             _request_lock=threading.RLock(),
                             jobs=SimpleNamespace(items=lambda: []))
    server.forbidden_asr = forbidden_asr
    return server


def request(server, path, *, method="GET", body=None, token=False, origin=None):
    """Run real handler dispatch against in-memory request/response objects."""
    handler = web.AudioRequestHandler.__new__(web.AudioRequestHandler)
    handler.server = server
    handler.command = method
    handler.path = path
    handler.client_address = ("127.0.0.1", 45678)
    handler.close_connection = False
    handler.headers = Message()
    handler.headers["Host"] = "127.0.0.1:8767"
    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    if body is not None:
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(payload))
    if token:
        handler.headers["X-Audio-Agent-Token"] = server.token
    if origin is not None:
        handler.headers["Origin"] = origin
    handler.rfile = io.BytesIO(payload)
    handler.wfile = io.BytesIO()
    responses = []
    handler._json = lambda status, data, **kwargs: responses.append((status, data))
    handler._handle()
    assert len(responses) == 1
    return responses[0]


def test_get_live_status_and_library_never_start_microphone_or_asr(fake_server):
    status, live = request(fake_server, "/api/live")
    assert status == 200 and live["live"]["active"] is False
    status, overall = request(fake_server, "/api/status")
    assert status == 200 and overall["live"]["active"] is False
    status, clips = request(fake_server, "/api/clips")
    assert status == 200 and clips["total"] == 0
    assert fake_server.live.actions == []
    fake_server.forbidden_asr.assert_not_called()


def test_device_list_only_enumerates_and_does_not_start_capture(fake_server, monkeypatch):
    enumerator = mock.Mock(return_value=[{"index": 0, "name": "Test microphone"}])
    monkeypatch.setattr(web, "list_audio_devices", enumerator)
    status, response = request(fake_server, "/api/devices")
    assert status == 200
    assert response["items"] == [{"index": 0, "name": "Test microphone"}]
    enumerator.assert_called_once()
    assert fake_server.live.actions == []


def test_live_start_requires_token_and_same_origin(fake_server):
    body = {"device": "Test microphone", "language": "zh"}
    status, error = request(fake_server, "/api/live/start", method="POST", body=body)
    assert status == 403 and error["error"]["code"] == "invalid_token"
    status, error = request(fake_server, "/api/live/start", method="POST", body=body,
                            token=True, origin="https://example.invalid")
    assert status == 403 and error["error"]["code"] == "invalid_origin"
    assert fake_server.live.actions == []
    status, response = request(fake_server, "/api/live/start", method="POST", body=body,
                               token=True, origin="http://127.0.0.1:8767")
    assert status in (200, 202) and response["live"]["active"] is True
    assert fake_server.live.actions == [("start", "Test microphone", "zh")]


@pytest.mark.parametrize("body", [
    {}, {"device": ""}, {"device": None}, {"device": 0},
    {"device": "Test microphone", "language": "unknown"},
    {"device": "Test microphone", "path": "/arbitrary/input"},
])
def test_invalid_start_does_not_reach_recorder(fake_server, body):
    status, error = request(fake_server, "/api/live/start", method="POST", body=body, token=True)
    assert status == 400 and "error" in error
    assert fake_server.live.actions == []


def test_stop_is_token_protected_and_only_targets_server_owned_manager(fake_server, monkeypatch):
    external_stop = mock.Mock(side_effect=AssertionError("must not stop CLI-owned capture"))
    monkeypatch.setattr(cli, "stop_background", external_stop)
    status, _ = request(fake_server, "/api/live/stop", method="POST", body={})
    assert status == 403 and fake_server.live.actions == []
    status, response = request(fake_server, "/api/live/stop", method="POST", body={}, token=True)
    assert status in (200, 202) and response["live"]["active"] is False
    assert fake_server.live.actions == [("stop",)]
    external_stop.assert_not_called()


def test_library_poll_returns_new_capture_clips_without_starting_asr(fake_server):
    status, before = request(fake_server, "/api/clips")
    assert status == 200 and before["total"] == 0
    clip = fake_server.store.add_pcm(b"\xe8\x03" * 1600, start_time=time.time() - 1,
                                    source="live:test-session")
    status, after = request(fake_server, "/api/clips")
    assert status == 200 and after["total"] == 1
    assert after["items"][0]["id"] == clip["id"]
    assert fake_server.live.actions == []
    fake_server.forbidden_asr.assert_not_called()
