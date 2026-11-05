import argparse
import asyncio
import os
from pathlib import Path
import shutil
from threading import RLock
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def check(base):
    workspace = Path(__file__).resolve().parents[1]
    base = Path(base).resolve()
    if base == workspace or any(part.name == 'runtime' for part in (base, *base.parents)):
        raise ValueError('shared_mcp_check_requires_isolated_root')
    root = base / ('m' + uuid4().hex[:6])
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(workspace / 'config/settings.toml.example', root / 'config/settings.toml')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root

    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.processing_lease import ProcessingLease
    from backend.memory_app.source_egress import recognition_service
    from backend.memory_app.document_recognition import ensure_document_experience
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.external_agent_settings import replace_external_agent_settings
    from backend.memory_app.v2.privacy import set_private_project
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.workspace_confirmation import WorkspaceConfirmation
    from backend.memory_app.workspace_generation import _draft
    from backend.memory_app.workspace_items import WorkspaceItems
    from backend.recognition import WorkScope
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context

    devices = DeviceRegistry(root / 'server')
    users = ServerUsers(root, records=devices.records)
    pair = devices.issue_pairing(user_id='local-user', actor='install')
    admin = devices.authenticate(devices.exchange(pair['code'], name='原生共享核验管理端')['key'])
    member = users.create(admin, name='共享调用人')
    pair = devices.issue_pairing(user_id=member['user_id'], actor=admin.device_id)
    full_token = devices.exchange(pair['code'], name='共享调用人设备')['key']
    actor = devices.authenticate(full_token)

    def child(path, user, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
        child_factory=child, port=8027)
    registry = server.state.shared_projects
    project = registry.create(actor, project_name='原生共享目录核验',
        operation_id='create-native-mcp-project')['project']
    project_id = project['id']
    data = registry.roots._data(project_id)
    text = '蓝色斑马过冬物资包括干草和净水，存放在北侧仓库。'
    items = WorkspaceItems(data.records, ProcessingLease(data.records, 'workspace_items', 'mcp-real-source'), RLock())
    draft = _draft({'title': '蓝色斑马过冬物资', 'summary': text, 'topics': [], 'facts': [], 'todos': [],
        'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}, text)
    original = items.create_upload({'id': 'native-mcp-material', 'project_id': project_id, 'input_kind': 'text',
        'title': '蓝色斑马过冬物资', 'source_text': text, 'status': 'ready', 'draft': draft, 'error': None,
        'document_id': None, 'created_at': '2026-11-05T00:00:00Z'})
    document = WorkspaceConfirmation(data.root, data.records, data.documents).confirm(
        original['id'], project_id, original['revision'], lambda _: '# 蓝色斑马过冬物资\n\n## 摘要\n\n' + text)
    service = recognition_service(data.records)
    experience, _ = ensure_document_experience(data.documents, service, project_id, document['document_id'])
    scope = WorkScope('local-user', project_id)
    candidate = service.propose(scope=scope, content=text, source_experience_ids=[experience])
    service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer=actor.user_id)
    owner = server.state.server_context.native_mcp.credentials
    origin = 'https://shared-mcp-qa.invalid'
    owner.configure(admin, origin=origin, expected_revision=0)
    token = owner.issue(actor=actor, target=actor.user_id, client='codex',
        scopes=['memory:read', 'memory:remember', 'memory:propose', 'memory:use'],
        expires_at=owner.now() + 3600, name='原生共享核验')['key']

    async def run():
        async with server.router.lifespan_context(server):
            pool = server.state.server_user_pool
            async with pool.lease(actor.user_id) as app:
                records = app.state.recognition_records
                replace_external_agent_settings(records, {'allow_remote': True, 'include_profile': False,
                    'daily_limit': 20, 'clients': {'claude': True, 'codex': True}}, expected_revision=0)
                set_private_project(records, project_id, True, 0)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url=origin,
                    headers={'Authorization': 'Bearer ' + token}) as client:
                async with streamable_http_client(origin + '/mcp', http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool('projects', {'scope_kind': 'shared'})
                        assert not result.isError, result.content
                        handoff = result.structuredContent
                        selected = {'scope_kind': 'shared', 'project': project_id}
                        recalled = await session.call_tool('recall', {**selected, 'query': '蓝色斑马过冬物资'})
                        assert not recalled.isError, recalled.content
                        recall = recalled.structuredContent
                        entries = recall['result']['entries']
                        assert entries and all(source['namespace'] == handoff['result']['projects'][0]['namespace']
                            for entry in entries for source in entry['sources'])
                        evidence = {'turn_id': recall['turn_id'], 'id': entries[0]['id']}
                        opened = await session.call_tool('read', {'id': evidence})
                        assert not opened.isError, opened.content
                        assert text in opened.structuredContent['result']['entries'][0]['excerpt']
                        usage = await session.call_tool('report_use', {'turn_id': recall['turn_id'], 'ids': [evidence['id']]})
                        assert not usage.isError, usage.content
                        assert data.records.list('v2_shared_mcp_usage')
                        proposal = await session.call_tool('propose_insight', {**selected, 'text': '净水与干草一同准备。',
                            'conditions': ['适用于过冬物资'], 'evidence_ids': [evidence], 'idempotency_key': 'shared-proposal-1'})
                        assert not proposal.isError, proposal.content
                        pending = proposal.structuredContent['result']
                        assert pending['state'] == 'pending'
                        remembered = await session.call_tool('remember', {**selected, 'text': '第二批蓝色斑马原件。',
                            'idempotency_key': 'shared-original-1'})
                        assert not remembered.isError, remembered.content
                        item = remembered.structuredContent['result']
                        repeated = await session.call_tool('remember', {**selected, 'text': '第二批蓝色斑马原件。',
                            'idempotency_key': 'shared-original-1'})
                        assert repeated.structuredContent == remembered.structuredContent
                        forbidden = await client.get(f'/api/v2/server/shared-projects/{project_id}/library/insights')
                        assert forbidden.status_code == 403
                        set_private_project(data.records, project_id, True, 0)
                        denied = await session.call_tool('read', {'id': evidence})
                        assert denied.isError and denied.structuredContent is None
            stored = data.records.read('workspace_items', item['item_id'])
            assert stored.payload['source_text'] == '第二批蓝色斑马原件。' and stored.payload['status'] == 'staged'
            stored = data.records.read('recognition_candidates', pending['candidate_id'])
            assert stored.payload['conditions'] == ['适用于过冬物资'] and stored.payload['state'] == 'pending'
            dependencies = data.records.list('v2_external_input_dependencies')
            assert len(dependencies) == 1 and dependencies[0].payload['namespace'] == pending['namespace']
            assert not data.records.list('v2_external_agent_intakes')
            assert not (data.root / '.rebuild-data' / 'ai-turns.sqlite3').exists()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url=origin,
                    headers={'Authorization': 'Bearer ' + full_token}) as human:
                library = await human.get(f'/api/v2/server/shared-projects/{project_id}/library/sources')
                assert library.status_code == 200 and any(row['id'] == item['item_id'] for row in library.json()['items'])
                decision = await human.post(f'/api/v2/server/shared-projects/{project_id}/library/insights/{pending["candidate_id"]}/review',
                    json={'action': 'confirm', 'expected_revision': pending['revision'],
                        'authority': registry.detail(actor, project_id)['project']['authority']})
                assert decision.status_code == 200 and decision.json()['insight']['confirmed_by'] == actor.user_id
            assert handoff['result']['version'] == 'handoff@3'
            rows = handoff['result']['projects']
            assert len(rows) == 1 and rows[0]['id'] == project_id and rows[0]['scope_kind'] == 'shared'
            async with pool.lease(actor.user_id) as app:
                api = app.state.external_context
                ref, archive, outcome = api._completed(handoff['turn_id'])
                proof = archive['shared']['catalog']['proofs'][0]
                assert proof['namespace'] == rows[0]['namespace']
                assert proof['lease']['project_id'] == project_id
                delivery = api.records.read('v2_external_agent_deliveries', handoff['turn_id'])
                assert delivery.payload['immutable_ref'] == ref and delivery.payload['outcome_ref'] == outcome
                for turn in (handoff['turn_id'], recall['turn_id'], opened.structuredContent['turn_id']):
                    assert all(event['type'] != 'model.requested' for event in api.turns.events_after(turn))
                assert api.records.read('v2_external_agent_intakes', item['receipt_id']).payload['namespace'] == item['namespace']
                assert api.records.read('v2_external_agent_intakes', pending['receipt_id']).payload['evidence_ids'] == [evidence]
            print('shared-native-mcp: directory, recall, read, usage, pending proposal, staged original, replay, privacy; model_events=0', flush=True)

    asyncio.run(run())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    check(parser.parse_args().root)
