"""External numbered evidence reaches the original pending and source consumers."""
from uuid import uuid4
from copy import deepcopy
import json
import sqlite3

import pytest

from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import WorkScope, RecognitionConflict
from backend.recognition.sql_source_identities import KIND as SQL_IDENTITIES
from backend.memory_app.source_graph import SourceGraph, validate_graph
from backend.memory_app.source_egress import _frozen_packet_authority
from core.effect_log import EffectState
from tests.memory_app.v2.test_external_context import env, settings, setup, request, DAY


COMPANION = 'external-context-identities-v1'
DEPENDENCIES = 'v2_external_input_dependencies'


def delivered_original(env, *, execute=True):
    store = env.domains.query.source_store
    payload = {'id': 'external-evidence-json', 'project_id': 'alpha', 'title': '合成公开原件',
        'metadata': {'content_snapshot': '真实 JSON 原件记录的完整合成证据'}}
    store.write('sources', payload['id'], payload, expected_revision=0)
    settings(env, allow_remote=True)
    api, runtime, runner = setup(env)
    turn = 'turn-' + uuid4().hex
    api.prepare(turn, request(tool='read'), [{'type': 'original_source', 'id': payload['id'],
        'project_id': 'alpha', 'revision': 1, 'layer': 'L0', 'windows': []}],
        session_id='session-external-evidence', operation_id='op-external-evidence',
        idempotency_key=turn, created_at=DAY.isoformat())
    if not execute:
        assert api.turns.events_after(turn)[-1]['type'] == 'turn.accepted'
        assert env.records.list('v2_external_agent_reservations') == ()
        return api, turn, store, payload
    result = api.execute(turn, runtime=runtime, runner=runner)
    assert result['entries'][0]['id'] == 'M1'
    proof, _arguments = api.delivered_proof(turn, 'M1', client='codex')
    assert {node['type'] for node in proof['snapshot']['nodes']} == {'original_source'}
    assert proof['snapshot']['nodes'][0]['incarnation'] == store.incarnation('sources', payload['id'])
    outcomes = [event for event in api.turns.events_after(turn) if event['type'] == 'tool.outcome.recorded']
    assert len(outcomes) == 1
    assert api.turns.effect_runner.log.get(outcomes[0]['correlation']['tool_call_id']).state is EffectState.SETTLED_OK
    assert env.model.calls == 0
    return api, turn, store, payload


def propose(env, turn, *, client='codex', project='alpha'):
    return env.http.post('/api/v2/external-agent/mcp/propose_insight', json={'client': client,
        'arguments': {'text': '基于交付原件提出的合成认识', 'conditions': ['阅读时'], 'project': project,
                      'evidence_ids': [{'turn_id': turn, 'id': 'M1'}]}})


def test_json_numbered_evidence_reaches_pending_publication_and_real_source_egress(env):
    api, turn, store, payload = delivered_original(env)
    before = store.read('sources', payload['id'])
    response = propose(env, turn)
    assert response.status_code == 200, response.text
    output = response.json()
    assert output['turn_id'] is None and output['result']['state'] == 'pending'
    candidate = env.records.read('recognition_candidates', output['result']['candidate_id'])
    assert candidate.payload['state'] == 'pending' and len(candidate.payload['source_experience_ids']) == 1
    experience = env.records.read('recognition_experiences', candidate.payload['source_experience_ids'][0])
    assert experience.payload['provenance']['kind'] == 'user_statement'
    assert experience.payload['provenance']['actor'] == 'codex'
    assert experience.payload['content'] == candidate.payload['content']
    marker = env.records.read(DEPENDENCIES, experience.object_id)
    assert marker is not None and marker.revision == 1
    assert 'content' not in marker.payload and 'text' not in marker.payload
    companion = api.turns.get_immutable_payload(turn, COMPANION)
    assert companion is not None and companion[1]['entries']['M1']['material']['id'] == payload['id']
    assert env.records.list('recognitions') == ()
    scope = WorkScope('local-user', 'alpha')
    recognition = env.service.publish(scope=scope, candidate_id=candidate.object_id,
        expected_revision=candidate.revision, reviewer='local-user')
    qualified = env.service.get_recognition(scope=scope, recognition_id=recognition.id)
    assert qualified.authorized
    authority = SourceEgressService(env.records)
    snapshot = authority.snapshot(scope, [{'type': 'recognition', 'id': recognition.id, 'revision': recognition.revision}])
    authority.require(snapshot, 'generation')
    authority.validate_snapshot(scope, snapshot)
    graph = SourceGraph()
    graph.snapshot(snapshot)
    validate_graph(graph.result(), scope.user_id)
    _frozen_packet_authority(scope, {'source_egress': snapshot}, [('recognition', recognition.id, recognition.revision)])
    assert any(node.get('dependency_revisions', {}).get('external_input_revision') == 1 for node in snapshot['nodes'])
    assert store.read('sources', payload['id']) == before and store.revision('sources', payload['id']) == 1
    assert env.model.calls == 0 and env.records.list('v2_usage_insight') == ()


