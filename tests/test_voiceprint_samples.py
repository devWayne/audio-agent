"""Explicit multi-reference self identity, using fake vectors only."""
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import math
import stat
import threading
import time
from unittest import mock
import uuid

import pytest

from audio_agent.speaker_service import SpeakerManager, _atomic_json
from audio_agent.updates import UpdatesFeed
from test_speaker_service import setup, clip, persisted, result, enroll, assert_no_embeddings, model_status
from test_updates import put_transcript


def reference(store, manager, vector=(1, 0), seconds=6, model="model-a"):
    source = clip(store, seconds=max(12, seconds))
    data = result([list(vector)], reference_seconds=seconds, model=model)
    data['speakers'][0]['speech_seconds'] = seconds
    data['turns'][0]['end'] = seconds
    return source, persisted(manager, source, data)


def append(manager, source, data):
    return enroll(manager, source, data, mode="append")


def legacy(manager, source, data):
    value = {"id": uuid.uuid4().hex, "created_at": time.time()-10, "label": "我",
             "clip_id": source['id'], "analysis_id": data['id'], "speaker_id": "speaker_1",
             "embedding_model_id": data['embedding_model_id'],
             "embedding": data['speakers'][0]['embedding'],
             "reference_seconds": data['speakers'][0]['reference_seconds']}
    _atomic_json(manager.profile_path, value)
    return value


def test_legacy_read_only_then_append_preserves_old_binding(setup):
    store, manager, analyzer = setup
    old, old_a = reference(store, manager, seconds=.59)
    old_value = legacy(manager, old, old_a)
    before = manager.profile_path.read_bytes(), manager.profile_path.stat().st_mtime_ns
    public = manager.voiceprint()
    assert public['sample_count'] == 1 and public['total_reference_seconds'] == .59
    old_id = public['samples'][0]['id']
    assert public['samples'][0]['model_compatible'] is None
    assert manager.analysis(old['id'])['speakers'][0]['identity']['source'] == 'user_reference'
    assert (manager.profile_path.read_bytes(), manager.profile_path.stat().st_mtime_ns) == before
    new, new_a = reference(store, manager, seconds=6)
    added = append(manager, new, new_a)
    saved = json.loads(manager.profile_path.read_text())
    assert saved['version'] == 2 and saved['samples'][0]['embedding'] == old_value['embedding']
    assert saved['samples'][0]['id'] == old_id
    assert added['sample_count'] == 2 and added['reference_seconds'] == pytest.approx(6.59)
    for source in (old, new):
        assert manager.analysis(source['id'])['speakers'][0]['identity']['source'] == 'user_reference'
    assert stat.S_IMODE(manager.profile_path.stat().st_mode) == 0o600
    assert_no_embeddings(added)
    analyzer.assert_not_called()


def test_duplicate_append_is_noop_even_at_limit(setup):
    store, manager, _ = setup
    values = [reference(store, manager) for _ in range(10)]
    for source, data in values: append(manager, source, data)
    before = manager.profile_path.read_bytes(), manager.profile_path.stat().st_mtime_ns
    public = append(manager, *values[0])
    assert public['sample_count'] == 10
    assert (manager.profile_path.read_bytes(), manager.profile_path.stat().st_mtime_ns) == before
    eleventh = reference(store, manager)
    with pytest.raises(ValueError, match='最多保存 10'):
        append(manager, *eleventh)
    assert manager.profile_path.read_bytes() == before[0]


def test_replace_remains_default_and_replaces_entire_group(setup):
    store, manager, _ = setup
    for _ in range(3): append(manager, *reference(store, manager))
    source, data = reference(store, manager, vector=(0,1))
    changed = enroll(manager, source, data)
    assert changed['sample_count'] == 1 and changed['clip_id'] == source['id']
    assert changed['samples'][0]['analysis_id'] == data['id']


@pytest.mark.parametrize('mode', ['other', None, True, 1, [], {}])
def test_bad_mode_rejected_without_replacing_reference(setup, mode):
    store, manager, _ = setup
    source, data = reference(store, manager)
    enroll(manager, source, data)
    before = manager.profile_path.read_bytes()
    with pytest.raises(ValueError, match='mode'):
        enroll(manager, source, data, mode=mode)
    assert manager.profile_path.read_bytes() == before


@pytest.mark.parametrize('confirm', [False, None, 1, 'true'])
def test_append_still_needs_explicit_confirmation(setup, confirm):
    store, manager, _ = setup
    source, data = reference(store, manager)
    with pytest.raises(ValueError, match='确认'):
        enroll(manager, source, data, mode='append', confirm=confirm)
    assert not manager.profile_path.exists()


