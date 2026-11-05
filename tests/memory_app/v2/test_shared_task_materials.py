import asyncio
from copy import deepcopy
import os
from pathlib import Path
import shutil
from uuid import uuid4

import pytest


def test_actual_task_admission_keeps_source_namespace_and_full_body(tmp_path_factory):
    _isolated_task_case(tmp_path_factory, publication_check=False)


def test_publication_history_keeps_same_id_sources_in_their_actual_namespace(tmp_path_factory):
    _isolated_task_case(tmp_path_factory, publication_check=True)


def test_shared_redo_rejects_prepared_and_accepted_unfinished_task(tmp_path_factory):
    _isolated_task_case(tmp_path_factory, publication_check=False, unfinished_redo=True)


def test_shared_task_steward_uses_actual_parent_origin(tmp_path_factory):
    _isolated_task_case(tmp_path_factory, publication_check=False, child_check=True)


def test_shared_redo_accepts_an_actual_failed_native_task(tmp_path_factory):
    _isolated_task_case(tmp_path_factory, publication_check=False, failed_redo=True)


def test_shared_expert_draft_retains_its_producer_and_sealed_origin(tmp_path_factory):
    _isolated_task_case(tmp_path_factory, publication_check=False, expert_draft=True)


def test_shared_owner_can_stop_their_accepted_task_after_leaving(tmp_path_factory):
    _isolated_task_case(tmp_path_factory, publication_check=False, user_stop=True)


