import asyncio
import argparse
import cProfile
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import time
from threading import Event, RLock
from time import perf_counter
from uuid import uuid4

import httpx
import pytest


def run_real_server_source_check(base, *, profile_only=False, profile_calls=False, constraints_only=False,
        primary_only=False):
    started = perf_counter()
    workspace = Path(__file__).resolve().parents[1]
    base = Path(base).resolve()
    if base == workspace or any(part.name == 'runtime' for part in (base, *base.parents)):
        raise ValueError('shared_check_requires_isolated_root')
    previous_root = os.environ.get('CHRIPTMAS_APP_ROOT')
    root = base / ('q' + uuid4().hex[:6])
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(workspace / 'config/settings.toml.example', root / 'config/settings.toml')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.shared_projects import SharedProjectError
    from backend.memory_app.v2.shared_origin_store import SharedOriginTurnStore
    from backend.memory_app.v2.material_origin_admission import BoundMaterialOrigins
    from backend.memory_app.v2.material_origin_privacy import freeze_origin_materials
    from backend.memory_app.v2.privacy import set_private_project
    from backend.recognition import RecognitionConflict
    from backend.security.user_context import authorize_user, user_context
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from core.storage_provider.connection_scope import connection_scope

    devices = DeviceRegistry(root / 'server')
    users = ServerUsers(root, records=devices.records)
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    admin = devices.authenticate(devices.exchange(pairing['code'], name='共享工厂核验')['key'])
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
    project = registry.create(owner, project_name='真实共享查询工厂', operation_id='create-' + uuid4().hex)['project']
    registry.invite(owner, project['id'], user_id=viewer.user_id, role='viewer', authority=project['authority'],
        operation_id='invite-' + uuid4().hex)
    invitation = registry.invitations(viewer)[0]
    registry.accept(viewer, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-' + uuid4().hex)

    async def run():
        async with server.router.lifespan_context(server):
            factory = server.state.shared_query_factory
            pool = server.state.server_user_pool
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url='http://127.0.0.1:8013') as client:
                visible = await client.get('/api/v2/server/shared-projects/' + project['id'],
                    headers={'Authorization': 'Bearer ' + tokens[1]})
                assert visible.status_code == 200
                forbidden = await client.get('/api/v2/server/shared-projects/' + project['id'],
                    headers={'Authorization': 'Bearer ' + tokens[2]})
                assert forbidden.status_code == 403
            async with pool.lease(viewer.user_id) as app:
                access = authorize_user(registry.users, viewer, viewer.user_id)
                with user_context(access), pool.request_scope(viewer.user_id, app, access):
                    personal = app.state.workspace_domains.query
                    assert app.state.shared_query_factory is factory
                    query = factory.query(app, viewer, project['id'])
                    print(f'shared-rendered: actual server setup and query construction {perf_counter() - started:.2f}s', flush=True)
                    assert query.execution_owner is query.profile_owner is personal
                    assert query.models is personal.models is app.state.recognition_models
                    assert query.receipt_records is personal.records is app.state.recognition_records
                    assert query.records is not personal.records
                    assert query.source_namespace == query.origin_admission.lease.namespace
                    assert factory.query(app, viewer, project['id']) is query
                    assert personal.origin_admission is None and personal.execution_owner is personal
                    local = query.local_reader()
                    assert local is not query and local.origin_admission is query.origin_admission
                    assert local.profile_owner is local.execution_owner is personal
                    assert local.offline is True and query.offline is False
                    assert local.origin_factory is factory and local.source_namespace == query.source_namespace
                    assert local.material_owner is query.material_owner and local.answer_turns is query.answer_turns
                    query.answer_turns._runtime()
                    assert isinstance(app.state.ai_turn_store, SharedOriginTurnStore)
                    assert app.state.ai_turn_store._path == registry.users.root_for(viewer.user_id) / '.rebuild-data/ai-turns.sqlite3'
                    data = registry.roots._data(project['id'])
                    turns = ([] if constraints_only or primary_only else _check_rendered_source_consumers(query, app, data, owner,
                        profile_only=profile_only, profile_calls=profile_calls))
                    if primary_only:
                        from check_shared_primary import (
                            check_primary_preparation, check_primary_wire_preparation, check_shared_history_consumer,
                        )
                        history_requests = await check_shared_history_consumer(query, app, server, tokens[1], owner)
                        constraint_requests = check_primary_preparation(query, app, data)
                        constraint_requests.extend(await check_primary_wire_preparation(query, app, owner))
                    else:
                        constraint_requests = _check_shared_constraint_consumer(query, app, data,
                            verify_materials=not constraints_only)
                    with pytest.raises(RecognitionConflict, match='shared_material_read_only'):
                        query.records.begin()
                    origins = BoundMaterialOrigins(query.origin_admission, query.origin_admission.lease, {})
                    _, privacy, _ = freeze_origin_materials(origins, query.models, project['id'], [])
                    assert privacy['allow_remote'] is False
                    configuration = query.models.public_for_purpose('generation')
                    query.models.update('generation', {'expected_revision': configuration['revision'], 'allow_remote': True})
                    _, privacy, _ = freeze_origin_materials(origins, query.models, project['id'], [])
                    assert privacy['allow_remote'] is True
                    set_private_project(data.records, project['id'], True, 0)
                    _, private, _ = freeze_origin_materials(origins, query.models, project['id'], [])
                    assert private['allow_remote'] is False
                    assert data.records.list('v2_turns') == ()
                    assert data.records.list('workspace_ask_receipts') == ()
                    personal_request = _check_personal_query_dispatch(personal, app)
                    retained_read = query.records.read
                    detail = registry.detail(owner, project['id'])
                    member = next(row for row in detail['members'] if row['user_id'] == viewer.user_id)
                    registry.change_member(owner, project['id'], user_id=viewer.user_id, action='remove',
                        expected_revision=member['revision'], authority=detail['project']['authority'],
                        operation_id='remove-' + uuid4().hex)
                    for read in (query.validate_retrieval_origin, local.validate_retrieval_origin,
                            lambda: retained_read('projects', project['id']),
                            lambda: factory.query(app, viewer, project['id']),
                            *(turn.validate for turn in turns),
                            *(lambda value=value: personal.project_constraint_input.validate(value)
                                for value in constraint_requests)):
                        with pytest.raises(SharedProjectError) as denied:
                            read()
                        assert denied.value.status == 403
                    assert personal._query_for_native_request(personal_request) is personal
                    personal.validate_answer_request(personal.models, personal_request)
                    assert personal.origin_admission is None and app.state.workspace_domains.query is personal
                    with app.state.ai_turn_store._connect() as connection:
                        assert connection.execute('SELECT COUNT(*) FROM ai_turns').fetchone()[0] == len(turns) + len(constraint_requests) + 1 + (len(history_requests) if primary_only else 0)
    try:
        with connection_scope():
            asyncio.run(run())
    finally:
        if previous_root is None:
            os.environ.pop('CHRIPTMAS_APP_ROOT', None)
        else:
            os.environ['CHRIPTMAS_APP_ROOT'] = previous_root


