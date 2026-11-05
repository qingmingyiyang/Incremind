from copy import deepcopy
from datetime import datetime, timedelta, timezone
import asyncio
import os
from pathlib import Path
import shutil
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.memory_app.constraints import ProjectConstraintService
from backend.memory_app.v2.automatic_constraint_input import automatic_bundle, frozen_messages, validate_automatic_spec
from backend.memory_app.v2.automatic_constraints import AutomaticConstraintAdmission, PREPARED
from backend.memory_app.v2.projects import assign_scene
from backend.memory_app.v2.turn_requests import freeze_product_turn
from backend.recognition import RecognitionConflict, WorkScope
from core.ai_kernel.product_admission import seal_admission
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


@pytest.fixture(scope='module')
def application(tmp_path_factory):
    # 完整工厂导入前已核定专属短目录，不读取已有运行目录或私人配置。
    root = tmp_path_factory.mktemp('g15').resolve(strict=True)
    assert root.is_absolute() and root.is_dir()
    assert not any(path.name.casefold() == 'runtime' for path in (root, *root.parents))
    config = root / 'config' / 'settings.toml'
    config.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', config)
    previous = os.environ.get('CHRIPTMAS_APP_ROOT')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    try:
        assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) == root
        from backend.memory_app.app import create_app
        app = create_app(runtime_root=root)
        assert Path(app.state.recognition_runtime_root).resolve(strict=True) == root
        models = app.state.recognition_models
        models.update('generation', {'expected_revision': models.public()['generation']['revision'],
            'base_url': 'http://127.0.0.1:9/v1', 'model': 'gpt-4o-mini',
            'api_key': 'synthetic-unused', 'enabled': True, 'allow_remote': False})
        with TestClient(app):
            app.state.workspace_domains.query.answer_turns._runtime()
            yield app
            with app.state.ai_turn_store._connect() as connection:
                assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0
                assert connection.execute("SELECT COUNT(*) FROM ai_turn_events WHERE json_extract(event_json, '$.type') IN ('model.requested', 'model.attempt.dispatched', 'model.completed', 'turn.completed')").fetchone()[0] == 0
    finally:
        if previous is None:
            os.environ.pop('CHRIPTMAS_APP_ROOT', None)
        else:
            os.environ['CHRIPTMAS_APP_ROOT'] = previous


def domain(app):
    records, models = app.state.recognition_records, app.state.recognition_models
    project = 'auto-' + uuid4().hex
    with records.begin() as tx:
        tx.put('v2_projects', project, {'name': '自动约束合成项目', 'scenes': ['会议', '预算'],
            'private': False, 'builtin': None}, expected_revision=0)
        tx.commit()
    owner = AutomaticConstraintAdmission(app.state.project_constraint_input)
    item = app.state.workspace_domains.items.create(project, 'text', '合成原件', '周报只属于材料正文')
    spec = {'producer': 'item-organize', 'mode': 'object', 'scene': None,
            'anchors': [{'type': 'item', 'id': item['id']}]}
    binding = owner.bind(records, models, kind='memory.organize', project=project, spec=spec)
    return records, owner, binding, project, item


def rule(owner, project, identity, content='保留适用条件', **changes):
    project_row = owner.scopes.records.read('v2_projects', project)
    return owner.scopes.upsert(WorkScope('local-user', project), project + '-' + identity,
        changes.pop('revision', 0), content,
        expected_scope_revision=changes.pop('scope_revision', 0), expected_project_revision=project_row.revision,
        mode=changes.pop('mode', 'always'), **changes)


def request(app, project, item):
    identity = 'memory-' + uuid4().hex
    return freeze_product_turn('memory.organize', records=app.state.recognition_records,
        models=app.state.recognition_models, project_id=project,
        materials=[{'type': 'original_item', 'id': item['id'], 'revision': item['revision'], 'project_id': project}],
        load_text=lambda selected: '周报材料', local_only=True, turn_id=identity,
        session_id='aux-' + identity, operation_id='op-' + identity, idempotency_key=identity,
        created_at=datetime.now(timezone.utc).isoformat(), capabilities=[])