def test_same_analysis_different_self_speaker_is_rejected_and_first_binding_reserved(setup):
    store, manager, _ = setup
    source = clip(store)
    raw = result([[1,0],[.8,.6]],reference_seconds=6)
    raw['speakers'][0]['reference_seconds'] = .4
    data = persisted(manager, source, raw)
    append(manager, source, data)
    with pytest.raises(ValueError, match='另一位说话人'):
        enroll(manager, source, data, mode='append', speaker_id='speaker_2')
    public = manager.analysis(source['id'])['speakers']
    assert public[0]['identity']['source'] == 'user_reference'
    assert public[1]['identity'] is None


def test_named_enrollment_checks_all_self_samples_not_only_latest(setup):
    store, manager, _ = setup
    old, old_a = reference(store, manager)
    append(manager, old, old_a)
    append(manager, *reference(store, manager))
    with pytest.raises(ValueError, match='移除对应本人样本'):
        manager.enroll_profile(old['id'],'speaker_1',old_a['id'],True,'人物','备注')
    public = manager.voiceprint()
    manager.remove_sample(public['samples'][0]['id'])
    named = manager.enroll_profile(old['id'],'speaker_1',old_a['id'],True,'人物','备注')
    assert len(named['items']) == 1 and named['voiceprint']['sample_count'] == 1


def test_remove_sample_restores_anonymous_without_touching_other_reference_or_transcript(setup):
    store, manager, _ = setup
    old, old_a = reference(store, manager, vector=(1,0), seconds=.5)
    new, new_a = reference(store, manager, vector=(0,1))
    append(manager, old, old_a); state = append(manager, new, new_a)
    transcript, path = put_transcript(store,clip_id=old['id'],segments=[{'start':0,'end':.5,'text':'测试'}])
    before = path.read_bytes()
    assert manager.annotate(transcript)['segments'][0]['speaker_labels'] == ['我']
    remaining = manager.remove_sample(state['samples'][0]['id'])
    assert remaining['sample_count'] == 1
    assert manager.annotate(transcript)['segments'][0]['speaker_labels'] == ['说话人 1']
    assert manager.analysis(new['id'])['speakers'][0]['identity']['source'] == 'user_reference'
    assert path.read_bytes() == before
    assert manager.remove_sample(remaining['samples'][0]['id'])['enrolled'] is False
    assert not manager.profile_path.exists()
    with pytest.raises(ValueError, match='不存在'):
        manager.remove_sample(remaining['samples'][0]['id'])
    with pytest.raises(ValueError, match='编号'):
        manager.remove_sample('../voiceprint.json')


def test_remove_legacy_last_sample_works_without_prior_migration(setup):
    store, manager, _ = setup
    source, data = reference(store, manager)
    legacy(manager, source, data)
    sample_id = manager.voiceprint()['samples'][0]['id']
    assert manager.remove_sample(sample_id)['sample_count'] == 0
    assert not manager.profile_path.exists()


def test_duration_weighted_score_short_reference_has_low_weight(setup):
    store, manager, _ = setup
    append(manager, *reference(store,manager,vector=(1,0),seconds=.59))
    append(manager, *reference(store,manager,vector=(0,1),seconds=10))
    later, _ = reference(store,manager,vector=(0,1))
    identity = manager.analysis(later['id'])['speakers'][0]['identity']
    assert identity['source'] == 'voiceprint_match'
    assert identity['score'] == pytest.approx(10/10.59)


def test_duration_weight_is_capped_at_ten_seconds(setup):
    store, manager, _ = setup
    append(manager,*reference(store,manager,vector=(1,0),seconds=30))
    append(manager,*reference(store,manager,vector=(0,1),seconds=10))
    candidate = manager._self_candidate(manager._profile(),'model-a')
    assert manager._candidate_score([1,0],candidate) == pytest.approx(.5)


def test_conflicting_point_six_references_do_not_inflate_to_one(setup):
    store, manager, _ = setup
    append(manager,*reference(store,manager,vector=(.6,.8)))
    append(manager,*reference(store,manager,vector=(.6,-.8)))
    later,_ = reference(store,manager,vector=(1,0))
    assert manager.analysis(later['id'])['speakers'][0]['identity'] is None
    candidate=manager._self_candidate(manager._profile(),'model-a')
    assert manager._candidate_score([1,0],candidate) == pytest.approx(.6)


def test_one_high_sample_cannot_win_by_max_and_samples_are_not_competing_identities(setup):
    store,manager,_=setup
    append(manager,*reference(store,manager,vector=(1,0)))
    append(manager,*reference(store,manager,vector=(0,1)))
    later,_=reference(store,manager,vector=(1,0))
    assert manager.analysis(later['id'])['speakers'][0]['identity'] is None
    manager.clear()
    append(manager,*reference(store,manager,vector=(1,0)))
    append(manager,*reference(store,manager,vector=(1,0)))
    identity=manager.analysis(later['id'])['speakers'][0]['identity']
    assert identity['profile_id']=='self' and identity['score']==pytest.approx(1)