def _isolated_task_case(tmp_path_factory, *, publication_check, unfinished_redo=False, child_check=False,
        failed_redo=False, expert_draft=False, user_stop=False):
    allocated = tmp_path_factory.mktemp('shared-task').resolve(strict=True)
    assert all(path.name != 'runtime' for path in (allocated, *allocated.parents))
    base = allocated / 'server'
    (base / 'config').mkdir(parents=True)
    repository = Path(__file__).resolve().parents[3]
    shutil.copyfile(repository / 'config/settings.toml.example', base / 'config/settings.toml')
    previous = {name: os.environ.get(name) for name in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ['CHRIPTMAS_APP_ROOT'], os.environ['CHRIPTMAS_DEPLOY'] = str(base.resolve(strict=True)), 'server'
    try:
        _actual_task_case(base, publication_check=publication_check, unfinished_redo=unfinished_redo,
            child_check=child_check, failed_redo=failed_redo, expert_draft=expert_draft, user_stop=user_stop)
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _actual_task_case(base, *, publication_check=False, unfinished_redo=False, child_check=False, failed_redo=False,
        expert_draft=False, user_stop=False):
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.source_graph import SourceGraph, validate_graph
    from backend.memory_app.privacy_policy import set_private_project_in_transaction
    from backend.memory_app.transaction_records import TransactionRecords
    from backend.memory_app.v2.shared_auxiliary import _qualified_commit
    from backend.memory_app.v2.material_origin_readers import qualified_record_read
    from backend.recognition.sql_source_identities import CollectingReader
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.shared_projects import SharedProjectError
    from backend.memory_app.v2.shared_task_materials import (
        read_task_material_item, validate_task_materials,
    )
    from backend.memory_app.workbench_identity_binding import capture_identity
    from backend.memory_app.v2 import _LazyOrganization
    from backend.memory_app.v2.task_do import TaskDo
    from backend.memory_app.v2.task_drafts import TaskDrafts
    from core.ai_kernel.product_admission import bundle_from_payload
    from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
    from backend.security.user_context import authorize_user, user_context
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context

    devices = DeviceRegistry(base / 'server')
    users = ServerUsers(base, records=devices.records)
    paired = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='材料检查管理员')
    admin = devices.authenticate(paired['key'])
    actors, credentials = [], []
    for name in ('材料检查所有者', '材料检查成员'):
        account = users.create(admin, name=name)
        pair = devices.exchange(devices.issue_pairing(user_id=account['user_id'], actor=admin.device_id)['code'], name=name)
        actors.append(devices.authenticate(pair['key']))
        credentials.append(pair['key'])
    owner, member = actors

    def child(path, user_id, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, base), server_context=context)

    server = create_server_application(DeploymentLayout('server', base / 'users/local-user', base), child_factory=child, port=8015)
    registry = server.state.shared_projects
    project = registry.create(owner, project_name='任务材料检查', operation_id='create-' + uuid4().hex)['project']
    registry.invite(owner, project['id'], user_id=member.user_id, role='viewer', authority=project['authority'],
        operation_id='invite-' + uuid4().hex)
    invitation = registry.invitations(member)[0]
    registry.accept(member, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-' + uuid4().hex)
    data = registry.roots._data(project['id'])
    source = RecognitionService(data.records)
    source_scope = WorkScope('local-user', project['id'])
    experience = source.stage_experience(scope=source_scope, content='输出列表时先逐项核对数量七。')
    candidate = source.propose(scope=source_scope, content='输出列表时先逐项核对数量七。',
        source_experience_ids=[experience], candidate_id='candidate-shared-number')
    recognized = source.publish(scope=source_scope, candidate_id=candidate.id,
        expected_revision=candidate.revision, reviewer=owner.user_id)

    async def scenario():
        async with server.router.lifespan_context(server):
            pool = server.state.server_user_pool
            async with pool.lease(member.user_id) as app:
                access = authorize_user(registry.users, member, member.user_id)
                with user_context(access), pool.request_scope(member.user_id, app, access):
                    query = server.state.shared_query_factory.query(app, member, project['id'])
                    if failed_redo:
                        generation = query.models.public_for_purpose('generation')
                        permission = query.models.update('generation', {'allow_remote': True,
                            'expected_revision': generation['revision']})
                        assert permission['allow_remote'] and not permission['configured'] and not permission['has_api_key']
                    query.answer_turns._runtime()
                    actor_service = app.state.recognition_service
                    own_scope = WorkScope('local-user', 'me')
                    original = actor_service.stage_experience(scope=own_scope, content='画像偏好为逐项核对。')
                    proposed = actor_service.propose(scope=own_scope, content='画像偏好为逐项核对。',
                        source_experience_ids=[original], candidate_id='candidate-profile-number')
                    personal = actor_service.publish(scope=own_scope, candidate_id=proposed.id,
                        expected_revision=proposed.revision, reviewer=member.user_id)
                    from backend.memory_app.v2.shared_task_context import style_materials, validate_style
                    from backend.memory_app.v2.policies import version
                    style = style_materials(query, project['id'], scene=None, version='@1', scope_version=version('scope'))
                    assert [item['namespace'] for item in style['items']] == [query.source_namespace]
                    validate_style(query, style)
                    materials = [
                        {'type': 'recognition', 'id': personal.id, 'revision': personal.revision,
                            'project_id': 'me', 'namespace': query.origin_admission.personal['namespace']},
                        {'type': 'recognition', 'id': recognized.id, 'revision': recognized.revision,
                            'project_id': project['id'], 'namespace': query.source_namespace},
                    ]
                    public = 'public-' + uuid4().hex
                    identity = capture_identity(app, query.receipt_records, app.state.recognition_documents,
                        project=project['id'], scene=None, thread_id='thread-' + uuid4().hex, turn_id=public)
                    organization = _LazyOrganization(app)
                    organization.method_query = app.state.workspace_domains.query
                    tasks = TaskDo(query.receipt_records, query.models,
                        TaskDrafts(query.receipt_records, app.state.recognition_documents), organization,
                        organization.read, organization.topology, query=app.state.workspace_domains.query,
                        service=actor_service, execution_getter=lambda: (app.state.ai_runtime, app.state.ai_turn_runner))
                    receipt, execution = tasks.with_source_query(query).initial(public, project['id'],
                        '按实际资料列出数量。', None, workbench_identity=identity)
                    request, turn = execution['request'], execution['request']['turn_id']
                    assert receipt['kernel_turn_id'] == turn and execution['started'] is False
                    native = app.state.ai_turn_store
                    bundle = bundle_from_payload(execution['admission_bundle'])
                    proof = next(item.payload['proof'] for item in bundle.items if item.kind == 'shared-task-materials-v1')
                    from backend.memory_app.v2.shared_task_workbench import validate_public_task
                    from backend.memory_app.v2.workbench_receipts import persist_workbench_turn
                    from backend.memory_app.v2.turn_logical_scope import TurnLogicalScope
                    with _qualified_commit(query.origin_admission), query.receipt_records.begin() as transaction:
                        row = persist_workbench_turn(transaction, turn_id=public, project=project['id'],
                            thread_id=identity['thread_id'], cleaned='按实际资料列出数量。', now=request['created_at'],
                            created_at=request['created_at'], intent='do', receipt={'do': receipt}, item_id=None,
                            item=None, instance=app.state.workbench_instance if user_stop else 'isolated-task-preparation',
                            run_id=uuid4().hex, title_prefix='',
                            replace_turn=None, research_state=execution)
                        validate_public_task(query, execution, reader=transaction)
                        assert TurnLogicalScope(app).read(row, reader=TransactionRecords(transaction)) == 'shared'
                        transaction.commit()
                    if user_stop:
                        await _shared_user_stop_case(app, server, query, row, request, bundle, member, credentials)
                        return
                    if unfinished_redo:
                        await _unfinished_redo_case(app, server, query, public, pair['key'])
                        return
                    if expert_draft:
                        _shared_expert_draft_case(app, query, execution, bundle)
                        return
                    if child_check:
                        await _shared_task_child_case(app, query, public)
                        return
                    if failed_redo:
                        try:
                            await _actual_failed_redo_case(app, server, query, public, pair['key'])
                        finally:
                            restored = query.models.update('generation', {'allow_remote': generation['allow_remote'],
                                'expected_revision': permission['revision']})
                            assert restored['allow_remote'] is generation['allow_remote']
                        return
                    if not publication_check:
                        await _redo_preparation_case(app, server, tasks, query, public, pair['key'])
                    from backend.memory_app.source_evidence_refs import qualified_material_ref
                    assert [ref['uri'] for ref in request['input']['refs'][:len(materials)]] == [qualified_material_ref(item) for item in materials]
                    assert all(row['ref']['uri'].startswith('crp://default/') for row in proof['bodies'])
                    with native.admission_bundle(request, bundle):
                        assert native.claim_turn(request) == (turn, True)
                    sealed = native.get_request(turn)
                    if not publication_check:
                        import httpx
                        before_outcome = tuple(native.events_after(turn))
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server),
                                base_url='http://127.0.0.1:8015', headers={'Authorization': 'Bearer ' + pair['key']}) as client:
                            for kind in ('shared', 'personal'):
                                unavailable = await client.get(f'/api/v2/workbench/turns/{public}/outcome',
                                    params={'project_id': project['id'], 'scope_kind': kind})
                                assert unavailable.status_code == 404
                                assert unavailable.json() == {'detail': 'task_outcome_not_found'}
                        assert tuple(native.events_after(turn)) == before_outcome
                    assert read_task_material_item(query, sealed) == proof
                    assert proof['selection']['origin_materials'] == materials
                    assert proof['bodies'][0]['payload']['content'] == personal.content
                    assert proof['bodies'][1]['payload']['content'] == recognized.content
                    assert {item['namespace'] for item in proof['selection']['sql_identities']} == {
                        query.source_namespace, query.origin_admission.personal['namespace']}
                    graph = SourceGraph()
                    for namespace, snapshot in zip((item['namespace'] for item in materials), proof['privacy']['source_snapshots']):
                        graph.snapshot(snapshot, namespace=namespace)
                    assert validate_graph(graph.result(), 'local-user') == graph.result()
                    with _qualified_commit(query.origin_admission) as qualification:
                        with qualified_record_read(query.receipt_records, validate=qualification._validate) as transaction:
                            observed = TransactionRecords(CollectingReader(TransactionRecords(transaction)))
                            assert qualification._actor_reader(observed) is observed
                            assert read_task_material_item(query, sealed, reader=observed) == proof
                    freshness = app.state.product_outcome_freshness
                    with _qualified_commit(query.origin_admission), freshness._read_scope(request=sealed) as view:
                        messages = [{'role': 'user', 'content': sealed['input']['text']}]
                        preparation = freshness.prepare_input_basis(request=sealed, input_kind='task-source-preflight',
                            messages=messages, reader=view)
                        saved = native.get(preparation)
                        initial = saved['context']['privacy_materials']
                        assert [{key: item[key] for key in ('type', 'id', 'revision', 'project_id', 'namespace')}
                            for item in initial] == materials
                        assert saved['context']['materials'] == []
                        assert [item['snapshot'] for item in initial] == proof['privacy']['source_snapshots']
                        assert all(item['role'] == 'initial_admission' for item in saved['sources'])
                        assert len(saved['sources']) == len(materials)
                        assert freshness.validate_input_preparation(request=sealed, input_kind='task-source-preflight',
                            preparation_ref=preparation, reader=view) == saved
                    before = tuple(native.events_after(turn))
                    if publication_check:
                        _publication_namespace_case(app, query, recognized, actor_service, source_scope)
                    changed = deepcopy(proof)
                    changed['bodies'][1]['payload']['content'] = '被改写的材料。'
                    with pytest.raises(RecognitionConflict, match='shared_task_material_body_changed'):
                        validate_task_materials(query, sealed, changed)
                    with _qualified_commit(query.origin_admission), query.receipt_records.begin() as transaction:
                        set_private_project_in_transaction(transaction, 'me', True, 0, source_owner=query.receipt_records)
                        with pytest.raises(RecognitionConflict, match='material_origin_privacy_changed'):
                            validate_task_materials(query, sealed, proof, reader=TransactionRecords(transaction))
                        transaction.rollback()
                    from backend.memory_app.source_egress import SourceEgressService
                    reference = {'source_id': recognized.id, 'locator': qualified_material_ref(materials[1])}
                    with _qualified_commit(query.origin_admission), freshness._read_scope(request=sealed) as view:
                        extra, = freshness._task_restoration_materials(view, sealed, [reference])
                        assert extra[0] == query.source_namespace and extra[1]['nodes'][0]['effective_purposes']
                    SourceEgressService(data.records).set_policy(source_scope, 'recognition', recognized.id,
                        recognized.revision, 0, [])
                    with _qualified_commit(query.origin_admission), freshness._read_scope(request=sealed) as view:
                        extra, = freshness._task_restoration_materials(view, sealed, [reference])
                        assert extra[0] == query.source_namespace
                        assert any(not item['effective_purposes'] for item in extra[1]['nodes'])
                    details = registry.detail(owner, project['id'])
                    membership = next(item for item in details['members'] if item['user_id'] == member.user_id)
                    registry.change_member(owner, project['id'], user_id=member.user_id, action='remove',
                        expected_revision=membership['revision'], authority=details['project']['authority'],
                        operation_id='remove-' + uuid4().hex)
                    with pytest.raises(SharedProjectError) as denied:
                        read_task_material_item(query, sealed)
                    assert denied.value.status == 403
                    if not publication_check:
                        from fastapi import HTTPException
                        from backend.memory_app.v2.shared_task_workbench import redo_query
                        with pytest.raises(HTTPException) as unavailable:
                            redo_query(app, app.state.recognition_records.read('v2_turns', public))
                        assert unavailable.value.status_code == 403
                    assert tuple(native.events_after(turn)) == before
                    assert not any(event['type'].startswith('model.') for event in before)

    with resource_context(server.state.server_context.resources):
        asyncio.run(scenario())


