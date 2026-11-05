from copy import deepcopy
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from threading import RLock
from uuid import uuid4
from time import perf_counter
import sys
import threading
import traceback
import re
from pathlib import Path

import pytest
import httpx
from fastapi import HTTPException


def check_primary_preparation(query, app, data):
    from backend.memory_app.kernel.answer_continuations import save_plan, PLAN_KIND
    from backend.memory_app.kernel.turn_requests import freeze_turn_request
    from backend.memory_app.processing_lease import ProcessingLease
    from backend.memory_app.v2.material_origin_admission import BoundMaterialOrigins
    from backend.memory_app.v2.material_origin_privacy import freeze_origin_materials
    from backend.memory_app.v2.model_material_coverage import validate_primary_material_proof
    from backend.memory_app.v2.material_input_binding import prepared_primary_input, PreparedMaterialInput
    from backend.memory_app.v2.material_read_context import prepared_primary_scope
    from backend.memory_app.v2.material_origin_sql import freeze_sql_identities
    from backend.memory_app.workspace_items import WorkspaceItems
    from backend.memory_app.workspace_confirmation import WorkspaceConfirmation
    from backend.memory_app.workspace_generation import _draft
    from backend.memory_app.v2.answer_evidence import build_partition_messages
    from backend.memory_app.workbench_identity_binding import prepare_admission
    from backend.recognition import RecognitionConflict, RecognitionError
    from backend.recognition import WorkScope
    from backend.memory_app.constraints import ProjectConstraintService

    project, store = query.origin_admission.lease.project_id, app.state.ai_turn_store
    records = app.state.recognition_records
    for purpose in ('generation', 'embedding', 'rerank'):
        current = query.models.public_for_purpose(purpose)
        query.models.update(purpose, {'expected_revision': current['revision'], 'model': 'no-wire-verification',
            'base_url': 'http://127.0.0.1:9/v1', 'allow_remote': False,
            **({'api_key': 'isolated-local-no-wire'} if purpose == 'generation' else {})})
    items = WorkspaceItems(data.records, ProcessingLease(data.records, 'workspace_items', 'primary-real'), RLock())
    source_text = '共享测试正文。\n末行保留换行。\n'
    draft = _draft({'title': '共享测试材料', 'summary': '共享测试摘要', 'topics': [], 'facts': [], 'todos': [],
        'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}, source_text)
    uploaded = items.create_upload({'id': 'primary-item', 'project_id': project, 'input_kind': 'text',
        'title': '共享测试材料', 'source_text': source_text, 'status': 'ready', 'draft': draft, 'error': None,
        'document_id': None, 'created_at': '2026-11-05T00:00:00Z'})
    WorkspaceConfirmation(data.root, data.records, data.documents).confirm(uploaded['id'], project,
        uploaded['revision'], lambda value: '# 共享测试材料\n\n' + source_text)
    ProjectConstraintService(data.records).upsert(WorkScope('local-user', project), 'primary-real-rule', 0,
        '只使用本轮已选资料中的内容')
    started = perf_counter()
    plan = query.prepare_ask(project, '共享测试正文')
    print(f'shared-primary-timing: prepare_ask {perf_counter() - started:.2f}s', flush=True)
    assert plan['chosen']
    assert plan['origin_lease'] == query.origin_admission.lease.payload()
    identity = 'shared-primary-' + uuid4().hex
    origins = BoundMaterialOrigins(query.origin_admission, query.origin_admission.lease, {})
    _, privacy, _ = freeze_origin_materials(origins, query.models, project, [], local_only=True)
    request = freeze_turn_request('project.answer', turn_id=identity, session_id='session-' + identity,
        operation_id='answer-' + identity, idempotency_key='answer-' + identity, project_id=project,
        created_at=datetime.now(timezone.utc).isoformat(), text=plan['question'], privacy=privacy,
        capabilities=['workbench.answer.execute'], template_version=2)
    request['policy_versions'] = plan['policy_versions']
    request['context_policy'].update(include_memory=False)
    request['capability_request'] = {'mode': 'execute_exact_v1', 'capability_id': 'workbench.answer.execute',
        'arguments': {'query': request['input']['text']}}
    bundle = prepare_admission(app, request, None, store, admission_prepare=query.origin_admission.prepare)
    with store.admission_bundle(request, bundle):
        assert store.claim_turn(request) == (identity, True)
    lease = store.try_acquire_run_lease(identity, 'shared-primary-reader', now=datetime.now(timezone.utc),
        stale_after=datetime.now(timezone.utc) + timedelta(minutes=5))
    assert lease is not None
    runtime = app.state.ai_runtime
    token = runtime._run_lease_context.set(lease)
    try:
        save_plan(query, identity, plan, {'text': '', 'turns': []}, request=request, store=store, runtime=runtime)
        assert records.read('v2_answer_continuations', identity) is not None
        assert data.records.read('v2_answer_continuations', identity) is None
        messages = build_partition_messages(plan['chosen'], plan['question'], profile=plan['profile'],
            compose_version=plan['policy_versions']['compose'])
        started = perf_counter()
        proof = query.execution_owner.prepare_model_material_proof(query.models, request, 'answer', messages)
        print(f'shared-primary-timing: first full proof {perf_counter() - started:.2f}s', flush=True)
        changed = deepcopy(messages)
        assert '共享测试正文' in changed[-1]['content']
        changed[-1]['content'] = changed[-1]['content'].replace('共享测试正文', '未获许可的替换正文', 1)
        for altered in (changed, [*deepcopy(messages), {'role': 'user', 'content': '未覆盖原件的额外材料正文'}]):
            with pytest.raises(RecognitionConflict, match='material_origin_model_messages_changed'):
                query.execution_owner.prepare_model_material_proof(query.models, request, 'answer', altered)
        print('shared-primary: actual prepare caller rejects replaced source body and extra uncovered message; model events zero', flush=True)
        assert proof['selection']['sql_identities'] and proof['selection']['origin_materials']
        assert all(row['namespace'] == query.source_namespace for row in proof['selection']['origin_materials'])
        wire_messages = query.execution_owner.project_constraint_input.messages(request, messages)
        assert wire_messages[0]['role'] == 'system' and '只使用本轮已选资料中的内容' in wire_messages[0]['content']
        query.execution_owner.validate_model_material_proof(query.models, proof, native_request=request,
            messages=wire_messages, logical_invocation='answer')
        for field in ('sql_identities', 'origin_materials'):
            changed = deepcopy(proof)
            changed['selection'][field].pop()
            with pytest.raises(RecognitionConflict):
                validate_primary_material_proof(query, query.models, changed, native_request=request,
                    messages=wire_messages, logical_invocation='answer')
        origins = query.origin_admission.restore(request)
        materials = proof['selection']['origin_materials']
        with prepared_primary_input(query, request, 'answer', materials) as prepared:
            assert freeze_sql_identities(origins, materials, query=query, prepared_input=prepared) == proof['selection']['sql_identities']
        with pytest.raises(RecognitionConflict, match='material_origin_prepared_input_expired'):
            prepared.body(query, origins, materials=materials)
        with pytest.raises(RecognitionConflict, match='material_origin_prepared_input_expired'):
            with prepared_primary_scope(PreparedMaterialInput()):
                raise AssertionError('unissued_primary_input_was_accepted')
        document_id = data.records.read('workspace_items', uploaded['id']).payload['document_id']
        document = data.documents.read(document_id)
        data.documents.save_user_edit(document_id, markdown='# 共享测试材料\n\n修订后的正文\n',
            expected_revision=document['revision'])
        with pytest.raises(RecognitionError):
            validate_primary_material_proof(query, query.models, proof, native_request=request,
                messages=wire_messages, logical_invocation='answer')
        saved = store.get_immutable_payload(identity, PLAN_KIND)
        assert saved is not None
        assert not any(event['type'].startswith('model.') for event in store.events_after(identity))
        print('shared-primary: actual native plan, actor continuation, source SQL/body and expired issuer checks passed; model events zero', flush=True)
    finally:
        runtime._run_lease_context.reset(token)
        store.release_strict_run_lease(lease)
    return [request]


async def check_primary_wire_preparation(query, app, owner):
    from backend.memory_app.kernel.answer_turns import ACTIVE_ANSWER
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.v2.model_material_coverage import validate_primary_input

    registry, actor = query.origin_admission.registry, query.origin_admission.actor
    project = registry.create(owner, project_name='共享外发失权核验', operation_id='wire-' + uuid4().hex)['project']
    registry.invite(owner, project['id'], user_id=actor.user_id, role='viewer', authority=project['authority'],
        operation_id='wire-invite-' + uuid4().hex)
    invitation = next(row for row in registry.invitations(actor) if row['project_id'] == project['id'])
    registry.accept(actor, invitation['id'], expected_revision=invitation['revision'],
        operation_id='wire-accept-' + uuid4().hex)
    query = query.origin_factory.query(app, actor, project['id'])
    data = registry.roots._data(project['id'])
    prepared_requests = check_primary_preparation(query, app, data)
    mutation_requests = await _check_actual_source_message_rejection(query, app)
    observed = []
    diagnostics = []
    boundary = ModelConfiguration.complete_governed.__code__
    original_method = ModelConfiguration.complete_governed
    from backend.memory_app.answer_diagnostics import log_answer_validation_failure

    def observe(frame, event, argument):
        if event == 'call' and frame.f_code is log_answer_validation_failure.__code__:
            failure = frame.f_locals['error']
            code = failure.args[0] if failure.args and type(failure.args[0]) is str else ''
            diagnostics.append({'type': type(failure).__name__,
                'code': code if re.fullmatch('[a-z][a-z0-9_]{0,96}', code) else None,
                'calls': [f'{Path(item.filename).name}:{item.lineno}:{item.name}'
                    for item in traceback.extract_tb(failure.__traceback__)]})
        if event != 'call' or frame.f_code is not boundary:
            return
        active = ACTIVE_ANSWER.get()
        assert active is not None and frame.f_locals['self'] is query.models
        request, store, personal, handles = active
        assert personal is query.execution_owner
        handle = frame.f_locals['metadata_sink']
        binding = store.get_immutable_payload(request['turn_id'],
            'answer-invocation-binding-' + handle.model_request_id)
        assert binding is not None
        actual = store.get_immutable_payload(request['turn_id'],
            'answer-model-input-' + binding[1]['invocation_key'])
        assert actual is not None and actual[0] == binding[1]['input_ref']
        proof = actual[1]['material_proof']
        assert actual[1]['messages'] == frame.f_locals['messages']
        validate_primary_input(query, store.get_request(request['turn_id']), 'answer', actual[0], proof,
            frame.f_locals['messages'])
        assert proof['selection']['sql_identities'] and proof['selection']['origin_materials']
        assert app.state.recognition_records.read('v2_answer_continuations', request['turn_id']) is not None
        assert data.records.read('v2_answer_continuations', request['turn_id']) is None
        observed.append(store.get_request(request['turn_id']))
        # 仅观察输入并撤销真实成员，原方法继续执行其生产外发门禁。
        detail = registry.detail(owner, project['id'])
        member = next(row for row in detail['members'] if row['user_id'] == actor.user_id)
        registry.change_member(owner, project['id'], user_id=actor.user_id, action='remove',
            expected_revision=member['revision'], authority=detail['project']['authority'],
            operation_id='wire-remove-' + uuid4().hex)

    previous = sys.getprofile()
    threaded = threading.getprofile()
    threading.setprofile_all_threads(observe)
    try:
        with pytest.raises(HTTPException) as denied:
            await _run_actual_preflight_steer(query, app)
        if diagnostics:
            print('shared-primary-diagnostic: ' + str(diagnostics), flush=True)
        assert denied.value.status_code in {403, 409}
    finally:
        threading.setprofile_all_threads(threaded)
        sys.setprofile(previous)
    assert len(observed) == 1
    assert ModelConfiguration.complete_governed is original_method
    request = observed[0]
    events = tuple(app.state.ai_turn_store.events_after(request['turn_id']))
    assert sum(event['type'] == 'model.requested' for event in events) == 1
    assert sum(event['type'] == 'turn.steer.received' for event in events) == 1
    assert sum(event['type'] == 'turn.steer.applied' for event in events) == 1
    assert not any(event['type'].startswith('model.attempt.') for event in events)
    assert not any(event['type'] == 'model.completed' for event in events)
    assert data.records.list('workspace_ask_receipts') == ()
    print('shared-primary: actual preflight steer adoption projected into bound model input; real member revocation rejected by unchanged production method; wire attempts zero', flush=True)
    return [*prepared_requests, *mutation_requests, *observed]


async def _run_actual_preflight_steer(query, app):
    from backend.memory_app.kernel.answer_turns import ACTIVE_ANSWER
    from backend.memory_app.v2.turn_frames import FrameProducerOwner, TurnFrames
    from backend.memory_app.v2.policies import get, override
    from backend.memory_app.workbench_identity_binding import capture_identity
    from backend.memory_app.v2.steer import SteerService
    if not hasattr(app.state.ai_runtime, 'product_steers'):
        with override(steer='@2'):
            service = SteerService(application=app, records=query.execution_owner.records,
                runtime_root=app.state.recognition_runtime_root)
        service.bind_runtime(app.state.ai_runtime)
        app.state.product_steers = service
    project, question = query.origin_admission.lease.project_id, '共享测试正文'
    identity, thread = 'turn-steer-shared-' + uuid4().hex, 'thread-steer-shared-' + uuid4().hex
    plan = query.prepare_ask(project, question)
    plan['policy_versions'] = {**plan['policy_versions'], 'steer': '@2'}
    preview = query.store_ask_preview(plan)
    binding = capture_identity(app, query.execution_owner.records, app.state.recognition_documents,
        project=project, scene=None, thread_id=thread, turn_id=identity)

    async def execute():
        active = ACTIVE_ANSWER.get()
        runtime, store = app.state.ai_runtime, active[1]
        request, lease = store.get_request(identity), runtime._run_lease_context.get()
        service = runtime.product_steers
        assert service.registered(identity)
        frames = TurnFrames(query.execution_owner.records, turn_id=identity, project_id=project, recipe=get('retry'),
            turn={'id': identity, 'thread_id': thread, 'intent': 'ask', 'user_text': question,
                'created_at': datetime.now(timezone.utc).isoformat()}, request=request,
            producer_owner=lambda: FrameProducerOwner(turn_id=identity, lease=lease, store=store))
        frames.start()
        service.bind_delivery(identity, frames.deliver_control)
        query.answer_turns.active[identity]['steer_reset'] = frames.reset_for_steer
        with runtime._event_commit(identity):
            event = runtime._new_event(identity, 'turn.steer.received', 'running', '已收到实际检查补充', actor='user')
            store.accept_steer_bundle(event, expected_sequence=event['sequence'] - 1, run_lease=lease,
                submission_key='shared-preflight-' + uuid4().hex[:12], text='回答时保留材料中的换行含义',
                snapshot={'answer_tail': ''}, visible_sequence=None, maximum_count=5, policy_version='@2')
        try:
            return await query.execute_ask(preview, project, question, True)
        finally:
            frames.close(completed=False)

    return await query.answer_turns.run(turn_id=identity, project=project, question=question,
        policy_versions=plan['policy_versions'], workbench_identity=binding,
        admission_prepare=query.origin_admission.prepare, privacy_prepare=query.freeze_answer_privacy,
        operation=execute)


async def _check_actual_source_message_rejection(query, app):
    from backend.memory_app.kernel import answer_turns
    observed = []
    original = answer_turns.generate_answer
    boundary = original.__code__
    for mode in ('replace-source-body', 'extra-source-message'):
        changed = []
        def observe(frame, event, argument):
            if event != 'call' or frame.f_code is not boundary:
                return
            active = answer_turns.ACTIVE_ANSWER.get()
            assert active is not None and frame.f_locals['models'] is query.models
            messages = frame.f_locals['messages']
            if mode == 'replace-source-body':
                assert '共享测试正文' in messages[-1]['content']
                messages[-1]['content'] = messages[-1]['content'].replace('共享测试正文', '未获许可的替换正文', 1)
            else:
                messages.append({'role': 'user', 'content': '未覆盖原件的额外材料正文'})
            changed.append(active[1].get_request(active[0]['turn_id']))
        previous, threaded = sys.getprofile(), threading.getprofile()
        threading.setprofile_all_threads(observe)
        try:
            with pytest.raises(HTTPException) as denied:
                await query.ask({'project_id': query.origin_admission.lease.project_id, 'question': '共享测试正文'})
            assert denied.value.status_code == 409
        finally:
            threading.setprofile_all_threads(threaded)
            sys.setprofile(previous)
        assert answer_turns.generate_answer is original and len(changed) == 1
        assert not any(event['type'].startswith('model.')
            for event in app.state.ai_turn_store.events_after(changed[0]['turn_id']))
        observed.extend(changed)
    print('shared-primary: unchanged actual generate caller rejects source-body mutation and uncovered extra message before any model request or POST', flush=True)
    return observed


async def check_shared_history_consumer(query, app, server, client_token, owner):
    from backend.memory_app.v2.shared_history import read_model_history_answer
    from backend.memory_app.v2.workbench import _answer_workbench
    from backend.memory_app.v2.shared_auxiliary import _qualified_commit
    from backend.memory_app.workbench_identity_binding import capture_identity
    from backend.recognition import RecognitionConflict
    from backend.security.user_context import authorize_user, user_context
    from backend.memory_app.v2.policies.pipelines import versions_for_turn
    from backend.memory_app.v2.policies import override
    from backend.memory_app.v2.shared_projects import SharedProjectError

    admission = query.origin_admission
    project = admission.registry.create(owner, project_name='共享历史原生核验',
        operation_id='history-' + uuid4().hex)['project']
    admission.registry.invite(owner, project['id'], user_id=admission.actor.user_id, role='viewer',
        authority=project['authority'], operation_id='history-invite-' + uuid4().hex)
    invitation = next(row for row in admission.registry.invitations(admission.actor)
        if row['project_id'] == project['id'])
    admission.registry.accept(admission.actor, invitation['id'], expected_revision=invitation['revision'],
        operation_id='history-accept-' + uuid4().hex)
    source = query.origin_factory.query(app, admission.actor, project['id'])
    records, store = app.state.recognition_records, app.state.ai_turn_store
    thread = 'thread-history-' + uuid4().hex
    workspace = replace(app.state.workspace_domains, query=source)

    async def complete_empty(question):
        identity = 'turn-history-' + uuid4().hex
        owner = capture_identity(app, records, app.state.recognition_documents, project=project['id'], scene=None,
            thread_id=thread, turn_id=identity)
        with override(compose='@1', retrieve='@1'):
            policies = versions_for_turn('project.answer')
        result = await source.answer_turns.run(turn_id=identity, project=project['id'], question=question,
            policy_versions=policies,
            admission_prepare=source.origin_admission.prepare, privacy_prepare=source.freeze_answer_privacy,
            workbench_identity=owner,
            operation=lambda: _answer_workbench(workspace, records, project['id'], question, None, thread,
                identity, None, None))
        assert result['receipt']['ask']['no_match'] is True
        assert result['receipt']['ask']['citations'] == [] and result['chosen'] == []
        with _qualified_commit(source.origin_admission) as qualification, records.begin() as transaction:
            now = datetime.now(timezone.utc).isoformat()
            transaction.put('v2_turns', identity, {'project_id': project['id'], 'thread_id': thread, 'intent': 'ask',
                'user_text': question, 'receipt': result['receipt'], 'created_at': now, 'updated_at': now,
                'item_id': None, 'instance': 'history-native-check'}, expected_revision=0)
            from backend.memory_app.transaction_records import TransactionRecords
            qualification._actor_reader(TransactionRecords(transaction))
            transaction.commit()
        return records.read('v2_turns', identity)

    first = await complete_empty('空项目有哪些资料')
    print('shared-history: actual legacy no-match turn completed and saved by its personal owner', flush=True)
    actual = read_model_history_answer(source, first.object_id, first.revision)
    assert actual['facts']['answer']['no_match'] is True and actual['model_inputs'] == []
    original = source
    devices = query.origin_factory.devices
    pairing = devices.issue_pairing(user_id=admission.actor.user_id, actor=admission.actor.device_id)
    exchanged = devices.exchange(pairing['code'], name='共享历史新设备')
    actor = devices.authenticate(exchanged['key'])
    access = authorize_user(admission.registry.users, actor, actor.user_id)
    with user_context(access), server.state.server_user_pool.request_scope(actor.user_id, app, access):
        source = query.origin_factory.query(app, actor, project['id'])
        workspace = replace(app.state.workspace_domains, query=source)
        assert source.origin_admission.lease.device_id != original.origin_admission.lease.device_id
        reloaded = read_model_history_answer(source, first.object_id, first.revision)
        assert reloaded['facts']['result_ref'] == actual['facts']['result_ref']
        with pytest.raises((RecognitionConflict, SharedProjectError)):
            source.origin_admission.restore(actual['facts']['request'])
        with pytest.raises(RecognitionConflict, match='material_origin_execution_caller_changed'):
            source.execution_owner._query_for_native_request(actual['facts']['request'])
        current = source.models.public_for_purpose('generation')
        source.models.update('generation', {'expected_revision': current['revision'], 'provider': 'openai',
            'model': 'deepseek-flash', 'base_url': 'https://api.deepseek.com/v1',
            'allow_remote': True, 'api_key': 'isolated-preparation-no-wire'})
        second, proofs = await _prepare_current_history(source, app, project['id'], thread, owner)
        assert proofs[0]['selection']['personal_context']['history'][0]['native_turn_id'] == first.object_id
        assert any(row['collection'] == 'v2_turns' and row['object_id'] == first.object_id
            for row in proofs[0]['selection']['sql_identities'])
    assert not any(event['type'].startswith('model.')
        for identity in (first.object_id, second) for event in store.events_after(identity))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://127.0.0.1:8013') as client:
        visible = await client.get('/api/v2/workbench/threads/' + thread,
            params={'project_id': project['id']}, headers={'Authorization': 'Bearer ' + client_token})
        assert visible.status_code == 200
        unavailable = await client.get('/api/v2/server/shared-projects/' + project['id'],
            headers={'Authorization': 'Bearer ' + client_token})
        assert unavailable.status_code == 403
    print('shared-history: real legacy completion, current-policy new-device input proof and strict original replay; own HTTP body survives lost membership; model events zero', flush=True)
    return [first.object_id, second]


async def _prepare_current_history(source, app, project, thread, owner):
    from backend.memory_app.kernel.answer_turns import ACTIVE_ANSWER
    from backend.memory_app.kernel.answer_continuations import save_plan
    from backend.memory_app.v2.answer_evidence import build_partition_messages
    from backend.memory_app.v2.followup import read_history
    from backend.memory_app.v2.model_material_coverage import build_primary_material_proof
    from backend.memory_app.workbench_identity_binding import capture_identity
    from backend.memory_app.v2.shared_projects import SharedProjectError
    proofs = []
    identity = 'turn-history-current-' + uuid4().hex
    question = '再说明一下'

    async def prepare():
        active = ACTIVE_ANSWER.get()
        request = active[1].get_request(identity)
        _, budget, overhead = source.ask_budget()
        history = read_history(source.execution_owner.records, project, thread, question, query=source,
            budget=budget, prompt_overhead=overhead)
        assert history['turns']
        plan = source.prepare_ask(project, question, history=history['text'])
        plan['history_count'] = len(history['turns'])
        save_plan(source, identity, plan, history, request=request, store=active[1], runtime=app.state.ai_runtime)
        messages = build_partition_messages(plan['chosen'], question, history=history['text'],
            profile=plan['profile'], compose_version=plan['policy_versions']['compose'])
        proof = build_primary_material_proof(source, request, 'answer', messages, plan=plan)
        proofs.append(proof)
        registry, actor = source.origin_admission.registry, source.origin_admission.actor
        detail = registry.detail(owner, project)
        member = next(row for row in detail['members'] if row['user_id'] == actor.user_id)
        registry.change_member(owner, project, user_id=actor.user_id, action='remove',
            expected_revision=member['revision'], authority=detail['project']['authority'],
            operation_id='history-remove-' + uuid4().hex)
        source.validate_answer_request(source.models, request)

    workbench_identity = capture_identity(app, source.execution_owner.records, app.state.recognition_documents, project=project,
        scene=None, thread_id=thread, turn_id=identity)
    with pytest.raises(SharedProjectError) as denied:
        await source.answer_turns.run(turn_id=identity, project=project, question=question,
            admission_prepare=source.origin_admission.prepare, privacy_prepare=source.freeze_answer_privacy,
            workbench_identity=workbench_identity, operation=prepare)
    assert denied.value.status == 403
    assert len(proofs) == 1
    return identity, proofs
