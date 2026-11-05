import argparse
import asyncio
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import httpx


def check(base):
    workspace, base = Path(__file__).resolve().parents[1], Path(base).resolve()
    if base == workspace or any(part.name == 'runtime' for part in (base, *base.parents)):
        raise ValueError('shared_check_requires_isolated_root')
    root = base / ('s' + uuid4().hex[:6])
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(workspace / 'config/settings.toml.example', root / 'config/settings.toml')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.shared_projects import MEMBERS, OPERATIONS, PROJECTS
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context

    devices = DeviceRegistry(root / 'server')
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    admin = devices.authenticate(devices.exchange(pairing['code'], name='容量管理设备')['key'])
    users, actors, tokens = ServerUsers(root, records=devices.records), [], []
    for label in ('所有者', '转入所有者'):
        user = users.create(admin, name=label)
        pairing = devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)
        token = devices.exchange(pairing['code'], name=label)['key']
        actors.append(devices.authenticate(token))
        tokens.append(token)
    a, b = actors

    def child(path, user, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
        child_factory=child, port=8013)
    registry, jobs = server.state.shared_projects, server.state.server_context.jobs
    assert server.state.shared_storage.registry is registry and server.state.shared_storage.jobs is jobs
    projects = [registry.create(actor, project_name=label, operation_id='create-' + label)['project']
                for actor, label in zip(actors, ('project-a', 'project-b'))]
    registry.invite(a, projects[0]['id'], user_id=b.user_id, role='member',
        authority=projects[0]['authority'], operation_id='invite-b')
    invitation = registry.invitations(b)[0]
    registry.accept(b, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-b')
    for project in projects:
        (registry.roots._data(project['id']).root / 'original.txt').write_bytes(b'S' * 700000)
    users.update(admin, b.user_id, expected_revision=users.get(b.user_id)['revision'], storage_limit_mb=1)
    detail = registry.detail(a, projects[0]['id'])
    member = next(row for row in detail['members'] if row['user_id'] == b.user_id)
    payload = {'action': 'transfer', 'role': None, 'expected_revision': member['revision'],
        'authority': detail['project']['authority'], 'operation_id': 'transfer-quota-api'}
    before = {collection: registry.records.list(collection) for collection in (PROJECTS, MEMBERS, OPERATIONS)}

    async def run():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server),
                    base_url='http://127.0.0.1:8013') as client:
                headers = {'Authorization': 'Bearer ' + tokens[0]}
                path = '/api/v2/server/shared-projects/' + projects[0]['id'] + '/members/' + b.user_id
                response = await client.post(path, headers=headers, json=payload)
                assert response.status_code == 429 and response.json()['detail'] == 'storage_quota_exceeded'
                assert {collection: registry.records.list(collection) for collection in before} == before
                users.update(admin, b.user_id, expected_revision=users.get(b.user_id)['revision'], storage_limit_mb=8)
                async with jobs.intake_many((a.user_id, b.user_id)):
                    response = await client.post(path, headers=headers, json=payload)
                    assert response.status_code == 429 and response.json()['detail'] == 'storage_admission_in_progress'
                assert registry.records.read(OPERATIONS, payload['operation_id']) is None
                response = await client.post(path, headers=headers, json=payload)
                assert response.status_code == 200 and response.json()['state'] == 'completed'
                source = registry.records.read(PROJECTS, projects[0]['id'])
                assert source.payload['owner_user_id'] == b.user_id
                assert jobs.storage_bytes(a.user_id) == jobs._root_bytes(users.root_for(a.user_id))
                assert jobs.storage_bytes(b.user_id) == jobs._storage_bytes(b.user_id,
                    [registry.roots._data(project['id']).root for project in projects])
                assert server.state.server_context.pool.loaded_user_ids == ()
                assert not tuple(root.glob('users/*/.rebuild-data/ai-turns.sqlite3'))
                assert not tuple(root.glob('shared/*/.rebuild-data/ai-turns.sqlite3'))
        print('shared-storage public: actual server factory, authentication, quota 429, busy 429, '
              'unchanged refused rows, completed transfer and current-owner bytes passed', flush=True)
        print('shared-storage public: real server lifecycle closed; user applications and model attempts were not started', flush=True)
    asyncio.run(run())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    check(args.root)
