"""Real HTTP admission retains its frozen placement selection across background work."""
import asyncio
import json

from fastapi.testclient import TestClient
import pytest

from backend.memory_app.v2.policies import ACTIVE, override
from backend.memory_app.v2.projects import scene_of
from tests.memory_app.v2.test_auto_confirm import runtime
from tests.memory_app.v2.test_placement import projects
from tests.memory_app.v2.test_insight_generation import response_for


@pytest.fixture
def workbench(runtime):
    from backend.memory_app.v2 import install_v2_routes
    from backend.memory_app.v2.devices import DeviceRegistry
    app = runtime.http.app
    app.state.device_registry = DeviceRegistry(runtime.records.database_path.parent / 'server')
    original = runtime.model.complete
    def completion(messages, *, max_tokens, validate_current=None):
        try:
            payload = json.loads(messages[-1]['content'])
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get('neighbors'), list):
            if validate_current:
                validate_current()
            runtime.model.calls += 1
            return response_for(messages, '{"insights": []}'), {}
        return original(messages, max_tokens=max_tokens, validate_current=validate_current)
    runtime.model.complete = completion
    runtime.model.public = lambda: {'generation': {'base_url': 'https://example.invalid/v1', 'allow_remote': True}}
    service = projects(runtime)
    install_v2_routes(app, runtime_root=runtime.records.database_path.parent, records=runtime.records,
        models=runtime.model, documents=runtime.documents, service=service, workspace=runtime.domains)
    with TestClient(app) as http:
        yield runtime, app, http


def finish(app, http, result):
    async def drain():
        await asyncio.gather(*tuple(app.state.workbench_tasks))
    http.portal.call(drain)
    response = http.get(f"/api/v2/workbench/threads/{result['thread_id']}?project_id=alpha")
    assert response.status_code == 200, response.text
    return next(turn for turn in response.json()['turns'] if turn['id'] == result['turn']['id'])


def test_http_admission_projects_hint_and_scene_from_frozen_place_two(workbench):
    runtime, app, http = workbench
    with override(place='@2'):
        response = http.post('/api/v2/workbench/turns', json={
            'project_id': 'alpha', 'text': '原文证据', 'intent': 'remember'})
        assert response.status_code == 200, response.text
        turn = finish(app, http, response.json())
    memory = turn['receipt']['remember']
    assert memory['state'] == 'done'
    assert memory['document_revision'] == 1
    assert memory['scene'] == '阅读' and memory['assignment_revision'] == 1
    assert memory['placement']['project_id'] == 'alpha' and memory['placement']['scene'] == '阅读'
    assert scene_of(runtime.records, 'document', memory['document_id'])['scene'] == '阅读'
    frozen = runtime.records.read('v2_place_inputs', turn['id'])
    assert frozen.payload == {'project_id': 'alpha', 'tagged': False, 'policy_version': '@2'}
    assert all(row.payload['request']['policy_versions']['place'] == '@2'
        for row in runtime.records.list('v2_memory_turn_keys') if row.payload['identity']['project'] == 'alpha')


def test_tagged_http_admission_has_no_hint_but_captures_skip(workbench):
    runtime, app, http = workbench
    with override(place='@2'):
        response = http.post('/api/v2/workbench/turns', json={
            'project_id': 'alpha', 'text': '#alpha 原文证据', 'intent': 'remember'})
        assert response.status_code == 200, response.text
        turn = finish(app, http, response.json())
    memory = turn['receipt']['remember']
    assert memory['state'] == 'done' and 'placement' not in memory
    assert runtime.records.list('v2_place_hints') == ()
    assert runtime.records.read('v2_place_inputs', turn['id']).payload['tagged'] is True


def test_historical_http_place_one_retains_receipt_and_request_shape(workbench):
    runtime, app, http = workbench
    with override(place='@1'):
        response = http.post('/api/v2/workbench/turns', json={
            'project_id': 'alpha', 'text': '原文证据', 'intent': 'remember'})
        assert response.status_code == 200, response.text
        turn = finish(app, http, response.json())
    assert turn['receipt']['remember']['state'] == 'done'
    assert 'placement' not in turn['receipt']['remember']
    assert 'document_revision' not in turn['receipt']['remember']
    assert runtime.records.list('v2_place_inputs') == runtime.records.list('v2_place_hints') == ()


def test_unknown_project_tag_keeps_404_and_can_be_created_then_resent(workbench):
    runtime, app, http = workbench
    body = {'project_id': 'alpha', 'text': '#新主题 原文证据', 'intent': 'remember'}
    response = http.post('/api/v2/workbench/turns', json=body)
    assert response.status_code == 404 and response.json()['detail'] == 'project_tag_not_found'
    created = http.post('/api/v2/projects', json={'name': '新主题'})
    assert created.status_code == 200
    response = http.post('/api/v2/workbench/turns', json=body)
    assert response.status_code == 200 and response.json()['turn']['intent'] == 'remember'
    async def drain():
        await asyncio.gather(*tuple(app.state.workbench_tasks))
    http.portal.call(drain)


def test_background_completion_and_cached_replay_keep_frozen_place_after_active_switch(workbench, monkeypatch):
    import importlib
    module = importlib.import_module('backend.memory_app.v2.workbench')
    runtime, app, http = workbench
    original = module.process_and_confirm
    monkeypatch.setitem(ACTIVE, 'place', '@2')
    async def switched(*args, **kwargs):
        monkeypatch.setitem(ACTIVE, 'place', '@1')
        return await original(*args, **kwargs)
    monkeypatch.setattr(module, 'process_and_confirm', switched)
    body = {'project_id': 'alpha', 'text': '原文证据', 'intent': 'remember'}
    headers = {'idempotency-key': 'placement-active-switch'}
    response = http.post('/api/v2/workbench/turns', json=body, headers=headers)
    assert response.status_code == 200, response.text
    turn = finish(app, http, response.json())
    assert ACTIVE['place'] == '@1'
    assert turn['receipt']['remember']['placement']['scene'] == '阅读'
    frozen = runtime.records.read('v2_place_inputs', turn['id'])
    assert frozen.payload['policy_version'] == '@2'
    before, calls = runtime.records.list_all(), runtime.model.calls
    replay = http.post('/api/v2/workbench/turns', json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == response.json()
    assert finish(app, http, response.json()) == turn
    from core.storage_provider.observability import STAGES
    after = {(row.collection, row.object_id): row for row in runtime.records.list_all()}
    assert all(after[(row.collection, row.object_id)] == row for row in before)
    keys = {(row.collection, row.object_id) for row in before}
    added = [row for key, row in after.items() if key not in keys]
    assert len(added) == 1
    timing, = added
    assert timing.collection == 'v2_turn_timings' and timing.revision == 1
    assert timing.payload['turn_id'] == timing.object_id and timing.object_id.startswith('timing-')
    assert set(timing.payload) == {'turn_id', 'operation', 'total_ms', 'stages_ms',
        'stage_observations', 'connection_count', 'statement_count'}
    assert timing.payload['operation'] == 'remember'
    assert set(timing.payload['stages_ms']) == set(timing.payload['stage_observations']) == set(STAGES)
    assert timing.payload['total_ms'] >= 0 and all(value >= 0 for value in timing.payload['stages_ms'].values())
    assert all(type(value) is int and value >= 0 for value in [timing.payload['connection_count'],
        timing.payload['statement_count'], *timing.payload['stage_observations'].values()])
    assert runtime.model.calls == calls
