"""Local speech HTTP validation, model inactivity, and explicit enrollment."""
import json
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from audio_agent import web
from audio_agent.speaker_service import SpeakerManager
from audio_agent.store import AudioStore
from test_web_live import request


@pytest.fixture
def server(tmp_path):
    store = AudioStore(tmp_path / "data")
    manager = SpeakerManager(store, analyzer=mock.Mock(side_effect=AssertionError("No model on GET")),
                             model_status=lambda: {"ready": False, "backend": "test", "reason": "missing"})
    result = SimpleNamespace(store=store, speakers=manager, server_port=8767, token="test",
                             _request_lock=threading.RLock(),
                             live=SimpleNamespace(start=mock.Mock(return_value={"active": True})))
    yield result
    manager.close()


def test_speech_gets_do_not_infer_or_enroll(server):
    clip = server.store.add_pcm(b"\x01\x00" * 1600, start_time=time.time()-1, source="test")
    assert request(server, "/api/speakers/status")[1]["available"] is False
    assert request(server, "/api/speakers/status")[1]["voiceprint"]["enrolled"] is False
    assert request(server, "/api/speaker-jobs")[1] == {"items": []}
    assert request(server, f"/api/clips/{clip['id']}/speakers")[1] == {"analysis": None}
    assert not (server.store.root / "voiceprint.json").exists()
    server.speakers.analyzer.assert_not_called()


@pytest.mark.parametrize("path,body", [
    ("/api/speakers/analyze", {"clip_id": "a"*32}),
    ("/api/speakers/settings", {"automatic_enabled": True}),
    ("/api/voiceprint/enroll", {"clip_id": "a"*32, "speaker_id": "speaker_1", "analysis_id": "b"*32, "confirm": True}),
    ("/api/voiceprint/clear", {}),
    ("/api/speaker-profiles/enroll", {"clip_id": "a"*32, "speaker_id": "speaker_1", "analysis_id": "b"*32,
                                       "confirm": True, "name": "同事", "note": ""}),
    ("/api/speaker-profiles/update", {"profile_id": "c"*32, "name": "同事", "note": ""}),
    ("/api/speaker-profiles/remove", {"profile_id": "c"*32}),
])
def test_all_speaker_mutations_require_token_and_origin(server, path, body):
    assert request(server, path, method="POST", body=body)[0] == 403
    assert request(server, path, method="POST", body=body, token=True, origin="https://evil.invalid")[0] == 403


@pytest.mark.parametrize("body", [None, {}, {"path": "/private/recording.wav"},
                                  {"clip_id": "a"*32, "num_speakers": True},
                                  {"clip_id": "a"*32, "num_speakers": 99}])
def test_invalid_analysis_payload_is_rejected(server, body):
    assert request(server, "/api/speakers/analyze", method="POST", body=body, token=True)[0] in (400, 415)


def test_enrollment_requires_exact_analysis_and_confirmation_fields(server):
    server.speakers.enroll = mock.Mock(return_value={"enrolled": True})
    body = {"clip_id": "a"*32, "speaker_id": "speaker_1", "confirm": True}
    assert request(server, "/api/voiceprint/enroll", method="POST", body=body, token=True)[0] == 400
    server.speakers.enroll.assert_not_called()
    body["analysis_id"] = "b"*32
    assert request(server, "/api/voiceprint/enroll", method="POST", body=body, token=True)[0] == 200
    server.speakers.enroll.assert_called_once_with("a"*32, "speaker_1", True, analysis_id="b"*32)


def test_vad_boolean_passes_only_on_explicit_start(server):
    body = {"device": "Test mic", "language": "zh", "vad_enabled": True}
    assert request(server, "/api/live/start", method="POST", body=body, token=True)[0] == 202
    server.live.start.assert_called_once_with("Test mic", language="zh", vad_enabled=True)
    server.live.start.reset_mock()
    body["vad_enabled"] = "false"
    assert request(server, "/api/live/start", method="POST", body=body, token=True)[0] == 400
    server.live.start.assert_not_called()


