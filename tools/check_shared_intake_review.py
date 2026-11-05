import argparse
import asyncio
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from uuid import uuid4

import httpx


def check(base):
    workspace, base = Path(__file__).resolve().parents[1], Path(base).resolve()
    if base == workspace or any(path.name == 'runtime' for path in (base, *base.parents)):
        raise ValueError('shared_check_requires_isolated_root')
    root = base / ('r' + uuid4().hex[:6])
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(workspace / 'config/settings.toml.example', root / 'config/settings.toml')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context

    devices = DeviceRegistry(root / 'server')
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    admin = devices.authenticate(devices.exchange(pairing['code'], name='核对管理')['key'])
    users, actors, tokens = ServerUsers(root, records=devices.records), [], []
    for name in ('核对成员', '核对只看成员'):
        user = users.create(admin, name=name)
        pairing = devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)
        token = devices.exchange(pairing['code'], name=name)['key']
        tokens.append(token)
        actors.append(devices.authenticate(token))
    owner, viewer = actors

    def child(path, user_id, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
        child_factory=child, port=8013)
    registry = server.state.shared_projects
    project = registry.create(owner, project_name='共享整理稿核对', operation_id='review-project')['project']
    registry.invite(owner, project['id'], user_id=viewer.user_id, role='viewer',
        authority=project['authority'], operation_id='review-invite')
    invitation = registry.invitations(viewer)[0]
    registry.accept(viewer, invitation['id'], expected_revision=invitation['revision'], operation_id='review-accept')
    prefix = '/api/v2/server/shared-projects/' + project['id']

    async def run():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://127.0.0.1:8013') as client:
                headers = {'Authorization': 'Bearer ' + tokens[0]}
                response = await client.post(prefix + '/intake/text',
                    headers={**headers, 'Idempotency-Key': 'review-text-' + uuid4().hex},
                    json={'text': '这份实际原件尚未经过模型整理，不具备确认与提议条件。'})
                assert response.status_code == 200, response.text
                item = response.json()['item']
                data = registry.roots._data(project['id'])
                before = data.records.read('workspace_items', item['id'])
                endpoint = prefix + '/intake/items/' + item['id']
                response = await client.post(endpoint + '/confirm',
                    headers={'Authorization': 'Bearer ' + tokens[1]}, json={'expected_revision': item['revision']})
                assert response.status_code == 403, response.text
                for operation in ('confirm', 'recognition'):
                    response = await client.post(endpoint + '/' + operation, headers=headers,
                        json={'expected_revision': item['revision']})
                    assert response.status_code == 409 and response.json()['detail'] == 'shared_organize_result_unavailable', response.text
                response = await client.put(endpoint + '/draft', headers=headers,
                    json={'expected_revision': item['revision'], 'title': '待核对', 'summary': '待核对',
                        **{field: [] for field in ('topics', 'facts', 'todos', 'uncertainties', 'people', 'dates', 'suggestions')}})
                assert response.status_code == 409 and response.json()['detail'] == 'shared_organize_result_unavailable', response.text
                assert data.records.read('workspace_items', item['id']) == before
                assert not data.records.list('documents') and not data.records.list('recognition_candidates')
                response = await client.get(endpoint, headers=headers)
                assert response.status_code == 200 and response.json()['item'] == item, response.text
                assert not (data.root / '.rebuild-data' / 'ai-turns.sqlite3').exists()
                native = root / 'users' / owner.user_id / '.rebuild-data' / 'ai-turns.sqlite3'
                if native.exists():
                    with sqlite3.connect(native.as_uri() + '?mode=ro', uri=True) as connection:
                        assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0
                print('shared-intake-review real source: viewer403; absent organization proof409 for draft/confirm/recognition; source unchanged and refresh preserved; no documents or candidates; actual model attempts0')
        print('shared-intake-review real server lifecycle closed')

    asyncio.run(run())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    arguments = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    check(arguments.root)