def pending(env):
    api, turn, store, payload = delivered_original(env)
    response = propose(env, turn)
    assert response.status_code == 200, response.text
    candidate = env.records.read('recognition_candidates', response.json()['result']['candidate_id'])
    return api, turn, store, payload, candidate


def publish(env, candidate):
    return env.service.publish(scope=WorkScope('local-user', 'alpha'), candidate_id=candidate.object_id,
        expected_revision=candidate.revision, reviewer='local-user')


def change_source(store, payload, change):
    if change in {'deleted', 'recreated'}:
        assert store.delete('sources', payload['id'])
    if change == 'recreated':
        store.write('sources', payload['id'], payload, expected_revision=0)
        assert store.revision('sources', payload['id']) == 1
    elif change == 'updated':
        store.write('sources', payload['id'], {**payload, 'title': '已修订的合成原件'}, expected_revision=1)


@pytest.mark.parametrize('change', ['deleted', 'recreated', 'updated'])
def test_json_evidence_generation_change_blocks_pending_confirmation(env, change):
    _api, _turn, store, payload, candidate = pending(env)
    birth = store.incarnation('sources', payload['id'])
    change_source(store, payload, change)
    if change == 'recreated':
        assert store.incarnation('sources', payload['id']) != birth
    before = deepcopy(candidate.payload)
    with pytest.raises(RecognitionConflict):
        publish(env, candidate)
    assert env.records.read('recognition_candidates', candidate.object_id).payload == before
    assert env.records.list('recognitions') == () and env.model.calls == 0


@pytest.mark.parametrize('change', ['deleted', 'recreated'])
def test_json_evidence_generation_change_revokes_qualified_and_egress_consumers(env, change):
    _api, _turn, store, payload, candidate = pending(env)
    recognition = publish(env, candidate)
    scope = WorkScope('local-user', 'alpha')
    change_source(store, payload, change)
    assert not env.service.get_recognition(scope=scope, recognition_id=recognition.id).authorized
    with pytest.raises(RecognitionConflict):
        SourceEgressService(env.records).snapshot(scope,
            [{'type': 'recognition', 'id': recognition.id, 'revision': recognition.revision}])
    assert env.records.read('recognitions', recognition.id).payload['state'] == 'active'
    assert env.model.calls == 0


def test_json_evidence_privacy_inherits_but_external_admission_is_not_permanent_source_authority(env):
    _api, _turn, _store, payload, candidate = pending(env)
    settings(env, allow_remote=False, clients={'claude': True, 'codex': False})
    scope = WorkScope('local-user', 'alpha')
    recognition = publish(env, candidate)
    assert env.service.get_recognition(scope=scope, recognition_id=recognition.id).authorized
    authority = SourceEgressService(env.records)
    authority.set_policy(scope, 'original_source', payload['id'], 1, 0, [])
    assert env.service.get_recognition(scope=scope, recognition_id=recognition.id).authorized
    snapshot = authority.snapshot(scope, [{'type': 'recognition', 'id': recognition.id, 'revision': recognition.revision}])
    assert any(node.get('dependency_revisions', {}).get('current_source_graph', {}).get('nodes')
        for node in snapshot['nodes'])
    with pytest.raises(RecognitionConflict):
        authority.require(snapshot, 'generation')
    assert env.model.calls == 0


