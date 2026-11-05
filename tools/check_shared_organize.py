import argparse
import asyncio
import os
import re
from pathlib import Path
import shutil
import sys
import threading
from uuid import uuid4

import httpx


def check(base):
    workspace, base = Path(__file__).resolve().parents[1], Path(base).resolve()
    if base == workspace or any(part.name == 'runtime' for part in (base, *base.parents)):
        raise ValueError('shared_check_requires_isolated_root')
    root = base / ('o' + uuid4().hex[:6])
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(workspace / 'config/settings.toml.example', root / 'config/settings.toml')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.privacy import set_private_project
    from backend.memory_app.v2.material_origin_readers import _readonly_connection
    from backend.security.user_context import authorize_user, user_context
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from core.storage_provider.connection_scope import connection_scope

    devices = DeviceRegistry(root / 'server')
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    admin_token = devices.exchange(pairing['code'], name='整理管理')['key']
    admin = devices.authenticate(admin_token)
    users, actors, tokens = ServerUsers(root, records=devices.records), [], []
    for label in ('整理成员', '整理只看成员'):
        user = users.create(admin, name=label)
        pairing = devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)
        token = devices.exchange(pairing['code'], name=label)['key']
        tokens.append(token)
        actors.append(devices.authenticate(token))
    actor, viewer = actors

    def child(path, user_id, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
        child_factory=child, port=8013)
    registry = server.state.shared_projects
    project = registry.create(actor, project_name='共享整理实际输入', operation_id='organize-project')['project']
    registry.invite(actor, project['id'], user_id=viewer.user_id, role='viewer',
        authority=project['authority'], operation_id='organize-invite')
    invitation = registry.invitations(viewer)[0]
    registry.accept(viewer, invitation['id'], expected_revision=invitation['revision'], operation_id='organize-accept')
    prefix = '/api/v2/server/shared-projects/' + project['id']

    async def run():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://127.0.0.1:8013') as client:
                headers = {'Authorization': 'Bearer ' + tokens[0]}
                response = await client.post(prefix + '/intake/text',
                    headers={**headers, 'Idempotency-Key': 'organize-text-' + uuid4().hex},
                    json={'text': '这份共享原件只由实际整理服务处理。所有认识都需要人工确认。'})
                assert response.status_code == 200, response.text
                item = response.json()['item']
                data = registry.roots._data(project['id'])
                async with server.state.server_context.pool.lease(actor.user_id) as app:
                    with user_context(authorize_user(users, actor, actor.user_id)):
                        models = app.state.recognition_models
                        models.update('generation', {'expected_revision': models.public()['generation']['revision'],
                            'base_url': 'https://api.openai.com/v1', 'model': 'gpt-4o-mini',
                            'clear_api_key': True, 'enabled': True, 'allow_remote': True})
                        assert models.public()['generation']['configured'] is False
                denied = await client.post(prefix + '/intake/items/' + item['id'] + '/process',
                    headers={'Authorization': 'Bearer ' + tokens[1]}, json={'expected_revision': item['revision']})
                assert denied.status_code == 403, denied.text
                set_private_project(data.records, project['id'], True, 0)
                private = await client.post(prefix + '/intake/items/' + item['id'] + '/process',
                    headers=headers, json={'expected_revision': item['revision']})
                assert private.status_code == 409, private.text
                assert data.records.read('workspace_items', item['id']).revision == item['revision']
                set_private_project(data.records, project['id'], False, 1)
                failures = []
                def trace(frame, event, argument):
                    filename = frame.f_code.co_filename.replace('\\', '/')
                    if not any(filename.endswith('/' + name) for name in
                            ('workspace_intake.py', 'shared_organize.py', 'organize_turns.py', 'automatic_constraints.py')):
                        return None
                    if event == 'exception':
                        message = str(argument[1])
                        code = message if re.fullmatch(r'(?:automatic|shared|material|model|source|project|memory|route|private)_[A-Za-z0-9_:-]{1,100}', message) else ''
                        failures.append((Path(filename).name, frame.f_code.co_name,
                            frame.f_lineno, argument[0].__name__, code))
                    return trace
                previous_trace, previous_threads = sys.gettrace(), threading.gettrace()
                sys.settrace(trace)
                threading.settrace_all_threads(trace)
                try:
                    processed = await client.post(prefix + '/intake/items/' + item['id'] + '/process',
                        headers=headers, json={'expected_revision': item['revision']})
                finally:
                    threading.settrace_all_threads(previous_threads)
                    sys.settrace(previous_trace)
                assert processed.status_code == 200, processed.text
                result = processed.json()
                assert result['scope_kind'] == 'shared' and result['item']['status'] == 'failed', result
                async with server.state.server_context.pool.lease(actor.user_id) as app:
                    records, store = app.state.recognition_records, app.state.ai_turn_store
                    assert records.read('workspace_items', item['id']) is None
                    stages = records.list('workspace_organize_steps')
                    if len(stages) != 1:
                        print('shared-organize actual exception codes', failures[-16:], flush=True)
                    assert len(stages) == 1, 'actual actor stages=' + str(len(stages))
                    request = store.get_request(stages[0].payload['turn_id'])
                    if request is None:
                        print('shared-organize pre-claim actual exception types/codes', failures[-16:], flush=True)
                    assert request is not None and request['desired_outcome'] == 'memory.organize'
                    bundle = store.restore_admission(request)
                    assert {entry.kind for entry in bundle.items} == {'material-origin-v1', 'project-constraints-v1'}
                    origin = next(entry.payload for entry in bundle.items if entry.kind == 'material-origin-v1')
                    assert origin['execution']['owner_user_id'] == actor.user_id
                    assert origin['auxiliary']['materials'][0]['namespace'] == result['namespace']
                    assert origin['auxiliary']['materials'][0]['id'] == item['id']
                    events = tuple(store.events_after(request['turn_id']))
                    assert events[0]['type'] == 'turn.accepted' and events[-1]['type'] == 'turn.failed'
                    print('shared-organize genuine failure phase', {'native': 'accepted_then_failed',
                        'public_error': result['item']['error'], 'exception_types_codes': failures[-12:]}, flush=True)
                    connection = _readonly_connection(store._path)
                    try:
                        assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0
                    finally:
                        connection.close()
                assert data.records.list('workspace_organize_steps') == ()
                assert data.records.list('workspace_organize_inputs') == ()
                assert data.records.list('documents') == () and data.records.list('recognitions') == ()
                assert not tuple(data.root.rglob('ai-turns.sqlite3'))
                refreshed = await client.get(prefix + '/intake/items/' + item['id'], headers=headers)
                assert refreshed.status_code == 200 and refreshed.json()['item']['status'] == 'failed', refreshed.text
                print('shared-organize actual public text source/actor native+automatic admission; viewer403/private409; '
                    'unconfigured model budget preflight refused before provider; no source draft/document claimed; refresh preserved; '
                    'actor-only stages/native, source native absent, actual attempts0', flush=True)
        print('shared-organize real server lifecycle closed', flush=True)

    with resource_context(server.state.server_context.resources), connection_scope():
        asyncio.run(run())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    check(args.root)
