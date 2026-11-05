import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
from uuid import uuid4

import pytest


def test_actual_shared_admission_draft_birth_and_personal_isolation():
    repo = Path(__file__).resolve().parents[3]
    base = Path(os.environ.get('CHRIPTMAS_SCOPE_CHECK_ROOT', repo / 'work')) / ('ms-' + uuid4().hex[:8])
    (base / 'config').mkdir(parents=True)
    base = base.resolve(strict=True)
    assert 'work' in base.parts and 'runtime' not in base.parts
    shutil.copyfile(repo / 'config/settings.toml.example', base / 'config/settings.toml')
    prior = {key: os.environ.get(key) for key in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ['CHRIPTMAS_APP_ROOT'], os.environ['CHRIPTMAS_DEPLOY'] = str(base), 'server'
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) == base
    try:
        _check_actual_birth(base)
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _check_actual_birth(base):
    from backend.memory_app.app import create_app
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.security.user_context import authorize_user, user_context
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context

    devices = DeviceRegistry(base / 'server')
    users = ServerUsers(base, records=devices.records)
    pair = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='归属核验管理员')
    admin = devices.authenticate(pair['key'])
    account = users.create(admin, name='归属核验成员')
    pairing = devices.exchange(devices.issue_pairing(user_id=account['user_id'], actor=admin.device_id)['code'], name='归属核验设备')
    owner = devices.authenticate(pairing['key'])

    def child(path, user_id, context):
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, base),
                server_context=context)

    server = create_server_application(DeploymentLayout('server', base / 'users/local-user', base), child_factory=child, port=8015)
    registry = server.state.shared_projects
    project = registry.create(owner, project_name='逻辑归属核验', operation_id='create-' + uuid4().hex)['project']

    async def scenario():
        async with server.router.lifespan_context(server):
            pool = server.state.server_user_pool
            async with pool.lease(owner.user_id) as app:
                access = authorize_user(registry.users, admin, owner.user_id)
                with user_context(access), pool.request_scope(owner.user_id, app, access):
                    query = server.state.shared_query_factory.query(app, admin, project['id'])
                    query.answer_turns._runtime()
                    _assert_drafts(app, query, project['id'], admin, owner)
    asyncio.run(scenario())


def _native(app, query, project, kind):
    from backend.memory_app.kernel.turn_requests import freeze_turn_request
    from backend.memory_app.v2.material_origin_admission import BoundMaterialOrigins
    from backend.memory_app.v2.material_origin_privacy import freeze_origin_materials
    from backend.memory_app.workbench_identity_binding import prepare_admission
    native = app.state.ai_turn_store
    identity = 'turn-scope-' + uuid4().hex
    origins = BoundMaterialOrigins(query.origin_admission, query.origin_admission.lease, {})
    _, privacy, _ = freeze_origin_materials(origins, query.models, project, [], local_only=True)
    request = freeze_turn_request(kind, turn_id=identity, session_id='session-' + identity,
        operation_id='operation-' + identity, idempotency_key='key-' + identity, project_id=project,
        created_at=datetime.now(timezone.utc).isoformat(), text='人工核对逻辑归属', privacy=privacy,
        template_version=3 if kind == 'project.task' else 1 if kind == 'web.search' else 2)
    bundle = prepare_admission(app, request, None, native, admission_prepare=query.origin_admission.prepare)
    with native.admission_bundle(request, bundle):
        assert native.claim_turn(request) == (identity, True)
    return native.get_request(identity)


