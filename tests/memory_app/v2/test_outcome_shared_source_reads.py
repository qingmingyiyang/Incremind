import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import os
from pathlib import Path
import shutil
from uuid import uuid4

import pytest


def test_real_factory_outcome_source_uses_original_shared_uow(tmp_path_factory):
    _isolated_factory_case(tmp_path_factory, check_human=False)


def test_real_shared_no_match_outcome_changes_use_current_human_lease(tmp_path_factory):
    _isolated_factory_case(tmp_path_factory, check_human=True)


def _isolated_factory_case(tmp_path_factory, *, check_human):
    allocated = tmp_path_factory.mktemp('outcome-source').resolve(strict=True)
    assert not any(path.name == 'runtime' for path in (allocated, *allocated.parents))
    base = allocated / ('s' + uuid4().hex[:8])
    (base / 'config').mkdir(parents=True)
    repo = Path(__file__).resolve().parents[3]
    shutil.copyfile(repo / 'config/settings.toml.example', base / 'config/settings.toml')
    prior = {key: os.environ.get(key) for key in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ['CHRIPTMAS_APP_ROOT'], os.environ['CHRIPTMAS_DEPLOY'] = str(base.resolve(strict=True)), 'server'
    try:
        _check_factory(base, check_human=check_human)
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _check_factory(base, *, check_human):
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.transaction_records import TransactionRecords
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.material_origin_readers import qualified_record_read
    from backend.memory_app.v2.outcome_source_reads import SourceReadRecords
    from backend.memory_app.v2.outcome_source_preparation import _task_context, answer_input_context
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.shared_projects import SharedProjectError
    from backend.memory_app.v2.shared_auxiliary import _qualified_commit
    from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
    from backend.security.user_context import authorize_user, user_context
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from core.storage_provider.sqlite_uow import begin_read

    devices = DeviceRegistry(base / 'server')
    users = ServerUsers(base, records=devices.records)
    pair = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='来源检查管理员')
    admin = devices.authenticate(pair['key'])
    actors = []
    for name in ('来源检查所有者', '来源检查成员'):
        account = users.create(admin, name=name)
        pairing = devices.exchange(devices.issue_pairing(user_id=account['user_id'], actor=admin.device_id)['code'], name=name)
        actors.append(devices.authenticate(pairing['key']))
    owner, viewer = actors

    def child(path, user_id, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, base),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', base / 'users/local-user', base), child_factory=child, port=8015)
    registry = server.state.shared_projects
    project = registry.create(owner, project_name='成果来源检查', operation_id='create-' + uuid4().hex)['project']
    registry.invite(owner, project['id'], user_id=viewer.user_id, role='viewer', authority=project['authority'],
        operation_id='invite-' + uuid4().hex)
    invitation = registry.invitations(viewer)[0]
    registry.accept(viewer, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-' + uuid4().hex)
    data = registry.roots._data(project['id'])
    source = RecognitionService(data.records)
    scope = WorkScope('local-user', project['id'])
    experience = source.stage_experience(scope=scope, content='人工输入的共享资料用于核对来源。')
    candidate = source.propose(scope=scope, content='逐项核对实际来源', source_experience_ids=[experience])
    insight = source.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer=owner.user_id)

    async def scenario():
        async with server.router.lifespan_context(server):
            factory, pool = server.state.shared_query_factory, server.state.server_user_pool
            if check_human:
                from httpx import ASGITransport, AsyncClient
                async with AsyncClient(transport=ASGITransport(app=server), base_url='http://test',
                        headers={'Authorization': 'Bearer ' + pairing['key']}) as first_request:
                    loaded = await first_request.get('/api/v2/projects')
                    assert loaded.status_code == 200, loaded.text
            async with pool.lease(viewer.user_id) as app:
                access = authorize_user(registry.users, viewer, viewer.user_id)
                with user_context(access), pool.request_scope(viewer.user_id, app, access):
                    query = factory.query(app, viewer, project['id'])
                    query.answer_turns._runtime()
                    freshness = app.state.product_outcome_freshness
                    from backend.shared.server_resources import RESOURCE_POOL
                    daily_context = app.state.memory_daily_jobs.task.get_context()
                    resources_bound = daily_context.get(RESOURCE_POOL) is server.state.server_context.resources
                    print('daily_actual_resources_bound=' + str(resources_bound), flush=True)
                    if check_human:
                        assert resources_bound
                        await _check_human_routes(server, registry, devices, viewer, pairing['key'], app)
                        return
                    material = {'type': 'recognition', 'id': insight.id, 'revision': insight.revision,
                        'project_id': project['id'], 'namespace': query.source_namespace,
                        'role': 'evidence', 'proof_ref': {'collection': 'recognitions', 'object_id': insight.id,
                            'revision': insight.revision}, 'snapshot': None}
                    with _qualified_commit(query.origin_admission) as original:
                        with freshness._read_scope() as actor:
                            assert isinstance(actor, SourceReadRecords)
                            selected = freshness._material_owner(material, actor)
                            assert selected is query.material_owner
                            actual = freshness._source_reader(selected, actor)
                            assert actual._transaction is original._source_reader
                            frozen = freshness._freeze_material(selected, material, reader=actor)
                            assert len(frozen) == 1 and frozen[0]['verified']
                            assert frozen[0]['origin']['namespace'] == query.source_namespace
                            assert frozen[0]['frozen']['id'] == insight.id
                            personal = freshness._source_owner(project['id'])
                            assert personal.records is freshness.records
                            assert freshness._fresh_owner(actor, {'project_id': project['id'],
                                'scope_origin': freshness._origin(personal), 'native_request': None}).records is freshness.records
                            with pytest.raises(RecognitionConflict, match='outcome_material_namespace_unavailable'):
                                freshness._material_owner({key: value for key, value in material.items() if key != 'namespace'}, actor)
                            with pytest.raises(RecognitionConflict, match='outcome_shared_task_material_proof_unavailable'):
                                _task_context(freshness, freshness._native(), {}, {}, [], actor)
                            with pytest.raises(RecognitionConflict, match='outcome_shared_answer_input_unavailable'):
                                answer_input_context(freshness, freshness._native(), {'turn_id': 'absent-native-input'},
                                    'answer-model-input-answer', [], actor)
                            with qualified_record_read(data.records, validate=original._validate) as another:
                                with pytest.raises(RecognitionConflict, match='material_origin_commit_source_reader_changed'):
                                    original.source_records(selected, TransactionRecords(another))
                            copied = copy_context()
                            with ThreadPoolExecutor(max_workers=1) as worker:
                                with pytest.raises(RecognitionConflict, match='material_origin_commit_scope_expired'):
                                    worker.submit(copied.run, actor.validate).result()
                            assert freshness._freeze_material(selected, material, reader=actor) == frozen
                    with pytest.raises(RecognitionConflict, match='outcome_source_read_scope_expired'):
                        actor.validate()
                    request = _claim_original_request(app, query, project['id'])
                    with freshness._read_scope(request=request) as resumed:
                        assert isinstance(resumed, SourceReadRecords)
                        assert resumed.query.origin_admission.lease == query.origin_admission.lease
                        assert freshness._freeze_material(freshness._material_owner(material, resumed),
                            material, reader=resumed) == frozen
                    with begin_read(freshness.records) as actor_only:
                        with pytest.raises(RecognitionConflict, match='outcome_source_scope_required_before_actor_transaction'):
                            with freshness._read_scope(actor_only, request=request):
                                raise AssertionError('actor_transaction_was_upgraded_to_source_scope')
                        with pytest.raises(RecognitionConflict, match='outcome_source_selection_unavailable'):
                            freshness._source_owner(project['id'], origin=frozen[0]['origin'], reader=actor_only)
                    revised = source.revise(scope=scope, recognition_id=insight.id, expected_revision=insight.revision,
                        content='修订后的人工共享认识')
                    from backend.memory_app.v2.steer_learning_privacy import qualify_publication_sources
                    with _qualified_commit(query.origin_admission), freshness._read_scope() as actor:
                        qualified = freshness._qualify_source_proofs(frozen, reader=actor, local_only=True)
                        assert qualified['complete'] is True and qualified['recognitions'] == []
                        assert qualified['current_sources'][0]['privacy_only'] is True
                        assert qualified['current_sources'][0]['material']['revision'] == revised.revision
                        with pytest.raises(RecognitionConflict, match='publication_privacy_scope_required'):
                            qualify_publication_sources(freshness, frozen, reader=actor, local_only=True)
                    from backend.memory_app.v2.privacy import set_private_project
                    set_private_project(data.records, project['id'], True, 0)
                    with _qualified_commit(query.origin_admission), freshness._read_scope() as actor:
                        private = freshness._qualify_source_proofs(frozen, reader=actor, local_only=True)
                        assert private['complete'] is True and private['privacy_ceiling']['effective_purposes'] == []
                    details = registry.detail(owner, project['id'])
                    membership = next(row for row in details['members'] if row['user_id'] == viewer.user_id)
                    registry.change_member(owner, project['id'], user_id=viewer.user_id, action='remove',
                        expected_revision=membership['revision'], authority=details['project']['authority'],
                        operation_id='remove-' + uuid4().hex)
                    with pytest.raises(SharedProjectError) as denied:
                        with _qualified_commit(query.origin_admission):
                            raise AssertionError('revoked_membership_was_accepted')
                    assert denied.value.status == 403
                    with pytest.raises(SharedProjectError):
                        with freshness.source_scope(request=request):
                            raise AssertionError('old_native_lease_was_revived')
                    with app.state.ai_turn_store._connect() as connection:
                        assert connection.execute('SELECT COUNT(*) FROM ai_turns').fetchone()[0] == 1
                        assert connection.execute("SELECT COUNT(*) FROM ai_turn_events WHERE json_extract(event_json,'$.type') LIKE ?",
                            ('model.%',)).fetchone()[0] == 0

    asyncio.run(scenario())


