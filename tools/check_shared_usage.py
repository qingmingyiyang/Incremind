import argparse
import asyncio
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from threading import RLock
from uuid import uuid4

import httpx


def check(base):
    workspace, base = Path(__file__).resolve().parents[1], Path(base).resolve()
    if base == workspace or any(part.name == 'runtime' for part in (base, *base.parents)):
        raise ValueError('shared_check_requires_isolated_root')
    root = base / ('u' + uuid4().hex[:6])
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(workspace / 'config/settings.toml.example', root / 'config/settings.toml')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.processing_lease import ProcessingLease
    from backend.memory_app.workspace_items import WorkspaceItems
    from backend.memory_app.workspace_confirmation import WorkspaceConfirmation
    from backend.memory_app.workspace_generation import _draft
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.privacy import set_private_project
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.usage import read_usage_count
    from backend.memory_app.v2.usage import UsageService
    from backend.memory_app.v2.links import InsightLinks
    from backend.memory_app.v2.shared_usage import SharedUsage
    from backend.memory_app.source_egress import recognition_service
    from backend.recognition import WorkScope
    from backend.security.user_context import authorize_user, user_context
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from core.storage_provider.connection_scope import connection_scope

    devices = DeviceRegistry(root / 'server')
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    admin = devices.authenticate(devices.exchange(pairing['code'], name='管理设备')['key'])
    users = ServerUsers(root, records=devices.records)
    actors = []
    for name in ('共享使用所有者', '共享使用只看成员', '共享使用非成员'):
        user = users.create(admin, name=name)
        pairing = devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)
        token = devices.exchange(pairing['code'], name=name)['key']
        actors.append((devices.authenticate(token), token))
    (owner, owner_token), (viewer, viewer_token), (outsider, outsider_token) = actors

    def child(path, user, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
        child_factory=child, port=8013)
    registry = server.state.shared_projects
    project = registry.create(owner, project_name='共享使用记录实际检查', operation_id='create-usage')['project']
    registry.invite(owner, project['id'], user_id=viewer.user_id, role='viewer',
        authority=project['authority'], operation_id='invite-viewer')
    invitation = registry.invitations(viewer)[0]
    registry.accept(viewer, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-viewer')
    data = registry.roots._data(project['id'])
    items = WorkspaceItems(data.records, ProcessingLease(data.records, 'workspace_items', 'usage-check'), RLock())
    text = '实际共享正文'
    draft = _draft({'title': '共享私密整理稿', 'summary': text, 'topics': [], 'facts': [], 'todos': [],
        'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}, text)
    item = items.create_upload({'id': 'usage-item', 'project_id': project['id'], 'input_kind': 'text',
        'title': '共享私密整理稿', 'source_text': text, 'status': 'ready', 'draft': draft, 'error': None,
        'document_id': None, 'created_at': '2026-11-05T00:00:00Z'})
    document = WorkspaceConfirmation(data.root, data.records, data.documents).confirm(
        item['id'], project['id'], item['revision'], lambda value: '# 共享私密整理稿\n\n' + text)
    set_private_project(data.records, project['id'], True, 0)
    body = {'project_id': project['id'], 'scope_kind': 'shared', 'kind': 'document', 'id': document['document_id']}

    async def run():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server),
                    base_url='http://127.0.0.1:8013') as client:
                async def opened(token, value):
                    response = await client.post('/api/v2/usage/open', json=value,
                        headers={'Authorization': 'Bearer ' + token})
                    return response
                response = await opened(owner_token, body)
                assert response.status_code == 204, response.text
                assert read_usage_count(data.records, 'document', document['document_id'], project['id']) == 1
                activity = registry.activity(viewer, project['id'])
                uses = [item for item in activity if item['action'] == 'use']
                assert len(uses) == 1 and uses[0]['actor_user_id'] == owner.user_id
                response = await opened(viewer_token, body)
                assert response.status_code == 204, response.text
                assert read_usage_count(data.records, 'document', document['document_id'], project['id']) == 1
                assert registry.activity(viewer, project['id']) == activity
                response = await opened(outsider_token, body)
                assert response.status_code == 403, response.text
                response = await opened(owner_token, {**body, 'scope_kind': 'personal'})
                assert response.status_code == 404, response.text
                response = await opened(owner_token, {**body, 'scope_kind': 'invalid'})
                assert response.status_code == 400, response.text
                for actor in (owner, viewer, outsider):
                    app = server.state.server_context.pool._children[actor.user_id].application
                    assert app.state.recognition_records.list('v2_usage_document') == ()
                    path = app.state.recognition_runtime_root / '.rebuild-data/ai-turns.sqlite3'
                    if path.exists():
                        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
                        try:
                            assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0
                        finally:
                            connection.close()
                app = server.state.server_context.pool._children[owner.user_id].application
                records = app.state.recognition_records
                with user_context(authorize_user(registry.users, owner, owner.user_id)):
                    with records.begin() as tx:
                        tx.put('v2_projects', project['id'], {'name': '同编号个人项目', 'scenes': [],
                            'private': False, 'builtin': None}, expected_revision=0)
                        tx.commit()
                    service = recognition_service(records)
                    def recognition(project_id, text):
                        scope = WorkScope('local-user', project_id)
                        experience = service.stage_experience(scope=scope, content=text,
                            provenance={'kind': 'user_statement', 'actor': 'local-user'})
                        candidate = service.propose(scope=scope, content=text, source_experience_ids=[experience])
                        return service.publish(scope=scope, candidate_id=candidate.id,
                            expected_revision=candidate.revision, reviewer=owner.user_id)
                    persona = recognition('me', '缓存读取必须保留真实来源资格')
                    personal = recognition(project['id'], '缓存读取必须保留真实来源资格并核对项目')
                    links = InsightLinks(records, service, app.state.recognition_models)
                    neighbors = links.discover(project['id'], persona.id)
                    assert any(value['other_id'] == personal.id and value['state'] == 'active' for value in neighbors)
                    UsageService(records).initialize('insight', personal.id, project['id'])
                    prior = records.read('v2_usage_insight', personal.id)
                    before_profile = read_usage_count(records, 'insight', persona.id, 'me') or 0
                    query = server.state.shared_query_factory.incoming_query(app, project['id'], scope_kind='shared')
                    SharedUsage(query)._profile_usage({'layer': 'L3', 'project_id': 'me',
                        'entry': {'id': persona.id, 'revision': persona.revision}}, turn_id=None)
                    assert read_usage_count(records, 'insight', persona.id, 'me') == before_profile + 1
                    assert records.read('v2_usage_insight', personal.id) == prior
                    assert data.records.list('v2_usage_insight') == ()
                    path = app.state.recognition_runtime_root / '.rebuild-data/ai-turns.sqlite3'
                    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
                    try:
                        assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0
                    finally:
                        connection.close()
                assert data.records.list('v2_turns') == ()
                print('shared-usage actual profile use incremented; real related insight in same-id personal project unchanged; no model citation claimed', flush=True)
        print('shared-usage actual HTTP owner source count/activity; viewer private human read without writes; outsider403/personal404; actor counters absent; model attempts 0', flush=True)

    with resource_context(server.state.server_context.resources), connection_scope():
        asyncio.run(run())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    check(args.root)