def _check_personal_query_dispatch(query, app):
    from backend.memory_app.kernel.turn_requests import freeze_turn_request
    from backend.memory_app.workbench_identity_binding import prepare_admission

    identity, project = 'personal-native-' + uuid4().hex, 'personal-query-' + uuid4().hex
    request = freeze_turn_request('project.answer', turn_id=identity, session_id='session-' + identity,
        operation_id='answer-' + identity, idempotency_key='answer-' + identity, project_id=project,
        created_at=datetime.now(timezone.utc).isoformat(), text='个人问答资格核验',
        privacy=query.freeze_answer_privacy(project, local_only=True),
        capabilities=['workbench.answer.execute'], template_version=2)
    request['context_policy'].update(include_memory=False)
    request['capability_request'] = {'mode': 'execute_exact_v1', 'capability_id': 'workbench.answer.execute',
        'arguments': {'query': request['input']['text']}}
    store = app.state.ai_turn_store
    bundle = prepare_admission(app, request, None, store)
    with store.admission_bundle(request, bundle):
        assert store.claim_turn(request) == (identity, True)
    assert query._query_for_native_request(request) is query
    query.validate_answer_request(query.models, request)
    assert not any(event['type'].startswith('model.') for event in store.events_after(identity))
    print('shared-query: actual ordinary personal native request retains its original owner', flush=True)
    return request