def _assert_drafts(app, query, project, admin, owner):
    from backend.memory_app.v2.task_drafts import TaskDrafts
    from backend.memory_app.v2.material_logical_scope import bind_material_scope_in_uow
    from backend.recognition import RecognitionConflict
    from backend.recognition.material_logical_scope import PREFIX, read_material_logical_scope, require_material_logical_scope
    from backend.shared.document_visibility import LegacyDocumentVisibility

    records, documents = app.state.recognition_records, app.state.recognition_documents
    drafts = TaskDrafts(records, documents)
    personal = drafts.create(turn_id='old-personal', project=project, operation='personal-draft',
        title='个人稿', markdown='人工输入的个人稿。')
    request = _native(app, query, project, 'project.task')
    inputs = dict(turn_id=request['turn_id'], project=project, operation='actual-shared-draft',
        title='共享任务稿', markdown='人工输入用于核对出生和读取权限的任务稿。')
    result = drafts.create(**inputs)
    assert drafts.create(**inputs, source_request=request) == result
    value = require_material_logical_scope(records, material_type='document', identity=result['document_id'],
        project_id=project, namespace_id=documents.namespace_id, source_request=request)
    assert value['execution']['owner_user_id'] == owner.user_id
    assert value['origin']['payload']['actor_user_id'] == admin.user_id
    assert admin.user_id != owner.user_id
    assert value['birth']['revision'] == 1
    assert records.read(PREFIX + 'document', result['document_id']).revision == 1
    assert read_material_logical_scope(records, material_type='document', identity=personal['document_id'], project_id=project) is None
    assert 'scope_kind' not in records.read('documents', result['document_id']).payload
    personal_view = LegacyDocumentVisibility.from_repository(documents, project_id=project)
    shared_view = LegacyDocumentVisibility.from_repository(documents, project_id=project, logical_scope_kind='shared')
    assert result['document_id'] in personal_view.excluded_logical_documents
    assert result['document_id'] not in shared_view.excluded_logical_documents
    assert personal['document_id'] in shared_view.excluded_logical_documents
    assert not shared_view.allows(documents.read(result['document_id']))
    with pytest.raises(RecognitionConflict):
        require_material_logical_scope(records, material_type='document', identity=result['document_id'],
            project_id=project, namespace_id='other-namespace')
    _assert_confirmed_birth(app, query, project)
    marker = records.read(PREFIX + 'document', result['document_id'])
    with records.begin() as tx:
        tx.delete(marker.collection, marker.object_id, expected_revision=marker.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict):
        require_material_logical_scope(records, material_type='document', identity=result['document_id'], project_id=project)
    assert result['document_id'] in LegacyDocumentVisibility.from_repository(documents,
        project_id=project).excluded_logical_documents
    with pytest.raises(RecognitionConflict, match='birth_uow_required'):
        with records.begin() as tx:
            bind_material_scope_in_uow(tx, records, material_type='document', identity=result['document_id'],
                project_id=project, namespace_id=documents.namespace_id, source_request=request)
    with app.state.ai_turn_store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_turn_events WHERE json_extract(event_json,'$.type')=?",
            ('model.attempt.dispatched',)).fetchone()[0] == 0
    print('actual_native_admission=1; shared_draft_birth=1; personal_collision_isolated=1; model_dispatches=0')


def _assert_confirmed_birth(app, query, project):
    from backend.memory_app.transaction_records import TransactionRecords
    from backend.memory_app.v2.library import LibraryRead
    from backend.memory_app.v2.material_logical_scope import bind_material_scope_in_uow
    from backend.recognition.material_logical_scope import require_material_logical_scope

    records, documents, domains = app.state.recognition_records, app.state.recognition_documents, app.state.workspace_domains
    request = _native(app, query, project, 'web.search')
    # 核对原件写入者的出生合同，正文由检查者提供，未执行搜索模型或伪造其终态。
    text = '人工提供的引用材料用于核对原件和整理稿的出生归属。'
    with records.begin() as tx:
        item = domains.items.with_records(TransactionRecords(tx)).create(project, 'text', '原件出生核验', text,
            origin='search', search_turn_id=request['turn_id'])
        bind_material_scope_in_uow(tx, records, material_type='workspace_item', identity=item['id'],
            project_id=project, namespace_id=documents.namespace_id, source_request=request)
        tx.commit()
    ready = domains.items.update(item['id'], project, {'staged'}, status='ready',
        draft={'title': '人工整理稿', 'summary': text, 'facts': [], 'todos': []})
    confirmed = domains.confirmations.confirm(item['id'], project, ready['revision'],
        lambda draft: '# ' + draft['title'] + '\n\n' + draft['summary'])
    source = require_material_logical_scope(records, material_type='original_source', identity=confirmed['source_id'],
        project_id=project, namespace_id=documents.namespace_id)
    document = require_material_logical_scope(records, material_type='document', identity=confirmed['document_id'],
        project_id=project, namespace_id=documents.namespace_id)
    assert document['birth']['lineage']['collection'] == 'documents'
    assert source['birth']['incarnation'] == domains.confirmations.source_store.incarnation('sources', confirmed['source_id'])
    assert source['origin'] == document['origin']
    assert confirmed['source_id'] not in {entry['id'] for entry in domains.query.query_entries(project)}
    library = LibraryRead(records, app.state.recognition_service, documents, domains)
    assert confirmed['document_id'] not in library.docs(project)
    assert confirmed['source_id'] not in {row['id'] for row in library.source_rows(project, library.docs(project))}
    assert library.source_text(project, confirmed['source_id']) == ''
    print('workspace_birth=1; original_json_incarnation_bound=1; confirmed_document_birth=1; personal_source_recall_isolated=1')
