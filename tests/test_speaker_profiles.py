"""Explicit named local references, ambiguity handling and public annotations."""
import json
import stat
import time
from unittest import mock
import uuid

import pytest

from audio_agent.speaker_service import SpeakerManager
from audio_agent.store import AudioStore
from audio_agent.updates import UpdatesFeed
from test_speaker_service import (assert_no_embeddings, clip, enroll, model_status,
                                  persisted, result)
from test_updates import put_transcript


@pytest.fixture
def library(tmp_path):
    store = AudioStore(tmp_path / "data")
    analyzer = mock.Mock(side_effect=AssertionError("No model for profiles"))
    manager = SpeakerManager(store, analyzer=analyzer, model_status=model_status)
    yield store, manager, analyzer
    manager.close()


def register(manager, source, analysis, speaker="speaker_1", name="小王", note="同事", **changes):
    values = {"clip_id": source["id"], "speaker_id": speaker, "analysis_id": analysis["id"],
              "confirm": True, "name": name, "note": note, **changes}
    return manager.enroll_profile(**values)["profile"]


def transcript(source):
    return {"id": uuid.uuid4().hex, "clip_id": source["id"], "text": "完整讲话",
            "segments": [{"start": 0, "end": 12, "text": "完整讲话"}]}


def test_named_short_reference_is_private_and_manual_current_label_wins(library):
    store, manager, analyzer = library
    source = clip(store)
    analysis = persisted(manager, source, result(reference_seconds=.15))
    before = (manager.folder / f"{source['id']}.json").read_bytes()
    person = register(manager, source, analysis, name="  小王  ", note="  设计同事  ")
    assert person["name"] == "小王" and person["note"] == "设计同事"
    assert person["reference_seconds"] == .15
    assert person["display_label"] == "小王 · 设计同事"
    assert stat.S_IMODE(manager.profiles_path.stat().st_mode) == 0o600
    identity = manager.analysis(source["id"])["speakers"][0]["identity"]
    assert identity["profile_id"] == person["id"] and identity["is_self"] is False
    assert identity["source"] == "user_reference" and identity["label"] == "小王"
    assert manager.annotate(transcript(source))["segments"][0]["speaker_labels"] == ["小王 · 设计同事", "说话人 2"]
    assert (manager.folder / f"{source['id']}.json").read_bytes() == before
    assert_no_embeddings(manager.profiles())
    assert_no_embeddings(manager.analysis(source["id"]))
    assert not manager.profile_path.exists()
    analyzer.assert_not_called()


def test_named_and_legacy_self_references_coexist_without_migration(library):
    store, manager, _ = library
    source = clip(store)
    analysis = persisted(manager, source)
    enroll(manager, source, analysis)
    own_bytes = manager.profile_path.read_bytes()
    other = register(manager, source, analysis, speaker="speaker_2", name="家人", note="")
    assert manager.profile_path.read_bytes() == own_bytes
    public = manager.profiles()
    assert public["voiceprint"]["enrolled"] is True and public["items"] == [other]
    assert manager.annotate(transcript(source))["segments"][0]["speaker_labels"] == ["我", "家人"]
    reopened = SpeakerManager(store, model_status=model_status)
    try:
        assert reopened.annotate(transcript(source))["segments"][0]["speaker_labels"] == ["我", "家人"]
        assert reopened.profiles()["items"] == [other]
    finally:
        reopened.close()
    manager.clear()
    assert manager.annotate(transcript(source))["segments"][0]["speaker_labels"] == ["说话人 1", "家人"]


@pytest.mark.parametrize("is_self", [False, True])
def test_short_manual_binding_reserves_identity_against_other_speakers(library, is_self):
    store, manager, _ = library
    source = clip(store)
    raw = result([[1, 0], [.8, .6]])
    raw["speakers"][0]["reference_seconds"] = .4
    analysis = persisted(manager, source, raw)
    if is_self:
        enroll(manager, source, analysis)
    else:
        register(manager, source, analysis)
    speakers = manager.analysis(source["id"])["speakers"]
    assert speakers[0]["identity"]["source"] == "user_reference"
    assert speakers[1]["identity"] is None


@pytest.mark.parametrize("confirm", [False, None, 1, "true"])
def test_named_enrollment_requires_exact_confirm_and_stale_analysis_is_rejected(library, confirm):
    store, manager, _ = library
    source = clip(store)
    analysis = persisted(manager, source)
    with pytest.raises(ValueError, match="确认"):
        register(manager, source, analysis, confirm=confirm)
    replacement = persisted(manager, source)
    with pytest.raises(ValueError, match="更新或未指定"):
        register(manager, source, analysis)
    assert not manager.profiles_path.exists()