def test_different_models_never_mix_and_dimension_mismatch_is_rejected(setup):
    store,manager,_=setup
    append(manager,*reference(store,manager,vector=(1,0),model='model-a'))
    append(manager,*reference(store,manager,vector=(0,1,0),model='model-b'))
    for model,vector in [('model-a',(1,0)),('model-b',(0,1,0))]:
        later,_=reference(store,manager,vector=vector,model=model)
        assert manager.analysis(later['id'])['speakers'][0]['identity']['score']==pytest.approx(1)
    later,_=reference(store,manager,vector=(1,0),model='model-c')
    assert manager.analysis(later['id'])['speakers'][0]['identity'] is None
    source,data=reference(store,manager,vector=(1,0,0),model='model-a')
    with pytest.raises(ValueError,match='维度'):
        append(manager,source,data)


def test_multisample_still_obeys_both_margins_and_candidate_two_seconds(setup):
    store,manager,_=setup
    for _ in range(2): append(manager,*reference(store,manager))
    later=clip(store)
    persisted(manager,later,result([[.8,.6],[.75,math.sqrt(1-.75**2)]]))
    assert all(p['identity'] is None for p in manager.analysis(later['id'])['speakers'])
    short,_=reference(store,manager,seconds=1.99)
    assert manager.analysis(short['id'])['speakers'][0]['identity'] is None
    other,analysis=reference(store,manager)
    manager.enroll_profile(other['id'],'speaker_1',analysis['id'],True,'其他人物','')
    later,_=reference(store,manager)
    assert manager.analysis(later['id'])['speakers'][0]['identity'] is None


@pytest.mark.parametrize('mutate', [
    lambda p:p['samples'].append(copy.deepcopy(p['samples'][0])),
    lambda p:p['samples'][0].update(embedding=[0,0]),
    lambda p:p['samples'][0].update(embedding=[float('nan'),0]),
    lambda p:p['samples'][0].update(created_at={'embedding':[1234]}),
    lambda p:p.update(samples=[]),
    lambda p:p.update(version=True),
])
def test_invalid_sample_library_fails_closed_and_no_private_status(setup,mutate):
    store,manager,_=setup
    source,data=reference(store,manager)
    append(manager,source,data)
    value=json.loads(manager.profile_path.read_text()); mutate(value)
    manager.profile_path.write_text(json.dumps(value))
    status=manager.status()
    assert status['voiceprint']['enrolled'] is False
    assert_no_embeddings(status)
    assert manager.analysis(source['id'])['speakers'][0]['identity'] is None


def test_atomic_failed_append_and_remove_preserve_original(setup):
    store,manager,_=setup
    state=append(manager,*reference(store,manager))
    append(manager,*reference(store,manager))
    before=manager.profile_path.read_bytes()
    source,data=reference(store,manager)
    with mock.patch('audio_agent.speaker_service.os.replace',side_effect=OSError('disk full')):
        with pytest.raises(OSError): append(manager,source,data)
        with pytest.raises(OSError): manager.remove_sample(state['samples'][0]['id'])
    assert manager.profile_path.read_bytes()==before


def test_two_managers_concurrent_append_no_lost_samples_and_get_no_inference(setup):
    store,manager,analyzer=setup
    second=SpeakerManager(store,analyzer=mock.Mock(side_effect=AssertionError('No inference')),model_status=model_status)
    values=[reference(store,manager) for _ in range(8)]
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures=[executor.submit(append,manager if i%2 else second,*value) for i,value in enumerate(values)]
            reads=[executor.submit(manager.status) for _ in range(5)]
            for future in futures: future.result(timeout=3)
            for future in reads: assert_no_embeddings(future.result(timeout=3))
        assert manager.voiceprint()['sample_count']==8
        analyzer.assert_not_called();second.analyzer.assert_not_called()
    finally: second.close()


@pytest.mark.asyncio
async def test_mcp_updates_refresh_after_sample_append_remove_without_inference(setup):
    store,manager,analyzer=setup
    old,old_a=reference(store,manager,vector=(1,0),seconds=.59)
    append(manager,old,old_a)
    target,target_a=reference(store,manager,vector=(0,1))
    transcript,path=put_transcript(store,clip_id=target['id'],segments=[{'start':0,'end':6,'text':'完整测试'}])
    original=path.read_bytes()
    feed=UpdatesFeed(store,poll_interval=.01)
    first=await feed.get_updates()
    assert first['items'][0]['segments'][0]['speaker_labels']==['说话人 1']
    new,new_a=reference(store,manager,vector=(0,1),seconds=10)
    state=append(manager,new,new_a)
    matched=await feed.get_updates(first['next_cursor'])
    assert matched['items'][0]['segments'][0]['speaker_labels']==['我（声纹匹配）']
    manager.remove_sample(state['samples'][-1]['id'])
    anonymous=await feed.get_updates(matched['next_cursor'])
    assert anonymous['items'][0]['segments'][0]['speaker_labels']==['说话人 1']
    assert path.read_bytes()==original
    analyzer.assert_not_called();assert_no_embeddings(matched)
