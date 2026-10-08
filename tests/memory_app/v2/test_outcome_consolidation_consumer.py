"""Actual outcome owners, ModelConfiguration, Turns, wires and commit effects."""
from datetime import timedelta
from copy import deepcopy
import json
import re
import sqlite3
import time
from types import SimpleNamespace

import pytest

from backend.memory_app.v2.consolidation import Consolidation
from backend.memory_app.v2.outcome_corrections import _time
from backend.memory_app.v2.consolidation_events import consumer_outcomes
from backend.memory_app.v2.memory_turn import MemoryTurn
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.model_config import ModelConfigurationError
from backend.recognition import RecognitionError, WorkScope
from tests.memory_app.v2.test_outcome_redos import (
    scenario, completed, redo, wait_product, NEW, blocked_new_main, cancel_active_main,
    legacy_outcome_defaults,
)
from tests.memory_app.v2.test_outcome_consolidation_events import edit, adjust, PROJECT
from tests.memory_app.v2.test_workbench_do import env as do_env
from tests.memory_app.test_fast_generation import choose
from tests.memory_app.v2.kernel_receipts import requests
from tests.memory_app.v2.kernel_receipts import wire_receipts
from tests.memory_app.v2.test_profile import publish as profile
from backend.memory_app.source_graph import SourceGraph


def consumer(env, *, before=None, after=None, failure=False, enabled=True, local=False):
    wires = []
    def respond(messages, **options):
        text = '\n'.join(message['content'] for message in messages)
        if before:
            before()
        wires.append({'model': options['model'], 'base_url': options.get('api_base'), 'messages': messages})
        if failure:
            raise RuntimeError('synthetic transport failure')
        if after:
            after()
        if '最多300字' in text:
            return json.dumps({'text': '合成导航摘要'})
        identities = re.findall(r'"event_id":\s*"([^"]+)"', text)
        return json.dumps({'text': '以后先核对用户目标再交付', 'conditions': ['准备成果时'],
            'event_ids': identities, 'kind': 'correction'}, ensure_ascii=False)
    env.models.handler = respond  # Only the external provider; the gateway/owners remain real.
    config = env.models.public()['generation']
    env.models.update('generation', {'enabled': enabled, 'expected_revision': config['revision']})
    if local:
        model = env.models.root / 'data/models/qwen2.5-1.5b-instruct/model.safetensors'
        model.parent.mkdir(parents=True, exist_ok=True)
        model.write_bytes(b'installed-model-test')
        env.models.update_generation_mode(mode='local', local_enabled=True,
            local_base_url='http://127.0.0.1:8001/local-model/v1', expected_revision=0)
    choose(env.models, 'qwen2.5-1.5b-instruct' if local else 'quick')
    fact = next((row for row in env.records.list('v2_outcome_corrections')
        if row.payload['kind'] == 'outcome_edit'), None)
    now = _time(fact.payload['last_saved_at']) + timedelta(seconds=601) if fact else None
    job = Consolidation(env.records, env.state.recognition_service, env.documents, env.models,
        **({'now': lambda: now} if now is not None else {}))
    return job, wires


def qualified(env, job, *, local=False):
    return consumer_outcomes(env.records, PROJECT, now=job.now().isoformat(), local_only=local)


def confirm(env, candidate):
    assert candidate.payload['state'] == 'pending'
    return env.state.recognition_service.publish(scope=WorkScope('local-user', PROJECT),
        candidate_id=candidate.object_id, expected_revision=candidate.revision, reviewer='local-user')


def auxiliary_completed(env):
    store = MemoryTurn.store_for(env.records)
    request, = [request for request in requests(env.records) if request['desired_outcome'] == 'memory.consolidate']
    completed, = [event for event in store.events_after(request['turn_id']) if event['type'] == 'model.completed']
    assert completed['data']['model_call_purpose'] == 'aux'


def parent_profile(env):
    return profile(SimpleNamespace(records=env.records, service=env.state.recognition_service), '本人偏好先核实成果')


