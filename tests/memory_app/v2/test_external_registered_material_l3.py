"""原材料整理、人工确认和 L3 到 L0 交付；只隔离模型原生传输。"""
import asyncio
import json
import sqlite3
from threading import RLock

from fastapi import HTTPException
import pytest

from backend.memory_app.processing_lease import ProcessingLease
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.external_agent_guard import ExternalAgentGuardError
from backend.memory_app.v2.external_context import DELIVERIES, ExternalContextError
from backend.memory_app.v2.mcp_memory import _read_selections
from backend.memory_app.workspace_confirmation import COLLECTION, WorkspaceConfirmation
from backend.memory_app.workspace_intake import WorkspaceIntake
from backend.memory_app.workspace_items import WorkspaceItems
from backend.memory_app.workspace_review import WorkspaceReview
from backend.recognition import WorkScope
from core.effect_log import EffectState
from tests.memory_app.v2.test_external_registered_context import (
    NOW, RESERVATIONS, RegisteredContext, no_factory_or_artifact_capture,
)
from tests.memory_app.v2.test_external_registered_read import (
    assert_child_not_accepted, parent_facts, prepare_read,
)


SOURCE = '礼物预算先确认实际需求，再选择常用物品。'
QUERY = '礼物预算'
SCOPE = WorkScope('local-user', 'alpha')