def birth(app, binding, project, item, *, commit=True):
    records, store = app.state.recognition_records, app.state.ai_turn_store
    frozen = request(app, project, item)
    identity = {'kind': 'memory.organize', 'project': project, 'key': uuid4().hex}
    with records.begin() as tx:
        before = deepcopy(frozen)
        additions = binding.collect(frozen, store, tx, producer_identity={'collection': 'v2_memory_turn_keys', **identity})
        assert frozen == before
        assert tx.read(PREPARED, frozen['turn_id']) is None
        assert store.get_request(frozen['turn_id']) is None
        admitted = seal_admission(frozen, additions, store)
        tx.put('v2_memory_turn_keys', frozen['turn_id'], {'identity': identity, 'request': deepcopy(frozen)},
            expected_revision=0)
        binding.stage(frozen, store, admitted, tx)
        if commit:
            tx.commit()
    return frozen, admitted


def test_factory_owner_collect_single_seal_real_claim_reopen_and_empty_user_text(application):
    app = application
    records, owner, binding, project, item = domain(app)
    rule(owner, project, 'always')
    rule(owner, project, 'meeting', mode='scene', scene='会议')
    rule(owner, project, 'mention', mode='mention', keywords=['周报'])
    assign_scene(records, 'item', item['id'], project, '会议')
    frozen, admitted = birth(app, binding, project, item)
    snapshot = automatic_bundle(frozen, admitted)
    assert snapshot['text'] == ''
    assert [row['id'].removeprefix(project + '-') for row in snapshot['items']] == ['always', 'meeting']
    installed = app.state.product_automatic_constraints
    assert installed.input is app.state.project_constraint_input
    assert app.state.recognition_models.product_automatic_constraints is installed
    store = app.state.ai_turn_store
    with store.admission_bundle(frozen, admitted):
        assert store.claim_turn(frozen) == (frozen['turn_id'], True)
    reopened = SQLiteAITurnStore(store._path)
    assert reopened.claim_turn(frozen) == (frozen['turn_id'], False)
    assert reopened.restore_admission(frozen) == admitted
    assert binding.validate_current(frozen, reopened) == snapshot
    assert not tuple(reopened.events_after(frozen['turn_id']))
    assert records.read(PREPARED, frozen['turn_id']).revision == 1


def test_empty_set_new_rule_and_original_write_transaction_are_rechecked(application):
    records, owner, binding, project, item = domain(application)
    frozen, admitted = birth(application, binding, project, item)
    assert automatic_bundle(frozen, admitted)['items'] == []
    assert binding.messages(frozen, application.state.ai_turn_store, [{'role': 'user', 'content': '周报'}]) == [
        {'role': 'user', 'content': '周报'}]
    with records.begin() as tx:
        binding.validate_commit(frozen, application.state.ai_turn_store, tx)
        ProjectConstraintService(records).upsert_in_uow(tx, WorkScope('local-user', project),
            'same-uow-rule', 0, '本事务新增的规则')
        with pytest.raises(ValueError, match='bundle_changed'):
            binding.validate_commit(frozen, application.state.ai_turn_store, tx)
    assert records.read('project_constraints', 'same-uow-rule') is None
    rule(owner, project, 'new-rule')
    with pytest.raises(ValueError, match='bundle_changed'):
        binding.validate_current(frozen, application.state.ai_turn_store)