def _claim_original_request(app, query, project):
    from datetime import datetime, timezone
    from backend.memory_app.kernel.turn_requests import freeze_turn_request
    from backend.memory_app.v2.material_origin_admission import BoundMaterialOrigins
    from backend.memory_app.v2.material_origin_privacy import freeze_origin_materials
    from backend.memory_app.workbench_identity_binding import prepare_admission

    native = app.state.ai_turn_store
    identity = 'source-read-' + uuid4().hex
    origins = BoundMaterialOrigins(query.origin_admission, query.origin_admission.lease, {})
    _, privacy, _ = freeze_origin_materials(origins, query.models, project, [], local_only=True)
    request = freeze_turn_request('project.answer', turn_id=identity, session_id='session-' + identity,
        operation_id='operation-' + identity, idempotency_key='key-' + identity, project_id=project,
        created_at=datetime.now(timezone.utc).isoformat(), text='原生共享读取权限检查', privacy=privacy,
        capabilities=['workbench.answer.execute'], template_version=2)
    request['context_policy'].update(include_memory=False)
    request['capability_request'] = {'mode': 'execute_exact_v1', 'capability_id': 'workbench.answer.execute',
        'arguments': {'query': request['input']['text']}}
    bundle = prepare_admission(app, request, None, native, admission_prepare=query.origin_admission.prepare)
    with native.admission_bundle(request, bundle):
        assert native.claim_turn(request) == (identity, True)
    return native.get_request(identity)