def failed_without_root(env):
    with blocked_new_main(env):
        response = env.client.post('/api/v2/workbench/turns', json={
            'project_id': PROJECT, 'intent': 'do', 'text': '准备一份成果'})
        assert response.status_code == 200, response.text
        data = response.json()
        turn = cancel_active_main(env, data)
    assert turn['receipt']['do']['state'] == 'failed' and turn['receipt']['do']['document_id'] is None
    return data, turn


def reject_inputs(env, *, enable):
    with sqlite3.connect(env.records.database_path) as connection:
        if enable:
            connection.execute("CREATE TRIGGER reject_consolidation_inputs BEFORE INSERT ON crp_structured_records "
                "WHEN NEW.collection = 'v2_consolidation_inputs' BEGIN SELECT RAISE(ABORT, 'controlled inputs failure'); END")
        else:
            connection.execute('DROP TRIGGER reject_consolidation_inputs')


def test_real_edit_division_redo_freeze_into_original_consumer_and_commit_once(scenario, legacy_outcome_defaults):
    env = scenario
    old, turn = completed(env)
    edited = edit(env, turn)
    adjust(env, old['turn']['id'])
    env.summary = NEW
    response = redo(env, old, revision=2)
    assert response.status_code == 200, response.text
    new_turn = wait_product(env, response.json())
    assert new_turn['receipt']['do']['state'] == 'done'
    facts = env.records.list('v2_outcome_corrections')
    assert len(facts) == 3 and len(env.models.calls) == 5
    job, wires = consumer(env)
    result = job.run(PROJECT)
    assert result['new_suggestions'] == 1 and result['failed_groups'] == 0
    request, = [request for request in requests(env.records) if request['desired_outcome'] == 'memory.consolidate']
    expected = {'outcome:' + row.object_id for row in facts}
    assert all(request['input']['text'].count(identity) == 1 for identity in expected)
    feedback, = [json.loads(piece)['outcomes'] for piece in request['input']['text'].split('\n\n')
        if piece.startswith('{"outcomes":')]
    changed, = [fact for fact in feedback if fact['type'] == 'outcome_edit']
    assert changed['before'] == edited.payload['before'] and changed['after'] == edited.payload['after']
    pattern, = env.records.list('v2_insight_patterns')
    candidate = env.records.read('recognition_candidates', pattern.object_id)
    assert candidate.payload['state'] == 'pending' and set(pattern.payload['event_ids']) == expected
    assert len(candidate.payload['source_experience_ids']) == 2
    for identity in candidate.payload['source_experience_ids']:
        source = env.records.read('recognition_experiences', identity)
        assert source.payload['provenance']['kind'] == 'model_generated_artifact'
        assert source.payload['provenance']['actor'] == 'system'
        document, = [ref for ref in source.payload['provenance']['source_refs'] if ref['type'] == 'document']
        assert document['revision'] == 1
    consumed, = env.records.list('v2_consolidation_inputs')
    assert consumed.payload['project_id'] == PROJECT and set(consumed.payload['event_ids']) == expected
    assert env.records.list('recognitions') == ()
    own_wire = next(wire for wire in wires if any('"outcomes"' in message['content'] for message in wire['messages']))
    assert own_wire['model'] == 'openai/quick'
    call_count = len(wires)
    assert job.run(PROJECT)['replayed'] is True and len(wires) == call_count
    assert env.records.read('v2_consolidation_inputs', consumed.object_id) == consumed