def test_prune_removes_clip_embeddings_but_preserves_confirmed_profile(server):
    clip = server.store.add_pcm(b"\x01\x00" * 1600, start_time=time.time()-1, source="test")
    sidecar = server.store.root / "speakers" / f"{clip['id']}.json"
    sidecar.write_text(json.dumps({"embedding": [1, 0]}))
    profile = server.store.root / "voiceprint.json"
    profile.write_text(json.dumps({"user_confirmed_reference": True}))
    server.store.prune(now=time.time()+48*3600)
    assert not sidecar.exists()
    assert profile.exists()


def test_automatic_setting_is_explicit_persistent_and_does_not_start_models_or_microphone(server):
    assert request(server, "/api/speakers/status")[1]["automatic_enabled"] is False
    status, response = request(server, "/api/speakers/settings", method="POST",
                               body={"automatic_enabled": True}, token=True)
    assert status == 200 and response["automatic_enabled"] is True
    assert response["automatic_error"] is None
    assert response["queued_count"] == response["running_count"] == 0
    assert json.loads((server.store.root / "speaker-settings.json").read_text()) == {"automatic_enabled": True}
    assert request(server, "/api/speakers/status")[1]["automatic_enabled"] is True
    assert request(server, "/api/speaker-jobs")[1] == {"items": []}
    server.speakers.analyzer.assert_not_called()
    server.live.start.assert_not_called()
    assert not (server.store.root / "voiceprint.json").exists()


@pytest.mark.parametrize("body", [None, {}, [], {"automatic_enabled": 1},
                                 {"automatic_enabled": "true"}, {"automatic_enabled": None},
                                 {"automatic_enabled": True, "path": "/private/file"}])
def test_automatic_setting_rejects_unknown_fields_and_non_booleans(server, body):
    status, _ = request(server, "/api/speakers/settings", method="POST", body=body, token=True)
    assert status in (400, 415)
    assert server.speakers.status()["automatic_enabled"] is False
    assert not (server.store.root / "speaker-settings.json").exists()


def test_named_profiles_http_roundtrip_refreshes_document_labels_and_revision_without_inference(server):
    from test_speaker_service import clip, persisted, result, assert_no_embeddings
    from test_updates import put_transcript
    status, initial_profiles = request(server, "/api/speaker-profiles")
    assert status == 200 and initial_profiles["items"] == []
    assert not server.speakers.profiles_path.exists()
    source = clip(server.store)
    analysis = persisted(server.speakers, source, result(reference_seconds=.5))
    put_transcript(server.store, clip_id=source["id"], segments=[{"start": 0, "end": 6, "text": "保留全文"}])
    _, anonymous = request(server, "/api/document")
    body = {"clip_id": source["id"], "speaker_id": "speaker_1", "analysis_id": analysis["id"],
            "confirm": True, "name": "林老师", "note": "导师"}
    status, created = request(server, "/api/speaker-profiles/enroll", method="POST", body=body, token=True)
    assert status == 200 and created["profile"]["display_label"] == "林老师 · 导师"
    profile_id = created["profile"]["id"]
    assert request(server, "/api/speaker-profiles")[1]["items"] == [created["profile"]]
    status, named = request(server, "/api/document")
    assert status == 200 and named["revision"] != anonymous["revision"]
    assert named["items"][0]["segments"][0]["speaker_labels"] == ["林老师 · 导师"]
    body = {"profile_id": profile_id, "name": "林老师", "note": "研究导师"}
    assert request(server, "/api/speaker-profiles/update", method="POST", body=body, token=True)[0] == 200
    _, edited = request(server, "/api/document")
    assert edited["revision"] != named["revision"]
    assert edited["items"][0]["segments"][0]["speaker_labels"] == ["林老师 · 研究导师"]
    status, removed = request(server, "/api/speaker-profiles/remove", method="POST", body={"profile_id": profile_id}, token=True)
    assert status == 200 and removed["items"] == []
    _, cleared = request(server, "/api/document")
    assert cleared["revision"] != edited["revision"]
    assert cleared["items"][0]["segments"][0]["speaker_labels"] == ["说话人 1"]
    for value in (created, named, edited, removed, cleared):
        assert_no_embeddings(value)
    server.speakers.analyzer.assert_not_called()
    server.live.start.assert_not_called()