@pytest.mark.parametrize('fault', ['client', 'scope', 'unknown', 'missing', 'bool'])
def test_json_number_requires_original_owner_mapping_and_companion(env, fault):
    api, turn, _store, _payload = delivered_original(env)
    client, project = ('claude' if fault == 'client' else 'codex'), ('beta' if fault == 'scope' else 'alpha')
    if fault in {'missing', 'bool'}:
        path = env.root / '.rebuild-data/ai-turns.sqlite3'
        with sqlite3.connect(path) as connection:
            if fault == 'missing':
                connection.execute('DELETE FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?', (turn, SQL_IDENTITIES))
            else:
                ref, companion = api.turns.get_immutable_payload(turn, SQL_IDENTITIES)
                companion['entries']['M1']['proof']['material']['revision'] = True
                connection.execute('UPDATE ai_turn_immutable_payloads SET payload_json=? WHERE payload_ref=?',
                    (json.dumps(companion), ref))
    if fault == 'unknown':
        response = env.http.post('/api/v2/external-agent/mcp/propose_insight', json={'client': client,
            'arguments': {'text': '合成认识', 'project': project, 'evidence_ids': [{'turn_id': turn, 'id': 'M99'}]}})
    else:
        response = propose(env, turn, client=client, project=project)
    assert response.status_code == 409
    for collection in (DEPENDENCIES, 'recognition_candidates', 'recognition_experiences', 'v2_external_agent_intakes',
                       'v2_external_agent_write_reservations'):
        assert env.records.list(collection) == ()
    assert env.model.calls == 0


def test_json_evidence_marker_and_quota_roll_back_with_the_real_pending_owner(env):
    _api, turn, _store, _payload = delivered_original(env)
    with env.records.begin() as tx:
        tx.connection.execute("CREATE TRIGGER reject_evidence_candidate BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='recognition_candidates' BEGIN SELECT RAISE(ABORT,'synthetic candidate rejection'); END")
        tx.commit()
    with env.records.begin() as tx:
        quotas = list(tx.connection.execute("SELECT collection,object_id,revision,payload_json FROM crp_structured_records WHERE collection LIKE 'v2_external_agent_quota_%' ORDER BY collection,object_id"))
    assert propose(env, turn).status_code == 409
    for collection in (DEPENDENCIES, 'recognition_candidates', 'recognition_experiences', 'v2_external_agent_intakes',
                       'v2_external_agent_write_reservations'):
        assert env.records.list(collection) == ()
    with env.records.begin() as tx:
        assert list(tx.connection.execute("SELECT collection,object_id,revision,payload_json FROM crp_structured_records WHERE collection LIKE 'v2_external_agent_quota_%' ORDER BY collection,object_id")) == quotas
    assert env.model.calls == 0


def test_missing_external_marker_cannot_revert_to_plain_user_statement_authority(env):
    _api, _turn, _store, _payload, candidate = pending(env)
    recognition = publish(env, candidate)
    experience = candidate.payload['source_experience_ids'][0]
    with env.records.begin() as tx:
        tx.delete(DEPENDENCIES, experience, expected_revision=1)
        tx.commit()
    scope = WorkScope('local-user', 'alpha')
    assert not env.service.get_recognition(scope=scope, recognition_id=recognition.id).authorized
    with pytest.raises(RecognitionConflict):
        SourceEgressService(env.records).snapshot(scope,
            [{'type': 'recognition', 'id': recognition.id, 'revision': recognition.revision}])
    assert env.model.calls == 0


