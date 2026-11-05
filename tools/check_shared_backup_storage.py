import argparse
import asyncio
import os
from pathlib import Path
import shutil
import sys
import threading
from uuid import uuid4

import httpx


def check(base):
    workspace, base = Path(__file__).resolve().parents[1], Path(base).resolve()
    if base == workspace or any(path.name == 'runtime' for path in (base, *base.parents)):
        raise ValueError('shared_check_requires_isolated_root')
    root = base / ('b' + uuid4().hex[:6])
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(workspace / 'config/settings.toml.example', root / 'config/settings.toml')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.material_origin_readers import _readonly_connection
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from core.storage_provider.connection_scope import connection_scope

    devices = DeviceRegistry(root / 'server')
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    admin = devices.authenticate(devices.exchange(pairing['code'], name='备份容量管理设备')['key'])
    users, actors, tokens = ServerUsers(root, records=devices.records), [], []
    for label in ('甲', '乙'):
        user = users.create(admin, name=label)
        pairing = devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)
        token = devices.exchange(pairing['code'], name=label)['key']
        actors.append(devices.authenticate(token))
        tokens.append(token)

    def child(path, user, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
        child_factory=child, concurrency=2, port=8013)
    context, registry = server.state.server_context, server.state.shared_projects
    projects = [registry.create(actor, project_name=label, operation_id='create-' + label)['project']
                for actor, label in zip(actors, ('a', 'b'))]
    for index in (0, 1):
        actor, invited, project = actors[index], actors[1 - index], projects[index]
        registry.invite(actor, project['id'], user_id=invited.user_id, role='viewer',
            authority=project['authority'], operation_id='invite-' + str(index))
        invitation = registry.invitations(invited)[0]
        registry.accept(invited, invitation['id'], expected_revision=invitation['revision'],
            operation_id='accept-' + str(index))
        data = registry.roots._data(project['id'])
        (data.root / 'workspace').mkdir()
        (data.root / 'workspace/original.txt').write_text('真实共享备份容量资料' + str(index), encoding='utf8')

    async def run():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server),
                    base_url='http://127.0.0.1:8013') as client:
                async def backup(index, project, key):
                    return await client.post('/api/v2/server/shared-projects/' + project['id'] + '/backups',
                        json={}, headers={'Authorization': 'Bearer ' + tokens[index], 'Idempotency-Key': key})
                response = await backup(0, projects[0], 'owner-own-project')
                assert response.status_code == 200 and response.json()['state'] == 'ready', response.text
                assert context.jobs.storage_bytes(actors[0].user_id) > 0
                print('backup-storage: owner backup with complete owned-root capacity passed', flush=True)

                barrier, guard, seen = threading.Barrier(2), threading.Lock(), set()
                previous = threading.getprofile()
                def synchronized(frame, event, _argument):
                    if event != 'call' or frame.f_code.co_name != 'reserve_up_to':
                        return
                    budget = frame.f_locals.get('self')
                    if getattr(budget, 'scheduler', None) is not context.jobs:
                        return
                    with guard:
                        first = budget.user_id not in seen
                        seen.add(budget.user_id)
                    if first:
                        barrier.wait(timeout=20)
                threading.setprofile_all_threads(synchronized)
                try:
                    a, b = await asyncio.wait_for(asyncio.gather(
                        backup(0, projects[1], 'cross-a-to-b'), backup(1, projects[0], 'cross-b-to-a')), 45)
                finally:
                    threading.setprofile_all_threads(previous)
                assert a.status_code == b.status_code == 200, (a.text, b.text)
                assert a.json()['state'] == b.json()['state'] == 'ready'
                assert seen == {actor.user_id for actor in actors}
                print('backup-storage: real two-worker cross-project reservations and backups passed', flush=True)

                changed = []
                source = registry.roots._data(projects[0]['id']).root
                def growth(frame, event, result):
                    if event != 'return' or frame.f_code.co_name != 'reserve_up_to' or changed:
                        return
                    budget = frame.f_locals.get('self')
                    if getattr(budget, 'scheduler', None) is not context.jobs or budget.user_id != actors[0].user_id:
                        return
                    with (source / 'workspace/late-growth.bin').open('wb') as stream:
                        stream.truncate(result.remaining + 1)
                    changed.append(result.remaining)
                threading.setprofile_all_threads(growth)
                try:
                    response = await backup(0, projects[0], 'growth-after-reserve')
                finally:
                    threading.setprofile_all_threads(previous)
                assert changed and response.status_code == 429 and response.json()['detail'] == 'shared_backup_quota_exceeded'
                app = context.pool._children[actors[0].user_id].application
                assert app.state.recognition_records.read('v2_shared_backup_commands', 'growth-after-reserve') is None
                assert context.jobs.storage_bytes(actors[0].user_id) >= changed[0] + 1
                for child in context.pool._children.values():
                    connection = _readonly_connection(child.application.state.ai_turn_store._path)
                    try:
                        assert connection.execute("SELECT COUNT(*) FROM ai_turn_events "
                            "WHERE json_extract(event_json, '$.type') LIKE 'model.%'").fetchone()[0] == 0
                    finally:
                        connection.close()
                print('backup-storage: real growth beyond the reserved cap refused before archive command; no false ready result', flush=True)
        print('backup-storage: actual server and child shutdown passed; no model calls', flush=True)

    with resource_context(context.resources), connection_scope():
        asyncio.run(run())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    check(args.root)