@pytest.mark.parametrize('change', ['content', 'disable', 'expiry', 'scope'])
def test_current_selection_rejects_rule_body_dates_and_scope_changes_but_history_stays_frozen(application, change):
    records, owner, binding, project, item = domain(application)
    row = rule(owner, project, 'rule', content='原冻结规则')
    frozen, admitted = birth(application, binding, project, item)
    base = [{'role': 'user', 'content': '合成材料'}]
    expected = frozen_messages(automatic_bundle(frozen, admitted), base)
    changes = {'content': {}, 'disable': {'enabled': False},
        'expiry': {'valid_until': (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
        'scope': {'mode': 'mention', 'keywords': ['周报']}}[change]
    rule(owner, project, 'rule', content='修改后的规则' if change == 'content' else '原冻结规则',
        revision=row['revision'], scope_revision=row['scope_revision'], **changes)
    with pytest.raises(ValueError, match='bundle_changed'):
        binding.validate_current(frozen, application.state.ai_turn_store)
    assert frozen_messages(automatic_bundle(frozen, admitted), base) == expected
    assert frozen_messages(automatic_bundle(frozen, admitted), expected) == expected
    with pytest.raises(ValueError, match='messages_changed'):
        frozen_messages(automatic_bundle(frozen, admitted), [{**expected[0], 'content': expected[0]['content'] + '额外要求'}])
    assert records.read(PREPARED, frozen['turn_id']).revision == 1


def test_scene_absence_revision_explicit_empty_and_rule_scope_are_bound(application):
    records, owner, binding, project, item = domain(application)
    rule(owner, project, 'meeting', mode='scene', scene='会议')
    frozen, admitted = birth(application, binding, project, item)
    descriptor = admitted.items[0].payload['automatic_descriptor']
    assert descriptor['assignments'] == [{'collection': 'v2_scene_assignments_item', 'id': item['id'],
        'revision': 0, 'payload': None}]
    assign_scene(records, 'item', item['id'], project, '会议')
    with pytest.raises(ValueError, match='descriptor_changed'):
        binding.validate_current(frozen, application.state.ai_turn_store)
    with records.begin() as tx:
        current = tx.read('v2_scene_assignments_item', item['id'])
        tx.put('v2_scene_assignments_item', item['id'], {'project_id': project, 'scene': None},
            expected_revision=current.revision)
        tx.commit()
    empty_request, empty_admission = birth(application, binding, project, item)
    assert automatic_bundle(empty_request, empty_admission)['items'] == []
    assert empty_admission.items[0].payload['automatic_descriptor']['assignments'][0]['revision'] == 2


def test_exact_old_key_or_native_preserves_unverified_qualification_and_new_damage_fails(application):
    records, _, binding, project, item = domain(application)
    store = application.state.ai_turn_store
    legacy = request(application, project, item)
    with pytest.raises(ValueError, match='legacy_unverifiable'):
        binding.validate_current(legacy, store)
    with records.begin() as tx:
        tx.put('v2_memory_turn_keys', legacy['turn_id'], {'identity': {'kind': 'memory.organize',
            'project': project, 'key': 'retained-old-key'}, 'request': deepcopy(legacy)}, expected_revision=0)
        tx.commit()
    assert binding.validate_current(legacy, store) is None
    assert records.read(PREPARED, legacy['turn_id']) is None
    assert store.claim_turn(legacy) == (legacy['turn_id'], True)
    assert binding.validate_current(legacy, store) is None
    frozen, _ = birth(application, binding, project, item)
    missing = deepcopy(frozen)
    missing['input']['refs'] = [ref for ref in missing['input']['refs'] if '/project-constraints/' not in ref['uri']]
    with pytest.raises(ValueError, match='marker_changed'):
        binding.validate_current(missing, store)
    with records.begin() as tx:
        row = tx.read(PREPARED, frozen['turn_id'])
        tx.put(PREPARED, frozen['turn_id'], row.payload, expected_revision=row.revision)
        tx.commit()
    with pytest.raises(ValueError, match='preparation_missing'):
        binding.validate_current(frozen, store)


def test_birth_rollback_cas_wrong_reader_and_actual_factory_owner_reject(application):
    records, owner, binding, project, item = domain(application)
    frozen, _ = birth(application, binding, project, item, commit=False)
    assert records.read(PREPARED, frozen['turn_id']) is None
    assert records.read('v2_memory_turn_keys', frozen['turn_id']) is None
    frozen, _ = birth(application, binding, project, item)
    foreign = SQLiteStructuredRecordStore(Path(application.state.recognition_runtime_root)
        / ('foreign-' + uuid4().hex + '.sqlite3'))
    with foreign.begin_read() as tx:
        with pytest.raises(ValueError, match='reader_changed'):
            binding.validate_current(frozen, application.state.ai_turn_store, reader=tx)
    with records.begin() as tx:
        row = tx.read(PREPARED, frozen['turn_id'])
        with pytest.raises(SQLiteUnitOfWorkConflict):
            tx.put(PREPARED, frozen['turn_id'], row.payload, expected_revision=0)
    with pytest.raises(ValueError, match='owner_changed'):
        owner.bind(SQLiteStructuredRecordStore(records.database_path), application.state.recognition_models,
            kind=binding.kind, project=project, spec=binding.spec)
    with records.begin_read() as tx:
        with pytest.raises(ValueError, match='reader_changed'):
            binding.validate_commit(frozen, application.state.ai_turn_store, tx)


def test_rules_count_in_real_capacity_and_noncontent_uses_stay_outside_contract(application):
    records, owner, binding, project, item = domain(application)
    rule(owner, project, 'large-rule', content='保留完整规则和适用条件' * 300)
    frozen, _ = birth(application, binding, project, item)
    messages = binding.messages(frozen, application.state.ai_turn_store, [{'role': 'user', 'content': '材料'}])
    from backend.memory_app.v2.task_attachment_input import input_tokens
    cost = input_tokens(messages)
    assert cost > input_tokens(messages[1:])
    models = application.state.recognition_models
    application.state.project_constraint_input.input_budget(models, messages,
        revision=models.public()['generation']['revision'], max_tokens=1000, limit=cost)
    with pytest.raises(RecognitionConflict, match='exceed input budget'):
        application.state.project_constraint_input.input_budget(models, messages,
            revision=models.public()['generation']['revision'], max_tokens=1000, limit=cost - 1)
    with pytest.raises(ValueError, match='producer_invalid'):
        validate_automatic_spec(binding.spec, 'memory.link_suggest')
    assert records.read(PREPARED, frozen['turn_id']).revision == 1


def test_scope_producers_use_actual_inherited_recognition_scene_and_cross_scene_none(application):
    from backend.memory_app.kernel.turn_requests import freeze_turn_request
    from core.document_engine import DocumentDraft
    records, owner, _, project, item = domain(application)
    rule(owner, project, 'meeting', mode='scene', scene='会议')
    documents, service = application.state.recognition_documents, application.state.recognition_service
    document = documents.create(DocumentDraft('合成手写文稿-' + project, 'note', '人工输入的会议材料',
        ({'source_id': item['id'], 'locator': 'workspace://' + item['id']},), project))
    assign_scene(records, 'document', document['id'], project, '会议')
    experience = service.stage_experience(scope=WorkScope('local-user', project), content='人工输入的会议材料',
        provenance={'kind': 'user_statement', 'actor': 'local-user', 'source_refs': [{'type': 'document', 'id': document['id'],
            'revision': document['revision']} ]})
    candidate = service.propose(scope=WorkScope('local-user', project), content='人工提出的待确认观点',
        source_experience_ids=[experience])
    recognition = service.publish(scope=WorkScope('local-user', project), candidate_id=candidate.id,
        expected_revision=candidate.revision, reviewer='local-user')
    privacy = application.state.workspace_domains.query.freeze_answer_privacy(project, local_only=True)
    for producer, kind, mode, anchor_type, anchor_id, scene in (
        ('document-insights', 'memory.propose_insights', 'object', 'document', document['id'], None),
        ('inspirations', 'memory.propose_insights', 'scope', 'document', document['id'], '会议'),
        ('overview', 'memory.overview', 'scope', 'document', document['id'], '会议'),
        ('skill-export', 'memory.skill_export', 'scope', 'recognition', recognition.id, '会议'),
        ('consolidation', 'memory.consolidate', 'project', 'document', document['id'], None),
    ):
        identity = 'memory-' + uuid4().hex
        frozen = freeze_turn_request(kind, project_id=project, turn_id=identity, session_id='aux-' + identity,
            operation_id='op-' + identity, idempotency_key=identity,
            created_at=datetime.now(timezone.utc).isoformat(), text='合成自动输入含周报', privacy=privacy)
        spec = {'producer': producer, 'mode': mode, 'scene': scene,
                'anchors': [{'type': anchor_type, 'id': anchor_id}]}
        binding = owner.bind(records, application.state.recognition_models, kind=kind, project=project, spec=spec)
        with records.begin() as tx:
            additions = binding.collect(frozen, application.state.ai_turn_store, tx,
                producer_identity={'collection': 'v2_memory_turn_keys', 'kind': kind, 'project': project, 'key': identity})
            admitted = seal_admission(frozen, additions, application.state.ai_turn_store)
            tx.put('v2_memory_turn_keys', identity, {'identity': {'kind': kind, 'project': project, 'key': identity},
                'request': deepcopy(frozen)}, expected_revision=0)
            binding.stage(frozen, application.state.ai_turn_store, admitted, tx)
            tx.commit()
        snapshot = automatic_bundle(frozen, admitted)
        assert snapshot['text'] == ''
        assert snapshot['scene'] == (None if mode == 'project' else '会议')
        assert bool(snapshot['items']) is (mode != 'project')
        if producer == 'skill-export':
            descriptor = additions[0].payload['automatic_descriptor']
            assert descriptor['resolved_scenes'][0]['assignment'] == {'project_id': project, 'scene': '会议'}
            assert any(row['collection'] == 'v2_scene_assignments_document'
                       and row['id'] == document['id'] for row in descriptor['assignments'])
            assert {'collection': 'recognition_candidates', 'id': candidate.id,
                'revision': records.read('recognition_candidates', candidate.id).revision} in descriptor['dependencies']
            with records.begin() as tx:
                alias = tx.read('recognition_candidates', candidate.id)
                tx.put(alias.collection, alias.object_id, dict(alias.payload), expected_revision=alias.revision)
                with pytest.raises(ValueError, match='descriptor_changed'):
                    binding.validate_commit(frozen, application.state.ai_turn_store, tx)


def test_shared_collector_uses_issued_source_rules_and_original_qualified_commit(application):
    from backend.memory_app.app import create_app
    from backend.memory_app.kernel.turn_requests import freeze_turn_request
    from backend.memory_app.server_runtime import create_server_application
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.shared_auxiliary import _qualified_commit
    from backend.security.user_context import authorize_user, user_context
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    base = Path(application.state.recognition_runtime_root) / ('s' + uuid4().hex[:4])
    (base / 'config').mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', base / 'config/settings.toml')
    devices = DeviceRegistry(base / 'server')
    users = ServerUsers(base, records=devices.records)
    paired = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='自动规则合成管理员')
    actor = devices.authenticate(paired['key'])

    def child(path, user_id, context):
        config = path / 'config' / 'settings.toml'
        config.parent.mkdir(parents=True, exist_ok=True)
        if not config.exists():
            shutil.copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', config)
        with resource_context(context.resources):
            return create_app(runtime_root=path, deployment_layout=DeploymentLayout('server', path, base), server_context=context)

    server = create_server_application(DeploymentLayout('server', base / 'users/local-user', base), child_factory=child, port=8188)
    registry = server.state.shared_projects
    project = registry.create(actor, project_name='合成共享规则', operation_id='create-' + uuid4().hex)['project']['id']
    data = registry.roots._data(project)
    rule_id = 'source-' + uuid4().hex
    ProjectConstraintService(data.records).upsert(WorkScope('local-user', project), rule_id, 0, '实际共享来源规则')

    async def scenario():
        async with server.router.lifespan_context(server):
            pool = server.state.server_user_pool
            async with pool.lease('local-user') as app:
                access = authorize_user(users, actor, 'local-user')
                with user_context(access), pool.request_scope('local-user', app, access):
                    models = app.state.recognition_models
                    models.update('generation', {'expected_revision': models.public()['generation']['revision'],
                        'base_url': 'http://127.0.0.1:9/v1', 'model': 'gpt-4o-mini',
                        'api_key': 'synthetic-unused', 'enabled': True, 'allow_remote': False})
                    query = server.state.shared_query_factory.query(app, actor, project)
                    query.answer_turns._runtime()
                    records, store = app.state.recognition_records, app.state.ai_turn_store
                    ProjectConstraintService(records).upsert(WorkScope('local-user', project), rule_id, 0, '同名个人规则')
                    owner = AutomaticConstraintAdmission(query.project_constraint_input)
                    binding = owner.bind(records, app.state.recognition_models, kind='memory.consolidate', project=project,
                        spec={'producer': 'consolidation', 'mode': 'project', 'scene': None, 'anchors': []})
                    identity = 'memory-' + uuid4().hex
                    frozen = freeze_turn_request('memory.consolidate', project_id=project, turn_id=identity,
                        session_id='aux-' + identity, operation_id='op-' + identity, idempotency_key=identity,
                        created_at=datetime.now(timezone.utc).isoformat(), text='合成归纳输入',
                        privacy=query.freeze_answer_privacy(project, local_only=True))
                    producer = {'kind': 'memory.consolidate', 'project': project, 'key': identity}
                    with _qualified_commit(query.origin_admission), records.begin() as tx:
                        additions = binding.collect(frozen, store, tx,
                            producer_identity={'collection': 'v2_memory_turn_keys', **producer})
                        admitted = seal_admission(frozen, (*query.origin_admission.prepare(frozen), *additions), store)
                        tx.put('v2_memory_turn_keys', identity, {'identity': producer, 'request': deepcopy(frozen)}, expected_revision=0)
                        binding.stage(frozen, store, admitted, tx)
                        binding.validate_commit(frozen, store, tx)
                        tx.commit()
                    assert [row['content'] for row in automatic_bundle(frozen, admitted)['items']] == ['实际共享来源规则']
                    with store.admission_bundle(frozen, admitted):
                        assert store.claim_turn(frozen) == (identity, True)
                    assert binding.validate_current(frozen, store)['origin']['kind'] == 'shared'
                    from backend.memory_app.v2.shared_auxiliary import SharedAuxiliaryTurn, PREPARED as SOURCE_PREPARED
                    actual = SharedAuxiliaryTurn(query.origin_admission, app.state.recognition_models,
                        kind='memory.consolidate', project=project, key=uuid4().hex, materials=[], validate=lambda: None,
                        automatic={'producer': 'consolidation', 'mode': 'project', 'scene': None, 'anchors': []})
                    assert records.read(PREPARED, actual.turn_id).revision == 1
                    assert records.read(SOURCE_PREPARED, actual.turn_id).revision == 1
                    combined = actual.admission.admission(actual.request, actual.store)
                    assert sorted(item.kind for item in combined.items) == ['material-origin-v1', 'project-constraints-v1']
                    with actual.store.admission_bundle(actual.request, combined):
                        assert actual.store.claim_turn(actual.request) == (actual.turn_id, True)
                    with actual.store.admission_bundle(actual.request, combined):
                        assert actual.store.claim_turn(actual.request) == (actual.turn_id, False)
                    from backend.memory_app.v2.shared_auxiliary import _ActorRecords, _AdmittedStore
                    original_actor = _ActorRecords(query.origin_admission, automatic=actual.admission.binding)
                    original_actor.validate_request = actual.actor_records.validate_request
                    original_store = _AdmittedStore(original_actor, query.origin_admission)
                    with original_store.admission_bundle(actual.request, combined):
                        assert original_store.claim_turn(actual.request) == (actual.turn_id, False)
                        with pytest.raises(RecognitionConflict, match='nested shared auxiliary admission'):
                            with actual.store.admission_bundle(actual.request, combined):
                                raise AssertionError('nested admission unexpectedly accepted')
                    assert SQLiteAITurnStore(actual.store._path).restore_admission(actual.request) == combined
                    with actual.records.begin() as tx:
                        actual.validate_commit(tx)
                    assert actual.admission.messages(actual.request, actual.store, [])[0]['content'].endswith('实际共享来源规则')
                    with records.begin() as tx:
                        with pytest.raises(RecognitionConflict, match='shared commit scope unavailable'):
                            binding.validate_commit(frozen, store, tx)
                    ProjectConstraintService(data.records).upsert(WorkScope('local-user', project), rule_id, 1, '改变后的来源规则')
                    with pytest.raises(ValueError, match='bundle_changed'):
                        binding.validate_current(frozen, store)
                    with pytest.raises(ValueError, match='bundle_changed'):
                        with actual.store.admission_bundle(actual.request, combined):
                            actual.store.claim_turn(actual.request)
                    assert frozen_messages(automatic_bundle(frozen, store.restore_admission(frozen)), [])[0]['content'].endswith('实际共享来源规则')
                    with store._connect() as connection:
                        assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0
    asyncio.run(scenario())


def test_committed_memory_producer_cannot_gain_late_proof_or_adopt_native_without_birth_stage(application):
    records, _, binding, project, item = domain(application)
    store = application.state.ai_turn_store
    frozen = request(application, project, item)
    producer = {'kind': 'memory.organize', 'project': project, 'key': uuid4().hex}
    with records.begin() as tx:
        additions = binding.collect(frozen, store, tx,
            producer_identity={'collection': 'v2_memory_turn_keys', **producer})
        admitted = seal_admission(frozen, additions, store)
        index = tx.put('v2_memory_turn_keys', frozen['turn_id'],
            {'identity': producer, 'request': deepcopy(frozen)}, expected_revision=0)
        tx.commit()
    assert records.read(PREPARED, frozen['turn_id']) is None
    with records.begin() as tx:
        with pytest.raises(ValueError, match='birth_uow_required'):
            binding.stage(frozen, store, admitted, tx)
    # 第二事务重写同一正文会产生新修订，仍不能变成原请求的出生事务。
    with records.begin() as tx:
        tx.put('v2_memory_turn_keys', frozen['turn_id'], index.payload, expected_revision=index.revision)
        with pytest.raises(ValueError, match='birth_uow_required'):
            binding.stage(frozen, store, admitted, tx)
    assert records.read('v2_memory_turn_keys', frozen['turn_id']).revision == 1
    with store.admission_bundle(frozen, admitted):
        assert store.claim_turn(frozen) == (frozen['turn_id'], True)
    with pytest.raises(ValueError, match='preparation_missing'):
        binding.validate_current(frozen, store)
    assert records.read(PREPARED, frozen['turn_id']) is None
    assert not tuple(store.events_after(frozen['turn_id']))


def test_organize_checkpoint_requires_its_absence_at_collect_and_original_birth_uow(application):
    records, _, binding, project, item = domain(application)
    store = application.state.ai_turn_store
    frozen = request(application, project, item)
    unsealed = deepcopy(frozen)
    key = 'organize-' + item['id'] + '-birth-contract'
    producer = {'collection': 'workspace_organize_steps', 'kind': 'memory.organize', 'project': project, 'key': key}
    with records.begin() as tx:
        additions = binding.collect(frozen, store, tx, producer_identity=producer)
        admitted = seal_admission(frozen, additions, store)
        tx.put('workspace_organize_steps', key, {'turn_id': frozen['turn_id'],
            'inputs': {'messages': [], 'max_tokens': 1000, 'project_id': project}}, expected_revision=0)
        tx.commit()
    with records.begin() as tx:
        with pytest.raises(ValueError, match='birth_already_exists'):
            binding.collect(unsealed, store, tx, producer_identity=producer)
        with pytest.raises(ValueError, match='birth_uow_required'):
            binding.stage(frozen, store, admitted, tx)
    assert records.read(PREPARED, frozen['turn_id']) is None
    assert store.get_request(frozen['turn_id']) is None
    next_request = request(application, project, item)
    with records.begin() as tx:
        with pytest.raises(ValueError, match='birth_already_exists'):
            binding.collect(next_request, store, tx, producer_identity=producer)
    # 新 checkpoint 与 proof 在原 UOW 出生，之后单独执行真实 native claim。
    producer = {**producer, 'key': key + '-new'}
    with records.begin() as tx:
        additions = binding.collect(next_request, store, tx, producer_identity=producer)
        next_admitted = seal_admission(next_request, additions, store)
        tx.put('workspace_organize_steps', producer['key'], {'turn_id': next_request['turn_id'],
            'inputs': {'messages': [], 'max_tokens': 1000, 'project_id': project}}, expected_revision=0)
        binding.stage(next_request, store, next_admitted, tx)
        tx.commit()
    with store.admission_bundle(next_request, next_admitted):
        assert store.claim_turn(next_request) == (next_request['turn_id'], True)
    assert binding.validate_current(next_request, SQLiteAITurnStore(store._path))['text'] == ''


def test_organize_new_turn_replaces_exact_checkpoint_in_the_original_cas_transaction(application):
    records, owner, binding, project, item = domain(application)
    store = application.state.ai_turn_store
    old_request = request(application, project, item)
    key = 'organize-' + item['id'] + '-retry-contract'
    producer = {'collection': 'workspace_organize_steps', 'kind': 'memory.organize', 'project': project, 'key': key}
    with records.begin() as tx:
        old = tx.put('workspace_organize_steps', key, {'turn_id': old_request['turn_id'],
            'inputs': {'messages': [], 'max_tokens': 1000, 'project_id': project}}, expected_revision=0)
        tx.commit()
    with records.begin() as tx:
        with pytest.raises(ValueError, match='previous_producer_changed'):
            binding.prepare(old_request, store, tx, producer_identity=producer, previous_producer=old)
    fresh = request(application, project, item)
    with records.begin() as tx:
        current = tx.read('workspace_organize_steps', key)
        admitted = binding.prepare(fresh, store, tx, producer_identity=producer, previous_producer=current)
        tx.put('workspace_organize_steps', key, {**current.payload, 'turn_id': fresh['turn_id']},
            expected_revision=current.revision)
        binding.stage(fresh, store, admitted, tx)
        tx.commit()
    descriptor = admitted.items[0].payload['automatic_descriptor']
    assert descriptor['schema_version'] == 2
    assert descriptor['producer_previous'] == {'collection': 'workspace_organize_steps', 'id': key,
        'revision': old.revision, 'payload': old.payload}
    assert records.read('workspace_organize_steps', key).revision == old.revision + 1
    assert records.read(PREPARED, old_request['turn_id']) is None
    with store.admission_bundle(fresh, admitted):
        assert store.claim_turn(fresh) == (fresh['turn_id'], True)
    assert binding.validate_current(fresh, SQLiteAITurnStore(store._path))['text'] == ''
    rule(owner, project, 'after-retry')
    with pytest.raises(ValueError, match='bundle_changed'):
        binding.validate_current(fresh, store)


def test_actual_memory_turn_port_stages_rule_proof_with_original_key_and_privacy_commit(application):
    from backend.memory_app.v2.memory_turn import MemoryTurn
    from backend.memory_app.v2.privacy import set_private_project
    records, owner, binding, project, item = domain(application)
    rule(owner, project, 'port-rule')
    turn = MemoryTurn(records, application.state.recognition_models, kind='memory.organize', project=project,
        key=uuid4().hex, admission=binding,
        materials=[{'type': 'original_item', 'id': item['id'], 'revision': item['revision'], 'project_id': project}],
        validate=lambda: owner.scopes.list(WorkScope('local-user', project)))
    assert records.read(PREPARED, turn.turn_id).revision == 1
    assert turn.store.get_request(turn.turn_id) is None
    assert turn.input_messages([{'role': 'user', 'content': '周报'}])[0]['content'].endswith('保留适用条件')
    with records.begin() as tx:
        turn.validate_commit(tx)
    admitted = binding.admission(turn.request, turn.store)
    with turn.store.admission_bundle(turn.request, admitted):
        assert turn.store.claim_turn(turn.request) == (turn.turn_id, True)
    assert binding.admission(turn.request, SQLiteAITurnStore(turn.store._path)) == admitted
    set_private_project(records, project, True, 0)
    with records.begin() as tx:
        with pytest.raises(RecognitionConflict, match='privacy revision'):
            turn.validate_commit(tx)