@pytest.mark.parametrize('private', [False, True])
def test_real_parentless_division_keeps_empty_refs_and_requires_confirmation(scenario, private):
    env = scenario
    old, _ = failed_without_root(env)
    fact = adjust(env, old['turn']['id'])
    if private:
        set_private_project(env.records, PROJECT, True, 0)
    job, wires = consumer(env, local=private)
    result = job.run(PROJECT)
    assert result['new_suggestions'] == 1 and result['failed_groups'] == 0
    request, = [request for request in requests(env.records) if request['desired_outcome'] == 'memory.consolidate']
    assert request['input']['refs'] == [] and request['privacy']['source_snapshots'] == []
    assert request['privacy']['material_refs'] == []
    assert request['privacy']['allow_remote'] is (not private)
    assert request['input']['text'].count('outcome:' + fact.object_id) == 1
    assert env.records.list('recognition_experiences') == ()
    candidate, = env.records.list('recognition_candidates')
    assert candidate.payload['scope'] == {'user_id': 'local-user', 'project_id': PROJECT}
    assert candidate.payload['source_experience_ids'] == [] and env.records.list('recognitions') == ()
    recognition = confirm(env, candidate)
    snapshot = SourceEgressService(env.records).snapshot(WorkScope('local-user', PROJECT),
        [{'type': 'recognition', 'id': recognition.id, 'revision': 1}])
    if private:
        with pytest.raises(RecognitionError):
            SourceEgressService(env.records).require(snapshot, 'generation')
    else:
        SourceEgressService(env.records).require(snapshot, 'generation')
    assert len(wires) == 1
    receipt, = wire_receipts(env.records)
    auxiliary_completed(env)
    assert receipt['status'] == 'succeeded' and receipt['usage']['total_tokens'] == 6
    assert receipt['execution_location'] == ('local_loopback' if private else 'remote')
    assert receipt['model_id'] == ('qwen2.5-1.5b-instruct' if private else 'quick')


def test_disabled_real_configuration_blocks_parentless_feedback_without_wire(scenario):
    env = scenario
    old, _ = failed_without_root(env)
    fact = adjust(env, old['turn']['id'])
    job, wires = consumer(env, enabled=False)
    assert len(qualified(env, job)) == 1
    result = job.run(PROJECT)
    assert result['new_suggestions'] == 0 and result['failed_groups'] == 0
    assert wires == [] and env.records.list('v2_consolidation_inputs') == ()
    assert not any(request['desired_outcome'] == 'memory.consolidate' for request in requests(env.records))
    assert len(qualified(env, job)) == 1 and qualified(env, job)[0]['event_id'] == 'outcome:' + fact.object_id


def test_parentful_division_without_root_defers_without_fabricating_experience(scenario):
    env = scenario
    parent_profile(env)
    old, _ = failed_without_root(env)
    adjust(env, old['turn']['id'])
    set_private_project(env.records, 'me', True, 0)
    before = env.records.list('recognition_experiences')
    job, wires = consumer(env, local=True)
    from backend.memory_app.v2.consolidation_events import outcomes
    event, = outcomes(env.records, PROJECT, now=job.now().isoformat(), local_only=True)
    assert event['_refs'] and event['_source_graph']['nodes']
    assert qualified(env, job, local=True) == []
    result = job.run(PROJECT)
    assert result['new_suggestions'] == 0 and wires == []
    assert env.records.list('recognition_experiences') == before
    assert env.records.list('v2_consolidation_inputs') == ()


def test_birth_artifact_preserves_private_me_closure_after_actual_local_wire_and_confirmation(scenario):
    env = scenario
    parent = parent_profile(env)
    _, turn = completed(env)
    edit(env, turn)
    set_private_project(env.records, 'me', True, 0)
    job, wires = consumer(env, local=True)
    result = job.run(PROJECT)
    assert result['new_suggestions'] == 1 and result['failed_groups'] == 0
    request, = [request for request in requests(env.records) if request['desired_outcome'] == 'memory.consolidate']
    assert request['privacy']['mode'] == 'local_only' and request['privacy']['allow_remote'] is False
    assert len(request['privacy']['material_refs']) == 1
    candidate = env.records.read('recognition_candidates', env.records.list('v2_insight_patterns')[0].object_id)
    experience, = candidate.payload['source_experience_ids']
    artifact = env.records.read('recognition_experiences', experience)
    assert artifact.payload['provenance']['kind'] == 'model_generated_artifact'
    document, = [ref for ref in artifact.payload['provenance']['source_refs'] if ref['type'] == 'document']
    assert document['revision'] == 1 and env.documents.read(document['id'])['revision'] == 2
    recognition = confirm(env, candidate)
    authority = SourceEgressService(env.records)
    snapshot = authority.snapshot(WorkScope('local-user', PROJECT),
        [{'type': 'recognition', 'id': recognition.id, 'revision': 1}])
    graph = SourceGraph()
    graph.snapshot(snapshot)
    assert any(node['type'] == 'recognition' and node['id'] == parent.id
        and node['scope']['project_id'] == 'me' and node['effective_purposes'] == [] for node in graph.result()['nodes'])
    with pytest.raises(RecognitionError):
        authority.require(snapshot, 'generation')
    own_wire, = [wire for wire in wires if '"outcomes"' in wire['messages'][-1]['content']]
    assert own_wire['model'] == 'openai/qwen2.5-1.5b-instruct'
    assert own_wire['base_url'] == 'http://127.0.0.1:8001/local-model/v1'
    receipt, = wire_receipts(env.records)
    auxiliary_completed(env)
    assert receipt['status'] == 'succeeded' and receipt['execution_location'] == 'local_loopback'