async def _redo_preparation_case(app, server, research, query, public_id, token):
    import httpx
    from backend.memory_app.workbench_identity_binding import capture_identity
    from backend.memory_app.v2.shared_task_workbench import redo_query, require_redo_query
    from backend.memory_app.v2.workbench_receipts import persist_workbench_turn

    records = app.state.recognition_records
    shared = records.read('v2_turns', public_id)
    current = redo_query(app, shared)
    assert current.origin_admission is not None and current.source_namespace == query.source_namespace
    collections = ('v2_turn_requests', 'v2_turn_message_order', 'v2_turns', 'v2_task_executions')
    before = {name: records.list(name) for name in collections}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server),
            base_url='http://127.0.0.1:8015', headers={'Authorization': 'Bearer ' + token}) as client:
        response = await client.post('/api/v2/workbench/turns/' + public_id + '/redo',
            json={'project_id': shared.payload['project_id'], 'expected_revision': 1, 'source_kind': 'execution'},
            headers={'Idempotency-Key': 'unavailable-redo-' + uuid4().hex})
    assert response.status_code == 409 and response.json()['detail'] == 'turn_not_retryable'
    assert {name: records.list(name) for name in collections} == before
    from core.ai_kernel.product_admission import ProductAdmissionItem, bundle_from_payload, seal_admission
    from backend.memory_app.v2.shared_task_redo import KIND
    execution = records.read('v2_task_executions', public_id).payload
    request = deepcopy(execution['request'])
    original = bundle_from_payload(execution['admission_bundle'])
    request['input']['refs'] = request['input']['refs'][:-len(original.items) - 1]
    bad = ProductAdmissionItem(KIND, {'kind': 'session_event', 'object_id': request['turn_id'],
        'uri': app.state.ai_turn_store.immutable_payload_reference(request['turn_id'], KIND)},
        original.items[0].scope, {'schema_version': 1})
    with pytest.raises(ValueError, match='invalid_shared_task_redo_admission'):
        seal_admission(request, (*original.items, bad), app.state.ai_turn_store)
    assert app.state.ai_turn_store.get_request(request['turn_id']) is None

    personal_id = 'personal-' + uuid4().hex
    identity = capture_identity(app, records, app.state.recognition_documents, project='inbox', scene=None,
        thread_id='thread-' + uuid4().hex, turn_id=personal_id)
    receipt, execution = research.initial(personal_id, 'inbox', '个人任务准备保留原范围。', None, workbench_identity=identity)
    now = execution['request']['created_at']
    with records.begin() as transaction:
        saved = persist_workbench_turn(transaction, turn_id=personal_id, project='inbox',
            thread_id=identity['thread_id'], cleaned='个人任务准备保留原范围。', now=now, created_at=now,
            intent='do', receipt={'do': receipt}, item_id=None, item=None, instance='isolated-personal-preparation',
            run_id=uuid4().hex, title_prefix='', replace_turn=None, research_state=execution)
        transaction.commit()
    assert redo_query(app, saved) is app.state.workspace_domains.query
    assert require_redo_query(app, saved) is app.state.workspace_domains.query