async def _check_human_routes(server, registry, devices, viewer, device_key, app):
    from httpx import ASGITransport, AsyncClient
    from backend.recognition import RecognitionConflict
    from backend.memory_app.v2.material_origins import ORIGIN_KIND
    from backend.memory_app.v2.outcome_source_reads import SourceReadRecords

    project = registry.create(viewer, project_name='共享历史空召回检查', operation_id='create-' + uuid4().hex)['project']
    models = app.state.recognition_models
    models.update('generation', {'expected_revision': models.public()['generation']['revision'],
        'base_url': 'https://127.0.0.2:9/v1', 'model': 'gpt-4o-mini',
        'api_key': 'isolated-no-wire-placeholder', 'enabled': True, 'allow_remote': False})
    async with AsyncClient(transport=ASGITransport(app=server), base_url='http://test',
            headers={'Authorization': 'Bearer ' + device_key}) as client:
        response = await client.post('/api/v2/workbench/turns', json={'project_id': project['id'],
            'scope_kind': 'shared', 'intent': 'ask', 'text': '共享项目中没有任何资料的合成问题'},
            headers={'Idempotency-Key': 'source-human-ask-' + uuid4().hex})
        assert response.status_code == 200, response.text
        turn = response.json()['turn']
        assert turn['receipt']['ask']['no_match'] is True
        assert turn['scope_kind'] == 'shared'
        native_request = app.state.ai_turn_store.get_request(turn['id'])
        assert native_request is not None
        origins = [item for item in app.state.ai_turn_store.restore_admission(native_request).items
            if item.kind == ORIGIN_KIND]
        assert len(origins) == 1
        assert origins[0].payload['actor_user_id'] == origins[0].payload['execution']['owner_user_id'] == viewer.user_id
        history = await client.get('/api/v2/workbench/threads/' + response.json()['thread_id'],
            params={'project_id': project['id'], 'scope_kind': 'shared'})
        assert history.status_code == 200, history.text
        assert [(row['id'], row['scope_kind']) for row in history.json()['turns']] == [(turn['id'], 'shared')]
        saved = await client.post('/api/v2/workbench/turns/' + turn['id'] + '/save-outcome',
            json={'project_id': project['id']})
        assert saved.status_code == 200, saved.text
        document_id = saved.json()['document_id']
        assert app.state.recognition_records.read('documents', document_id) is not None
        assert registry.roots._data(project['id']).records.read('documents', document_id) is None
        base = '/api/v2/library/outcomes/' + document_id
        before = await client.get(base + '/changes', params={'project_id': project['id']})
        assert before.status_code == 200, before.text
        original = before.json()
        assert original['verified'] is True and original['fresh_verified'] is True
        assert original['fresh_count'] == 0
        projection = app.state.recognition_records.read('v2_outcome_fresh', document_id)
        assert projection.payload['source_origin']['namespace'].startswith('shared/' + project['id'] + '/')
        another = devices.exchange(devices.issue_pairing(user_id=viewer.user_id, actor=viewer.device_id)['code'],
            name='当前设备读取历史')
        current_key = another['key']
        newer = await client.get(base + '/changes', params={'project_id': project['id']},
            headers={'Authorization': 'Bearer ' + current_key})
        assert newer.status_code == 200, newer.text
        assert newer.json() == original
        ack = await client.post(base + '/ack', json={'project_id': project['id'],
            'expected_revision': original['ack_revision'], 'observation': original['observation']},
            headers={'Authorization': 'Bearer ' + current_key})
        assert ack.status_code == 200, ack.text
        assert ack.json()['ack_revision'] == original['ack_revision'] + 1
        blocked = await client.post(base + '/update', json={'project_id': project['id'],
            'expected_revision': ack.json()['ack_revision'], 'observation': ack.json()['observation']},
            headers={'Authorization': 'Bearer ' + current_key, 'Idempotency-Key': 'source-human-update-' + uuid4().hex})
        assert blocked.status_code == 409
        assert blocked.json()['detail'] == 'outcome_shared_task_material_proof_unavailable'
        assert app.state.recognition_records.read('documents', document_id) is not None
        with app.state.product_outcome_freshness.outcome_scope(project=project['id'], document_id=document_id):
            with app.state.product_outcome_freshness._read_scope() as view:
                assert isinstance(view, SourceReadRecords)
                context = app.state.product_outcome_freshness.completion_context(app.state.ai_turn_store.get_request(turn['id']), reader=view)
                with pytest.raises(RecognitionConflict, match='outcome_shared_empty_context_privacy_unavailable'):
                    app.state.product_outcome_freshness.qualify_learning_context(context, reader=view, local_only=True)
        with app.state.ai_turn_store._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM ai_turn_events WHERE json_extract(event_json,'$.type') = ?",
                ('model.attempt.dispatched',)).fetchone()[0] == 0