def test_real_input_insert_failure_rolls_back_candidate_and_consumption_then_reuses_cached_wire(scenario):
    env = scenario
    _, turn = completed(env)
    edit(env, turn)
    def records_released():
        with sqlite3.connect(env.records.database_path, timeout=0) as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.rollback()
    job, wires = consumer(env, before=records_released)
    events = qualified(env, job)
    reject_inputs(env, enable=True)
    try:
        with pytest.raises(sqlite3.IntegrityError, match='controlled inputs failure'):
            job._pattern(PROJECT, [], use_corrections=True, outcome_events=events)
    finally:
        reject_inputs(env, enable=False)
    assert len(wires) == 1 and env.records.list('recognition_candidates') == ()
    assert env.records.list('v2_insight_patterns') == () and env.records.list('v2_consolidation_inputs') == ()
    assert wire_receipts(env.records)[0]['status'] == 'succeeded'
    time.sleep(MemoryTurn.store_for(env.records).effect_runner.lease_seconds + 1)
    assert job._pattern(PROJECT, [], use_corrections=True, outcome_events=events) == 1
    assert len(wires) == 1 and len(env.records.list('recognition_candidates')) == 1
    assert len(env.records.list('v2_consolidation_inputs')) == 1


def test_cached_generation_cannot_commit_after_actual_event_owner_cas_drift(scenario):
    env = scenario
    _, turn = completed(env)
    fact = edit(env, turn)
    job, wires = consumer(env)
    events = qualified(env, job)
    reject_inputs(env, enable=True)
    try:
        with pytest.raises(sqlite3.IntegrityError, match='controlled inputs failure'):
            job._pattern(PROJECT, [], use_corrections=True, outcome_events=events)
    finally:
        reject_inputs(env, enable=False)
    with env.records.begin() as tx:
        tx.put('v2_outcome_corrections', fact.object_id, fact.payload, expected_revision=fact.revision)
        tx.commit()
    with pytest.raises(RecognitionError):
        job._pattern(PROJECT, [], use_corrections=True, outcome_events=events)
    assert len(wires) == 1 and env.records.list('recognition_candidates') == ()
    assert env.records.list('v2_consolidation_inputs') == ()


def test_real_transport_failure_and_unchanged_retry_never_consume_feedback(scenario):
    env = scenario
    _, turn = completed(env)
    edit(env, turn)
    job, wires = consumer(env, failure=True)
    events = qualified(env, job)
    for _ in range(2):
        with pytest.raises((RecognitionError, ModelConfigurationError)):
            job._pattern(PROJECT, [], use_corrections=True, outcome_events=events)
    assert len(wires) == 1
    assert env.records.list('recognition_candidates') == () and env.records.list('v2_consolidation_inputs') == ()
    receipt, = wire_receipts(env.records)
    assert receipt['status'] == 'failed_transport' and receipt['usage'] is None


