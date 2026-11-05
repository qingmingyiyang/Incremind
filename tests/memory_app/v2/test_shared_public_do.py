import asyncio
import os
from pathlib import Path
import shutil
from uuid import uuid4

import httpx


def test_real_server_shared_do_preserves_qualification_and_unconfigured_failure(tmp_path_factory):
    root = tmp_path_factory.mktemp('shared-public-do').resolve(strict=True)
    assert not any(path.name == 'runtime' for path in (root, *root.parents))
    (root / 'config').mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', root / 'config/settings.toml')
    previous = {key: os.environ.get(key) for key in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ.update(CHRIPTMAS_APP_ROOT=str(root), CHRIPTMAS_DEPLOY='server')
    try:
        _server_case(root)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _server_case(root):
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.privacy_policy import set_private_project
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context

    devices = DeviceRegistry(root / 'server')
    users = ServerUsers(root, records=devices.records)
    token = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'],
        name='任务资格合成管理员')['key']
    owner = devices.authenticate(token)
    account = users.create(owner, name='任务资格合成只看成员')
    viewer_key = devices.exchange(devices.issue_pairing(user_id=account['user_id'], actor=owner.device_id)['code'],
        name='任务资格合成设备')['key']
    viewer = devices.authenticate(viewer_key)

    def child(path, user_id, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root), server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
        child_factory=child, port=8016)
    registry = server.state.shared_projects
    project = registry.create(owner, project_name='共享任务资格', operation_id='create-' + uuid4().hex)['project']
    registry.invite(owner, project['id'], user_id=viewer.user_id, role='viewer', authority=project['authority'],
        operation_id='invite-' + uuid4().hex)
    invitation = registry.invitations(viewer)[0]
    registry.accept(viewer, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-' + uuid4().hex)
    source = registry.roots._data(project['id'])

    async def run():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server),
                    base_url='http://127.0.0.1:8016', headers={'Authorization': 'Bearer ' + viewer_key}) as client:
                scopes = await client.get('/api/v2/server/shared-projects/scopes')
                assert scopes.status_code == 200, scopes.text
                scope, = scopes.json()['items']
                assert scope['role'] == 'viewer' and scope['can_do'] and scope['can_ask'] and not scope['can_write']
                app = server.state.server_context.pool._children[viewer.user_id].application
                records = app.state.recognition_records
                activities = source.records.list('v2_shared_activity')
                body = {'project_id': project['id'], 'scope_kind': 'shared', 'intent': 'do', 'executor': 'builtin',
                    'text': '请根据共享项目资料写一份行动清单。'}
                headers = {'Idempotency-Key': 'public-do-' + uuid4().hex, 'Accept': 'application/json'}
                response = await client.post('/api/v2/workbench/turns', json=body, headers=headers)
                assert response.status_code == 409 and response.json()['detail'] == 'turn_interrupted', response.text
                native = getattr(app.state, 'ai_turn_store', None)
                if native is not None:
                    with native._connect() as connection:
                        before = tuple(connection.execute('SELECT turn_id, request_json FROM ai_turns'))
                        assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0
                replay = await client.post('/api/v2/workbench/turns', json=body, headers=headers)
                assert replay.status_code == response.status_code and replay.json() == response.json()
                if native is not None:
                    with native._connect() as connection:
                        assert tuple(connection.execute('SELECT turn_id, request_json FROM ai_turns')) == before
                        assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0
                refreshed = await client.get('/api/v2/workbench/threads', params={
                    'project_id': project['id'], 'scope_kind': 'shared'})
                assert refreshed.status_code == 200 and refreshed.json() == {'items': []}, refreshed.text
                assert not records.list('v2_turns') and not records.list('v2_task_executions')
                set_private_project(source.records, project['id'], True, 0)
                blocked = await client.post('/api/v2/workbench/turns', json=body,
                    headers={**headers, 'Idempotency-Key': 'private-do-' + uuid4().hex})
                assert blocked.status_code == 409 and blocked.json()['detail'] == 'turn_interrupted', blocked.text
                assert source.records.list('v2_shared_activity') == activities
                details = registry.detail(owner, project['id'])
                member = next(row for row in details['members'] if row['user_id'] == viewer.user_id)
                registry.change_member(owner, project['id'], user_id=viewer.user_id, action='remove',
                    authority=details['project']['authority'], expected_revision=member['revision'],
                    operation_id='remove-' + uuid4().hex)
                after_membership = source.records.list('v2_shared_activity')
                denied = await client.post('/api/v2/workbench/turns', json=body,
                    headers={**headers, 'Idempotency-Key': 'denied-do-' + uuid4().hex})
                assert denied.status_code == 403, denied.text
                assert source.records.list('v2_shared_activity') == after_membership
                assert not (source.root / '.rebuild-data' / 'ai-turns.sqlite3').exists()
                assert not records.list('v2_turns') and not records.list('v2_task_executions')
    asyncio.run(run())