def _check_shared_constraint_consumer(query, app, data, *, verify_materials):
    from backend.recognition import RecognitionConflict, WorkScope
    from backend.memory_app.constraints import ProjectConstraintService
    from backend.memory_app.kernel.turn_requests import freeze_turn_request
    from backend.memory_app.transaction_records import TransactionRecords
    from backend.memory_app.workbench_identity_binding import prepare_admission
    from backend.memory_app.v2.material_origin_admission import BoundMaterialOrigins
    from backend.memory_app.v2.material_origin_privacy import freeze_origin_materials
    from backend.memory_app.v2.project_constraint_input import KIND
    from backend.memory_app.v2.shared_auxiliary import _qualified_commit
    from core.ai_kernel.product_admission import seal_admission
    from backend.memory_app.workspace_query import WorkspaceQuery
    from backend.memory_app.source_egress import recognition_service
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.v2.shared_runtime import SharedWriteRecords

    project, store = query.origin_admission.lease.project_id, app.state.ai_turn_store
    scope = WorkScope('local-user', project)
    personal = query.execution_owner.project_constraint_input
    shared = query.project_constraint_input
    requests = []
    context, entered = copy_context(), Event()

    def read_from_other_thread():
        def observe(frame, event, value):
            if event == 'call' and frame.f_code is WorkspaceQuery.validate_retrieval_origin.__code__:
                entered.set()
            return observe
        sys.setprofile(observe)
        try:
            return query.origin_factory.query(app, query.origin_admission.actor, project,
                lease=query.origin_admission.lease)
        finally:
            sys.setprofile(None)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with _qualified_commit(query.origin_admission):
            future = executor.submit(context.run, read_from_other_thread)
            assert entered.wait(10)
            acquired = query.origin_factory._lock.acquire(timeout=1)
            if acquired:
                query.origin_factory._lock.release()
        assert future.result(timeout=10) is query
        assert acquired
    print('shared-constraints: actual cross-thread source validation leaves factory dictionary available', flush=True)
    admission = query.origin_admission
    writable = admission.registry.create(admission.actor, project_name='共享写事务读取核验',
        operation_id='write-read-' + uuid4().hex)['project']
    write_query = query.origin_factory.query(app, admission.actor, writable['id'])
    writer = SharedWriteRecords(admission.registry, admission.actor, writable['id'], authority=writable['authority'],
        action='read-check', object_kind='source', object_id='read-check', object_title='借读核验')
    with writer.begin() as transaction:
        borrowed = TransactionRecords(transaction)
        service = recognition_service(borrowed, source_owner=write_query.material_owner)
        assert service._records.read('v2_shared_root', 'identity').payload['project_id'] == writable['id']
        with _qualified_commit(write_query.origin_admission):
            service = recognition_service(borrowed, source_owner=write_query.material_owner)
            assert service._records.read('v2_shared_root', 'identity').payload['project_id'] == writable['id']
    print('shared-constraints: existing shared writer and qualified original writer reader checks passed', flush=True)

    def prepare(*, missing=False, kind='project.answer'):
        identity = 'shared-constraint-' + uuid4().hex
        origins = BoundMaterialOrigins(query.origin_admission, query.origin_admission.lease, {})
        _, privacy, _ = freeze_origin_materials(origins, query.models, project, [], local_only=True)
        request = freeze_turn_request(kind, turn_id=identity, session_id='session-' + identity,
            operation_id='answer-' + identity, idempotency_key='answer-' + identity, project_id=project,
            created_at=datetime.now(timezone.utc).isoformat(),
            text='共享规则检查' if kind == 'project.answer' else '{"task":"共享规则检查"}', privacy=privacy,
            **({'capabilities': ['workbench.answer.execute']} if kind == 'project.answer' else {}),
            template_version=2 if kind == 'project.answer' else 3)
        request['context_policy'].update(include_memory=False)
        if kind == 'project.answer':
            request['capability_request'] = {'mode': 'execute_exact_v1', 'capability_id': 'workbench.answer.execute',
                'arguments': {'query': request['input']['text']}}
        if missing:
            origin = query.origin_admission.prepare(request)[0]
            bundle = seal_admission(request, (origin,), store)
        else:
            bundle = prepare_admission(app, request, None, store,
                admission_prepare=query.origin_admission.prepare)
        with store.admission_bundle(request, bundle):
            assert store.claim_turn(request) == (identity, True)
        requests.append(request)
        return request

    empty = prepare()
    assert shared.validate(empty)['items'] == []
    assert personal.validate(empty)['items'] == []
    print('shared-constraints: actual native empty source rule set preserved', flush=True)
    source_rules = ProjectConstraintService(data.records)
    source_rules.upsert(scope, 'shared-rule', 0, '共享回答应注明材料范围')
    ProjectConstraintService(app.state.recognition_records).upsert(scope, 'shared-rule', 0, '操作者空间的同编号规则')
    source_rules.upsert(scope, 'shared-disabled', 0, '停用规则', enabled=False)
    source_rules.upsert(scope, 'shared-future', 0, '未来规则',
        valid_from=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    source_rules.upsert(WorkScope('local-user', 'other-project'), 'shared-other', 0, '其他项目规则')
    with pytest.raises(RecognitionConflict, match='project constraints changed'):
        personal.validate(empty)
    active = prepare()
    snapshot = personal.validate(active)
    assert query.execution_owner._query_for_native_request(active) is query
    query.execution_owner.validate_answer_request(query.models, active)
    rebuilt_models = ModelConfiguration(app.state.recognition_records, app.state.recognition_runtime_root,
        secrets=query.models.secrets)
    with pytest.raises(RecognitionConflict, match='material_origin_model_owner_changed'):
        query.execution_owner.validate_answer_request(rebuilt_models, active)
    with pytest.raises(RecognitionConflict, match='material_origin_model_owner_changed'):
        query.execution_owner.freeze_materials(rebuilt_models, project, [], local_only=True, native_request=active)
    allowed, privacy, selection = query.execution_owner.freeze_materials(query.models, project, [],
        local_only=True, native_request=active)
    assert allowed == [] and privacy['allow_remote'] is False
    assert selection['origin_materials'] == [] and selection['origin_ref'] == query.origin_admission._reference(active)
    if verify_materials:
        source_item = data.records.read('workspace_items', 'rendered-item')
        assert source_item is not None
        descriptor = {'type': 'original_item', 'id': source_item.object_id, 'revision': source_item.revision,
            'project_id': project}
        allowed, privacy, selection = query.execution_owner.freeze_materials(query.models, project, [descriptor],
            local_only=True, native_request=active)
        assert len(allowed) == 1 and privacy['source_snapshots'][0]['scope']['project_id'] == project
        assert selection['origin_materials'] == [{**descriptor, 'namespace': query.source_namespace}]
    assert shared.validate(active, reader=app.state.recognition_records) == snapshot
    with pytest.raises(RecognitionConflict, match='project constraints reader changed'):
        shared.validate(active, reader=data.records)
    assert [(row['id'], row['content']) for row in snapshot['items']] == [('shared-rule', '共享回答应注明材料范围')]
    assert snapshot['source']['namespace'] == query.source_namespace
    task = prepare(kind='project.task')
    assert personal.validate(task) == snapshot
    messages = personal.messages(active, [{'role': 'user', 'content': '共享规则检查'}])
    assert messages[0]['role'] == 'system' and '共享回答应注明材料范围' in messages[0]['content']
    assert '操作者空间的同编号规则' not in messages[0]['content']
    reference = personal.record_input(active, active, messages, store, logical_input={'shared': active['turn_id']})
    assert store.get(reference)['messages'] == messages
    changed = deepcopy(snapshot)
    changed['items'][0]['content'] = '同编号同修订的替换规则'
    with pytest.raises(RecognitionConflict, match='project constraints changed'):
        shared.validate_plan({'project_id': project, '_project_constraints': changed})
    missing = prepare(missing=True)
    with pytest.raises(RecognitionConflict, match='project constraints shared admission unavailable'):
        personal.validate(missing)
    with app.state.recognition_records.begin() as transaction:
        with pytest.raises(RecognitionConflict, match='project constraints shared commit scope unavailable'):
            personal.validate_public_answer(active['turn_id'], reader=TransactionRecords(transaction))
    with _qualified_commit(query.origin_admission), app.state.recognition_records.begin() as transaction:
        assert personal.validate_public_answer(active['turn_id'], reader=TransactionRecords(transaction)) == snapshot
        transaction.put('v2_shared_constraint_inputs', active['turn_id'], {'project_id': project}, expected_revision=0)
        transaction.commit()
    assert app.state.recognition_records.read('v2_shared_constraint_inputs', active['turn_id']) is not None
    print('shared-constraints: source rule body, actor ownership and final transaction checks passed', flush=True)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=30)
    source_rules.upsert(scope, 'shared-expiring', 0, '短期规则', valid_until=expiry.isoformat())
    expiring = prepare()
    assert {row['id'] for row in personal.validate(expiring)['items']} == {'shared-rule', 'shared-expiring'}
    time.sleep(max(0, (expiry - datetime.now(timezone.utc)).total_seconds()) + 0.05)
    with pytest.raises(RecognitionConflict, match='project constraints changed'):
        personal.validate(expiring)
    for request in requests:
        assert not any(event['type'].startswith('model.') for event in store.events_after(request['turn_id']))
        bundle = store.restore_admission(request)
        assert sum(item.kind == KIND for item in bundle.items) == (0 if request is missing else 1)
    print('shared-constraints: native messages, SQL owner, final transaction, empty set and expiry checks passed', flush=True)
    return requests


