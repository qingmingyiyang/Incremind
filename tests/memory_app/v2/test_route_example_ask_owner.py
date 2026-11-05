from copy import deepcopy
from functools import partial
from inspect import getclosurevars, unwrap
import os
from pathlib import Path
from shutil import copyfile
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.memory_app.kernel.route_source_snapshot import require_closed_answer
from backend.memory_app.kernel.turn_requests import freeze_turn_request
from backend.memory_app.source_egress import SourceEgressService, _frozen_packet_authority
from backend.memory_app.route_correction_originals import validate_original_binding
from backend.memory_app.transaction_records import TransactionRecords
from backend.memory_app.v2.insights import source_experiences
from backend.memory_app.v2.inspirations import validate_task_inputs
from backend.memory_app.v2.turn_requests import freeze_product_turn, validate_frozen_inputs
from backend.memory_app.v2.external_agent_guard import ExternalAgentGuard, ExternalAgentGuardError
from backend.memory_app.v2.external_agent_settings import external_agent_settings, replace_external_agent_settings
from backend.memory_app.v2.multipart import execute_parts, part_finished
from backend.memory_app.v2.route import RoutePart, RoutePlan
from backend.recognition import RecognitionConflict
from backend.recognition import WorkScope, RecognitionError
from backend.recognition.sql_source_identities import CollectingReader


@pytest.fixture(scope='module')
def empty_project(tmp_path_factory):
    root = tmp_path_factory.mktemp('route-example-owner').resolve(strict=True)
    allocated = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    assert root.is_relative_to(allocated)
    prior_root = os.environ['CHRIPTMAS_APP_ROOT']
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    config = root / 'config/settings.toml'
    config.parent.mkdir()
    copyfile(Path(__file__).parents[3] / 'config/settings.toml.example', config)
    # 应用全局工厂仅在专属目录完成解析后导入。
    from backend.memory_app.app import create_app
    app = create_app(runtime_root=root)
    models = app.state.recognition_models
    models.update('generation', {'expected_revision': models.public()['generation']['revision'],
        'base_url': 'https://127.0.0.2:9/v1', 'model': 'gpt-4o-mini',
        'api_key': 'isolated-no-wire-placeholder', 'enabled': True, 'allow_remote': False})
    try:
        with TestClient(app) as client:
            response = client.post('/api/v2/projects', json={'name': '纠正样例空项目'})
            assert response.status_code == 200
            yield app, client, response.json()['id']
    finally:
        os.environ['CHRIPTMAS_APP_ROOT'] = prior_root


def wait_for_example(records, identity):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        example = records.read('v2_route_examples', identity)
        if example is not None:
            return example
        time.sleep(.05)
    raise AssertionError('completed_answer_example_was_not_saved')


def assert_no_wire(app, row):
    assert row.payload['receipt']['ask']['no_match'] is True
    assert row.payload['receipt']['ask']['egress_receipt_id'] is None
    assert require_closed_answer(app.state.recognition_records, row) == 'completed'
    store = app.state.ai_turn_store
    assert not [event for event in store.events_after(row.object_id)
        if event['type'].startswith('model.attempt.')]


def test_completed_ask_stages_with_actual_owner_and_rejects_same_transaction_changes(empty_project):
    app, client, project = empty_project
    response = client.post('/api/v2/workbench/turns', json={
        'project_id': project, 'text': '这个空项目有哪些已确认资料？', 'intent': 'ask'})
    assert response.status_code == 200, response.json()
    records = app.state.recognition_records
    row = records.read('v2_turns', response.json()['turn']['id'])
    assert_no_wire(app, row)
    example = wait_for_example(records, row.object_id)
    parts = deepcopy(example.payload['parts'])
    with records.begin() as tx:
        app.state.workbench_route_examples.stage(tx, row.object_id, parts=parts)
        tx.commit()
    assert records.read('v2_route_examples', row.object_id) == example
    with records.begin() as tx:
        tx.put('v2_turns', row.object_id, {**row.payload, 'user_text': '事务内另一问题'},
            expected_revision=row.revision)
        with pytest.raises(RecognitionConflict):
            app.state.workbench_route_examples.stage(tx, row.object_id, parts=parts)
        tx.rollback()
    assert records.read('v2_turns', row.object_id) == row
    assert records.read('v2_route_examples', row.object_id) == example