def test_actual_owner_drift_before_freezing_never_dispatches_feedback(scenario):
    env = scenario
    _, turn = completed(env)
    fact = edit(env, turn)
    job, wires = consumer(env)
    events = qualified(env, job)
    assert len(events) == 1
    with env.records.begin() as tx:
        tx.put('v2_outcome_corrections', fact.object_id, fact.payload, expected_revision=fact.revision)
        tx.commit()
    with pytest.raises(RecognitionError):
        job._pattern(PROJECT, [], use_corrections=True, outcome_events=events)
    assert wires == [] and env.records.list('v2_memory_turn_keys') == ()
    assert env.records.list('recognition_candidates') == () and env.records.list('v2_consolidation_inputs') == ()


@pytest.mark.parametrize('change', ['event_cas', 'privacy'])
def test_real_post_wire_guard_discards_output_but_keeps_actual_paid_usage(scenario, change):
    env = scenario
    _, turn = completed(env)
    fact = edit(env, turn)
    def drift():
        if change == 'privacy':
            set_private_project(env.records, PROJECT, True, 0)
        else:
            with env.records.begin() as tx:
                tx.put('v2_outcome_corrections', fact.object_id, fact.payload, expected_revision=fact.revision)
                tx.commit()
    job, wires = consumer(env, after=drift)
    events = qualified(env, job)
    with pytest.raises(RecognitionError):
        job._pattern(PROJECT, [], use_corrections=True, outcome_events=events)
    assert len(wires) == 1 and env.records.list('recognition_candidates') == ()
    assert env.records.list('v2_consolidation_inputs') == ()
    key, = env.records.list('v2_memory_turn_keys')
    assert MemoryTurn.store_for(env.records).get_immutable_payload(key.object_id, 'memory-generation-output-v1') is None
    receipt, = wire_receipts(env.records)
    assert receipt['status'] == 'succeeded' and receipt['usage']['total_tokens'] == 6
    if change == 'privacy':
        assert job._pattern(PROJECT, [], use_corrections=True, outcome_events=events) == 0
    else:
        with pytest.raises(RecognitionError):
            job._pattern(PROJECT, [], use_corrections=True, outcome_events=events)
    assert len(wires) == 1


def test_private_me_feedback_cannot_use_actual_remote_fast_route(scenario):
    env = scenario
    parent_profile(env)
    _, turn = completed(env)
    edit(env, turn)
    set_private_project(env.records, 'me', True, 0)
    job, wires = consumer(env)
    assert qualified(env, job) == []
    captured = qualified(env, job, local=True)
    assert len(captured) == 1
    with pytest.raises(RecognitionError):
        job._pattern(PROJECT, [], use_corrections=True, outcome_events=captured)
    assert wires == [] and env.records.list('v2_consolidation_inputs') == ()
    assert not any(request['desired_outcome'] == 'memory.consolidate' for request in requests(env.records))


def test_unknown_current_division_receipt_never_becomes_parentless_feedback(scenario):
    env = scenario
    old, _ = failed_without_root(env)
    adjust(env, old['turn']['id'])
    job, wires = consumer(env)
    original = env.records.read('v2_turns', old['turn']['id'])
    assert original.payload['receipt']['do']['document_id'] is None and len(qualified(env, job)) == 1
    for value in ('', 0, 'missing'):
        payload = deepcopy(original.payload)
        if value == 'missing':
            payload['receipt']['do'].pop('document_id')
        else:
            payload['receipt']['do']['document_id'] = value
        with env.records.begin() as tx:
            current = tx.read('v2_turns', original.object_id)
            tx.put('v2_turns', original.object_id, payload, expected_revision=current.revision)
            tx.commit()
        assert qualified(env, job) == []
        assert wires == [] and env.records.list('v2_consolidation_inputs') == ()
    with env.records.begin() as tx:
        current = tx.read('v2_turns', original.object_id)
        tx.put('v2_turns', original.object_id, original.payload, expected_revision=current.revision)
        tx.commit()
    events = qualified(env, job)
    assert len(events) == 1 and events[0]['_roots'] == []
    assert job._pattern(PROJECT, [], use_corrections=True, outcome_events=events) == 1
    assert len(wires) == 1 and len(env.records.list('v2_consolidation_inputs')) == 1


