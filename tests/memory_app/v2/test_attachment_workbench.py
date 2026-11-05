import asyncio
from functools import partial
from inspect import getclosurevars

import pytest
from fastapi.testclient import TestClient

from tests.memory_app.v2.test_library_unfinished import build_app
from backend.memory_app.v2.route import RoutePart, RoutePlan
from backend.memory_app.v2.multipart import execute_parts
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import WorkScope, RecognitionError


def upload(client, project='alpha', title='访谈.txt'):
    response = client.post('/api/v2/workbench/files', data={'project_id': project},
        files={'file': (title, '预算十万元，交付时间五月。'.encode(), 'text/plain')})
    assert response.status_code == 200, response.text
    return response.json()['id']


def test_fields_scope_and_fallback_use_real_http_without_models(tmp_path):
    app = build_app(tmp_path)
    records = app.state.recognition_records
    with TestClient(app) as client:
        ids = [upload(client, title=f'访谈{index}.txt') for index in range(3)]
        before = records.list('v2_turn_requests')
        for value in ([], [''], [True], 'one', ['a'] * 11):
            failure = client.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'item_ids': value})
            assert failure.status_code == 400 and failure.json()['detail'] == 'invalid_turn_fields'
        failure = client.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'item_ids': ids, 'item_id': ids[0]})
        assert failure.status_code == 400
        failure = client.post('/api/v2/workbench/turns', json={'project_id': 'beta', 'item_ids': ids})
        assert failure.status_code == 404
        assert records.list('v2_turn_requests') == before
        body = {'project_id': 'alpha', 'text': '对比三份的分歧', 'item_ids': ids}
        result = client.post('/api/v2/workbench/turns', json=body, headers={'Idempotency-Key': 'three-attachments'})
        assert result.status_code == 200, result.text
        parts = result.json()['turn']['receipt']['parts']
        assert len(parts) == 3 and all(part['intent'] == 'remember' for part in parts)
        assert [part['receipt']['remember']['item_id'] for part in parts] == ids
        assert result.json()['turn']['user_text'] == body['text']
        replay = client.post('/api/v2/workbench/turns', json=body, headers={'Idempotency-Key': 'three-attachments'})
        assert replay.status_code == 200 and replay.json() == result.json()
        assert len(records.list('v2_route_turn_keys')) == 0


def test_real_single_executors_block_failed_original_dependencies(tmp_path):
    app = build_app(tmp_path)
    records = app.state.recognition_records
    prepared = getclosurevars(app.state.workbench_turn_execution.execute).nonlocals['execute_turn_prepared']
    execute = getclosurevars(prepared).nonlocals['execute_routed_part']
    with TestClient(app) as client:
        ids = [upload(client, title=f'访谈{index}.txt') for index in range(3)]
        plan = RoutePlan(mode='rules', parts=[RoutePart(intent='remember', span=''),
            RoutePart(intent='do', span='对比三份的分歧', depends_on=[0])])
        result = client.portal.call(partial(execute_parts, {'item_ids': ids, 'text': '对比三份的分歧'}, plan,
            records=records, instance=app.state.workbench_instance, execute=execute, project='alpha', scene=None,
            request_key=None, complete=app.state.workbench_turn_execution.complete))
        parts = result['turn']['receipt']['parts']
        assert len(parts) == 4
        assert parts[3]['depends_on'] == [0, 1, 2]
        assert parts[3]['state'] == 'failed' and parts[3]['error'] == 'dependency_failed'
        assert records.list('v2_task_executions') == ()
        assert records.list('recognition_model_requests') == ()
        assert records.list('v2_route_turn_keys') == ()


def test_restart_projects_uncreated_children_as_interrupted(tmp_path):
    app = build_app(tmp_path)
    records = app.state.recognition_records
    with records.begin() as tx:
        tx.put('v2_turns', 'multi-old', {'id': 'multi-old', 'thread_id': 'unfinished-turn',
            'project_id': 'alpha', 'instance': 'old-instance', 'intent': 'multi', 'user_text': '对比',
            'created_at': '2026-10-25', 'part_turn_ids': ['uncreated'],
            'receipt': {'parts': [{'index': 0, 'intent': 'do', 'span': '对比', 'state': 'waiting',
                'receipt': {}, 'turn_id': None, 'project_id': None}]}}, expected_revision=0)
        tx.commit()
    with TestClient(app) as client:
        response = client.get('/api/v2/workbench/threads/unfinished-turn?project_id=alpha')
        parent = next(turn for turn in response.json()['turns'] if turn['id'] == 'multi-old')
        assert parent['receipt']['parts'][0]['state'] == 'interrupted'
    assert records.read('v2_turns', 'uncreated') is None


def test_ready_body_snapshot_keeps_coordinates_and_rechecks_privacy(tmp_path):
    app = build_app(tmp_path)
    records = app.state.recognition_records
    from backend.memory_app.v2.privacy import set_private_project
    set_private_project(records, 'alpha', False, 1)
    with records.begin() as tx:
        row = tx.read('workspace_items', 'unfinished-original')
        tx.put('workspace_items', row.object_id, {**row.payload, 'source_text': '重复。预算十万。重复。预算二十万。',
               'status': 'ready'}, expected_revision=row.revision)
        tx.commit()
    authority = SourceEgressService(records)
    scope = WorkScope('local-user', 'alpha')
    snapshot = authority.snapshot_original_content(scope, 'unfinished-original',
        windows=[{'start': 8, 'end': 11, 'text': '重复。'}])
    assert snapshot['content']['windows'][0]['start'] == 8
    authority.validate_original_content(scope, snapshot, 'generation')
    source = records.read('workspace_items', 'unfinished-original')
    authority.set_policy(scope=scope, source_type='original_item', source_id=source.object_id,
        expected_source_revision=source.revision, expected_policy_revision=0, allowed_purposes=[])
    with pytest.raises(RecognitionError):
        authority.validate_original_content(scope, snapshot, 'generation')