def test_exact_binding_conflicts_require_explicit_replacement_or_removal(library):
    store, manager, _ = library
    source = clip(store)
    analysis = persisted(manager, source)
    first = register(manager, source, analysis)
    before = manager.profiles_path.read_bytes()
    with pytest.raises(ValueError, match="replace_profile_id"):
        register(manager, source, analysis, name="第二个人")
    assert manager.profiles_path.read_bytes() == before
    with pytest.raises(ValueError, match="移除"):
        enroll(manager, source, analysis)
    changed = register(manager, source, analysis, name="更正名字", replace_profile_id=first["id"])
    assert changed["id"] == first["id"] and changed["created_at"] == first["created_at"]
    assert len(manager.profiles()["items"]) == 1
    manager.remove_profile(first["id"])
    enroll(manager, source, analysis)
    with pytest.raises(ValueError, match="取消本人登记"):
        register(manager, source, analysis)
    assert manager.voiceprint()["enrolled"] is True


def test_explicit_replacement_can_refresh_reference_from_new_audio_without_merging_people(library):
    store, manager, _ = library
    original = clip(store)
    original_analysis = persisted(manager, original)
    first = register(manager, original, original_analysis, name="张老师")
    another = register(manager, original, original_analysis, speaker="speaker_2", name="李老师")
    replacement = clip(store)
    new_analysis = persisted(manager, replacement, result([[.9, .1]]))
    updated = register(manager, replacement, new_analysis, name="张老师", note="更清楚的参考",
                       replace_profile_id=first["id"])
    assert updated["id"] == first["id"] and updated["created_at"] == first["created_at"]
    assert updated["clip_id"] == replacement["id"] and updated["analysis_id"] == new_analysis["id"]
    assert len(manager.profiles()["items"]) == 2
    before = manager.profiles_path.read_bytes()
    with pytest.raises(ValueError, match="先移除"):
        register(manager, original, original_analysis, speaker="speaker_2", replace_profile_id=first["id"])
    assert manager.profiles_path.read_bytes() == before
    assert any(value["id"] == another["id"] for value in manager.profiles()["items"])


@pytest.mark.parametrize("name,note", [("", ""), (" ", ""), ("我", ""), ("x"*81, ""),
                                      ("姓名", "x"*201), ("姓\n名", ""), ("姓名", "note\x00"),
                                      (1, ""), ("姓名", None)])
def test_person_names_and_notes_are_bounded_plain_strings(library, name, note):
    store, manager, _ = library
    source = clip(store)
    analysis = persisted(manager, source)
    with pytest.raises(ValueError):
        register(manager, source, analysis, name=name, note=note)
    assert not manager.profiles_path.exists()


def test_matching_chooses_between_people_and_preserves_notes(library):
    store, manager, _ = library
    source = clip(store)
    analysis = persisted(manager, source)
    alice = register(manager, source, analysis, name="甲", note="项目同事")
    bob = register(manager, source, analysis, speaker="speaker_2", name="乙", note="朋友")
    later = clip(store)
    persisted(manager, later, result([[0, 1], [1, 0]]))
    speakers = manager.analysis(later["id"])["speakers"]
    assert [speaker["identity"]["profile_id"] for speaker in speakers] == [bob["id"], alice["id"]]
    labels = manager.annotate(transcript(later))["segments"][0]["speaker_labels"]
    assert labels == ["乙 · 朋友（声纹匹配）", "甲 · 项目同事（声纹匹配）"]


@pytest.mark.parametrize("second_vector", [[1, 0], [.999, .01]])
def test_same_or_confusing_person_vectors_are_not_matched_to_first_profile(library, second_vector):
    store, manager, _ = library
    source = clip(store)
    analysis = persisted(manager, source, result([[1, 0], second_vector]))
    register(manager, source, analysis, name="甲")
    register(manager, source, analysis, speaker="speaker_2", name="乙")
    # Explicit labels on the listened-to source remain valid even when the
    # references are indistinguishable. New audio must remain anonymous.
    assert [speaker["identity"]["name"] for speaker in manager.analysis(source["id"])["speakers"]] == ["甲", "乙"]
    later = clip(store)
    persisted(manager, later, result([[1, 0]]))
    assert manager.analysis(later["id"])["speakers"][0]["identity"] is None


@pytest.mark.parametrize("vectors,model,seconds", [([[.64, .768]], "model-a", 6),
                                                  ([[1, 0]], "different-model", 6),
                                                  ([[1, 0, 0]], "model-a", 6),
                                                  ([[1, 0]], "model-a", 1.99),
                                                  ([[1, 0], [.99, .1]], "model-a", 6)])