def test_existing_real_recognition_support_retains_birth_revision_and_human_review(scenario):
    env = scenario
    service, scope = env.state.recognition_service, WorkScope('local-user', PROJECT)
    source = service.stage_experience(scope=scope, content='合成的已确认用户判断')
    target = service.propose(scope=scope, content='以后先核对用户目标再交付', source_experience_ids=[source])
    recognition = service.publish(scope=scope, candidate_id=target.id, expected_revision=1, reviewer='local-user')
    before = env.records.read('recognitions', recognition.id)
    _, turn = completed(env)
    edit(env, turn)
    job, wires = consumer(env)
    assert job._pattern(PROJECT, [], use_corrections=True, outcome_events=qualified(env, job)) == 1
    support, = env.records.list('v2_insight_evidence_support')
    assert support.payload['target_id'] == recognition.id and support.payload['state'] == 'pending'
    document, = support.payload['documents']
    assert document == {'id': turn['receipt']['do']['document_id'], 'revision': 1}
    assert env.documents.read(document['id'])['revision'] == 2
    experience, = support.payload['experience_ids']
    artifact = env.records.read('recognition_experiences', experience)
    assert artifact.payload['provenance']['kind'] == 'model_generated_artifact'
    assert {'type': 'document', **document} in artifact.payload['provenance']['source_refs']
    assert len(wires) == 1 and len(env.records.list('v2_consolidation_inputs')) == 1
    assert env.records.read('recognitions', recognition.id) == before
    response = env.client.post('/api/v2/library/evidence-support/' + support.object_id + '/accept',
        json={'project_id': PROJECT, 'expected_revision': support.revision})
    assert response.status_code == 200, response.text
    assert env.records.read('v2_insight_evidence_support', support.object_id).payload['state'] == 'approved'
    assert env.records.read('recognitions', recognition.id) == before


def test_private_me_parent_support_keeps_original_source_guard_and_public_target(scenario):
    env = scenario
    parent = parent_profile(env)
    service, scope = env.state.recognition_service, WorkScope('local-user', PROJECT)
    source = service.stage_experience(scope=scope, content='合成公开用户判断')
    target = service.propose(scope=scope, content='以后先核对用户目标再交付', source_experience_ids=[source])
    recognition = service.publish(scope=scope, candidate_id=target.id, expected_revision=1, reviewer='local-user')
    before = env.records.read('recognitions', recognition.id)
    _, turn = completed(env)
    edit(env, turn)
    set_private_project(env.records, 'me', True, 0)
    job, wires = consumer(env, local=True)
    assert job._pattern(PROJECT, [], use_corrections=True, outcome_events=qualified(env, job, local=True)) == 1
    support, = env.records.list('v2_insight_evidence_support')
    assert support.payload['state'] == 'pending' and support.payload['target_id'] == recognition.id
    assert support.payload['documents'] == [{'id': turn['receipt']['do']['document_id'], 'revision': 1}]
    own_snapshot = support.payload['snapshots'][0]['snapshot']
    graph = SourceGraph()
    graph.snapshot(own_snapshot)
    assert any(node['type'] == 'recognition' and node['id'] == parent.id
        and node['scope']['project_id'] == 'me' and node['effective_purposes'] == [] for node in graph.result()['nodes'])
    with pytest.raises(RecognitionError):
        SourceEgressService(env.records).require(own_snapshot, 'generation')
    assert len(wires) == 1 and wires[0]['model'] == 'openai/qwen2.5-1.5b-instruct'
    response = env.client.post('/api/v2/library/evidence-support/' + support.object_id + '/accept',
        json={'project_id': PROJECT, 'expected_revision': support.revision})
    assert response.status_code == 200, response.text
    assert env.records.read('v2_insight_evidence_support', support.object_id).payload['state'] == 'approved'
    assert env.records.read('recognitions', recognition.id) == before