async def _unfinished_redo_case(app, server, query, public_id, token):
    import httpx
    from core.ai_kernel.product_admission import bundle_from_payload

    native = app.state.ai_turn_store
    records = query.receipt_records
    execution = records.read('v2_task_executions', public_id)
    public = records.read('v2_turns', public_id)
    identity = execution.payload['request']['turn_id']
    command = {'project_id': public.payload['project_id'], 'expected_revision': public.revision, 'source_kind': 'execution'}
    headers = {'Authorization': 'Bearer ' + token, 'Idempotency-Key': 'unfinished-redo-' + uuid4().hex}
    collections = ('v2_turn_requests', 'v2_turns', 'v2_task_executions')
    before = {name: records.list(name) for name in collections}
    assert native.get_request(identity) is None and execution.payload['started'] is False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://127.0.0.1:8015') as client:
        url = '/api/v2/workbench/turns/' + public_id + '/redo'
        response = await client.post(url, json=command, headers=headers)
        assert response.status_code == 409 and response.json()['detail'] == 'turn_not_retryable'
        accepted = app.state.ai_runtime.accept_turn(execution.payload['request'],
            admission_bundle=bundle_from_payload(execution.payload['admission_bundle']))
        assert accepted.turn_id == identity and accepted.status == 'accepted'
        events = tuple(native.events_after(identity))
        assert [event['type'] for event in events] == ['turn.accepted']
        response = await client.post(url, json=command, headers=headers)
        assert response.status_code == 409 and response.json()['detail'] == 'turn_not_retryable'
    assert tuple(native.events_after(identity)) == events
    assert not native.model_attempt_bindings(identity)
    assert {name: records.list(name) for name in collections} == before