def test_malformed_external_marker_rejects_the_qualified_consumer_without_type_escape(env):
    _api, _turn, _store, _payload, candidate = pending(env)
    recognition = publish(env, candidate)
    experience = candidate.payload['source_experience_ids'][0]
    with env.records.begin() as tx:
        marker = tx.read(DEPENDENCIES, experience)
        bad = {**marker.payload, 'client': []}
        tx.connection.execute('UPDATE crp_structured_records SET payload_json=? WHERE collection=? AND object_id=? AND revision=?',
            (json.dumps(bad), DEPENDENCIES, experience, 1))
        tx.commit()
    scope = WorkScope('local-user', 'alpha')
    assert not env.service.get_recognition(scope=scope, recognition_id=recognition.id).authorized
    with pytest.raises(RecognitionConflict):
        SourceEgressService(env.records).snapshot(scope,
            [{'type': 'recognition', 'id': recognition.id, 'revision': recognition.revision}])
    assert env.model.calls == 0


@pytest.mark.parametrize('fault', ['accepted_only', 'delivery', 'effect', 'dispatch', 'terminal'])
def test_actual_original_kernel_and_delivery_facts_are_required_for_evidence(env, fault):
    _api, turn, _store, _payload = delivered_original(env, execute=fault != 'accepted_only')
    if fault == 'delivery':
        with env.records.begin() as tx:
            tx.delete('v2_external_agent_deliveries', turn, expected_revision=1)
            tx.commit()
    elif fault in {'effect', 'dispatch', 'terminal'}:
        with sqlite3.connect(env.root / '.rebuild-data/ai-turns.sqlite3') as connection:
            if fault == 'effect':
                connection.execute("UPDATE effect SET state='UNKNOWN' WHERE turn_id=?", (turn,))
            else:
                kind = 'tool.dispatch.claimed' if fault == 'dispatch' else 'turn.completed'
                rows = [(row[0], json.loads(row[1])) for row in connection.execute(
                    'SELECT sequence,event_json FROM ai_turn_events WHERE turn_id=?', (turn,))]
                selected = [seq for seq, event in rows if event['type'] == kind]
                assert len(selected) == 1
                connection.execute('DELETE FROM ai_turn_events WHERE turn_id=? AND sequence=?', (turn, selected[0]))
    assert propose(env, turn).status_code == 409
    for collection in (DEPENDENCIES, 'recognition_candidates', 'recognition_experiences',
                       'v2_external_agent_intakes', 'v2_external_agent_write_reservations'):
        assert env.records.list(collection) == ()
    assert env.model.calls == 0


def test_uncommitted_json_payload_cannot_retain_delivered_evidence_authority(env, monkeypatch):
    from core.storage_provider import runtime as storage_runtime
    from core.storage_provider.source_retrieval_index import index_store, namespace_projection, COLLECTION
    _api, _turn, store, payload, candidate = pending(env)
    paths = store._object_paths('sources', payload['id'])
    original_write = storage_runtime._write_json_atomic

    def interrupted_write(path, value):
        if path == paths.meta_path:
            raise OSError('synthetic interruption before source metadata commit')
        return original_write(path, value)

    monkeypatch.setattr(storage_runtime, '_write_json_atomic', interrupted_write)
    changed = {**payload, 'metadata': {'content_snapshot': '尚未提交的新正文'}}
    with pytest.raises(OSError, match='synthetic interruption'):
        store.write('sources', payload['id'], changed, expected_revision=1)
    assert store.read('sources', payload['id']) == changed
    assert store.revision('sources', payload['id']) == 1
    projection = namespace_projection(index_store(store).read(COLLECTION, payload['id']), store.namespace_id)
    assert projection['state'] == 'invalid'
    assert projection['invalidated_revision'] == 1
    with pytest.raises(RecognitionConflict):
        publish(env, candidate)
    assert env.records.list('recognitions') == ()
    assert env.records.read('recognition_candidates', candidate.object_id).payload['state'] == 'pending'