def test_correction_with_completed_ask_commits_terminal_and_corrected_example(empty_project):
    app, client, project = empty_project
    records = app.state.recognition_records
    service = app.state.workbench_turn_execution
    prepared = getclosurevars(unwrap(service.execute)).nonlocals['execute_turn_prepared']
    execute = getclosurevars(prepared).nonlocals['execute_routed_part']
    questions = ['这个空项目有哪些资料？', '这个空项目有哪些认识？']
    result = client.portal.call(partial(execute_parts,
        {'project_id': project, 'text': '\n'.join(questions)},
        RoutePlan(mode='manual', parts=[RoutePart(intent='ask', span=text) for text in questions]),
        records=records, instance=app.state.workbench_instance, execute=execute,
        project=project, scene=None, request_key=None, complete=service.complete))
    original = records.read('v2_turns', result['turn']['id'])
    children = [records.read('v2_turns', identity) for identity in original.payload['part_turn_ids']]
    for child in children:
        assert_no_wire(app, child)
    source = client.get('/api/v2/workbench/turns/' + original.object_id + '/route-correction',
        params={'project_id': project})
    assert source.status_code == 200, source.json()
    operation_id = 'ask-owner-' + uuid4().hex
    accepted = client.post('/api/v2/workbench/turns/' + original.object_id + '/route-correction', json={
        'project_id': project, 'operation_id': operation_id, 'expected_revision': 0,
        'source_revision': source.json()['source_revision'],
        'parts': [{'sources': [0], 'intent': 'ask', 'depends_on': [], 'target_source': 0}]})
    assert accepted.status_code == 200, accepted.json()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get('/api/v2/workbench/route-corrections/operations/' + operation_id)
        assert response.status_code == 200
        terminal = response.json()
        if terminal['status'] in {'completed', 'partial', 'unknown', 'cancelled'}:
            break
        time.sleep(.05)
    assert terminal['status'] == 'completed', terminal
    replacement = records.read('v2_turns', terminal['replacement_turn_id'])
    assert replacement.payload['part_turn_ids'] == [children[0].object_id]
    example = records.read('v2_route_examples', replacement.object_id)
    assert example.payload['corrected'] is True and example.payload['deleted'] is False
    assert example.payload['parts'] == [{'intent': 'ask', 'span': questions[0], 'depends_on': [], 'instruction': None}]
    assert records.read('v2_route_corrections', original.object_id).payload['status'] == 'completed'
    assert records.read('v2_turns', original.object_id) == original
    assert [records.read('v2_turns', child.object_id) for child in children] == children


def test_native_approval_remains_unfinished_until_real_user_action(empty_project):
    app, _, project = empty_project
    records, runtime, store = app.state.recognition_records, app.state.ai_runtime, app.state.ai_turn_store
    public_id, native_id = 'approval-public-' + uuid4().hex, 'approval-native-' + uuid4().hex
    created = '2026-11-04T00:00:00Z'
    request = freeze_turn_request('project.answer', turn_id=native_id, session_id='session-' + public_id,
        operation_id='approval-operation-' + uuid4().hex, idempotency_key='approval-' + uuid4().hex,
        project_id=project, created_at=created, text='核对公开资料',
        privacy=app.state.workspace_domains.query.freeze_answer_privacy(project, local_only=True),
        capabilities=['workbench.answer.execute'], template_version=2)
    request['capability_policy']['require_approval'] = ['workbench.answer.execute']
    request['capability_request'] = {'mode': 'execute_exact_v1',
        'capability_id': 'workbench.answer.execute', 'arguments': {'query': '核对公开资料'}}
    waiting = runtime.submit_turn(request)
    assert waiting.status == 'waiting_approval'
    try:
        assert part_finished({'intent': 'do', 'receipt': {'do': {'state': waiting.status}}}) is False
        assert records.read('v2_turns', public_id) is None
        assert records.read('v2_route_examples', public_id) is None
        assert not [event for event in store.events_after(native_id) if event['type'].startswith('model.')]
    finally:
        cancelled = runtime.apply_action({'schema_version': '1.0.0', 'action_id': 'cancel-' + uuid4().hex,
            'turn_id': native_id, 'type': 'cancel', 'target_event_id': None, 'actor': 'user',
            'reason': '结束隔离审批验收', 'expected_sequence': len(tuple(store.events_after(native_id))),
            'idempotency_key': 'cancel-' + uuid4().hex, 'created_at': created})
        assert cancelled.status == 'cancelled'
        assert part_finished({'intent': 'do', 'receipt': {'do': {'state': cancelled.status}}}) is True


