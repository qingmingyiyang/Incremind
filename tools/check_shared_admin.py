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
import pytest


def check(base):
    workspace, base = Path(__file__).resolve().parents[1], Path(base).resolve()
    if base == workspace or any(part.name == 'runtime' for part in (base, *base.parents)):
        raise ValueError('shared_check_requires_isolated_root')
    root = base / ('a' + uuid4().hex[:6])
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(workspace / 'config/settings.toml.example', root / 'config/settings.toml')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.transaction_records import TransactionRecords
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.material_origin_admission import read_origin_item
    from backend.memory_app.v2.material_origin_context import freeze_personal_context
    from backend.memory_app.v2.material_origin_readers import qualified_record_read
    from backend.memory_app.v2.material_origins import ORIGIN_KIND
    from backend.memory_app.v2.policies import override
    from backend.memory_app.v2.policies.pipelines import versions_for_turn
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.shared_auxiliary import _qualified_commit
    from backend.memory_app.v2.shared_extraction import PersonalDestination
    from backend.memory_app.v2.shared_history import read_model_history_answer
    from backend.memory_app.v2.shared_projects import SharedProjectError
    from backend.memory_app.v2.shared_scopes import personal_history_scopes
    from backend.memory_app.v2.workbench import _answer_workbench
    from backend.memory_app.v2.workbench_receipts import persist_workbench_turn
    from backend.memory_app.workbench_identity_binding import capture_identity
    from backend.recognition import RecognitionConflict
    from backend.security.user_context import UserError, authorize_user, user_context
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from core.storage_provider.connection_scope import connection_scope

    devices = DeviceRegistry(root / 'server')
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    admin_token = devices.exchange(pairing['code'], name='真实代理管理设备')['key']
    admin = devices.authenticate(admin_token)
    users, actors, tokens = ServerUsers(root, records=devices.records), [], []
    for label in ('代理目标', '普通成员'):
        user = users.create(admin, name=label)
        pairing = devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)
        token = devices.exchange(pairing['code'], name=label)['key']
        actors.append(devices.authenticate(token))
        tokens.append(token)
    target, other = actors

    def child(path, user, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, root),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', root / 'users/local-user', root),
        child_factory=child, port=8013)
    registry, factory = server.state.shared_projects, server.state.shared_query_factory
    project = registry.create(admin, project_name='管理员代理空项目', operation_id='create-admin')['project']
    for actor, role in ((target, 'viewer'), (other, 'member')):
        registry.invite(admin, project['id'], user_id=actor.user_id, role=role,
            authority=project['authority'], operation_id='invite-' + actor.user_id)
        invitation = registry.invitations(actor)[0]
        registry.accept(actor, invitation['id'], expected_revision=invitation['revision'],
            operation_id='accept-' + actor.user_id)

    async def run():
        context = server.state.server_context
        async with server.router.lifespan_context(server):
            async with context.pool.lease(target.user_id) as app, context.pool.lease(other.user_id) as other_app:
                records, store = app.state.recognition_records, app.state.ai_turn_store
                access = authorize_user(users, admin, target.user_id)
                with user_context(access), context.pool.request_scope(target.user_id, app, access):
                    query = factory.query(app, admin, project['id'])
                    assert query.origin_admission.actor == admin
                    assert query.origin_admission.destination.owner_user_id == target.user_id
                    assert query.models is app.state.recognition_models and query.models.records is records
                    assert query.models.server_model_access.user_id == target.user_id
                    assert query.execution_owner is app.state.workspace_domains.query
                    assert query.receipt_records is records and query.profile_owner is query.execution_owner
                    identity, thread = 'turn-admin-' + uuid4().hex, 'thread-admin-' + uuid4().hex
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
                    request = store.get_request(identity)
                    bundle = store.restore_admission(request)
                    origin = next(item for item in bundle.items if item.kind == ORIGIN_KIND)
                    assert origin.scope['owner_user_id'] == target.user_id
                    assert origin.payload['actor_user_id'] == admin.user_id
                    assert origin.payload['execution']['owner_user_id'] == target.user_id
                    assert origin.payload['profile']['owner_user_id'] == target.user_id
                    lease, _ = read_origin_item(request, origin, actor_user_id=admin.user_id,
                        personal=query.origin_admission.personal)
                    assert lease.actor_user_id == admin.user_id and lease.device_id == admin.device_id
                    assert factory.dispatch(app, request) is query
                    now = datetime.now(timezone.utc).isoformat()
                    with _qualified_commit(query.origin_admission) as qualification, records.begin() as tx:
                        persist_workbench_turn(tx, turn_id=identity, project=project['id'], thread_id=thread,
                            cleaned=question, now=now, created_at=now, intent='ask', receipt=result['receipt'],
                            item_id=None, item=None, instance='admin-real', run_id=None, title_prefix=None,
                            replace_turn=None, research_state=None)
                        qualification._actor_reader(TransactionRecords(tx))
                        tx.commit()
                    frozen = freeze_personal_context(query, request, {'history': ''}, {'turns': [], 'text': ''})
                    assert frozen['owner_user_id'] == target.user_id
                    with _qualified_commit(query.origin_admission) as qualification:
                        with qualified_record_read(other_app.state.recognition_records,
                                validate=qualification._validate) as wrong:
                            with pytest.raises(RecognitionConflict):
                                qualification._actor_reader(TransactionRecords(wrong))
                    with pytest.raises(RecognitionConflict, match='material_origin_binding_changed'):
                        read_origin_item(request, origin, actor_user_id=admin.user_id,
                            personal={**query.origin_admission.personal, 'owner_user_id': other.user_id})
                    original = query.models.server_model_access
                    query.models.server_model_access = other_app.state.recognition_models.server_model_access
                    try:
                        with pytest.raises(SharedProjectError, match='shared_personal_owner_mismatch'):
                            query.validate_retrieval_origin()
                    finally:
                        query.models.server_model_access = original
                    original = app.state.recognition_models
                    app.state.recognition_models = other_app.state.recognition_models
                    try:
                        with pytest.raises(SharedProjectError, match='shared_personal_owner_mismatch'):
                            query.validate_retrieval_origin()
                    finally:
                        app.state.recognition_models = original
                    with pytest.raises(RecognitionConflict):
                        factory.query(other_app, admin, project['id'])
                with pytest.raises(UserError, match='user_forbidden'):
                    authorize_user(users, other, target.user_id)
                other_access = authorize_user(users, other, other.user_id)
                with user_context(other_access), context.pool.request_scope(other.user_id, other_app, other_access):
                    with pytest.raises(RecognitionConflict):
                        factory.query(app, other, project['id'])
                    before = tuple(store.events_after(identity))
                    with pytest.raises(RecognitionConflict, match='material_origin_execution_caller_changed'):
                        store.claim_turn(request)
                    assert tuple(store.events_after(identity)) == before
                owner_access = authorize_user(users, target, target.user_id)
                with user_context(owner_access), context.pool.request_scope(target.user_id, app, owner_access):
                    destination = PersonalDestination.from_application(registry, target, app)
                    assert personal_history_scopes(destination)[project['id']][0]['id'] == thread
                    current_query = factory.query(app, target, project['id'])
                    row = records.read('v2_turns', identity)
                    assert read_model_history_answer(current_query, identity, row.revision)['public'] == row
                    with pytest.raises(RecognitionConflict, match='material_origin_execution_caller_changed'):
                        factory.dispatch(app, request)
                native_before = tuple(store.events_after(identity))
                assert not any(event['type'].startswith('model.') for event in native_before)
                assert other_app.state.recognition_records.list('v2_turns') == ()
                assert registry.roots._data(project['id']).records.list('v2_turns') == ()
                print('shared-admin native: actual target NoMatch completed; caller/device remain admin; '
                      'execution/profile/model binding/history belong to target; wrong app/model/billing and ordinary cross-target refused', flush=True)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server),
                    base_url='http://127.0.0.1:8013') as client:
                response = await client.get('/api/v2/workbench/threads/' + thread,
                    params={'project_id': project['id']}, headers={'Authorization': 'Bearer ' + tokens[0]})
                assert response.status_code == 200 and identity in response.text
                response = await client.get('/api/v2/server/shared-projects/scopes',
                    headers={'Authorization': 'Bearer ' + admin_token, 'x-chriptmas-target-user': target.user_id})
                assert response.status_code == 200 and response.json()['items'][0]['can_ask']
                detail = registry.detail(admin, project['id'])
                member = next(row for row in detail['members'] if row['user_id'] == target.user_id)
                registry.change_member(admin, project['id'], user_id=target.user_id, action='remove',
                    expected_revision=member['revision'], authority=detail['project']['authority'], operation_id='remove-target')
                async with context.pool.lease(target.user_id) as app:
                    access = authorize_user(users, target, target.user_id)
                    with user_context(access), context.pool.request_scope(target.user_id, app, access):
                        with pytest.raises(ValueError):
                            read_model_history_answer(current_query, identity, row.revision)
                response = await client.post('/api/v2/devices/' + admin.device_id + '/revoke',
                    headers={'Authorization': 'Bearer ' + admin_token}, json={'expected_revision': admin.revision})
                assert response.status_code == 200
                async with context.pool.lease(target.user_id) as app:
                    with user_context(None), pytest.raises(RecognitionConflict, match='material_origin_identity_changed'):
                        factory.dispatch(app, request)
                response = await client.get('/api/v2/workbench/threads/' + thread,
                    params={'project_id': project['id']}, headers={'Authorization': 'Bearer ' + tokens[0]})
                assert response.status_code == 200 and identity in response.text
                response = await client.get('/api/v2/server/shared-projects/scopes',
                    headers={'Authorization': 'Bearer ' + tokens[0]})
                item = response.json()['items'][0]
                assert response.status_code == 200 and item['has_personal_history'] and not item['can_ask']
                assert item['membership'] == 'unavailable' and item['threads'][0]['id'] == thread
                assert tuple(store.events_after(identity)) == native_before
        print('shared-admin public: target own thread and authenticated admin target scopes read; '
              'actual server and child lifecycles closed; model events 0', flush=True)
    with resource_context(server.state.server_context.resources), connection_scope():
        asyncio.run(run())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    check(args.root)
