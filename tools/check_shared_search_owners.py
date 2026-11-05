import argparse
import asyncio
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import httpx
import pytest


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
    from backend.memory_app.v2.search import _execution_records, _native_search_snapshot, _search_facts, validate_search_decision
    from backend.recognition import RecognitionConflict
    from backend.security.user_context import authorize_user, user_context
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from core.storage_provider.connection_scope import connection_scope

    devices = DeviceRegistry(root / 'server')
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    admin = devices.authenticate(devices.exchange(pairing['code'], name='搜索归属管理')['key'])
    users = ServerUsers(root, records=devices.records)
    user = users.create(admin, name='搜索归属用户')
    pairing = devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)
    token = devices.exchange(pairing['code'], name='搜索归属设备')['key']
    actor = devices.authenticate(token)

    def child(path, user_id, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
        child_factory=child, port=8013)
    registry = server.state.shared_projects
    project = registry.create(actor, project_name='搜索执行归属', operation_id='search-owner')['project']

    async def run():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server),
                    base_url='http://127.0.0.1:8013') as client:
                loaded = await client.get('/api/v2/projects', headers={'Authorization': 'Bearer ' + token})
                assert loaded.status_code == 200, loaded.text
                async with server.state.server_context.pool.lease(actor.user_id) as app:
                    with user_context(authorize_user(users, actor, actor.user_id)):
                        models = app.state.recognition_models
                        models.update('generation', {'expected_revision': models.public()['generation']['revision'],
                            'base_url': 'https://127.0.0.2:9/v1', 'model': 'gpt-4o-mini',
                            'api_key': 'isolated-no-wire-placeholder', 'enabled': True, 'allow_remote': False})
                response = await client.post('/api/v2/workbench/turns',
                    headers={'Authorization': 'Bearer ' + token, 'Idempotency-Key': 'question-' + uuid4().hex},
                    json={'project_id': project['id'], 'scope_kind': 'shared', 'intent': 'ask',
                        'text': '本项目有哪些资料？'})
                assert response.status_code == 200, response.text
                value = response.json()
                assert value['turn']['receipt']['ask']['no_match'] is True
                async with server.state.server_context.pool.lease(actor.user_id) as app:
                    with user_context(authorize_user(users, actor, actor.user_id)):
                        query = server.state.shared_query_factory.incoming_query(app, project['id'], scope_kind='shared')
                        records, store = app.state.recognition_records, app.state.ai_turn_store
                        assert _execution_records(query) is records
                        turn_id = value['turn']['id']
                        before = tuple(store.events_after(turn_id))
                        snapshot = _native_search_snapshot(_execution_records(query), turn_id)
                        assert snapshot.get_request(turn_id) == store.get_request(turn_id)
                        assert tuple(snapshot.events_after(turn_id)) == before
                        with pytest.raises(RecognitionConflict, match='rag_search_decision_changed'):
                            validate_search_decision(_execution_records(query), turn_id, 1, '缺少真实模型决策的查询')
                        with pytest.raises(RecognitionConflict, match='search_proof_missing'):
                            _search_facts(_execution_records(query), 'missing-search')
                        data = registry.roots._data(project['id'])
                        with pytest.raises(RecognitionConflict, match='route_correction_actual_owner_required'):
                            _native_search_snapshot(data.records, turn_id)
                        assert not tuple(data.root.rglob('ai-turns.sqlite3'))
                        assert tuple(store.events_after(turn_id)) == before
                        assert not any(event['type'].startswith('model.') for event in before)
                        print('shared-search actual public NoMatch/Core snapshot stays in actor namespace; '
                            'missing genuine model decision rejected; wrong source owner rejected; '
                            'source native database absent; existing native events unchanged; model dispatch0', flush=True)
        print('shared-search owner real server lifecycle closed', flush=True)

    with resource_context(server.state.server_context.resources), connection_scope():
        asyncio.run(run())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    check(args.root)