async def _shared_task_child_case(app, query, public_id):
    from backend.memory_app.v2.material_origins import ORIGIN_KIND
    from backend.memory_app.v2.shared_task_context import task_query
    from backend.recognition import RecognitionConflict

    records = query.receipt_records
    tasks = app.state.workbench_research_service
    native, agents = app.state.ai_turn_store, app.state.agent_runtime_composition.store
    state = records.read('v2_task_executions', public_id)
    root_id = state.payload['request']['turn_id']
    assert query.models.public()['generation']['configured'] is False
    _assert_empty_ref_origins(app, query, tasks, state.payload['project_id'])
    await tasks.advance(public_id)
    state = records.read('v2_task_executions', public_id)
    assert state.payload['started'] is True
    root = native.get_request(root_id)
    bundle = native.restore_admission(root)
    origin = next(item for item in bundle.items if item.kind == ORIGIN_KIND)
    main, _ = agents.get_run_by_turn_id(root_id, project_id=state.payload['project_id'])
    children = agents.list_runs(project_id=main.project_id, parent_run_id=main.run_id)
    steward, = [run for run in children if run.profile_id == 'steward.scheduler']
    child = native.get_request(steward.turn_id)
    assert child['desired_outcome'] == 'agent.steward.plan' and child['input']['refs'].count(origin.ref) == 1
    assert native.events_after(steward.turn_id)[0]['type'] == 'turn.accepted'
    assert native.restore_admission(child) is None
    issued, parent = task_query(query.execution_owner, child)
    assert parent == root and issued.origin_admission.lease == query.origin_admission.lease
    assert native.claim_turn(child) == (steward.turn_id, False)

    forged = {**deepcopy(child), 'turn_id': 'turn-' + uuid4().hex, 'idempotency_key': 'unbound-' + uuid4().hex}
    marker = next(ref for ref in forged['input']['refs'] if ref == origin.ref)
    marker['uri'] += '/other'
    with pytest.raises(RecognitionConflict, match='material_origin_binding_missing'):
        native.claim_turn(forged)
    assert native.get_request(forged['turn_id']) is None
    assert all(not native.model_attempt_bindings(run.turn_id) for run in (main, *children))
    assert not (query.material_owner.root / '.rebuild-data' / 'ai-turns.sqlite3').exists()


async def _actual_failed_redo_case(app, server, query, public_id, token):
    from dataclasses import replace
    import httpx
    from starlette.concurrency import run_in_threadpool
    from backend.memory_app.v2.shared_task_redo import KIND, capture_source

    records, tasks = query.receipt_records, app.state.workbench_research_service
    native, agents = app.state.ai_turn_store, app.state.agent_runtime_composition.store
    original = records.read('v2_task_executions', public_id)
    root_id, project = original.payload['request']['turn_id'], original.payload['project_id']
    assert query.models.public()['generation']['configured'] is False
    profiles = app.state.agent_runtime_composition.profiles
    profile = profiles.get('main.orchestrator')
    limited = profiles.update(replace(profile, revision=profile.revision + 1,
        budget_limit=replace(profile.budget_limit, tool_calls=0)), expected_revision=profile.revision)
    await tasks.advance(public_id)
    assert records.read('v2_task_executions', public_id).payload['started'] is True
    async with asyncio.timeout(35):
        while True:
            main, _ = agents.get_run_by_turn_id(root_id, project_id=project)
            children = agents.list_runs(project_id=project, parent_run_id=main.run_id)
            links = {link.child_run_id: link for link in agents.list_child_links(project_id=project,
                parent_run_id=main.run_id)}
            closed = all(run.is_terminal or (run.run_id in links and links[run.run_id].status == 'cancelled'
                and native.get_request(run.turn_id) is None) for run in children)
            events = tuple(native.events_after(root_id))
            if children and closed and events[-1]['type'] == 'turn.failed':
                break
            await asyncio.sleep(0.15)
    await run_in_threadpool(app.state.agent_organization_runtime.recover)
    main, _ = agents.get_run_by_turn_id(root_id, project_id=project)
    assert main.status == 'failed' and main.terminal_receipt_ref and main.budget_limit.tool_calls == 0
    assert any(event['type'] == 'context.resolved' for event in events)
    await tasks.advance(public_id)
    saved = records.read('v2_turns', public_id)
    assert saved.payload['receipt']['do']['state'] == 'failed'
    assert all(not native.model_attempt_bindings(run.turn_id) for run in (main, *children))
    source = capture_source(query, public_id, source_kind='execution')
    assert source['failed_execution'] == {'terminal_sequence': events[-1]['sequence'],
        'error_code': events[-1]['data']['error_code']}
    assert source['proof_refs'] == []
    assert query.source_namespace in {item['namespace'] for item in source['materials']}
    restored = profiles.update(replace(profile, revision=limited.revision + 1), expected_revision=limited.revision)
    assert restored.budget_limit == profile.budget_limit

    command = {'project_id': project, 'source_kind': 'execution', 'expected_revision': source['revision']}
    headers = {'Authorization': 'Bearer ' + token, 'Idempotency-Key': 'actual-failed-redo-' + uuid4().hex}
    url = '/api/v2/workbench/turns/' + public_id + '/redo'
    steward, = [run for run in children if run.profile_id == 'steward.scheduler']
    claimed_child = deepcopy(native.get_request(steward.turn_id))
    claimed_child.update(turn_id='turn-' + uuid4().hex, idempotency_key='unbound-' + uuid4().hex)
    claimed_child['input']['refs'] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://127.0.0.1:8015') as client:
        invalid = await client.post('/api/ai/turns', json=claimed_child, headers=headers)
        assert invalid.status_code == 400 and invalid.json()['reason'] == 'material_origin_binding_missing'
        assert native.get_request(claimed_child['turn_id']) is None
        response = await client.post(url, json=command, headers=headers)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['turn']['scope_kind'] == 'shared' and result['turn']['id'] != public_id
        replay = await client.post(url, json=command, headers=headers)
        assert replay.status_code == 200 and replay.json()['turn']['id'] == result['turn']['id']
        thread = await client.get('/api/v2/workbench/threads/' + result['thread_id'],
            params={'project_id': project, 'scope_kind': 'shared'}, headers=headers)
        assert thread.status_code == 200 and {turn['id'] for turn in thread.json()['turns']} >= {public_id, result['turn']['id']}
    async with asyncio.timeout(20):
        while (redo := native.get_request(result['turn']['receipt']['do']['kernel_turn_id'])) is None:
            await asyncio.sleep(0.15)
    sealed = native.restore_admission(redo)
    inheritance, = [item for item in sealed.items if item.kind == KIND]
    assert inheritance.payload['source'] == source
    assert tuple(native.events_after(root_id)) == events
    assert not native.model_attempt_bindings(redo['turn_id'])
    assert not (query.material_owner.root / '.rebuild-data' / 'ai-turns.sqlite3').exists()