def test_real_corrected_original_can_be_read_in_external_and_collecting_transactions(empty_project):
    app, client, project = empty_project
    records = app.state.recognition_records
    response = client.post('/api/v2/workbench/turns', json={
        'project_id': project, 'intent': 'inspiration', 'text': '合成想法：保留每一笔实际单位。'})
    assert response.status_code == 200, response.json()
    original = records.read('v2_turns', response.json()['turn']['id'])
    own_project = original.payload['project_id']
    source = client.get('/api/v2/workbench/turns/' + original.object_id + '/route-correction',
        params={'project_id': own_project})
    assert source.status_code == 200, source.json()
    operation_id = 'borrowed-source-' + uuid4().hex
    accepted = client.post('/api/v2/workbench/turns/' + original.object_id + '/route-correction', json={
        'project_id': own_project, 'operation_id': operation_id, 'expected_revision': 0,
        'source_revision': source.json()['source_revision'],
        'parts': [{'sources': [0], 'intent': 'remember', 'depends_on': [], 'target_source': 0}]})
    assert accepted.status_code == 200, accepted.json()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        operation = client.get('/api/v2/workbench/route-corrections/operations/' + operation_id).json()
        if operation['status'] in {'completed', 'partial', 'unknown'}:
            break
        time.sleep(.05)
    assert operation['status'] == 'partial', operation
    parent = records.read('v2_turns', operation['replacement_turn_id'])
    child = records.read('v2_turns', parent.payload['part_turn_ids'][0])
    item = records.read('workspace_items', child.payload['receipt']['remember']['item_id'])
    scope = WorkScope('local-user', own_project)
    birth = validate_original_binding(records, scope, item)
    settings = external_agent_settings(records)
    replace_external_agent_settings(records, {'allow_remote': True, 'include_profile': False,
        'daily_limit': 200, 'clients': {'claude': True, 'codex': True}}, expected_revision=settings['revision'])
    guard_id = 'guard-source-' + uuid4().hex
    request = {'client': 'claude', 'tool': 'read', 'query': '读取合成原件',
        'scope': {'user_id': 'local-user', 'project_id': own_project}, 'budget': 1000}
    material = {'type': 'original_item', 'id': item.object_id, 'revision': item.revision, 'project_id': own_project}
    candidate = records.read('recognition_candidates', original.payload['receipt']['inspiration']['insight']['id'])
    experiences = candidate.payload['source_experience_ids']
    expected_sources = source_experiences(records, scope, experiences)
    auxiliary = freeze_product_turn('memory.organize', records=records,
        models=app.state.recognition_models, project_id=own_project, materials=[material],
        load_text=lambda resolved: resolved['payload']['source_text'], turn_id='auxiliary-' + uuid4().hex,
        session_id='session-' + uuid4().hex, operation_id='operation-' + uuid4().hex,
        idempotency_key='key-' + uuid4().hex, created_at=item.payload['created_at'])
    guard = ExternalAgentGuard(records, owner_id='local-user')
    frozen = guard.freeze(guard_id, request, [material])
    assert guard.validate(guard_id, request)[0]['id'] == item.object_id
    snapshot = frozen['source_snapshots'][0]
    roots = [(root['type'], root['id'], root['revision']) for root in snapshot['roots']]
    assert _frozen_packet_authority(scope, {'source_egress': snapshot}, roots)['nodes'] == snapshot['nodes']
    for fault in ('unknown_field', 'wrong_type', 'invalid_uuid', 'boolean_revision', 'missing_field', 'invalid_birth'):
        invalid = deepcopy(snapshot)
        node = next(row for row in invalid['nodes'] if row['type'] == 'original_item' and row['id'] == item.object_id)
        metadata = node['dependency_revisions']
        if fault == 'unknown_field':
            metadata['unknown'] = 1
        elif fault == 'wrong_type':
            node['type'] = 'experience'
        elif fault == 'invalid_uuid':
            node['id'] = 'workspace-route-invalid'
        elif fault == 'boolean_revision':
            metadata['route_original_revision'] = True
        elif fault == 'missing_field':
            del metadata['route_correction_child_id']
        else:
            metadata['route_original_birth_revision'] = 2
        with pytest.raises(RecognitionConflict):
            _frozen_packet_authority(scope, {'source_egress': invalid}, roots)
    with records.begin() as tx:
        borrowed = TransactionRecords(tx)
        assert source_experiences(borrowed, scope, experiences, source_owner=records) == expected_sources
        with pytest.raises(RecognitionConflict, match='actual_owner_unavailable'):
            source_experiences(borrowed, scope, experiences)
        validate_frozen_inputs(borrowed, app.state.recognition_models, auxiliary, source_owner=records)
        validate_task_inputs(borrowed, app.state.recognition_models, auxiliary, source_owner=records)
        with pytest.raises(RecognitionConflict, match='actual owner unavailable'):
            validate_frozen_inputs(borrowed, app.state.recognition_models, auxiliary)
        with pytest.raises(RecognitionConflict, match='actual owner unavailable'):
            validate_task_inputs(borrowed, app.state.recognition_models, auxiliary)
        collector = CollectingReader(tx)
        SourceEgressService(records, reader=collector).validate_snapshot(scope, frozen['source_snapshots'][0])
        assert collector.selected_keys()
        inherited = birth['snapshots'][0]['roots'][0]
        authority = SourceEgressService(records, reader=tx)
        revision = authority.policy(scope, inherited['type'], inherited['id'])['policy_revision']
        authority.set_policy(scope, inherited['type'], inherited['id'], inherited['revision'], revision, [])
        with pytest.raises((RecognitionError, ExternalAgentGuardError)):
            ExternalAgentGuard(records, owner_id='local-user', reader=tx).validate(guard_id, request)
        with pytest.raises(RecognitionConflict):
            validate_frozen_inputs(borrowed, app.state.recognition_models, auxiliary, source_owner=records)
        tx.rollback()
    assert records.read('workspace_items', item.object_id) == item
    assert guard.validate(guard_id, request)[0]['id'] == item.object_id