def test_automatic_matching_keeps_model_threshold_duration_and_speaker_margin(library, vectors, model, seconds):
    store, manager, _ = library
    source = clip(store)
    register(manager, source, persisted(manager, source))
    later = clip(store)
    persisted(manager, later, result(vectors, model=model, reference_seconds=seconds))
    assert all(speaker["identity"] is None for speaker in manager.analysis(later["id"])["speakers"])


def test_edit_and_remove_relabel_existing_transcripts_without_mutation_or_inference(library):
    store, manager, analyzer = library
    source = clip(store)
    analysis = persisted(manager, source)
    profile = register(manager, source, analysis)
    original, path = put_transcript(store, clip_id=source["id"], segments=[{"start": 0, "end": 6, "text": "原文"}])
    before = path.read_bytes()
    assert manager.update_profile(profile["id"], "李老师", "研究导师")["profile"]["display_label"] == "李老师 · 研究导师"
    assert manager.annotate(original)["segments"][0]["speaker_labels"] == ["李老师 · 研究导师"]
    assert manager.remove_profile(profile["id"])["items"] == []
    assert manager.annotate(original)["segments"][0]["speaker_labels"] == ["说话人 1"]
    assert path.read_bytes() == before and "speaker_labels" not in original["segments"][0]
    analyzer.assert_not_called()


def test_atomic_failure_preserves_old_profiles_and_raw_audio_expiry_does_not_remove_people(library):
    store, manager, _ = library
    source = clip(store)
    profile = register(manager, source, persisted(manager, source))
    before = manager.profiles_path.read_bytes()
    with mock.patch("audio_agent.speaker_service.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            manager.update_profile(profile["id"], "更改", "")
        with pytest.raises(OSError):
            manager.remove_profile(profile["id"])
    assert manager.profiles_path.read_bytes() == before
    store.prune(now=time.time()+90000)
    assert manager.profiles()["items"] == [profile]


def test_private_profile_metadata_cannot_smuggle_embedding_objects_to_http(library):
    store, manager, _ = library
    source = clip(store)
    register(manager, source, persisted(manager, source))
    data = json.loads(manager.profiles_path.read_text())
    data["items"][0]["note"] = {"embedding": [1234, 5678]}
    manager.profiles_path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        manager.profiles()
    assert_no_embeddings(manager.analysis(source["id"]))
    assert all(speaker["identity"] is None for speaker in manager.analysis(source["id"])["speakers"])


@pytest.mark.parametrize("corrupt_self", [True, False])
def test_any_corrupt_reference_library_disables_automatic_matches_but_keeps_valid_manual_labels(library, corrupt_self):
    store, manager, analyzer = library
    source = clip(store)
    analysis = persisted(manager, source, result([[1, 0], [.8, .6]]))
    enroll(manager, source, analysis)
    register(manager, source, analysis, speaker="speaker_2", name="人物甲", note="同事")
    later = clip(store)
    persisted(manager, later, result([[1, 0]]))
    assert manager.analysis(later["id"])["speakers"][0]["identity"]["is_self"] is True
    corrupt_path = manager.profile_path if corrupt_self else manager.profiles_path
    corrupt_path.write_text("broken reference JSON")
    # If the missing competitor were silently ignored, a damaged self library
    # would incorrectly turn the same voice into 人物甲 at cosine .8.
    assert manager.analysis(later["id"])["speakers"][0]["identity"] is None
    preserved_index = 1 if corrupt_self else 0
    current = manager.analysis(source["id"])["speakers"]
    assert current[preserved_index]["identity"]["source"] == "user_reference"
    assert current[preserved_index]["identity"]["is_self"] is (not corrupt_self)
    assert current[1-preserved_index]["identity"] is None
    analyzer.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_readonly_feed_notices_named_profile_creation_edit_and_removal(library):
    store, manager, analyzer = library
    source = clip(store)
    analysis = persisted(manager, source)
    put_transcript(store, clip_id=source["id"], segments=[{"start": 0, "end": 6, "text": "正文"}])
    feed = UpdatesFeed(store)
    initial = await feed.get_updates()
    profile = register(manager, source, analysis, name="朋友", note="同学")
    labeled = await feed.get_updates(initial["next_cursor"])
    assert labeled["items"][0]["segments"][0]["speaker_labels"] == ["朋友 · 同学"]
    manager.update_profile(profile["id"], "新名字", "新备注")
    edited = await feed.get_updates(labeled["next_cursor"])
    assert edited["items"][0]["segments"][0]["speaker_labels"] == ["新名字 · 新备注"]
    manager.remove_profile(profile["id"])
    removed = await feed.get_updates(edited["next_cursor"])
    assert removed["items"][0]["segments"][0]["speaker_labels"] == ["说话人 1"]
    assert_no_embeddings(removed)
    analyzer.assert_not_called()
