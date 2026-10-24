"""Private voice-reference policy with fake analysis; no models or hardware."""
import copy
import json
import math
from pathlib import Path
import stat
import threading
import time
from unittest import mock
import uuid
import wave

import pytest

from audio_agent.pipeline import asr_lock
from audio_agent.speaker_service import SpeakerManager, _atomic_json, _cosine
from audio_agent.store import AudioStore


def model_status():
    return {"ready": True, "backend": "fake-local", "reason": None}


def result(vectors=None, *, reference_seconds=6, model="model-a"):
    vectors = vectors if vectors is not None else [[1.0, 0.0], [0.0, 1.0]]
    return {"embedding_model_id": model, "backend": "fake-local", "elapsed_seconds": .1,
            "speaker_count": len(vectors), "notes": ["Anonymous labels are per clip."],
            "speakers": [{"id": f"speaker_{i+1}", "embedding": vector, "speech_seconds": 6,
                          "reference_seconds": reference_seconds if vector is not None else 0}
                         for i, vector in enumerate(vectors)],
            "turns": [{"start": i * 6, "end": (i + 1) * 6, "speaker": f"speaker_{i+1}"}
                      for i in range(len(vectors))]}


@pytest.fixture
def setup(tmp_path):
    store = AudioStore(tmp_path)
    analyzer = mock.Mock(side_effect=lambda *args, **kwargs: result())
    manager = SpeakerManager(store, analyzer=analyzer, model_status=model_status)
    yield store, manager, analyzer
    manager.close()


def clip(store, *, seconds=12, age=0):
    return store.add_pcm(bytes(round(seconds * 32000)), start_time=time.time() - age - seconds,
                         source="test-synthetic")


def persisted(manager, source, data=None):
    data = copy.deepcopy(data or result())
    data.update(id=uuid.uuid4().hex, clip_id=source["id"], created_at=time.time())
    _atomic_json(manager.folder / f"{source['id']}.json", data)
    return data


def enroll(manager, source, data, **changes):
    values = {"clip_id": source["id"], "speaker_id": "speaker_1", "confirm": True, "analysis_id": data["id"]}
    values.update(changes)
    return manager.enroll(**values)