def _check_rendered_source_consumers(query, app, data, owner, *, profile_only=False, profile_calls=False):
    from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
    from backend.recognition_retrieval import SQLiteEmbeddingCache, HttpEmbeddingProvider, retrieve
    from backend.recognition_retrieval.service import _entry
    from backend.memory_app.retrieval_models import ConfiguredTransport
    from backend.memory_app.v2.shared_auxiliary import SharedAuxiliaryTurn
    from backend.memory_app.v2.shared_auxiliary import _qualified_commit
    from backend.memory_app.v2.shared_auxiliary import _QualifiedCommitScope
    from backend.memory_app.v2.shared_auxiliary import _AdmittedStore
    from core.ai_kernel.product_admission import MANIFEST_KIND
    from backend.memory_app.v2.shared_rendered_preparation import (
        RenderedEmbeddingBatch, recognition_wire_input, chunk_wire_input, rerank_wire_input,
    )
    from backend.memory_app.v2.material_origin_privacy import validate_origin_privacy
    from backend.memory_app.v2.material_origin_sql import freeze_sql_identities
    from backend.memory_app.kernel.answer_continuations import decode, encode
    from backend.memory_app.context_adapter import format_recognition_content
    from backend.memory_app.source_egress import SourceEgressService, recognition_service
    from backend.memory_app.v2.layers import summary_of
    from backend.memory_app.v2.shared_vector_keys import rendered_chunk_text, material_vector_namespace
    from backend.memory_app.v2.contextual_chunk_vectors import chunk_vector_scores, embedding_input_validation
    from backend.memory_app.v2.material_origins import SharedMaterialOwner, SharedProjectLease
    from backend.memory_app.v2.material_origin_readers import _ReadConnection, _ReadCursor
    from backend.memory_app.v2.material_origin_readers import qualified_record_read
    from backend.memory_app.v2.shared_projects import SharedProjects
    from backend.memory_app.v2.shared_runtime import SharedDataRoots
    from backend.memory_app.v2.server_deletion import _directory
    from backend.memory_app.v2.material_origin_sql import (
        _identity, validate_sql_identities, _CommitMetadataReader, MaterialSQLCollector,
    )
    from backend.memory_app.v2.shared_extraction import PersonalDestination
    from backend.memory_app.v2.material_origin_admission import SharedOriginAdmission
    from backend.memory_app.v2.material_origin_admission import BoundMaterialOrigins
    from backend.memory_app.transaction_records import TransactionRecords
    from core.storage_provider.sqlite_uow import SQLiteUnitOfWorkError
    from backend.memory_app.workspace_items import WorkspaceItems
    from backend.memory_app.workspace_confirmation import WorkspaceConfirmation
    from backend.memory_app.workspace_generation import _draft
    from backend.memory_app.processing_lease import ProcessingLease
    from backend.memory_app.document_recognition import ensure_document_experience
    from core.search_and_recall.evidence_windows import EvidenceWindow

    project = query.origin_admission.lease.project_id
    models = app.state.recognition_models
    scope = WorkScope('local-user', project)
    with data.records.begin_read() as source_reader:
        authority = SourceEgressService(data.records, reader=source_reader)
        assert authority._owner_records is data.records
        with pytest.raises(RecognitionConflict, match='source reader owner changed'):
            SourceEgressService(query.execution_owner.records, reader=source_reader)
    items = WorkspaceItems(data.records, ProcessingLease(data.records, 'workspace_items', 'rendered-real'), RLock())
    draft = _draft({'title': '原材料', 'summary': '真实摘要', 'topics': [], 'facts': [], 'todos': [],
        'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}, '真实材料正文')
    uploaded = items.create_upload({'id': 'rendered-item', 'project_id': project, 'input_kind': 'text',
        'title': '原材料', 'source_text': '真实材料正文', 'status': 'ready', 'draft': draft, 'error': None,
        'document_id': None, 'created_at': '2026-11-04T00:00:00Z'})
    document = WorkspaceConfirmation(data.root, data.records, data.documents).confirm(
        uploaded['id'], project, uploaded['revision'],
        lambda value: '# 原材料\n\n## 摘要\n\n真实摘要\n\n## 内容\n\n真实材料正文\n\n真实材料正文\n')
    service = RecognitionService(data.records)
    experience, _ = ensure_document_experience(data.documents, service, project, document['document_id'])
    insights = []
    for conditions in ([], ['仅适用场景乙']):
        proposal = service.propose(scope=scope, content='真实认识正文', conditions=conditions,
            source_experience_ids=[experience])
        insights.append(service.publish(scope=scope, candidate_id=proposal.id, expected_revision=proposal.revision,
            reviewer=owner.user_id))

    origins = BoundMaterialOrigins(query.origin_admission, query.origin_admission.lease, {})
    with _qualified_commit(query.origin_admission) as qualification:
        with qualified_record_read(app.state.recognition_records, validate=qualification._validate) as actor_transaction:
            actor_reader = TransactionRecords(actor_transaction)
            source_reader = qualification.reader_for(origins, origins.shared, actor_reader=actor_reader)
            assert source_reader is qualification._source_reader
            assert qualification.reader_for(origins, origins.profile, actor_reader=actor_reader) is actor_reader
            collected_source = MaterialSQLCollector(source_reader)
            qualified = recognition_service(TransactionRecords(collected_source),
                source_owner=query.material_owner)
            source_entry = qualified._qualified_recognition(collected_source,
                qualified._require_scope(collected_source, 'recognitions', insights[0].id, scope))
            assert source_entry is not None and source_entry.authorized
            assert source_entry.retrieval_projection()['content'] == '真实认识正文'
            for invalid in (actor_reader, TransactionRecords(actor_transaction)):
                with pytest.raises(RecognitionConflict, match='material_origin_commit_source_reader_changed'):
                    recognition_service(invalid, source_owner=query.material_owner)
            with qualified_record_read(data.records, validate=qualification._validate) as separate_source:
                with pytest.raises(RecognitionConflict, match='material_origin_commit_source_reader_changed'):
                    recognition_service(TransactionRecords(separate_source), source_owner=query.material_owner)
            with pytest.raises(RecognitionConflict, match='material_origin_commit_actor_reader_changed'):
                qualification.reader_for(origins, origins.shared, actor_reader=TransactionRecords(source_reader))
            captured = copy_context()
            with ThreadPoolExecutor(max_workers=1) as worker:
                with pytest.raises(RecognitionConflict, match='material_origin_commit_scope_expired'):
                    worker.submit(captured.run, query.validate_retrieval_origin).result(timeout=5)
    assert source_reader.closed and actor_transaction.closed
    with pytest.raises(SQLiteUnitOfWorkError, match='closed'):
        source_reader.read('v2_projects', project)
    with pytest.raises(RecognitionConflict, match='material_origin_commit_scope_expired'):
        captured.run(query.validate_retrieval_origin)
    with pytest.raises(RecognitionConflict, match='material_origin_commit_scope_expired'):
        qualification.reader_for(origins, origins.shared, actor_reader=actor_reader)
    with pytest.raises(RecognitionConflict, match='material_origin_commit_scope_expired'):
        captured.run(recognition_service, TransactionRecords(source_reader), source_owner=query.material_owner)
    query.validate_retrieval_origin()

    personal_records = app.state.recognition_records
    personal_items = WorkspaceItems(personal_records,
        ProcessingLease(personal_records, 'workspace_items', 'profile-real'), RLock())
    personal_draft = _draft({'title': '个人原材料', 'summary': '个人摘要', 'topics': [], 'facts': [], 'todos': [],
        'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}, '个人材料正文')
    personal_uploaded = personal_items.create_upload({'id': 'rendered-item', 'project_id': 'me', 'input_kind': 'text',
        'title': '个人原材料', 'source_text': '个人材料正文', 'status': 'ready', 'draft': personal_draft, 'error': None,
        'document_id': None, 'created_at': '2026-11-04T00:00:00Z'})
    WorkspaceConfirmation(app.state.recognition_runtime_root, personal_records, app.state.recognition_documents).confirm(
        personal_uploaded['id'], 'me', personal_uploaded['revision'], lambda value: '# 个人原材料\n\n个人材料正文\n')
    personal_item = personal_records.read('workspace_items', personal_uploaded['id'])
    personal_material = {'type': 'original_item', 'id': personal_item.object_id, 'revision': personal_item.revision,
        'project_id': 'me', 'namespace': origins.profile.namespace}
    personal_sql = freeze_sql_identities(origins, [personal_material])
    assert personal_sql and all(row['namespace'] == origins.profile.namespace for row in personal_sql)
    assert any(row['collection'] == 'workspace_items' and row['object_id'] == 'rendered-item' for row in personal_sql)
    with pytest.raises(RecognitionConflict, match='material_origin_scope_changed'):
        freeze_sql_identities(origins, [{**personal_material, 'namespace': origins.shared.namespace}])
    print('shared-rendered: actual source and actor readers, expiry, thread and profile checks passed', flush=True)
    entries = [query.service.get_recognition(scope=scope, recognition_id=insight.id).retrieval_projection()
        for insight in insights]
    materials = [{'type': 'recognition', 'id': row['id'], 'revision': row['revision'],
        'project_id': project, 'namespace': query.source_namespace} for row in entries]
    assert entries[0]['content'] == entries[1]['content'] and entries[0]['conditions'] != entries[1]['conditions']
    print('shared-rendered: source records prepared', flush=True)
    for purpose in ('embedding', 'rerank'):
        models.update(purpose, {'expected_revision': models.public()[purpose]['revision'],
            'base_url': 'http://127.0.0.1:9/v1', 'model': 'no-wire-verification', 'allow_remote': False})
    turns = []

    counters = {function.__code__: name for name, function in {
        'lease_validate': SharedProjectLease.validate,
        'lease_data_scope': SharedProjectLease._current_data.__wrapped__,
        'source_owner_validate': SharedMaterialOwner.validate_current,
        'shared_root_read': SharedDataRoots._data,
        'directory_check': _directory,
        'central_authorize': SharedProjects.authorize,
        'source_sql_execute': _ReadConnection.execute,
        'source_sql_fetchone': _ReadCursor.fetchone,
        'source_sql_fetchall': _ReadCursor.fetchall,
        'freeze_sql': freeze_sql_identities,
        'validate_sql': validate_sql_identities,
        'sql_identity': _identity,
        'commit_metadata_read': _CommitMetadataReader.read,
        'commit_scope_full_validate': _QualifiedCommitScope._validate,
        'execution_owner_validate': SharedOriginAdmission.validate_execution_owner,
        'personal_destination_validate': PersonalDestination.validate,
    }.items()}

    def observed(stage, call, *args, enabled=True, **kwargs):
        if not profile_calls or not enabled:
            return call(*args, **kwargs)
        observation = cProfile.Profile()
        try:
            return observation.runcall(call, *args, **kwargs)
        finally:
            counts = dict.fromkeys(counters.values(), 0)
            counts.update({counters[row.code]: row.callcount for row in observation.getstats() if row.code in counters})
            print('shared-rendered-profile: ' + json.dumps({'stage': stage, 'calls': counts}, sort_keys=True), flush=True)

    def checked_turns():
        for turn in turns:
            assert not any(event['type'].startswith('model.') for event in app.state.ai_turn_store.events_after(turn.turn_id))
        assert data.records.list('v2_memory_turn_keys') == ()
        print('shared-rendered: all model event counts remain zero', flush=True)
        return turns

    def claim_after_source_revision(turn, insight, *, replay):
        native_before = app.state.ai_turn_store.get_request(turn.turn_id)
        manifest_before = app.state.ai_turn_store.get_immutable_payload(turn.turn_id, MANIFEST_KIND)
        assert (native_before is not None) is replay
        assert (manifest_before is not None) is replay
        changed = []
        boundary = _qualified_commit.__wrapped__.__code__
        previous = sys.getprofile()

        def observe_scope(frame, event, argument):
            if (event == 'call' and frame.f_code is boundary and not changed
                    and frame.f_locals.get('admission') is query.origin_admission):
                caller = frame.f_back
                while caller is not None and caller.f_code is not _AdmittedStore._claim_with_source_sql.__code__:
                    caller = caller.f_back
                if caller is None:
                    return
                # 观察真实资格事务入口，在取得其锁之前提交原领域服务的实际修订。
                changed.append(service.revise(scope=scope, recognition_id=insight.id,
                    expected_revision=insight.revision, content='接纳前已经修订的认识'))

        failure = None
        sys.setprofile(observe_scope)
        try:
            try:
                turn.store.claim_turn(turn.request)
            except RecognitionConflict as error:
                failure = error
        finally:
            sys.setprofile(previous)
        assert len(changed) == 1 and changed[0].revision > insight.revision
        native_after = app.state.ai_turn_store.get_request(turn.turn_id)
        manifest_after = app.state.ai_turn_store.get_immutable_payload(turn.turn_id, MANIFEST_KIND)
        print('shared-rendered: claim race ' + ('replay' if replay else 'first')
            + ' native=' + str(native_after is not None) + ' manifest=' + str(manifest_after is not None)
            + ' rejected=' + str(failure is not None), flush=True)
        assert native_after == native_before and manifest_after == manifest_before
        assert failure is not None and str(failure) == 'material_origin_sql_identity_changed'

    def claim(preparation, payload, selected, label, purpose='embedding'):
        started = perf_counter()
        with preparation as prepared:
            turn = observed('prepare', SharedAuxiliaryTurn, query.origin_admission, models, kind='memory.link_suggest',
                key=label, materials=selected, validate=query.validate_retrieval_origin,
                purpose=purpose, rendered_input=prepared, enabled=label == 'duplicate')
            prepared_at = perf_counter()
            accepted = observed('native-claim', turn.store.claim_turn, turn.request, enabled=label == 'duplicate')
            assert accepted == (turn.turn_id, True)
            claimed_at = perf_counter()
        print(f'shared-rendered: {label} prepare {prepared_at - started:.2f}s native-claim {claimed_at - prepared_at:.2f}s', flush=True)
        with pytest.raises(RecognitionConflict, match='shared_rendered_input_preparation_expired'):
            prepared.public_payload
        if label == 'duplicate':
            turn.validate()
            assert turn.store.claim_turn(turn.request) == (turn.turn_id, False)
            print(f'shared-rendered: repeated validation and claim {perf_counter() - claimed_at:.2f}s', flush=True)
        origins = query.origin_admission.restore(turn.request)
        auxiliary = origins.auxiliary
        assert auxiliary['selection']['schema_version'] == 3
        assert auxiliary['rendered_input']['wire_payload'] == payload
        assert freeze_sql_identities(origins, selected) == auxiliary['selection']['sql_identities']
        assert app.state.ai_turn_store.get_request(turn.turn_id) == turn.request
        turns.append(turn)
        print('shared-rendered: claimed ' + label, flush=True)
        return turn, origins

    payload = {'model': 'no-wire-verification', 'input': ['提问正文', *[row['content'] for row in entries]]}
    duplicate, duplicate_origins = claim(recognition_wire_input(query, payload=payload, question='提问正文',
        entries=entries), payload, materials, 'duplicate')
    assert [row['qualified_material']['id'] for row in duplicate_origins.auxiliary['rendered_input']['items'][1:]] == [
        insight.id for insight in insights]
    original_auxiliary = deepcopy(duplicate_origins.auxiliary)
    assert original_auxiliary['selection']['sql_identities']
    for changed_part in ('revision-type', 'lineage', 'missing'):
        changed_sql = deepcopy(original_auxiliary)
        identities = changed_sql['selection']['sql_identities']
        if changed_part == 'revision-type':
            identities[0]['revision'] = True
            expected_error = 'material_origin_sql_identity_changed'
        elif changed_part == 'lineage':
            identities[0]['fact_id'] = '0' * 32
            expected_error = 'material_origin_auxiliary_sql_identity_unavailable'
        else:
            identities.pop()
            expected_error = 'material_origin_auxiliary_sql_identity_unavailable'
        duplicate_origins.auxiliary = changed_sql
        with pytest.raises(RecognitionConflict, match=expected_error):
            validate_origin_privacy(duplicate_origins, models, project, duplicate.request['privacy'],
                purpose='embedding', selection=changed_sql['selection'])
    duplicate_origins.auxiliary = original_auxiliary
    changed = deepcopy(duplicate_origins.auxiliary)
    candidate = decode(changed['rendered_input']['items'][2]['rendering']['candidate'])
    candidate[0]['entry']['conditions'] = []
    candidate[0]['excerpt'] = format_recognition_content(candidate[0]['entry'])
    changed['rendered_input']['items'][2]['rendering']['candidate'] = encode(candidate)
    duplicate_origins.auxiliary = changed
    with pytest.raises(RecognitionConflict, match='material_origin_model_body_changed'):
        validate_origin_privacy(duplicate_origins, models, project, duplicate.request['privacy'],
            purpose='embedding', selection=changed['selection'])

    race_proposal = service.propose(scope=scope, content='并发来源正文', source_experience_ids=[experience])
    race_insight = service.publish(scope=scope, candidate_id=race_proposal.id,
        expected_revision=race_proposal.revision, reviewer=owner.user_id)
    race_entry = query.service.get_recognition(scope=scope, recognition_id=race_insight.id).retrieval_projection()
    race_material = {'type': 'recognition', 'id': race_entry['id'], 'revision': race_entry['revision'],
        'project_id': project, 'namespace': query.source_namespace}
    race_payload = {'model': 'no-wire-verification', 'input': ['提问正文', race_entry['content']]}
    with recognition_wire_input(query, payload=race_payload, question='提问正文', entries=[race_entry]) as prepared:
        race_turn = SharedAuxiliaryTurn(query.origin_admission, models, kind='memory.link_suggest',
            key='claim-race', materials=[race_material], validate=query.validate_retrieval_origin,
            purpose='embedding', rendered_input=prepared)
    claim_after_source_revision(race_turn, race_insight, replay=False)
    if profile_only:
        claim_after_source_revision(duplicate, insights[0], replay=True)
        return checked_turns()

    projections = [{'id': row['id'], 'revision': row['revision'], 'project_id': project,
        'content': row['content']} for row in entries]
    cache = SQLiteEmbeddingCache(str(app.state.recognition_runtime_root / 'manual-batch-cache.sqlite3'))
    partial_turns = []
    try:
        for cached_position in (0, 1):
            provider = HttpEmbeddingProvider(ConfiguredTransport('not-sent'),
                'http://127.0.0.1:9/v1/embeddings', 'cache-io-only-' + str(cached_position))
            # 手动向量只充当真实缓存记录；在调用 embed 之前明确终止检索。
            cache.write(model_id=provider.cache_identity, entry=_entry(projections[cached_position]), vector=(1.0,))
            batch, seen = RenderedEmbeddingBatch(projections), []

            class PreparedBatchObserved(BaseException):
                pass

            class CacheObserver:
                def read(self, *, model_id, entry):
                    value = cache.read(model_id=model_id, entry=entry)
                    batch.observe(entry, value)
                    seen.append(entry)
                    if len(seen) == len(projections):
                        raise PreparedBatchObserved()
                    return value

                def write(self, *, model_id, entry, vector):
                    cache.write(model_id=model_id, entry=entry, vector=vector)

            with pytest.raises(PreparedBatchObserved):
                retrieve(project, ' \n提问正文 \t', projections, limit=2, vector_candidate_limit=2,
                    keyword_enabled=False, embedding_provider=provider, embedding_cache=CacheObserver())
            missing = 1 - cached_position
            assert batch.missing_ids == (entries[missing]['id'],)
            partial_payload = {'model': 'no-wire-verification', 'input': ['提问正文', entries[missing]['content']]}
            partial, origins = claim(recognition_wire_input(query, payload=partial_payload, question='提问正文',
                entries=[entries[missing]]), partial_payload, materials, 'partial')
            assert [row['qualified_material']['id'] for row in origins.auxiliary['rendered_input']['items'][1:]] == [
                entries[missing]['id']]
            partial_turns.append(partial)
        assert partial_turns[0].rendered_payload == partial_turns[1].rendered_payload
        assert partial_turns[0].turn_id != partial_turns[1].turn_id
    finally:
        cache.close()

    doc_entry = query.query_entries(project, selected=[{'kind': 'document', 'id': document['document_id']}])[0]
    markdown = data.documents.markdown(document['document_id'])
    summary, _, _ = summary_of(markdown)
    snapshot = query.original_snapshot(scope, doc_entry, SourceEgressService(query.records))
    raw_rendered = rendered_chunk_text(doc_entry, summary, markdown)
    assert summary == '真实摘要' and raw_rendered.endswith('\n')
    assert material_vector_namespace(query, project, raw_rendered, 'cache-only') == material_vector_namespace(
        query, project, raw_rendered.strip(), 'cache-only')
    doc_materials = [{'type': 'document', 'id': doc_entry['id'], 'revision': doc_entry['revision'],
        'project_id': project, 'namespace': query.source_namespace}]
    for source in snapshot['roots']:
        material = {**source, 'project_id': project, 'namespace': query.source_namespace}
        if material not in doc_materials:
            doc_materials.append(material)
    models.update('embedding', {'expected_revision': models.public()['embedding']['revision'],
        'enabled': True, 'api_key': 'io-only-not-a-credential'})
    actual_material = query.retrieval_index.vector_material(project, doc_entry, 'L1')
    assert actual_material is not None
    actual_window = next(window for window in actual_material[3] if window.text.endswith('\n'))
    observed_wire = []

    class PreparedSourceWireObserved(BaseException):
        pass

    def capture_original_wire(value):
        if isinstance(value, list) and value and all(isinstance(text, str) for text in value):
            observed_wire.append(value)
            raise PreparedSourceWireObserved()

    with embedding_input_validation(capture_original_wire), pytest.raises(PreparedSourceWireObserved):
        chunk_vector_scores(query, project, doc_entry, scope, snapshot,
            [EvidenceWindow(0, len(actual_window.text), actual_window.text)],
            layer='L1', span_start=actual_window.start, summary=summary, question=' \n提问正文 \t')
    assert observed_wire == [['提问正文', rendered_chunk_text(doc_entry, summary, actual_window.text).strip()]]
    print('shared-rendered: actual normalized wire observed before model call', flush=True)
    assert actual_window.text.endswith('\n')
    from backend.memory_app.v2.insight_vectors import insight_vector_scores
    from backend.memory_app.v2.reranking import layer_reranker
    local = query.local_reader()
    assert layer_reranker(local, project, '提问正文') is None
    assert insight_vector_scores(local, project, scope, entries, '提问正文') == {}
    assert chunk_vector_scores(local, project, doc_entry, scope, snapshot,
        [actual_window], layer='L1', span_start=0, summary=summary, question='提问正文') == {}
    repeated = '真实材料正文'
    starts = [markdown.index(repeated), markdown.rindex(repeated)]
    assert starts[0] < starts[1]
    chunks = [EvidenceWindow(start, start + len(repeated), repeated) for start in starts]
    chunk_payload = {'model': 'no-wire-verification',
        'input': ['提问正文', *[rendered_chunk_text(doc_entry, summary, repeated).strip() for _ in starts]]}
    chunk_turn, chunk_origins = claim(chunk_wire_input(query, payload=chunk_payload, question='提问正文',
        entry=doc_entry, scope=scope, snapshot=snapshot, layer='L1', span_start=0, summary=summary,
        chunks=chunks), chunk_payload, doc_materials, 'repeated-chunks')
    candidates = [decode(row['rendering']['candidate'])[0]
        for row in chunk_origins.auxiliary['rendered_input']['items'][1:]]
    assert [row['windows'][0].start for row in candidates] == starts
    rerank_payload = {'model': 'no-wire-verification', 'query': '提问正文',
        'documents': [row['title'] + '\n' + row['excerpt'] for row in candidates]}
    rerank_turn, rerank_origins = claim(rerank_wire_input(query, payload=rerank_payload, question='提问正文',
        candidates=candidates), rerank_payload, doc_materials, 'repeated-rerank', purpose='rerank')
    assert [decode(row['rendering']['candidate'])[0]['windows'][0].start
        for row in rerank_origins.auxiliary['rendered_input']['items'][1:]] == starts
    changed = deepcopy(chunk_origins.auxiliary)
    candidate = decode(changed['rendered_input']['items'][2]['rendering']['candidate'])
    candidate[0]['windows'] = (EvidenceWindow(starts[1] + 1, starts[1] + len(repeated) + 1, repeated),)
    changed['rendered_input']['items'][2]['rendering']['candidate'] = encode(candidate)
    chunk_origins.auxiliary = changed
    with pytest.raises(RecognitionConflict, match='material_origin_model_body_changed'):
        validate_origin_privacy(chunk_origins, models, project, chunk_turn.request['privacy'],
            purpose='embedding', selection=changed['selection'])
    full_payload = {'model': 'no-wire-verification', 'input': ['提问正文', raw_rendered.strip()]}
    full, full_origins = claim(chunk_wire_input(query, payload=full_payload, question=' \n提问正文 \t',
        entry=doc_entry, scope=scope, snapshot=snapshot, layer='L1', span_start=0, summary=summary,
        chunks=[EvidenceWindow(0, len(markdown), markdown)]), full_payload, doc_materials, 'whole-chunk')
    original = decode(full_origins.auxiliary['rendered_input']['items'][1]['rendering']['candidate'])[0]['windows'][0]
    assert original.text == markdown and original.text.endswith('\n') and original.end == len(markdown)
    changed = deepcopy(full_origins.auxiliary)
    changed['rendered_input']['items'][1]['rendering']['summary'] = '替换摘要'
    full_origins.auxiliary = changed
    with pytest.raises(RecognitionConflict, match='shared_rendered_input_body_changed'):
        validate_origin_privacy(full_origins, models, project, full.request['privacy'],
            purpose='embedding', selection=changed['selection'])
    changed = deepcopy(rerank_origins.auxiliary)
    candidate = decode(changed['rendered_input']['items'][1]['rendering']['candidate'])
    candidate[0]['excerpt'] = '替换片段'
    changed['rendered_input']['items'][1]['rendering']['candidate'] = encode(candidate)
    changed['rendered_input']['items'][1]['rendered_text'] = candidate[0]['title'] + '\n替换片段'
    changed['rendered_input']['wire_payload']['documents'][0] = changed['rendered_input']['items'][1]['rendered_text']
    rerank_origins.auxiliary = changed
    with pytest.raises(RecognitionConflict, match='shared_rendered_input_body_changed'):
        validate_origin_privacy(rerank_origins, models, project, rerank_turn.request['privacy'],
            purpose='rerank', selection=changed['selection'])
    claim_after_source_revision(duplicate, insights[0], replay=True)
    with pytest.raises(RecognitionConflict):
        duplicate.validate()
    return checked_turns()


def main():
    parser = argparse.ArgumentParser(description='使用 config/codex-hooks.toml 指定的解释器和完整运行资产检查真实服务器共享来源，全部模型调用在发送前终止。')
    parser.add_argument('--root', required=True, help='本检查独占的隔离父目录，禁止使用正式 runtime。')
    parser.add_argument('--profile', action='store_true', help='记录实际准备和原生接纳调用次数。')
    parser.add_argument('--profile-only', action='store_true', help='只执行准备、接纳及其失效诊断，完整检查使用默认入口。')
    parser.add_argument('--constraints-only', action='store_true', help='只执行实际共享约束消费和最终事务检查。')
    parser.add_argument('--primary-only', action='store_true', help='只检查实际共享回答的原生输入与正文准备，不发送模型请求。')
    arguments = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(workspace / 'src'))
    run_real_server_source_check(arguments.root, profile_only=arguments.profile_only, profile_calls=arguments.profile,
        constraints_only=arguments.constraints_only, primary_only=arguments.primary_only)
    print('real server shared source checks passed', flush=True)


if __name__ == '__main__':
    main()