@pytest.mark.parametrize("path,body", [
    ("enroll", {"clip_id": "a"*32, "speaker_id": "speaker_1", "analysis_id": "b"*32,
                "confirm": True, "name": "同事", "note": "", "embedding": [1, 0]}),
    ("enroll", {"clip_id": "a"*32, "speaker_id": "speaker_1", "confirm": True, "name": "同事", "note": ""}),
    ("update", {"profile_id": "a"*32, "name": "同事", "note": "", "clip_id": "b"*32}),
    ("remove", {"profile_id": "../voiceprint.json"}),
    ("remove", {"profile_id": "a"*32, "confirm": True}),
])
def test_named_profile_api_rejects_unknown_private_fields_and_invalid_identifiers(server, path, body):
    status, response = request(server, f"/api/speaker-profiles/{path}", method="POST", body=body, token=True)
    assert status == 400
    assert response["error"]["code"] == "invalid_speaker_request"
    assert not server.speakers.profiles_path.exists()


def test_multisample_http_append_remove_and_document_revision(server):
    from test_speaker_service import clip, persisted, result, assert_no_embeddings
    from test_updates import put_transcript
    status = request(server, '/api/speakers/status')[1]
    assert status['voiceprint_multisample'] is True and status['voiceprint_max_samples'] == 10
    sources = [clip(server.store) for _ in range(2)]
    analyses = [persisted(server.speakers, source, result([[1, 0]], reference_seconds=.5)) for source in sources]
    for source in sources:
        put_transcript(server.store, clip_id=source['id'], segments=[{'start':0,'end':6,'text':'测试全文'}])
    before = request(server, '/api/document')[1]
    states=[]
    for source,analysis in zip(sources,analyses):
        body={'clip_id':source['id'],'analysis_id':analysis['id'],'speaker_id':'speaker_1','confirm':True,'mode':'append'}
        code,payload=request(server,'/api/voiceprint/enroll',method='POST',body=body,token=True)
        assert code==200
        states.append(payload['voiceprint'])
    assert states[-1]['sample_count']==2 and states[-1]['total_reference_seconds']==1
    after=request(server,'/api/document')[1]
    assert before['revision']!=after['revision']
    assert all(item['segments'][0]['speaker_labels']==['我'] for item in after['items'])
    code,removed=request(server,'/api/voiceprint/samples/remove',method='POST',
                         body={'sample_id':states[0]['samples'][0]['id']},token=True)
    assert code==200 and removed['voiceprint']['sample_count']==1
    changed=request(server,'/api/document')[1]
    assert changed['revision']!=after['revision']
    labels={item['clip_id']:item['segments'][0]['speaker_labels'] for item in changed['items']}
    assert labels[sources[0]['id']]==['说话人 1'] and labels[sources[1]['id']]==['我']
    assert request(server,'/api/voiceprint/clear',method='POST',body={},token=True)[1]['voiceprint']['enrolled'] is False
    for value in (status,states,after,removed,changed):assert_no_embeddings(value)
    server.speakers.analyzer.assert_not_called();server.live.start.assert_not_called()


@pytest.mark.parametrize('body',[{}, {'sample_id':'../other'}, {'sample_id':'a'*32,'extra':True}, {'sample_id':True}])
def test_remove_sample_rejects_invalid_or_unknown_input(server,body):
    code,payload=request(server,'/api/voiceprint/samples/remove',method='POST',body=body,token=True)
    assert code==400 and payload['error']['code']=='invalid_speaker_request'


def test_remove_sample_requires_csrf_and_origin(server):
    body={'sample_id':'a'*32}
    assert request(server,'/api/voiceprint/samples/remove',method='POST',body=body)[0]==403
    assert request(server,'/api/voiceprint/samples/remove',method='POST',body=body,token=True,origin='https://evil.invalid')[0]==403


def test_invalid_multisample_mode_is_rejected_before_any_enrollment(server):
    from test_speaker_service import clip,persisted,result
    source=clip(server.store);analysis=persisted(server.speakers,source,result())
    body={'clip_id':source['id'],'analysis_id':analysis['id'],'speaker_id':'speaker_1','confirm':True,'mode':'unknown'}
    assert request(server,'/api/voiceprint/enroll',method='POST',body=body,token=True)[0]==400
    assert not server.speakers.profile_path.exists()