def wait_job(manager, job_id, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = next(item for item in manager.items() if item["id"] == job_id)
        if item["status"] in ("completed", "failed"):
            return item
        time.sleep(.005)
    raise AssertionError("Fake speaker task did not finish")


def assert_no_embeddings(value):
    if isinstance(value, dict):
        assert not any("embedding" in key for key in value)
        for child in value.values():
            assert_no_embeddings(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_embeddings(child)


@pytest.mark.parametrize("confirm", [False, None, 1, "true"])
def test_enrollment_requires_explicit_boolean_confirmation(setup, confirm):
    store, manager, _ = setup
    source = clip(store)
    data = persisted(manager, source)
    with pytest.raises(ValueError, match="确认"):
        enroll(manager, source, data, confirm=confirm)
    assert manager.voiceprint()["enrolled"] is False
    assert not manager.profile_path.exists()


@pytest.mark.parametrize("seconds", [.5, 1, 1.5, 2, 4.999])
def test_short_reference_can_be_enrolled_reloaded_and_labeled(setup, seconds):
    store, manager, _ = setup
    source = clip(store)
    data = persisted(manager, source, result(reference_seconds=seconds))
    assert manager.analysis(source["id"])["speakers"][0]["can_enroll"] is True
    assert enroll(manager, source, data)["reference_seconds"] == seconds
    reopened = SpeakerManager(store, model_status=model_status)
    try:
        assert reopened.voiceprint()["enrolled"] is True
        assert reopened.analysis(source["id"])["speakers"][0]["identity"]["source"] == "user_reference"
    finally:
        reopened.close()


@pytest.mark.parametrize("seconds,vector", [(0, [1., 0.]), (1, None)])
def test_enrollment_still_requires_nonempty_valid_reference(setup, seconds, vector):
    store, manager, _ = setup
    source = clip(store)
    data = persisted(manager, source, result([vector], reference_seconds=seconds))
    assert manager.analysis(source["id"])["speakers"][0]["can_enroll"] is False
    with pytest.raises(ValueError, match="有效声纹"):
        enroll(manager, source, data)
    assert not manager.profile_path.exists()


def test_enrollment_binds_to_reviewed_analysis_id_and_preserves_old_profile_on_stale_request(setup):
    store, manager, _ = setup
    source = clip(store)
    first = persisted(manager, source)
    enroll(manager, source, first)
    before = manager.profile_path.read_bytes()
    latest = persisted(manager, source, result([[0, 1], [1, 0]]))
    for stale in (first["id"], None, "invalid"):
        with pytest.raises(ValueError, match="更新或未指定"):
            enroll(manager, source, latest, analysis_id=stale)
        assert manager.profile_path.read_bytes() == before


def test_reference_is_private_and_cancel_restores_previously_analyzed_anonymous_results(setup):
    store, manager, _ = setup
    source = clip(store)
    data = persisted(manager, source)
    original = (manager.folder / f"{source['id']}.json").read_bytes()
    public_profile = enroll(manager, source, data)
    assert public_profile["enrolled"] is True
    assert stat.S_IMODE(manager.profile_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(manager.folder.stat().st_mode) == 0o700
    assert_no_embeddings(public_profile)
    identity = manager.analysis(source["id"])["speakers"][0]["identity"]
    assert identity["source"] == "user_reference"
    assert manager.clear()["enrolled"] is False
    assert all(person["identity"] is None for person in manager.analysis(source["id"])["speakers"])
    assert (manager.folder / f"{source['id']}.json").read_bytes() == original


@pytest.mark.parametrize("vectors,model,winner", [
    ([[.8, .6], [0, 1]], "model-a", "speaker_1"),
    ([[.64, math.sqrt(1-.64**2)], [0, 1]], "model-a", None),
    ([[.8, .6], [.75, math.sqrt(1-.75**2)]], "model-a", None),
    ([[1, 0], [0, 1]], "model-b", None),
    ([[1, 0, 0], [0, 1, 0]], "model-a", None),
    ([[0, 1], [1, 0]], "model-a", "speaker_2"),
])
def test_matching_requires_similarity_margin_and_exact_model_identity(setup, vectors, model, winner):
    store, manager, _ = setup
    reference = clip(store)
    data = persisted(manager, reference)
    enroll(manager, reference, data)
    later = clip(store)
    persisted(manager, later, result(vectors, model=model))
    public = manager.analysis(later["id"])
    identities = [person for person in public["speakers"] if person["identity"]]
    assert [person["id"] for person in identities] == ([winner] if winner else [])
    if winner:
        assert identities[0]["identity"]["source"] == "voiceprint_match"
    assert_no_embeddings(public)


def test_short_reference_is_not_used_for_automatic_matching(setup):
    store, manager, _ = setup
    reference = clip(store)
    data = persisted(manager, reference)
    enroll(manager, reference, data)
    later = clip(store)
    persisted(manager, later, result([[1, 0]], reference_seconds=1.99))
    assert manager.analysis(later["id"])["speakers"][0]["identity"] is None


def test_annotation_supports_multiple_speaker_labels_without_mutating_transcript_file(setup):
    store, manager, _ = setup
    source = clip(store)
    data = persisted(manager, source)
    enroll(manager, source, data)
    transcript = {"id": uuid.uuid4().hex, "clip_id": source["id"], "text": "fake ASR",
                  "segments": [{"start": 5, "end": 7, "text": "both speakers"},
                               {"start": 7, "end": 8, "text": "second speaker"}]}
    path = store.root / "transcripts" / f"{transcript['id']}.json"
    path.parent.mkdir()
    path.write_text(json.dumps(transcript))
    before = path.read_bytes()
    annotated = manager.annotate(transcript)
    assert annotated["segments"][0]["speaker_ids"] == ["speaker_1", "speaker_2"]
    assert annotated["segments"][0]["speaker_labels"] == ["我", "说话人 2"]
    assert annotated["segments"][1]["speaker_labels"] == ["说话人 2"]
    assert "speaker_labels" not in transcript["segments"][0]
    assert path.read_bytes() == before
    manager.clear()
    assert manager.annotate(transcript)["segments"][0]["speaker_labels"] == ["说话人 1", "说话人 2"]


def test_public_analysis_drops_private_and_unknown_nested_fields(setup):
    store, manager, _ = setup
    source = clip(store)
    raw = result()
    raw["turns"][0]["embedding"] = [1234, 5678]
    raw["speakers"][0]["debug"] = {"embedding": [1234, 5678]}
    raw["private_debug"] = {"embedding": [1234, 5678]}
    persisted(manager, source, raw)
    public = manager.analysis(source["id"])
    assert_no_embeddings(public)
    assert "debug" not in json.dumps(public)
    assert "1234" not in json.dumps(public)


def test_explicit_async_analysis_is_separate_from_asr_and_uses_a_snapshot(setup):
    store, manager, analyzer = setup
    source = clip(store)
    started, release = threading.Event(), threading.Event()
    snapshots = []
    def fake(audio, num_speakers=None):
        snapshots.append(audio)
        assert Path(audio) != Path(source["path"])
        with wave.open(str(audio)) as wav:
            assert wav.getnframes() == 12 * 16000
        assert num_speakers == 2
        started.set()
        assert release.wait(3)
        return result()
    analyzer.side_effect = fake
    assert manager.status()["available"] is True
    assert manager.analysis(source["id"]) is None
    analyzer.assert_not_called()
    job = manager.submit(source["id"], num_speakers=2)
    try:
        assert started.wait(3)
        assert job["kind"] == "diarization"
        assert job["result"] is None
        assert manager.items()[0]["status"] == "running"
        queued = manager.submit(source["id"], num_speakers=2)
        assert queued["status"] == "queued"
        assert manager.items()[0]["status"] == "queued"
        with pytest.raises(RuntimeError, match="转写任务"):
            with asr_lock(store.root):
                pass
    finally:
        release.set()
    completed = wait_job(manager, job["id"])
    assert wait_job(manager, queued["id"])["status"] == "completed"
    assert completed["status"] == "completed"
    assert completed["kind"] == "diarization"
    assert "transcript" not in completed["result"]
    assert_no_embeddings(completed)
    assert not snapshots[0].exists()
    assert not (store.root / "transcripts").exists()


def test_expired_clip_cannot_be_read_enrolled_or_submitted_even_if_audio_still_exists(setup):
    store, manager, analyzer = setup
    source = clip(store, age=90000)
    data = persisted(manager, source)
    assert Path(source["path"]).exists()
    for action in (lambda: manager.analysis(source["id"]), lambda: enroll(manager, source, data),
                   lambda: manager.submit(source["id"])):
        with pytest.raises(ValueError, match="保留期限"):
            action()
    analyzer.assert_not_called()


def test_queued_job_rechecks_expiration_before_reading_audio(setup):
    store, manager, analyzer = setup
    source = clip(store)
    # Leave a genuinely queued job without letting the worker race the clock.
    with mock.patch("audio_agent.speaker_service.threading.Thread") as thread:
        queued = manager.submit(source["id"])
        thread.return_value.start.assert_called_once()
    with mock.patch("audio_agent.speaker_service.time.time", return_value=time.time() + 90000), \
            mock.patch.object(store, "snapshot_clip") as snapshot:
        manager._run(queued["id"], None)
        snapshot.assert_not_called()
    analyzer.assert_not_called()
    assert manager.items()[0]["status"] == "failed"
    assert "保留期限" in manager.items()[0]["error"]


def test_completed_job_hides_expired_analysis_in_public_poll(setup):
    store, manager, _ = setup
    source = clip(store)
    job = manager.submit(source["id"])
    assert wait_job(manager, job["id"])["status"] == "completed"
    with mock.patch("audio_agent.speaker_service.time.time", return_value=time.time() + 90000):
        assert manager.items()[0]["result"] == {"analysis": None}


def test_failed_enrollment_write_does_not_replace_previous_reference(setup):
    store, manager, _ = setup
    source = clip(store)
    data = persisted(manager, source)
    enroll(manager, source, data)
    before = manager.profile_path.read_bytes()
    with mock.patch("audio_agent.speaker_service.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            enroll(manager, source, data, speaker_id="speaker_2")
    assert manager.profile_path.read_bytes() == before
    assert list(store.root.glob(".*.tmp")) == []


@pytest.mark.parametrize("mutation", [
    lambda d: d["speakers"][0].update(reference_seconds=float("nan")),
    lambda d: d["speakers"][0].update(reference_seconds=True),
    lambda d: d["speakers"][0].update(embedding=[False, 1]),
    lambda d: d["speakers"][0].update(embedding=[0, 0]),
    lambda d: d["turns"][0].update(speaker=[]),
    lambda d: d["turns"][0].update(start=10**400),
    lambda d: d.update(notes=[{"embedding": [1, 0]}]),
])
def test_malformed_analysis_cannot_enroll_or_leak_private_objects(setup, mutation):
    store, manager, _ = setup
    source = clip(store)
    data = persisted(manager, source)
    mutation(data)
    (manager.folder / f"{source['id']}.json").write_text(json.dumps(data))
    with pytest.raises(ValueError):
        manager.analysis(source["id"])
    with pytest.raises(ValueError):
        enroll(manager, source, data)
    assert not manager.profile_path.exists()


def test_profile_metadata_cannot_be_used_to_smuggle_embeddings_into_status(setup):
    store, manager, _ = setup
    source = clip(store)
    data = persisted(manager, source)
    enroll(manager, source, data)
    profile = json.loads(manager.profile_path.read_text())
    profile["created_at"] = {"embedding": [1234, 5678]}
    manager.profile_path.write_text(json.dumps(profile))
    status = manager.status()
    assert status["voiceprint"]["enrolled"] is False
    assert_no_embeddings(status)
    assert "1234" not in json.dumps(status)
    assert manager.clear()["enrolled"] is False


def test_cosine_is_finite_for_large_vectors_and_rejects_invalid_values():
    assert _cosine([1e308, 1e308], [1e308, 1e308]) == pytest.approx(1)
    assert _cosine([1e-308, 0], [1, 0]) == pytest.approx(1)
    for invalid in ([0, 0], [True, 0], [float("nan"), 0], [10**400, 0]):
        assert _cosine(invalid, [1, 0]) is None
