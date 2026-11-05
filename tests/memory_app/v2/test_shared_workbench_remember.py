import asyncio
import os
from pathlib import Path
import shutil

import httpx


def test_shared_workbench_remember_keeps_source_and_recoverable_actor_receipt(tmp_path_factory):
    _isolated(tmp_path_factory, proof_checks=False)


def test_shared_automatic_ask_replay_and_remember_original_birth(tmp_path_factory):
    _isolated(tmp_path_factory, proof_checks=True)


def _isolated(tmp_path_factory, *, proof_checks):
    repository = Path(__file__).resolve().parents[3]
    root = tmp_path_factory.mktemp('sr').resolve(strict=True)
    assert 'runtime' not in root.parts and root != repository
    (root / 'config').mkdir()
    shutil.copyfile(repository / 'config/settings.toml.example', root / 'config/settings.toml')
    previous = {key: os.environ.get(key) for key in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ['CHRIPTMAS_APP_ROOT'], os.environ['CHRIPTMAS_DEPLOY'] = str(root), 'server'
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) == root
    try:
        _check(root, proof_checks=proof_checks)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _check(root, *, proof_checks):
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.material_origin_readers import _readonly_connection
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.shared_intake import COMMANDS as SOURCE_COMMANDS
    from backend.memory_app.v2.shared_projects import MEMBERS, member_id
    from backend.memory_app.v2.shared_workbench_remember import COMMANDS, TURNS
    from backend.memory_app.v2.steer_learning import TERMINALS
    from backend.memory_app.v2.turn_message_order import ACCEPTANCES, COLLECTION, HEADS
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from core.storage_provider.record_lineage import (
        HEAD_COLLECTIONS, WITNESS_COLLECTIONS, capture_lineage, verify_lineage,
    )

    devices = DeviceRegistry(root / 'server')
    users = ServerUsers(root, records=devices.records)

    def pair(user_id, actor, name):
        token = devices.exchange(devices.issue_pairing(user_id=user_id, actor=actor)['code'], name=name)['key']
        return devices.authenticate(token), {'Authorization': 'Bearer ' + token}

    admin, _ = pair('local-user', 'install', '记住管理设备')
    owner, _ = pair(users.create(admin, name='资料所有者')['user_id'], admin.device_id, '所有者设备')
    member, headers = pair(users.create(admin, name='共同投喂成员')['user_id'], admin.device_id, '投喂设备')
    _, second = pair(member.user_id, admin.device_id, '同一账户的第二台设备')
    viewer, viewer_headers = pair(users.create(admin, name='只看成员')['user_id'], admin.device_id, '只看设备')

    def child(path, _, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root),
                server_context=context)

    layout = DeploymentLayout('server', root / 'users/local-user', root)
    server = create_server_application(layout, child_factory=child, port=8013)
    registry = server.state.shared_projects
    project = registry.create(owner, project_name='工作台共同投喂', operation_id='remember-project')['project']['id']
    for actor, role in ((member, 'member'), (viewer, 'viewer')):
        registry.invite(owner, project, user_id=actor.user_id, role=role,
            authority=registry.detail(owner, project)['project']['authority'], operation_id='invite-' + role)
        invitation = registry.invitations(actor)[0]
        registry.accept(actor, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-' + role)
    key = 'remember-workbench-one'
    prefix = '/api/v2/workbench'
    command_path = prefix + '/remember-commands/' + key
    params = {'project_id': project, 'scope_kind': 'shared'}
    body = {**params, 'intent': 'remember', 'text': '隔离验收：共享原件由成员提交，原对话仍保存在自己的空间。'}
    headers['Idempotency-Key'] = key
    data = registry.roots._data(project)

    def no_wire(path):
        if path.is_file():
            with _readonly_connection(path) as connection:
                assert connection.execute('SELECT count(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0

    async def verify_binding():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://test') as client:
                async with server.state.server_user_pool.lease(member.user_id) as app:
                    models = app.state.recognition_models
                    models.update('generation', {'expected_revision': models.public()['generation']['revision'],
                        'base_url': 'https://127.0.0.2:9/v1', 'model': 'gpt-4o-mini',
                        'api_key': 'isolated-no-wire-placeholder', 'enabled': True, 'allow_remote': False})
                ask_body = {**params, 'intent': 'auto', 'text': '这个空项目里有什么？'}
                ask_key = 'shared-auto-ask'
                first = await client.post(prefix + '/turns',
                    headers={**headers, 'Idempotency-Key': ask_key}, json=ask_body)
                assert first.status_code == 200, first.text
                answer = first.json()
                assert answer['turn']['intent'] == 'ask' and answer['turn']['receipt']['ask']['no_match']
                replay = await client.post(prefix + '/turns',
                    headers={**second, 'Idempotency-Key': ask_key}, json=ask_body)
                assert replay.status_code == 200 and replay.json() == answer, replay.text
                unknown = await client.get(prefix + '/remember-commands/' + ask_key, params=params, headers=second)
                assert (unknown.status_code, unknown.json()['detail']) == (404, 'shared_remember_not_received')
                async with server.state.server_user_pool.lease(member.user_id) as app:
                    no_wire(app.state.ai_turn_store._path)
                    models = app.state.recognition_models
                    models.update('generation', {'expected_revision': models.public()['generation']['revision'],
                        'enabled': False, 'allow_remote': False})
                posted = await client.post(prefix + '/turns', headers=headers, json=body)
                assert posted.status_code == 200, posted.text
                turn, thread_id = posted.json()['turn'], posted.json()['thread_id']
                item_id = turn['receipt']['remember']['item_id']
                retry_path = prefix + '/turns/' + turn['id'] + '/retry'
                async with server.state.server_user_pool.lease(member.user_id) as app:
                    await asyncio.gather(*tuple(app.state.workbench_tasks))
                    records = app.state.recognition_records
                    previous = records.read('v2_turns', turn['id'])
                    assert previous.payload['receipt']['remember']['state'] == 'failed'
                    initial = records.read(ACCEPTANCES, turn['id']).payload
                    head = records.read(HEADS, thread_id).payload
                    assert (head['project_id'], head['public_id'], head['sequence']) == (project, turn['id'], 1)
                    assert initial['previous_id'] is None and initial['thread_id'] == thread_id
                    assert records.read(COLLECTION, turn['id']) is None and records.read(TERMINALS, turn['id']) is None
                    command, = data.records.list(SOURCE_COMMANDS)
                    assert command.payload['item_id'] == item_id
                    original = data.records.read('workspace_items', item_id)
                    neighbor = await client.post(prefix + '/turns', headers={**headers, 'Idempotency-Key': 'neighbor-source'},
                        json={**body, 'thread_id': thread_id})
                    assert neighbor.status_code == 200, neighbor.text
                    subsequent = neighbor.json()['turn']['id']
                    await asyncio.gather(*tuple(app.state.workbench_tasks))
                    following = records.read(ACCEPTANCES, subsequent).payload
                    assert following['previous_id'] == turn['id'] and following['sequence'] == 2
                    assert records.read(COLLECTION, subsequent) is None and records.read(TERMINALS, subsequent) is None
                    assert records.read(HEADS, thread_id).payload['public_id'] == subsequent
                    adjacent = next(row for row in data.records.list(SOURCE_COMMANDS) if row != command)
                    assert adjacent.payload['item_id'] != item_id

                    def swap_birth(tx, birth):
                        tx.delete(SOURCE_COMMANDS, command.object_id, expected_revision=1)
                        tx.put(SOURCE_COMMANDS, command.object_id, {**command.payload, 'birth': birth}, expected_revision=0)

                    async def rejected():
                        reply = await client.post(retry_path, headers=headers, json={'project_id': project})
                        assert (reply.status_code, reply.json()['detail']) == (409, 'shared_remember_source_changed'), reply.text
                        assert records.read('v2_turns', turn['id']) == previous
                        assert app.state.workbench_tasks == set()

                    # 隔离库中以真实 SQL 事务模拟旁路记录损坏，原 HTTP 工作台逐次拒绝。
                    with data.records.begin() as tx:
                        swap_birth(tx, adjacent.payload['birth'])
                        tx.commit()
                    await rejected()
                    with data.records.begin() as tx:
                        tx.delete('workspace_items', item_id, expected_revision=original.revision)
                        tx.put('workspace_items', item_id, original.payload, expected_revision=0)
                        swap_birth(tx, capture_lineage(tx, 'workspace_items', item_id))
                        tx.commit()
                    await rejected()
                    with data.records.begin() as tx:
                        for collection in (HEAD_COLLECTIONS['workspace_items'], WITNESS_COLLECTIONS['workspace_items']):
                            row = tx.read(collection, item_id)
                            tx.delete(collection, item_id, expected_revision=row.revision)
                        swap_birth(tx, capture_lineage(tx, 'workspace_items', item_id))
                        tx.commit()
                    await rejected()
                    no_wire(app.state.ai_turn_store._path)
        assert server.state.server_user_pool.loaded_user_ids == ()

    if proof_checks:
        with resource_context(server.state.server_context.resources):
            asyncio.run(verify_binding())
        return

    async def scenario():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://test') as client:
                missing = await client.get(command_path, params=params, headers=headers)
                assert (missing.status_code, missing.json()['detail']) == (404, 'shared_remember_not_received')
                forbidden = await client.post(prefix + '/turns',
                    headers={**viewer_headers, 'Idempotency-Key': 'viewer-remember'}, json=body)
                assert (forbidden.status_code, forbidden.json()['detail']) == (403, 'shared_forbidden'), forbidden.text
                assert data.records.list('workspace_items') == ()
                posted = await client.post(prefix + '/turns', headers=headers, json=body)
                assert posted.status_code == 200, posted.text
                result = posted.json()
                turn, thread = result['turn'], result['thread_id']
                identity = turn['id']
                memory = turn['receipt']['remember']
                item = memory['item_id']
                assert turn['intent'] == 'remember' and turn['scope_kind'] == 'shared'
                assert memory['state'] == 'processing' and memory['document_id'] is None and not memory['verified']

                async def finished():
                    async with server.state.server_user_pool.lease(member.user_id) as application:
                        await asyncio.gather(*tuple(application.state.workbench_tasks))
                        actor_records = application.state.recognition_records
                        saved = actor_records.read('v2_turns', identity)
                        assert saved.payload['receipt']['remember']['state'] == 'failed', saved.payload['receipt']
                        assert actor_records.read(TURNS, identity).payload['item_id'] == item
                        assert actor_records.read(COMMANDS, key).payload['caller']['device_id'] == member.device_id
                        assert actor_records.list('workspace_items') == () and actor_records.list('documents') == ()
                        no_wire(application.state.ai_turn_store._path)
                        return saved

                saved = await finished()
                error = saved.payload['receipt']['remember']['error']
                assert error == 'model_request_failed'
                existing = data.records.list(SOURCE_COMMANDS)
                assert len(existing) == 1 and existing[0].payload['actor_user_id'] == member.user_id
                assert existing[0].payload['item_id'] == item
                verify_lineage(data.records, existing[0].payload['birth'])
                original = data.records.read('workspace_items', item)
                assert original.payload['source_text'] == body['text']
                assert (original.payload['status'], original.payload['error']) == ('failed', error)
                assert data.records.list('v2_turns') == () and data.records.list('documents') == ()
                assert data.records.list('recognition_candidates') == ()
                no_wire(data.root / '.rebuild-data' / 'ai-turns.sqlite3')

                found = await client.get(command_path, params=params, headers=headers)
                assert found.status_code == 200, found.text
                command = found.json()
                assert command['state'] == 'accepted' and command['result']['turn']['id'] == identity
                assert command['result']['turn']['receipt']['remember']['error'] == error
                wrong_device = await client.get(command_path, params=params, headers=second)
                assert (wrong_device.status_code, wrong_device.json()['detail']) == (403, 'shared_remember_owner_changed')
                replay = await client.post(prefix + '/turns', headers=headers, json=body)
                assert replay.status_code == 200 and replay.json() == result

                path = prefix + '/threads/' + thread
                current = await client.get(path, params=params, headers=headers)
                assert current.status_code == 200 and current.json()['turns'] == [command['result']['turn']], current.text
                retry = await client.post(prefix + '/turns/' + identity + '/retry', headers=second,
                    json={'project_id': project})
                assert retry.status_code == 200 and retry.json()['id'] == identity, retry.text
                assert retry.json()['receipt']['remember']['item_id'] == item
                await finished()
                assert data.records.list(SOURCE_COMMANDS) == existing
                assert data.records.read('workspace_items', item).revision > original.revision
                membership = registry.records.read(MEMBERS, member_id(project, member.user_id))
                registry.change_member(owner, project, user_id=member.user_id, action='remove',
                    expected_revision=membership.revision, authority=registry.detail(owner, project)['project']['authority'],
                    operation_id='remove-remember-member')
                history = await client.get(path, params=params, headers=headers)
                assert history.status_code == 200, history.text
                old = history.json()['turns'][0]
                assert old['id'] == identity and old['user_text'] == body['text']
                assert old['receipt']['remember']['sources_available'] is False
                scopes = await client.get('/api/v2/server/shared-projects/scopes', headers=headers)
                assert scopes.status_code == 200, scopes.text
                recovered_scope = next(value for value in scopes.json()['items'] if value['id'] == project)
                assert recovered_scope['membership'] == 'unavailable' and recovered_scope['has_personal_history']
                assert [entry['id'] for entry in recovered_scope['threads']] == [thread]
                denied = await client.post(prefix + '/turns/' + identity + '/retry', headers=headers,
                    json={'project_id': project})
                assert denied.status_code == 403, denied.text
                assert data.records.list(SOURCE_COMMANDS) == existing
                return path, history.json(), error

    with resource_context(server.state.server_context.resources):
        path, previous, error = asyncio.run(scenario())
    assert server.state.server_user_pool.loaded_user_ids == ()

    cold = create_server_application(layout, child_factory=child, port=8013)

    async def read_cold():
        async with cold.router.lifespan_context(cold):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=cold), base_url='http://test') as client:
                history = await client.get(path, params=params, headers=headers)
                assert history.status_code == 200 and history.json() == previous, history.text
                found = await client.get(command_path, params=params, headers=headers)
                assert found.status_code == 200, found.text
                assert found.json()['result']['turn']['receipt']['remember']['error'] == error
                async with cold.state.server_user_pool.lease(member.user_id) as application:
                    assert application.state.workbench_tasks == set()
                    no_wire(application.state.ai_turn_store._path)

    with resource_context(cold.state.server_context.resources):
        asyncio.run(read_cold())
    assert cold.state.server_user_pool.loaded_user_ids == ()
    print('shared_remember actual_actor_receipt=1 actual_source_item=1 replay=1 retry=1 cold=1 model_dispatches=0')
