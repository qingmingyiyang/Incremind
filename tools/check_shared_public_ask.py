import argparse
import asyncio
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import httpx
from httpx_sse import EventSource


def check(base):
    workspace, base = Path(__file__).resolve().parents[1], Path(base).resolve()
    if base == workspace or any(part.name == 'runtime' for part in (base, *base.parents)):
        raise ValueError('shared_check_requires_isolated_root')
    root = base / ('p' + uuid4().hex[:6])
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(workspace / 'config/settings.toml.example', root / 'config/settings.toml')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.material_origin_readers import _readonly_connection
    from backend.memory_app.v2.material_origins import ORIGIN_KIND
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from core.storage_provider.connection_scope import connection_scope

    devices = DeviceRegistry(root / 'server')
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    admin = devices.authenticate(devices.exchange(pairing['code'], name='管理设备')['key'])
    users = ServerUsers(root, records=devices.records)
    user = users.create(admin, name='共享问题操作人')
    pairing = devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)
    token = devices.exchange(pairing['code'], name='实际操作设备')['key']
    actor = devices.authenticate(token)

    def child(path, user, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
        child_factory=child, port=8013)
    registry = server.state.shared_projects
    project = registry.create(actor, project_name='实际公开共享问题', operation_id='create-public-ask')['project']

    async def run():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server),
                    base_url='http://127.0.0.1:8013', headers={'Authorization': 'Bearer ' + token}) as client:
                response = await client.get('/api/v2/projects')
                assert response.status_code == 200
                app = server.state.server_context.pool._children[actor.user_id].application
                records, runtime_root = app.state.recognition_records, app.state.recognition_runtime_root
                native_paths = (runtime_root / '.rebuild-data/ai-turns.sqlite3', runtime_root / 'ai-turns.sqlite3')
                native_presence = tuple(path.exists() for path in native_paths)
                with records.begin() as tx:
                    tx.put('v2_threads', 'legacy-personal-thread', {'project_id': 'inbox', 'title': '个人旧正文',
                        'created_at': '2025-01-01T00:00:00Z', 'updated_at': '2025-01-01T00:00:00Z'}, expected_revision=0)
                    tx.put('v2_turns', 'legacy-personal-answer', {'project_id': 'inbox',
                        'thread_id': 'legacy-personal-thread', 'intent': 'ask', 'user_text': '原先的问题',
                        'created_at': '2025-01-01T00:00:00Z', 'receipt': {'ask': {'answer': '原先保存的正文',
                            'citations': [], 'trace': [], 'no_match': False}}}, expected_revision=0)
                    tx.commit()
                legacy = await client.get('/api/v2/workbench/threads/legacy-personal-thread', params={'project_id': 'inbox'})
                assert legacy.status_code == 200 and legacy.json()['turns'][0]['scope_kind'] == 'personal', legacy.text
                assert legacy.json()['turns'][0]['receipt']['ask']['answer'] == '原先保存的正文'
                assert tuple(path.exists() for path in native_paths) == native_presence
                print('shared-public actual normal factory legacy personal GET preserved; native database presence unchanged', flush=True)
                models = app.state.recognition_models
                models.update('generation', {'expected_revision': models.public()['generation']['revision'],
                    'base_url': 'https://127.0.0.2:9/v1', 'model': 'gpt-4o-mini',
                    'api_key': 'isolated-no-wire-placeholder', 'enabled': True, 'allow_remote': False})
                body = {'project_id': project['id'], 'scope_kind': 'shared', 'intent': 'ask', 'text': '共享空项目中保存过哪些资料'}
                command = {'Idempotency-Key': 'public-shared-' + uuid4().hex}
                response = await client.post('/api/v2/workbench/turns', json=body,
                    headers={**command, 'Accept': 'text/event-stream'})
                messages = [event async for event in EventSource(response).aiter_sse()]
                announced = next(event.json() for event in messages if event.event == 'started')
                assert announced['turn']['scope_kind'] == 'shared'
                value = next(event.json() for event in messages if event.event == 'done')
                store, records = app.state.ai_turn_store, app.state.recognition_records
                connection = _readonly_connection(store._path)
                try:
                    identities = [row[0] for row in connection.execute('SELECT turn_id FROM ai_turns')]
                finally:
                    connection.close()
                requests = [store.get_request(identity) for identity in identities]
                origins = [item for request in requests for item in store.restore_admission(request).items
                    if item.kind == ORIGIN_KIND]
                events = [event for identity in identities for event in store.events_after(identity)]
                print('shared-public-ask status=' + str(response.status_code) + ' native=' + str(len(requests))
                    + ' public=' + str(len(records.list('v2_turns'))) + ' origins=' + str(len(origins))
                    + ' model_events=' + str(sum(event['type'].startswith('model.') for event in events)), flush=True)
                assert response.status_code == 200, response.text
                assert len(requests) == len(origins) == 1
                assert origins[0].payload['actor_user_id'] == origins[0].payload['execution']['owner_user_id'] == actor.user_id
                assert value['turn']['receipt']['ask']['no_match'] is True
                assert value['turn']['scope_kind'] == 'shared'
                assert not any(event['type'].startswith('model.') for event in events)
                data = registry.roots._data(project['id'])
                assert data.records.list('v2_turns') == ()
                examples = records.list('v2_route_examples')
                assert len(examples) == 1 and examples[0].payload['origin']['kind'] == 'shared'
                assert examples[0].payload['origin']['namespace'] == 'shared/' + project['id'] + '/' + data.namespace
                native_before = tuple(store.events_after(identities[0]))
                replay = await client.post('/api/v2/workbench/turns', json=body, headers=command)
                assert replay.status_code == 200 and replay.json() == value
                assert tuple(store.events_after(identities[0])) == native_before
                assert len(records.list('v2_turns')) == 2 and len(records.list('v2_route_examples')) == 1
                stream = await client.get('/api/v2/workbench/turns/' + value['turn']['id'] + '/stream',
                    params={'project_id': project['id'], 'after': '0'})
                assert stream.status_code == 200, stream.text
                replayed = [event async for event in EventSource(stream).aiter_sse()]
                assert all(event.json()['turn']['scope_kind'] == 'shared' for event in replayed if event.event == 'started')
                assert next(event.json() for event in replayed if event.event == 'done')['turn']['scope_kind'] == 'shared'
                assert tuple(store.events_after(identities[0])) == native_before
                continued = await client.post('/api/v2/workbench/turns/' + value['turn']['id'] + '/continue',
                    json={'project_id': project['id']}, headers={'Idempotency-Key': 'finished-continue-' + uuid4().hex})
                assert continued.status_code == 409 and continued.json()['detail'] == 'turn_interrupted', continued.text
                pairing = devices.issue_pairing(user_id=actor.user_id, actor=admin.device_id)
                new_token = devices.exchange(pairing['code'], name='继续请求的新设备')['key']
                changed_device = await client.post('/api/v2/workbench/turns/' + value['turn']['id'] + '/continue',
                    json={'project_id': project['id']}, headers={'Idempotency-Key': 'new-device-continue-' + uuid4().hex,
                        'Authorization': 'Bearer ' + new_token})
                assert changed_device.status_code == 409 and changed_device.json()['detail'] == 'source_changed_retry', changed_device.text
                assert tuple(store.events_after(identities[0])) == native_before
                print('shared-public continue original finished turn409; actual new device rejected by frozen source lease; native events unchanged', flush=True)
                response = await client.get('/api/v2/workbench/threads/' + value['thread_id'],
                    params={'project_id': project['id'], 'scope_kind': 'shared'})
                assert response.status_code == 200
                with records.begin() as tx:
                    tx.put('v2_projects', project['id'], {'name': '同编号个人项目', 'scenes': [],
                        'private': False, 'builtin': None}, expected_revision=0)
                    tx.commit()
                personal = await client.post('/api/v2/workbench/turns', json={'project_id': project['id'],
                    'thread_id': value['thread_id'], 'intent': 'ask', 'text': '个人项目中保存过哪些资料'},
                    headers={'Idempotency-Key': 'personal-same-id-' + uuid4().hex})
                assert personal.status_code == 200 and personal.json()['turn']['scope_kind'] == 'personal', personal.text
                personal_id = personal.json()['turn']['id']
                for kind, expected in (('shared', value['turn']['id']), ('personal', personal_id)):
                    response = await client.get('/api/v2/workbench/threads/' + value['thread_id'],
                        params={'project_id': project['id'], 'scope_kind': kind})
                    assert response.status_code == 200
                    turns = response.json()['turns']
                    assert len(turns) == 1 and turns[0]['id'] == expected and turns[0]['scope_kind'] == kind
                response = await client.get('/api/v2/server/shared-projects/scopes')
                assert response.status_code == 200 and response.json()['items'][0]['has_personal_history']
                from copy import deepcopy
                import pytest
                from backend.recognition import RecognitionConflict
                from backend.memory_app.v2.agentic_rag import pending_clarification
                from backend.memory_app.v2.followup import read_history
                from backend.security.user_context import authorize_user, user_context
                row = records.read('v2_turns', personal_id)
                changed = deepcopy(row.payload)
                changed['receipt']['ask']['trace'] = [{'agentic': {'version': 1, 'status': 'clarify',
                    'invocation': 'public-clarify-integrity', 'questions': ['请说明实际范围']}}]
                with records.begin() as tx:
                    tx.put('v2_turns', personal_id, changed, expected_revision=row.revision)
                    tx.commit()
                with user_context(authorize_user(registry.users, actor, actor.user_id)):
                    query = server.state.shared_query_factory.incoming_query(app, project['id'], scope_kind='shared')
                    from backend.memory_app.v2.shared_usage import record_shared_answer_usage
                    prior = records.read('v2_turns', value['turn']['id'])
                    record_shared_answer_usage(query, [{'layer': 'L3', 'project_id': project['id'],
                        'entry': {'id': 'unproven-statistics-input'}}], [], turn_id=value['turn']['id'])
                    assert records.read('v2_turns', value['turn']['id']) == prior
                    assert data.records.list('v2_usage_insight') == ()
                    assert tuple(store.events_after(identities[0])) == native_before
                    print('shared-public completed actual native fact rejected unproven statistics without changing saved answer/source usage/native events', flush=True)
                    assert pending_clarification(query, records, project['id'], value['thread_id']) is None
                    empty = read_history(records, project['id'], value['thread_id'], '继续',
                        excluded_turn=value['turn']['id'], query=query)
                    assert empty['turns'] == [] and 'clarification' not in empty
                    with pytest.raises(RecognitionConflict, match='rag_clarification_changed'):
                        pending_clarification(app.state.workspace_domains.query, records,
                            project['id'], value['thread_id'])
                before = len(records.list('v2_turns'))
                for intent in ('remember', 'inspiration'):
                    rejected = await client.post('/api/v2/workbench/turns', json={**body, 'intent': intent},
                        headers={'Idempotency-Key': 'unsupported-shared-' + uuid4().hex})
                    assert rejected.status_code == 409 and rejected.json()['detail'] == 'shared_intent_not_available'
                assert len(records.list('v2_turns')) == before
                automatic = await client.post('/api/v2/workbench/turns', json={**body, 'intent': 'auto',
                    'text': '共享项目有哪些资料？'}, headers={'Idempotency-Key': 'shared-auto-' + uuid4().hex})
                assert automatic.status_code == 200, automatic.text
                assert automatic.json()['turn']['intent'] == 'ask' and automatic.json()['turn']['scope_kind'] == 'shared'
                auto_request = store.get_request(automatic.json()['turn']['id'])
                assert any(item.kind == ORIGIN_KIND for item in store.restore_admission(auto_request).items)
                from backend.memory_app.v2.shared_route import SharedRouteSource, shared_routes
                from backend.memory_app.v2.route import RouteService
                from backend.memory_app.v2.privacy import set_private_project
                from datetime import datetime, timezone
                import json
                with user_context(authorize_user(registry.users, actor, actor.user_id)):
                    query = server.state.shared_query_factory.incoming_query(app, project['id'], scope_kind='shared')
                    source = SharedRouteSource(query)
                    personal_routes = RouteService(records, models, examples=app.state.workbench_route_examples)
                    source_routes = shared_routes(personal_routes, query)
                    assert source_routes._identity(project['id'], 'same-command') != personal_routes._identity(project['id'], 'same-command')
                    models.update('generation', {'expected_revision': models.public()['generation']['revision'],
                        'allow_remote': True})
                    eligible = source_routes.examples.similar(project['id'], '共享项目资料')
                    assert any(item['origin']['kind'] == 'shared'
                        and item['origin']['namespace'] == query.source_namespace for item in eligible)
                    assert all(item['id'] != personal_id and item['origin']['kind'] == 'shared'
                        and item['origin']['namespace'] == query.source_namespace for item in eligible)
                    route_id = 'route-preparation-' + uuid4().hex
                    request, bundle = source.freeze(json.dumps({'original_text': '共享范围自动路由准备',
                        'project_id': project['id'], 'has_files': False, 'examples': [],
                        'source_origin': source_routes.examples.current(project['id'])}, ensure_ascii=False),
                        turn_id=route_id, session_id='aux-' + route_id, operation_id='op-' + route_id,
                        idempotency_key=route_id, created_at=datetime.now(timezone.utc).isoformat(), capabilities=[])
                    source.validate(request, bundle)
                    assert request['privacy']['allow_remote'] is True
                    assert bundle['items'][0]['payload']['execution']['owner_user_id'] == actor.user_id
                    assert data.records.list('v2_route_turn_keys') == ()
                    set_private_project(data.records, project['id'], True, 0)
                    with pytest.raises(RecognitionConflict):
                        source_routes.examples.validate_frozen(eligible)
                    with pytest.raises(RecognitionConflict, match='material_origin_egress_revoked'):
                        source.validate(request, bundle)
                    rejected = source_routes.route_model('共享私密项目的自动路由', project_id=project['id'],
                        request_key='private-route-' + uuid4().hex)
                    assert rejected.reason == 'remote_disabled' and rejected.turn_id is None
                    assert records.list('v2_route_turn_keys') == ()
                    from backend.memory_app.v2.route import is_fast_route
                    from backend.memory_app.v2.turn_requests import freeze_product_turn
                    from backend.memory_app.model_binding import public_model_configuration
                    from backend.memory_app.turn_routing import _revision
                    quick_text = '共享项目有哪些资料？'
                    assert is_fast_route(quick_text)
                    personal_route_id, quick_key = 'route-personal-' + uuid4().hex, 'shared-fast-' + uuid4().hex
                    personal_input = {'original_text': quick_text, 'project_id': project['id'],
                        'examples': [], 'has_files': False}
                    prepared_personal = freeze_product_turn('workbench.route', records=records, models=models,
                        project_id=project['id'], load_text=lambda item: '',
                        text=json.dumps(personal_input, ensure_ascii=False, sort_keys=True),
                        turn_id=personal_route_id, session_id='aux-' + personal_route_id,
                        operation_id='op-' + personal_route_id, idempotency_key=personal_route_id,
                        created_at=datetime.now(timezone.utc).isoformat(), capabilities=[])
                    identity = personal_routes._identity(project['id'], quick_key)
                    with records.begin() as tx:
                        tx.put('v2_route_turn_keys', _revision(identity), {'identity': identity,
                            'inputs': personal_input, 'request': prepared_personal,
                            'configuration': public_model_configuration(models.public()['generation'])}, expected_revision=0)
                        tx.commit()
                    quick = source_routes.route(quick_text, project_id=project['id'], request_key=quick_key)
                    assert quick.reason == 'fast_path' and quick.turn_id is None
                    assert store.get_request(personal_route_id) is None
                    assert records.read('v2_route_turn_keys', _revision(source_routes._identity(project['id'], quick_key))) is None
                connection = _readonly_connection(store._path)
                try:
                    assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0
                finally:
                    connection.close()
                print('shared-public actual auto ASK200/shared native origin; route preparation actor owner/namespace key isolation/private revocation; fast shared-private branch ignores genuine personal prepared same-key and personal examples; model attempts 0', flush=True)
                assert not any(event['type'].startswith('model.') for event in store.events_after(identities[0]))
                print('shared-public clarification scope/empty history/native inconsistency and unsupported intent rejected; model events 0', flush=True)
        print('shared-public-ask original HTTP/native/shared origin/actor history and lifecycle passed; model events 0', flush=True)

    with resource_context(server.state.server_context.resources), connection_scope():
        asyncio.run(run())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    check(args.root)
