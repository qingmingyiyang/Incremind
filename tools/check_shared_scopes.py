import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
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
    from backend.memory_app.v2.policies import override
    from backend.memory_app.v2.policies.pipelines import versions_for_turn
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.shared_auxiliary import _qualified_commit
    from backend.memory_app.v2 import _LazyOrganization
    from backend.memory_app.v2.task_do import TaskDo
    from backend.memory_app.v2.task_drafts import TaskDrafts
    from backend.memory_app.source_egress import recognition_service
    from backend.memory_app.v2.workbench import _answer_workbench
    from backend.memory_app.v2.workbench_receipts import persist_workbench_turn
    from backend.memory_app.workbench_identity_binding import capture_identity
    from backend.memory_app.transaction_records import TransactionRecords
    from backend.security.user_context import authorize_user, user_context
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from core.storage_provider.connection_scope import connection_scope

    devices = DeviceRegistry(root / 'server')
    users = ServerUsers(root, records=devices.records)
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    admin = devices.authenticate(devices.exchange(pairing['code'], name='范围核验')['key'])
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
    registry, pool = server.state.shared_projects, server.state.server_user_pool
    project = registry.create(owner, project_name='只在当前资格可见的名称', operation_id='create-' + uuid4().hex)['project']
    registry.invite(owner, project['id'], user_id=viewer.user_id, role='viewer', authority=project['authority'],
        operation_id='invite-' + uuid4().hex)
    invitation = registry.invitations(viewer)[0]
    registry.accept(viewer, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-' + uuid4().hex)

    async def run():
        async with server.router.lifespan_context(server):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://127.0.0.1:8013') as client:
                headers = {'Authorization': 'Bearer ' + tokens[1]}
                path = '/api/v2/server/shared-projects/scopes'
                response = await client.get(path, headers=headers)
                assert response.status_code == 200, response.status_code
                current = response.json()['items']
                assert len(current) == 1 and current[0]['id'] == project['id']
                assert current[0]['role'] == 'viewer' and current[0]['membership'] == 'active'
                assert current[0]['can_ask'] and current[0]['can_read_sources'] and not current[0]['can_write']
                assert not current[0]['has_personal_history'] and current[0]['threads'] == []
                response = await client.get(path, headers={'Authorization': 'Bearer ' + tokens[2]})
                assert response.status_code == 200 and response.json()['items'] == []
                response = await client.get(path, headers={**headers, 'x-chriptmas-target-user': outsider.user_id})
                assert response.status_code == 403
                async with pool.lease(viewer.user_id) as app:
                    access = authorize_user(registry.users, viewer, viewer.user_id)
                    with user_context(access), pool.request_scope(viewer.user_id, app, access):
                        query = server.state.shared_query_factory.query(app, viewer, project['id'])
                        records, store = app.state.recognition_records, app.state.ai_turn_store
                        identity, thread = 'turn-scopes-' + uuid4().hex, 'thread-scopes-' + uuid4().hex
                        question = '这个空项目保存过哪些资料'
                        binding = capture_identity(app, records, app.state.recognition_documents,
                            project=project['id'], scene=None, thread_id=thread, turn_id=identity)
                        with override(compose='@1', retrieve='@1'):
                            policies = versions_for_turn('project.answer')
                        result = await query.answer_turns.run(turn_id=identity, project=project['id'], question=question,
                            policy_versions=policies, admission_prepare=query.origin_admission.prepare,
                            privacy_prepare=query.freeze_answer_privacy, workbench_identity=binding,
                            operation=lambda: _answer_workbench(replace(app.state.workspace_domains, query=query),
                                records, project['id'], question, None, thread, identity, None, None))
                        assert result['receipt']['ask']['no_match'] is True and not result['chosen']
                        now = datetime.now(timezone.utc).isoformat()
                        with _qualified_commit(query.origin_admission) as qualification, records.begin() as tx:
                            persist_workbench_turn(tx, turn_id=identity, project=project['id'], thread_id=thread,
                                cleaned=question, now=now, created_at=now, intent='ask', receipt=result['receipt'],
                                item_id=None, item=None, instance='scopes-real', run_id=None, title_prefix=None,
                                replace_turn=None, research_state=None)
                            qualification._actor_reader(TransactionRecords(tx))
                            tx.commit()
                        native_before = tuple(store.events_after(identity))
                        assert not any(event['type'].startswith('model.') for event in native_before)
                        organization = _LazyOrganization(app)
                        organization.method_query = query
                        research = TaskDo(records, query.models, TaskDrafts(records, app.state.recognition_documents),
                            organization, organization.read, organization.topology, query=query,
                            service=recognition_service(records),
                            execution_getter=lambda: (app.state.ai_runtime, app.state.ai_turn_runner))
                        pending_id, pending_thread = 'turn-pending-' + uuid4().hex, 'thread-pending-' + uuid4().hex
                        pending_text = '整理空项目中的待办事项'
                        pending_binding = capture_identity(app, records, app.state.recognition_documents,
                            project=project['id'], scene=None, thread_id=pending_thread, turn_id=pending_id)
                        initial, prepared = research.initial(pending_id, project['id'], pending_text, None,
                            workbench_identity=pending_binding, admission_prepare=query.origin_admission.prepare)
                        assert prepared['started'] is False and prepared['admission_bundle']
                        pending_native = prepared['request']['turn_id']
                        assert store.get_request(pending_native) is None
                        with _qualified_commit(query.origin_admission) as qualification, records.begin() as tx:
                            persist_workbench_turn(tx, turn_id=pending_id, project=project['id'], thread_id=pending_thread,
                                cleaned=pending_text, now=now, created_at=now, intent='do', receipt={'do': initial},
                                item_id=None, item=None, instance='scopes-prepared', run_id=None, title_prefix=None,
                                replace_turn=None, research_state=prepared)
                            qualification._actor_reader(TransactionRecords(tx))
                            tx.commit()
                response = await client.get(path, headers=headers)
                assert response.status_code == 200, (response.status_code, response.text)
                current = response.json()['items'][0]
                assert current['has_personal_history'] and current['threads'][0]['id'] == thread
                assert all(item['id'] != pending_thread for item in current['threads'])
                async with pool.lease(viewer.user_id) as app:
                    assert app.state.ai_turn_store.get_request(pending_native) is None
                    row = app.state.recognition_records.read('v2_turns', identity)
                    with app.state.recognition_records.begin() as tx:
                        changed = tx.put('v2_turns', identity, {**row.payload, 'user_text': '已接纳轮的正文变更'},
                            expected_revision=row.revision)
                        tx.commit()
                    response = await client.get(path, headers=headers)
                    assert response.status_code == 409
                    with app.state.recognition_records.begin() as tx:
                        tx.put('v2_turns', identity, dict(row.payload), expected_revision=changed.revision)
                        tx.commit()
                print('scopes: real TaskDo.initial and original public-save window stays unaccepted and uncounted; accepted identity corruption still refuses', flush=True)
                pairing = devices.issue_pairing(user_id=viewer.user_id, actor=viewer.device_id)
                new_token = devices.exchange(pairing['code'], name='历史新设备')['key']
                new_headers = {'Authorization': 'Bearer ' + new_token}
                response = await client.get(path, headers=new_headers)
                assert response.status_code == 200 and response.json()['items'][0]['threads'][0]['id'] == thread
                detail = registry.detail(owner, project['id'])
                member = next(row for row in detail['members'] if row['user_id'] == viewer.user_id)
                registry.change_member(owner, project['id'], user_id=viewer.user_id, action='remove',
                    expected_revision=member['revision'], authority=detail['project']['authority'],
                    operation_id='remove-' + uuid4().hex)
                for actual_headers in (headers, new_headers):
                    response = await client.get(path, headers=actual_headers)
                    assert response.status_code == 200, (response.status_code, response.text)
                    saved = response.json()['items'][0]
                    assert saved['id'] == project['id'] and saved['name'] == project['id']
                    assert saved['membership'] == 'unavailable' and saved['role'] is None
                    assert not saved['can_ask'] and not saved['can_write'] and not saved['can_read_sources']
                    assert saved['has_personal_history'] and saved['threads'][0]['id'] == thread
                    response = await client.get('/api/v2/workbench/threads/' + thread,
                        params={'project_id': project['id']}, headers=actual_headers)
                    assert response.status_code == 200
                    response = await client.get('/api/v2/server/shared-projects/' + project['id'], headers=actual_headers)
                    assert response.status_code == 403
                response = await client.get(path, headers={'Authorization': 'Bearer ' + tokens[2]})
                assert response.status_code == 200 and response.json()['items'] == []
                async with pool.lease(viewer.user_id) as app:
                    assert tuple(app.state.ai_turn_store.events_after(identity)) == native_before
                print('scopes: actual viewer role, own native history, second device and membership loss passed; own body remains readable', flush=True)
                print('scopes: no shared name disclosure after exit; no other-user history; model events 0 and native facts unchanged', flush=True)

    with connection_scope():
        asyncio.run(run())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    check(args.root)