def _assert_empty_ref_origins(app, query, tasks, shared_project):
    from backend.memory_app.workbench_identity_binding import capture_identity
    from backend.recognition import RecognitionConflict
    from core.ai_kernel.product_admission import bundle_from_payload

    records = query.receipt_records
    native, agents = app.state.ai_turn_store, app.state.agent_runtime_composition.store
    coordinator = app.state.agent_runtime_composition.coordinator
    organization = app.state.agent_organization_runtime

    def prepare_empty_refs(owner_tasks, project):
        public = 'public-' + uuid4().hex
        identity = capture_identity(app, records, app.state.recognition_documents,
            project=project, scene=None, thread_id='thread-' + uuid4().hex, turn_id=public)
        _, execution = owner_tasks.initial(public, project, '检查来源是否沿原任务继承。', None,
            workbench_identity=identity)
        request = organization._with_host_capabilities(execution['request'])
        prepared = coordinator.accept_and_register_main(request,
            admission_bundle=bundle_from_payload(execution['admission_bundle']))
        return prepared, {'parent_turn_id': prepared.run.turn_id, 'project_id': project,
            'scope': prepared.request['scope'], 'privacy': prepared.request['privacy'],
            'operation_id': 'op-empty-refs-' + uuid4().hex, 'tool_call_id': 'call-' + uuid4().hex,
            'arguments': {'profile_id': 'subagent.explorer', 'task': '检查当前依据。', 'input_refs': []}}

    shared_parent, shared_args = prepare_empty_refs(tasks.with_source_query(query), shared_project)
    with pytest.raises(RecognitionConflict, match='material_origin_binding_missing'):
        coordinator.spawn(**shared_args)
    blocked, = agents.list_runs(project_id=shared_project, parent_run_id=shared_parent.run.run_id)
    assert native.get_request(blocked.turn_id) is None
    blocked_link, = agents.list_child_links(project_id=shared_project, parent_run_id=shared_parent.run.run_id)
    assert blocked_link.status == 'cancelled'

    _, personal_args = prepare_empty_refs(tasks, 'inbox')
    personal = coordinator.prepare_child(**personal_args)
    assert native.get_request(personal.run.turn_id)['input']['refs'] == []
    assert not native.model_attempt_bindings(personal.run.turn_id)