@pytest.fixture
def local_human_published_material(tmp_path):
    # 此命名确认仅建立合成用户素材；外部客户端仍只执行原 recall/read。
    native_calls = []

    def completion(**request):
        assert request['messages'][-1]['content'] == SOURCE
        assert all(isinstance(message['content'], str) for message in request['messages'])
        native_calls.append(request['model'])
        draft = {'title': '礼物预算材料', 'summary': SOURCE, 'topics': [QUERY],
            'facts': [{'text': SOURCE, 'evidence': {'quote': SOURCE}}],
            'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}
        return {'choices': [{'message': {'content': json.dumps(draft, ensure_ascii=False)},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 30}}

    actual = RegisteredContext(tmp_path, completion_fn=completion)
    try:
        actual.models.update('generation', {'base_url': 'https://provider.invalid/v1',
            'model': 'material-writer', 'api_key': 'synthetic-local-only',
            'allow_remote': True, 'expected_revision': 0})
        items = WorkspaceItems(actual.records,
            ProcessingLease(actual.records, 'workspace_items', 'synthetic-material-owner'), RLock())
        intake = WorkspaceIntake(tmp_path, items, actual.models)
        confirmations = WorkspaceConfirmation(tmp_path, actual.records, actual.documents)
        review = WorkspaceReview(items, actual.documents, actual.service, confirmations, None, intake.root)
        staged = asyncio.run(intake.add_text({'project_id': 'alpha', 'text': SOURCE}))
        assert staged['status'] == 'staged' and staged['revision'] == 1
        ready = asyncio.run(intake.process(staged['id'], {'project_id': 'alpha'}))
        assert ready['status'] == 'ready' and ready['draft']['facts'][0]['evidence'] == {
            'start': 0, 'end': len(SOURCE), 'quote': SOURCE}
        assert len(native_calls) == 1
        checkpoints = actual.records.list('workspace_organize_steps')
        assert len(checkpoints) == 1 and not checkpoints[0].payload['rejected']
        organize = checkpoints[0].payload['turn_id']
        events = actual.turns.events_after(organize)
        assert actual.turns.get_request(organize)['desired_outcome'] == 'memory.organize'
        assert events[-1]['type'] == 'turn.completed'
        assert len([event for event in events if event['type'] == 'model.completed']) == 1
        dispatched = [event for event in events if event['type'] == 'model.attempt.dispatched']
        terminal = [event for event in events if event['type'] == 'model.attempt.terminal']
        assert len(dispatched) == len(terminal) == 1
        dispatch_ref = dispatched[0]['data']['payload_ref']
        dispatch = actual.turns.get(dispatch_ref)
        receipt_ref = terminal[0]['data']['receipt_ref']
        receipt = actual.turns.get(receipt_ref)
        assert receipt['status'] == 'succeeded' and receipt['turn_id'] == organize
        assert receipt['attempt_id'] == dispatch['attempt_id']
        assert terminal[0]['data']['evidence_refs'] == [dispatch_ref]
        effect = actual.turns.effect_runner.log.get(dispatch['attempt_id'])
        assert effect.state is EffectState.SETTLED_OK and effect.turn_id == organize
        assert effect.intent_ref == dispatch_ref and effect.result_ref == receipt_ref
        with sqlite3.connect(tmp_path / '.rebuild-data/ai-turns.sqlite3') as connection:
            saved = connection.execute('SELECT status,terminal_status,dispatch_payload_ref,terminal_receipt_ref '
                'FROM ai_model_attempt_reservations WHERE attempt_id=?', (dispatch['attempt_id'],)).fetchall()
        assert saved == [('terminal', 'succeeded', dispatch_ref, receipt_ref)]

        confirmed = asyncio.run(review.confirm(staged['id'], {'project_id': 'alpha',
            'expected_revision': ready['revision']}))
        assert confirmed['status'] == 'confirmed'
        operations = actual.records.list(COLLECTION)
        assert len(operations) == 1 and operations[0].payload['state'] == 'committed'
        assert operations[0].payload['workspace_item_id'] == staged['id']
        assert operations[0].payload['document_id'] == confirmed['document_id']
        source = actual.sources.read('sources', confirmed['source_id'])
        assert source == operations[0].payload['source_payload']
        assert source['metadata']['content_snapshot'] == SOURCE and source['content_hash'] is None
        extracted = asyncio.run(review.recognition(staged['id'], {'project_id': 'alpha'}))
        document = actual.documents.read(extracted['document_id'])
        experience = actual.records.read('recognition_experiences', extracted['experience_id'])
        assert experience.payload['provenance'] == {'kind': 'workspace_confirmed_document',
            'actor': 'local-user', 'source_refs': [{'type': 'document', 'id': document['id'],
                'revision': document['revision']}], 'epistemic_status': 'unverified',
            'recorded_at': experience.payload['created_at']}
        candidate = actual.records.read('recognition_candidates', extracted['candidate_id'])
        assert candidate.payload['state'] == 'pending'
        published = actual.service.publish(scope=SCOPE, candidate_id=extracted['candidate_id'],
            expected_revision=candidate.revision, reviewer='local-human-test-reviewer')
        recognition = actual.records.read('recognitions', published.id)
        assert recognition.payload['published_by'] == 'local-human-test-reviewer'
        assert recognition.payload['source_experience_ids'] == [extracted['experience_id']]
        assert recognition.payload['source_experience_revisions'] == {extracted['experience_id']: experience.revision}
        actual.material_items, actual.material_item_id = items, staged['id']
        actual.material_source_id, actual.material_document_id = confirmed['source_id'], document['id']
        actual.material_recognition_id, actual.native_material_calls = published.id, native_calls
        # 实际图由原 Source owner 展开；测试不写关系、refs 或资格证明。
        graph = SourceEgressService(actual.records).snapshot(SCOPE,
            [{'type': 'recognition', 'id': published.id, 'revision': recognition.revision}])
        SourceEgressService(actual.records).validate_snapshot(SCOPE, graph)
        assert any(node['type'] == 'original_item' and node['id'] == staged['id'] for node in graph['nodes'])
        assert any(node['type'] == 'experience' and node['id'] == extracted['experience_id'] for node in graph['nodes'])
        yield actual
    finally:
        assert actual.runner.shutdown(timeout_seconds=3) == ()
        assert actual.transport_calls == [] and len(native_calls) == 1


def delivered_l3(actual, *, client='codex'):
    parent = f'turn-material-l3-{client}'
    request = {'client': client, 'tool': 'recall', 'query': QUERY, 'budget': 3000,
        'scope': {'user_id': 'local-user', 'project_id': 'alpha'}}
    frozen = actual.context.prepare_recall(parent, request, session_id=f'session-{parent}',
        operation_id=f'op-{parent}', idempotency_key=parent, created_at=NOW.isoformat())
    delivered = actual.execute(parent)
    actual.assert_completed(parent, frozen, delivered)
    entry = next(row for row in delivered['entries'] if row['object_id'] == actual.material_recognition_id)
    assert entry['layer'] == 'L3' and SOURCE in entry['excerpt']
    proof, original = actual.context.delivered_proof(parent, entry['id'], client=client)
    assert proof['material']['type'] == 'recognition' and proof['material']['id'] == actual.material_recognition_id
    SourceEgressService(actual.records).validate_snapshot(SCOPE, proof['snapshot'])
    root = next(node for node in proof['snapshot']['nodes']
        if node['type'] == 'original_item' and node['id'] == actual.material_item_id)
    assert root['source_revision'] == actual.records.read('workspace_items', actual.material_item_id).revision
    return parent, entry, proof, original


@pytest.mark.parametrize('client,window', [('codex', None), ('codex', {'start': 2, 'end': 6}), ('claude', None)])
def test_actual_material_l3_drills_into_its_original(local_human_published_material, client, window):
    actual = local_human_published_material
    parent, entry, proof, original = delivered_l3(actual, client=client)
    before = parent_facts(actual, parent)
    selections = _read_selections(proof, window)
    assert any(row['type'] == 'original_item' and row['id'] == actual.material_item_id for row in selections)
    child, frozen = prepare_read(actual, parent, entry['id'], original, selections, suffix='material')
    delivered = actual.execute(child)
    actual.assert_completed(child, frozen, delivered)
    row = next(row for row in delivered['entries'] if row['object_id'] == actual.material_item_id)
    assert row['layer'] == 'L0' and row['excerpt'] == (SOURCE if window is None else SOURCE[2:6])
    child_proof, arguments = actual.context.delivered_proof(child, row['id'], client=client)
    assert arguments == {**original, 'tool': 'read'}
    child_ref, archive = actual.context._archive(child)
    parent_ref, _, parent_outcome = actual.context._completed(parent)
    assert archive['origin'] == {'turn_id': parent, 'id': entry['id'],
        'immutable_ref': parent_ref, 'outcome_ref': parent_outcome}
    assert archive['selections'] == selections
    assert actual.records.read(DELIVERIES, child).payload['immutable_ref'] == child_ref
    parent_nodes = {(node['type'], node['id']): node for node in proof['snapshot']['nodes']}
    for node in child_proof['snapshot']['nodes']:
        assert node == parent_nodes[node['type'], node['id']]
    assert parent_facts(actual, parent) == before
    assert len(actual.records.list(RESERVATIONS)) == len(actual.records.list(DELIVERIES)) == 2
    assert len(actual.native_material_calls) == 1


@pytest.mark.parametrize('change', ['revision', 'private'])
def test_actual_l3_child_rechecks_original_qualification(local_human_published_material, change):
    actual = local_human_published_material
    parent, entry, proof, original = delivered_l3(actual)
    before = parent_facts(actual, parent)
    selections = _read_selections(proof, None)
    prior = actual.records.read('workspace_items', actual.material_item_id)
    if change == 'revision':
        changed = actual.material_items.update(actual.material_item_id, 'alpha', {'confirmed'},
            source_text=SOURCE + '这是来源的新修订。')
        assert changed['revision'] == prior.revision + 1
    else:
        SourceEgressService(actual.records).set_policy(SCOPE, 'original_item', actual.material_item_id,
            prior.revision, 0, [])
    with pytest.raises(ExternalAgentGuardError, match='external_agent_binding_invalid'):
        actual.context.delivered_proof(parent, entry['id'], client='codex')
    with pytest.raises(ExternalAgentGuardError, match='external_agent_binding_invalid'):
        prepare_read(actual, parent, entry['id'], original, selections, suffix='material')
    assert_child_not_accepted(actual, 'turn-read-codex-material')
    assert parent_facts(actual, parent) == before and len(actual.records.list(DELIVERIES)) == 1
    assert len(actual.native_material_calls) == 1


@pytest.mark.parametrize('window', [{'start': False, 'end': 2}, {'start': 0, 'end': len(SOURCE) + 1}])
def test_actual_l3_original_window_uses_original_identity_and_bounds(local_human_published_material, window):
    actual = local_human_published_material
    parent, entry, proof, original = delivered_l3(actual)
    before = parent_facts(actual, parent)
    if type(window['start']) is bool:
        with pytest.raises(HTTPException) as caught:
            _read_selections(proof, window)
        assert caught.value.status_code == 400 and caught.value.detail == 'external_agent_window_invalid'
    else:
        selections = _read_selections(proof, window)
        with pytest.raises(ExternalContextError, match='external_context_selection_invalid'):
            prepare_read(actual, parent, entry['id'], original, selections, suffix='material')
    assert_child_not_accepted(actual, 'turn-read-codex-material')
    assert parent_facts(actual, parent) == before and len(actual.records.list(DELIVERIES)) == 1
    assert len(actual.native_material_calls) == 1
