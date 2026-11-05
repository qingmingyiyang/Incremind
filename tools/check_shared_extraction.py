import argparse
import asyncio
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from threading import RLock
from time import perf_counter
from uuid import uuid4

import httpx


def check(base):
    workspace = Path(__file__).resolve().parents[1]
    base = Path(base).resolve()
    if base == workspace or any(part.name == 'runtime' for part in (base, *base.parents)):
        raise ValueError('shared_check_requires_isolated_root')
    root = base / ('x' + uuid4().hex[:6])
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(workspace / 'config/settings.toml.example', root / 'config/settings.toml')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root
    from backend.memory_app.app import create_app
    from backend.memory_app.document_recognition import ensure_document_experience
    from backend.memory_app.processing_lease import ProcessingLease
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.source_egress import SourceEgressService, recognition_service
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.shared_extraction import COMMANDS, COPIES, PREVIEWS
    from backend.memory_app.workspace_confirmation import WorkspaceConfirmation
    from backend.memory_app.workspace_generation import _draft
    from backend.memory_app.workspace_items import WorkspaceItems
    from backend.recognition import WorkScope
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from backend.shared.interprocess_lock import interprocess_file_lock
    from core.storage_provider import SQLiteStructuredRecordStore
    from core.storage_provider.connection_scope import connection_scope

    started = perf_counter()
    devices = DeviceRegistry(root / 'server')
    users = ServerUsers(root, records=devices.records)
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    admin = devices.authenticate(devices.exchange(pairing['code'], name='提取权限核验')['key'])
    actors, tokens = [], []
    for label in ('所有者', '只看成员', '非成员'):
        account = users.create(admin, name=label)
        pairing = devices.issue_pairing(user_id=account['user_id'], actor=admin.device_id)
        token = devices.exchange(pairing['code'], name=label)['key']
        actors.append(devices.authenticate(token))
        tokens.append(token)
    owner, viewer, outsider = actors

    def child(path, user, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
        child_factory=child, port=8013)
    registry = server.state.shared_projects
    project = registry.create(owner, project_name='共享提取真实验收', operation_id='create-' + uuid4().hex)['project']
    registry.invite(owner, project['id'], user_id=viewer.user_id, role='viewer', authority=project['authority'],
        operation_id='invite-' + uuid4().hex)
    invitation = registry.invitations(viewer)[0]
    registry.accept(viewer, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-' + uuid4().hex)
    data = registry.roots._data(project['id'])
    items = WorkspaceItems(data.records, ProcessingLease(data.records, 'workspace_items', 'extraction-real'), RLock())
    text = '共享原始资料。认识甲与认识乙来自同一份已保留资料。'
    draft = _draft({'title': '提取依据', 'summary': text, 'topics': [], 'facts': [], 'todos': [],
        'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}, text)
    uploaded = items.create_upload({'id': 'extraction-item', 'project_id': project['id'], 'input_kind': 'text',
        'title': '提取依据', 'source_text': text, 'status': 'ready', 'draft': draft, 'error': None,
        'document_id': None, 'created_at': '2026-11-05T00:00:00Z'})
    document = WorkspaceConfirmation(data.root, data.records, data.documents).confirm(
        uploaded['id'], project['id'], uploaded['revision'], lambda value: text)
    service = recognition_service(data.records)
    experience, _ = ensure_document_experience(data.documents, service, project['id'], document['document_id'])
    scope = WorkScope('local-user', project['id'])
    insights = []
    for content in ('共享认识甲', '共享认识乙'):
        candidate = service.propose(scope=scope, content=content, source_experience_ids=[experience])
        insights.append(service.publish(scope=scope, candidate_id=candidate.id,
            expected_revision=candidate.revision, reviewer=owner.user_id))
    SourceEgressService(data.records).set_policy(scope, 'recognition', insights[1].id,
        insights[1].revision, 0, [])

    async def run():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server),
                    base_url='http://127.0.0.1:8013') as client:
                headers = {'Authorization': 'Bearer ' + tokens[1]}
                other = {'Authorization': 'Bearer ' + tokens[2]}
                target = await client.post('/api/v2/projects', headers=headers, json={'name': '提取目标'})
                assert target.status_code == 200, target.status_code
                target_id = target.json()['id']
                other_target = await client.post('/api/v2/projects', headers=other, json={'name': '非成员目标'})
                assert other_target.status_code == 200
                print(f'extraction: actual server and personal target {perf_counter() - started:.2f}s', flush=True)
                base_url = '/api/v2/server/shared-projects'
                payload = {'target_project_id': target_id, 'recognition_ids': None}
                denied = await client.post(base_url + '/' + project['id'] + '/extractions', headers=other,
                    json={**payload, 'target_project_id': other_target.json()['id']})
                assert denied.status_code == 403, denied.status_code
                key = 'extract-command-' + uuid4().hex
                command_url = base_url + '/extraction-commands/' + key
                response = await client.get(command_url, headers=headers)
                assert response.status_code == 200 and response.json() == {'command_id': key, 'state': 'unreceived'}
                keyed_headers = {**headers, 'Idempotency-Key': key}
                # 首次响应正文不参与恢复，客户端只凭已保留的原命令读取实际结果。
                response = await client.post(base_url + '/' + project['id'] + '/extractions', headers=keyed_headers, json=payload)
                assert response.status_code == 200, response.status_code
                response = await client.get(command_url, headers=headers)
                assert response.status_code == 200 and response.json()['state'] == 'received'
                recovered_id = response.json()['preview_id']
                response = await client.get(base_url + '/extractions/' + recovered_id, headers=headers)
                assert response.status_code == 200
                preview = response.json()
                response = await client.post(base_url + '/' + project['id'] + '/extractions', headers=keyed_headers, json=payload)
                assert response.status_code == 200 and response.json() == preview
                response = await client.post(base_url + '/' + project['id'] + '/extractions', headers=keyed_headers,
                    json={**payload, 'recognition_ids': [insights[0].id]})
                assert response.status_code == 409
                async with server.state.server_user_pool.lease(viewer.user_id) as app:
                    records = app.state.recognition_records
                    assert len(records.list(PREVIEWS)) == 1
                    assert SQLiteStructuredRecordStore(records.database_path).read(COMMANDS, key).payload['preview_id'] == recovered_id
                    with interprocess_file_lock(app.state.recognition_runtime_root / '.rebuild-data' /
                            'shared-extraction-commands' / key, timeout_seconds=0):
                        response = await client.get(command_url, headers=headers)
                        assert response.status_code == 409 and response.json()['detail'] == 'shared_extraction_preview_in_progress'
                response = await client.get(command_url, headers=other)
                assert response.status_code == 200 and response.json() == {'command_id': key, 'state': 'unreceived'}
                print('extraction: original command recovers actual preview without first response body; replay, conflict and real busy lock passed', flush=True)
                assert preview['recognition_ids'] == sorted(item.id for item in insights)
                assert any(item['private'] for item in preview['materials'])
                path = base_url + '/extractions/' + preview['id']
                get = await client.get(path, headers=headers)
                assert get.status_code == 200 and get.json() == preview
                denied = await client.get(path, headers=other)
                assert denied.status_code == 404
                wrong = {'expected_revision': preview['revision'] + 1,
                    'confirmed_ids': preview['recognition_ids'], 'batch_size': 1}
                response = await client.post(path + '/adopt', headers=headers, json=wrong)
                assert response.status_code == 409, response.status_code
                wrong['expected_revision'] = preview['revision']
                response = await client.post(path + '/adopt', headers=headers,
                    json={**wrong, 'confirmed_ids': preview['recognition_ids'][:1]})
                assert response.status_code == 409
                response = await client.post(path + '/adopt', headers=headers, json={**wrong, 'owner_user_id': outsider.user_id})
                assert response.status_code == 400
                response = await client.post(path + '/adopt', headers=headers, json=wrong)
                assert response.status_code == 202, (response.status_code, response.text)
                progress = response.json()
                assert progress['completed'] == 1 and progress['remaining'] == 1 and progress['state'] == 'pending'
                response = await client.get(path + '/progress', headers=headers)
                assert response.status_code == 202 and response.json() == progress
                response = await client.post(path + '/adopt', headers=headers, json=wrong)
                assert response.status_code == 200, (response.status_code, response.text)
                progress = response.json()
                assert progress['completed'] == 2 and progress['remaining'] == 0 and progress['state'] == 'completed'
                before = list(progress['copies'])
                response = await client.post(path + '/adopt', headers=headers, json=wrong)
                assert response.status_code == 200 and response.json()['copies'] == before
                async with server.state.server_user_pool.lease(viewer.user_id) as app:
                    records = app.state.recognition_records
                    copies = records.list(COPIES)
                    assert len(copies) == 2
                    personal = recognition_service(records)
                    for copy in copies:
                        result = personal.get_recognition(scope=WorkScope('local-user', target_id),
                            recognition_id=copy.payload['target_recognition_id'])
                        assert result.authorized and result.effective_state == 'active'
                        assert result.content in {'共享认识甲', '共享认识乙'}
                        if result.content == '共享认识乙':
                            snapshot = SourceEgressService(records).snapshot(WorkScope('local-user', target_id),
                                [{'type': 'recognition', 'id': result.id, 'revision': result.revision}])
                            assert all(not node['effective_purposes'] for node in snapshot['nodes'])
                    assert records.read(PREVIEWS, preview['id']).payload['confirmed_ids'] == preview['recognition_ids']
                activities = registry.activity(viewer, project['id'])
                assert sum(item['action'] == 'extract' for item in activities) == 2
                response = await client.post(base_url + '/' + project['id'] + '/extractions', headers=headers,
                    json={**payload, 'recognition_ids': [insights[0].id]})
                assert response.status_code == 200
                stale = response.json()
                service.revise(scope=scope, recognition_id=insights[0].id,
                    expected_revision=insights[0].revision, content='共享认识甲已更新')
                response = await client.post(base_url + '/extractions/' + stale['id'] + '/adopt', headers=headers,
                    json={'expected_revision': stale['revision'], 'confirmed_ids': stale['recognition_ids'], 'batch_size': 1})
                assert response.status_code == 409
                response = await client.get(command_url, headers=headers)
                assert response.status_code == 409
                async with server.state.server_user_pool.lease(viewer.user_id) as app:
                    assert len(app.state.recognition_records.list(COPIES)) == 2
                print('extraction: actual source revision refuses stale adoption without another personal copy', flush=True)
                details = registry.detail(owner, project['id'])
                member = next(item for item in details['members'] if item['user_id'] == viewer.user_id)
                registry.change_member(owner, project['id'], user_id=viewer.user_id, action='remove',
                    expected_revision=member['revision'], authority=details['project']['authority'],
                    operation_id='remove-' + uuid4().hex)
                response = await client.get(path, headers=headers)
                assert response.status_code == 403
                response = await client.get(path + '/progress', headers=headers)
                assert response.status_code == 200 and response.json()['state'] == 'completed'
                for copy in before:
                    endpoint = base_url + '/extracted-insights/' + copy['id']
                    response = await client.get(endpoint, headers=headers)
                    assert response.status_code == 200 and response.json()['target_project_id'] == target_id
                    response = await client.get(endpoint, headers=other)
                    assert response.status_code == 404
                assert data.records.list('v2_turns') == ()
                assert data.records.list('workspace_ask_receipts') == ()
                print('extraction: viewer preview, explicit confirmation, bounded resume, own active copies, activity and post-exit reads passed', flush=True)
        for path in root.glob('users/*/.rebuild-data/ai-turns.sqlite3'):
            with sqlite3.connect(path) as connection:
                assert connection.execute('SELECT COUNT(*) FROM ai_turn_events').fetchone()[0] == 0
        print('extraction: native model events 0; no provider calls', flush=True)

    with connection_scope():
        asyncio.run(run())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    check(args.root)