def _shared_expert_draft_case(app, query, execution, bundle):
    from backend.memory_app.v2.material_origins import ORIGIN_KIND
    from backend.recognition import RecognitionConflict, WorkScope
    from backend.recognition.material_logical_scope import native_material_scope, require_material_logical_scope
    from backend.recognition.product_draft_dependencies import ProductDraftDependencyError, product_draft_source

    records = query.receipt_records
    project = execution['project_id']
    native = app.state.ai_turn_store
    coordinator = app.state.agent_runtime_composition.coordinator
    organization = app.state.agent_organization_runtime
    request = organization._with_host_capabilities(execution['request'])
    main = coordinator.accept_and_register_main(request, admission_bundle=bundle)
    origin, = [item for item in bundle.items if item.kind == ORIGIN_KIND]
    worker = coordinator.prepare_child(parent_turn_id=main.run.turn_id, project_id=project,
        scope=main.request['scope'], privacy=main.request['privacy'], operation_id='op-draft-' + uuid4().hex,
        tool_call_id='call-' + uuid4().hex, arguments={'profile_id': 'subagent.worker', 'task': '记录实际分工依据。',
            'input_refs': [origin.ref['uri']], 'capability_ids': ['document.draft.propose']})
    assert worker.link.status == 'spawned' and 'document.draft.propose' in worker.run.capability_ids
    assert native.get_request(worker.run.turn_id) == worker.request
    assert native.events_after(worker.run.turn_id)[0]['type'] == 'turn.accepted'
    assert native.restore_admission(worker.request) is None
    actual, authority, bound = native_material_scope(records, worker.run.turn_id)
    assert actual == worker.request and authority == main.request and bound['ref'] == origin.ref

    drafts = app.state.task_drafts
    args = {'turn_id': worker.run.turn_id, 'project': project, 'operation': 'op-tool-draft-' + uuid4().hex,
        'title': '实际专家稿', 'markdown': '保留专家原轮产生的草稿正文。'}
    output = drafts.create(**args)
    assert drafts.create(**args, source_request=worker.request) == output
    identity, revision = output['document_id'], output['document_revision']
    namespace = drafts.documents.namespace_id
    saved = require_material_logical_scope(records, material_type='document', identity=identity, project_id=project,
        revision=revision, namespace_id=namespace, source_request=main.request)
    assert saved['native_turn_id'] == worker.run.turn_id and saved['origin'] == bound
    assert saved['birth']['source_refs'] == [{'source_id': worker.run.turn_id, 'locator': 'task://' + worker.run.turn_id}]
    assert drafts.documents.markdown(identity, revision=revision) == args['markdown']
    with pytest.raises(RecognitionConflict, match='material_logical_scope_unavailable'):
        require_material_logical_scope(records, material_type='document', identity=identity, project_id=project,
            scope_kind='personal')
    with pytest.raises(ProductDraftDependencyError, match='product draft execution is unavailable'):
        product_draft_source(records, WorkScope('local-user', project), identity, revision)
    assert query.material_owner.records.read('documents', identity) is None
    assert not native.model_attempt_bindings(main.run.turn_id) and not native.model_attempt_bindings(worker.run.turn_id)


async def _shared_user_stop_case(app, server, query, row, request, bundle, member, credentials):
    import httpx
    import json
    from backend.memory_app.kernel.task_continuations import STOPS

    native = app.state.ai_turn_store
    composition, organization = app.state.agent_runtime_composition, app.state.agent_organization_runtime
    accepted = composition.coordinator.accept_and_register_main(
        organization._with_host_capabilities(request), admission_bundle=bundle)
    assert native.get_request(accepted.run.turn_id) == accepted.request
    registry = server.state.shared_projects
    project = row.payload['project_id']
    authority = registry.detail(member, project)['project']['authority']
    registry.change_member(member, project, user_id=member.user_id, action='leave',
        expected_revision=authority['member_revision'], authority=authority, operation_id='stop-leave-' + uuid4().hex)
    owner_headers, member_headers = ({'Authorization': 'Bearer ' + key} for key in credentials)
    url = '/api/v2/workbench/turns/' + row.object_id
    thread = '/api/v2/workbench/threads/' + row.payload['thread_id']
    params = {'project_id': project, 'scope_kind': 'shared'}
    events = tuple(native.events_after(accepted.run.turn_id))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://127.0.0.1:8097') as client:
        before = await client.get(thread, params=params, headers=member_headers)
        assert before.status_code == 200
        before_task = before.json()['turns'][0]['receipt']['do']
        assert before_task['kernel_turn_id'] == accepted.run.turn_id and before_task['state'] == 'running'
        assert tuple(native.events_after(accepted.run.turn_id)) == events
        assert (await client.post(url + '/stop', json={'project_id': project}, headers=owner_headers)).status_code == 404
        assert (await client.post(url + '/stop', json={'project_id': 'inbox'}, headers=member_headers)).status_code == 404
        stopped = await client.post(url + '/stop', json={'project_id': project}, headers=member_headers)
        assert stopped.status_code == 202 and stopped.json() == {'state': 'stopped'}
        assert (await client.post(url + '/stop', json={'project_id': project}, headers=member_headers)).json() == stopped.json()
        reread = await client.get(thread, params=params, headers=member_headers)
        assert reread.status_code == 200 and reread.json()['turns'][0]['receipt']['do']['state'] == 'stopped'
        stream = await client.get(url + '/stream', params={'project_id': project, 'after': 0}, headers=member_headers)
        assert stream.status_code == 200 and 'event: done' in stream.text
        event, = [json.loads(line.removeprefix('data: ')) for line in stream.text.splitlines() if line.startswith('data: ')]
        assert event['turn']['id'] == row.object_id and event['turn']['receipt']['do']['state'] == 'stopped'
    new_events = tuple(native.events_after(accepted.run.turn_id))
    assert new_events[:len(events)] == events
    assert new_events[-1]['type'] == 'turn.cancel.requested' and new_events[-1]['data']['status'] == 'waiting_approval'
    assert query.receipt_records.read(STOPS, row.object_id).payload['state'] == 'paused'
    assert not native.model_attempt_bindings(accepted.run.turn_id)
    assert not (query.material_owner.root / '.rebuild-data' / 'ai-turns.sqlite3').exists()


def _publication_namespace_case(app, query, recognized, actor_service, scope):
    from backend.memory_app.source_egress import SourceEgressService
    from backend.memory_app.source_graph import SourceGraph
    from backend.memory_app.v2.shared_auxiliary import _qualified_commit
    from backend.memory_app.v2.steer_learning_privacy import (
        _permit_snapshot, _publication_history, publication_privacy_active,
        publication_privacy_scope, qualify_publication_sources,
    )
    from backend.recognition import RecognitionConflict
    from backend.recognition.sql_source_identities import CollectingReader
    from core.storage_provider import SQLiteStructuredRecordStore
    from core.storage_provider.sqlite_uow import begin_read

    freshness = app.state.product_outcome_freshness
    original = actor_service.stage_experience(scope=scope, content='个人空间同编号认识的原件。')
    candidate = actor_service.propose(scope=scope, content='个人空间同编号认识。', source_experience_ids=[original])
    actor = actor_service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision,
        reviewer='local-user', recognition_id=recognized.id)
    actor = actor_service.revise(scope=scope, recognition_id=actor.id, expected_revision=actor.revision,
        content='个人空间同编号认识的实际第二修订。')
    SourceEgressService(freshness.records).set_policy(scope, 'recognition', actor.id, actor.revision, 0, [])
    sources = []
    with _qualified_commit(query.origin_admission), freshness._read_scope() as view:
        for namespace, revision in ((query.source_namespace, recognized.revision),
                (query.origin_admission.personal['namespace'], actor.revision)):
            material = {'type': 'recognition', 'id': recognized.id, 'revision': revision,
                'project_id': scope.project_id, 'namespace': namespace, 'role': 'evidence',
                'proof_ref': {'collection': 'recognitions', 'object_id': recognized.id, 'revision': revision}}
            owner = freshness._material_owner(material, view)
            sources.extend(freshness._freeze_material(owner, material, reader=view))
        assert all(source['verified'] for source in sources)
    actor_service.split(scope=scope, recognition_id=actor.id, expected_revision=actor.revision,
        parts=['人工分项一', '人工分项二'])
    with _qualified_commit(query.origin_admission), freshness._read_scope() as view:
        # 图仅由刚才真实冻结的两个空间材料构成，不作为发布或模型消费证明。
        graph = SourceGraph()
        for source in sources:
            graph.snapshot(source['snapshot'], namespace=source['origin']['namespace'])
        combined = {'scope': {'user_id': scope.user_id, 'project_id': scope.project_id},
            'nodes': graph.result()['nodes'], 'roots': []}
        for source in sources:
            namespace = source['origin']['namespace']
            permits = {}
            _permit_snapshot(permits, combined, namespace=namespace, source_namespace=query.source_namespace)
            assert permits[(scope.user_id, scope.project_id, recognized.id)]['revisions'] == {
                source['frozen']['revision']}
        pairs = _publication_history(freshness, {'snapshot': combined, 'origin': sources[0]['origin']},
            sources, reader=view)
        roots = [(snapshot, origin) for snapshot, origin in pairs
            if snapshot['roots'][0]['id'] == recognized.id]
        assert len(roots) == 2
        current = {origin['namespace']: snapshot for snapshot, origin in roots}
        assert current[query.source_namespace]['roots'][0]['revision'] == recognized.revision
        assert current[query.origin_admission.personal['namespace']]['roots'][0]['revision'] == actor.revision + 1
        assert current[query.source_namespace]['nodes'][0]['effective_purposes']
        assert any(not node['effective_purposes'] for node in current[query.origin_admission.personal['namespace']]['nodes'])
        from backend.memory_app.v2.outcomes import update_materials
        projected = update_materials({'namespaced_materials': True, 'current_sources': [], 'privacy_ceiling': {
            'sources': [{'source': sources[0]['material'], 'origin': sources[0]['origin'],
                'current': current[query.source_namespace], 'history_current': [item[0] for item in roots],
                'history_origins': [item[1] for item in roots]}]}})
        assert {(item['namespace'], item['revision']) for item in projected if item['id'] == recognized.id} == {
            (query.source_namespace, recognized.revision),
            (query.origin_admission.personal['namespace'], actor.revision + 1)}
        assert publication_privacy_active(freshness, view) is False
        assert publication_privacy_active(freshness, CollectingReader(view)) is False
        with pytest.raises(RecognitionConflict, match='publication_privacy_scope_required'):
            qualify_publication_sources(freshness, sources, reader=view, local_only=True)
        with pytest.raises(RecognitionConflict, match='publication_proof_not_issued'):
            with publication_privacy_scope(freshness, view, {}, publication_proof={}):
                raise AssertionError('unissued_publication_proof_was_accepted')
        unrelated = SQLiteStructuredRecordStore(freshness.root / 'unrelated-publication.sqlite3')
        with begin_read(unrelated) as other:
            with pytest.raises(RecognitionConflict, match='material_origin_commit_actor_reader_changed'):
                _publication_history(freshness, sources[0], sources, reader=other)
    with pytest.raises(RecognitionConflict, match='outcome_source_read_scope_expired'):
        publication_privacy_active(freshness, view)
