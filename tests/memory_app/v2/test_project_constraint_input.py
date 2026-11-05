import asyncio
import inspect
import os
import shutil
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.memory_app.constraints import ProjectConstraintService
from backend.memory_app.transaction_records import TransactionRecords
from backend.memory_app.workbench_identity_binding import prepare_admission
from backend.memory_app.kernel.turn_requests import freeze_turn_request
from backend.memory_app.v2.project_constraint_input import KIND, ProjectConstraintInput
from backend.recognition import RecognitionConflict, WorkScope
from core.ai_kernel.product_admission import ProductAdmissionItem, seal_admission
from core.storage_provider import SQLiteStructuredRecordStore


ROOT = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
REPOSITORY = Path(__file__).resolve().parents[3]
assert Path(os.environ['CHRIPTMAS_APP_ROOT']).is_absolute()
assert ROOT != Path(ROOT.anchor) and ROOT.name.casefold() != 'runtime'
assert ROOT.is_relative_to(REPOSITORY / 'work') or not ROOT.is_relative_to(REPOSITORY)


def prepare_config(root):
    config = root / 'config' / 'settings.toml'
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / 'config' / 'settings.toml.example', config)


@pytest.fixture(scope='module')
def application():
    # 应用导入前的隔离目录由启动命令固定，完整工厂沿实际资产和运行主人创建。
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) == ROOT
    prepare_config(ROOT)
    from backend.memory_app.app import create_app
    app = create_app(runtime_root=ROOT)
    models = app.state.recognition_models
    models.update('generation', {'expected_revision': models.public()['generation']['revision'],
        'base_url': 'http://127.0.0.1:9/v1', 'model': 'gpt-4o-mini',
        'api_key': 'local-test-placeholder', 'enabled': True, 'allow_remote': False})
    with TestClient(app):
        app.state.workspace_domains.query.answer_turns._runtime()
        yield app


def project():
    return 'qa-' + uuid4().hex


def save(app, scope, identity=None, content='回答时列出实际依据', **values):
    return ProjectConstraintService(app.state.recognition_records).upsert(
        WorkScope('local-user', scope), identity or 'constraint-' + uuid4().hex,
        values.pop('revision', 0), content, **values)


def request(app, scope, *, kind='project.answer'):
    query = app.state.workspace_domains.query
    identity = 'turn-' + uuid4().hex
    frozen = freeze_turn_request(kind, turn_id=identity, session_id='session-' + identity,
        operation_id='operation-' + identity, idempotency_key='request-' + identity,
        project_id=scope, created_at=datetime.now(timezone.utc).isoformat(),
        text='请说明依据' if kind == 'project.answer' else '{"task":"撰写有依据的说明"}',
        privacy=query.freeze_answer_privacy(scope, local_only=True),
        template_version=2 if kind == 'project.answer' else 3,
        **({'capabilities': ['workbench.answer.execute']} if kind == 'project.answer' else {}))
    return frozen


def admit(app, frozen):
    bundle = prepare_admission(app, frozen, None, app.state.ai_turn_store)
    assert bundle is not None
    receipt = app.state.ai_runtime.accept_turn(frozen, admission_bundle=bundle)
    assert receipt.status == 'accepted'
    assert not any(event['type'] in {'model.requested', 'model.completed', 'turn.completed'}
                   for event in app.state.ai_turn_store.events_after(frozen['turn_id']))
    return bundle


def test_actual_factory_mounts_owner_and_existing_http_constraint_route(application):
    app = application
    owner = app.state.project_constraint_input
    assert app.state.workspace_domains.query.project_constraint_input is owner
    assert app.state.recognition_models.product_constraint_input is owner
    scope, identity = project(), 'constraint-' + uuid4().hex
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8014') as client:
            response = await client.put('/api/recognition/constraints/' + identity,
                json={'project_id': scope, 'content': '保留实际单位', 'enabled': True, 'expected_revision': 0})
            assert response.status_code == 200
            current = await client.get('/api/recognition/constraints', params={'project_id': scope})
            assert current.status_code == 200
            assert current.json()['items'][0]['content'] == '保留实际单位'
    asyncio.run(check())
    assert owner.snapshot(scope)['items'][0]['content'] == '保留实际单位'


def test_selection_uses_actual_project_enabled_and_time_boundaries(application):
    scope = project()
    now = datetime.now(timezone.utc)
    active = save(application, scope)
    save(application, scope, enabled=False)
    save(application, scope, valid_from=(now + timedelta(days=1)).isoformat())
    save(application, scope, valid_until=(now - timedelta(days=1)).isoformat())
    save(application, 'me', content='个人项目要求')
    save(application, project(), content='其他项目要求')
    snapshot = application.state.project_constraint_input.snapshot(scope)
    assert [item['id'] for item in snapshot['items']] == [active['id']]
    assert snapshot['items'][0]['content'] in snapshot['prompt']
    assert '个人项目要求' not in snapshot['prompt'] and '其他项目要求' not in snapshot['prompt']


@pytest.mark.parametrize('kind', ['project.answer', 'project.task'])
def test_actual_native_claim_preserves_complete_constraint_and_final_messages(application, kind):
    scope = project()
    row = save(application, scope)
    frozen = request(application, scope, kind=kind)
    bundle = admit(application, frozen)
    item = next(item for item in bundle.items if item.kind == KIND)
    assert item.payload['bundle']['items'][0]['id'] == row['id']
    owner = application.state.project_constraint_input
    base = [{'role': 'user', 'content': '真实输入准备'}]
    messages = owner.messages(frozen, base)
    assert messages[0] == {'role': 'system', 'content': item.payload['bundle']['prompt']}
    assert messages[1:] == base
    assert owner.messages(frozen, messages) == messages
    ref = owner.record_input(frozen, frozen, messages, application.state.ai_turn_store, logical_input='真实输入准备')
    assert application.state.ai_turn_store.get(ref)['messages'] == messages


@pytest.mark.parametrize('change', ['content', 'enabled', 'date', 'new'])
def test_actual_native_snapshot_rejects_changed_active_set(application, change):
    scope = project()
    row = save(application, scope)
    frozen = request(application, scope)
    admit(application, frozen)
    if change == 'new':
        save(application, scope, content='新增加的明确约束')
    else:
        values = {'content': {'content': '另一条实际要求'}, 'enabled': {'enabled': False},
                  'date': {'valid_until': (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}}[change]
        save(application, scope, row['id'], revision=row['revision'], **values)
    with pytest.raises(RecognitionConflict):
        application.state.project_constraint_input.messages(frozen, [{'role': 'user', 'content': '准备'}])


def test_empty_set_is_frozen_and_cannot_accept_a_later_constraint(application):
    scope = project()
    frozen = request(application, scope)
    bundle = admit(application, frozen)
    assert next(item for item in bundle.items if item.kind == KIND).payload['bundle']['items'] == []
    save(application, scope)
    with pytest.raises(RecognitionConflict):
        application.state.project_constraint_input.validate(frozen)


def test_same_id_revision_with_tampered_content_does_not_gain_authority(application):
    scope = project()
    save(application, scope)
    frozen = request(application, scope)
    owner, store = application.state.project_constraint_input, application.state.ai_turn_store
    item = owner.augment(frozen, store, ())[0]
    payload = deepcopy(item.payload)
    payload['bundle']['items'][0]['content'] = '非当前用户设置的要求'
    payload['bundle']['prompt'] = '非当前用户设置的要求'
    changed = ProductAdmissionItem(item.kind, item.ref, item.scope, payload)
    bundle = seal_admission(frozen, (changed,), store)
    assert application.state.ai_runtime.accept_turn(frozen, admission_bundle=bundle).status == 'accepted'
    with pytest.raises(RecognitionConflict):
        owner.validate(frozen)


def test_final_transaction_reads_its_own_constraint_change_and_rolls_back(application):
    scope = project()
    row = save(application, scope)
    frozen = request(application, scope)
    admit(application, frozen)
    records, owner = application.state.recognition_records, application.state.project_constraint_input
    with records.begin() as tx:
        ProjectConstraintService(TransactionRecords(tx)).upsert_in_uow(tx,
            WorkScope('local-user', scope), row['id'], row['revision'], '同事务修改')
        with pytest.raises(RecognitionConflict):
            owner.validate_public_answer(frozen['turn_id'], reader=tx)
        tx.rollback()
    assert owner.validate(frozen)['items'][0]['content'] == row['content']


def test_missing_marker_new_native_remains_rejected_after_reconstruction(application):
    scope = project()
    frozen = request(application, scope)
    app = application
    assert app.state.ai_runtime.accept_turn(frozen).status == 'accepted'
    with pytest.raises(RecognitionConflict):
        app.state.project_constraint_input.validate(frozen)
    restarted = ProjectConstraintInput(app, app.state.recognition_records, app.state.recognition_documents, ROOT)
    assert restarted.installed_at == app.state.project_constraint_input.installed_at
    with pytest.raises(RecognitionConflict):
        restarted.validate(frozen)


@contextmanager
def isolated_application(root):
    previous = os.environ['CHRIPTMAS_APP_ROOT']
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root.resolve(strict=True))
    try:
        prepare_config(root)
        from backend.memory_app.app import create_app
        app = create_app(runtime_root=root)
        with TestClient(app):
            app.state.workspace_domains.query.answer_turns._runtime()
            yield app
    finally:
        os.environ['CHRIPTMAS_APP_ROOT'] = previous


def test_real_prior_native_acceptance_and_logical_copy_keep_the_original_boundary(application, tmp_path):
    from core.ai_kernel.runtime import SynchronousAIRuntime
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore
    root = tmp_path / 'a'
    root.mkdir()
    store = SQLiteAITurnStore(root / '.rebuild-data' / 'ai-turns.sqlite3')
    original = application.state.ai_runtime
    runtime = SynchronousAIRuntime(planner=original._planner, registry=original._registry,
        events=store, payloads=store, state=store)
    frozen = request(application, project())
    assert runtime.accept_turn(frozen).status == 'accepted'
    with isolated_application(root) as old:
        owner = old.state.project_constraint_input
        boundary, physical = owner.installed_at, owner.physical
        accepted = next(event for event in store.events_after(frozen['turn_id']) if event['type'] == 'turn.accepted')
        assert datetime.fromisoformat(accepted['occurred_at']) < boundary
        assert owner.validate(frozen) is None
        new = request(old, project())
        assert old.state.ai_runtime.accept_turn(new).status == 'accepted'
        with pytest.raises(RecognitionConflict):
            owner.validate(new)
    copied = tmp_path / 'b'
    shutil.copytree(root, copied)
    with isolated_application(copied) as restored:
        owner = restored.state.project_constraint_input
        assert owner.installed_at == boundary and owner.physical != physical
        assert owner.validate(frozen) is None
        with pytest.raises(RecognitionConflict):
            owner.validate(new)


def test_only_first_activation_prepared_binding_can_use_the_legacy_path(application, tmp_path):
    from backend.memory_app.storage_authority import resolve_recognition_document_store
    root = tmp_path / 'c'
    root.mkdir()
    records, _ = resolve_recognition_document_store(root)
    frozen = request(application, project(), kind='project.task')
    identity = frozen['session_id'].removeprefix('session-')
    created = datetime.now(timezone.utc).isoformat()
    with records.begin() as tx:
        tx.put('v2_task_executions', identity, {'project_id': frozen['scope']['project_id'],
            'request': frozen, 'started': False, 'owner': None}, expected_revision=0)
        tx.put('v2_turns', identity, {'project_id': frozen['scope']['project_id'], 'intent': 'do',
            'created_at': created, 'receipt': {'do': {'state': 'running', 'kernel_turn_id': frozen['turn_id']}}},
            expected_revision=0)
        tx.commit()
    with isolated_application(root) as old:
        owner = old.state.project_constraint_input
        assert owner.validate(frozen) is None
        changed = deepcopy(frozen)
        changed['input']['text'] = '{"task":"同一原生标识下的新任务"}'
        with old.state.recognition_records.begin() as tx:
            execution = tx.read('v2_task_executions', identity)
            tx.put('v2_task_executions', identity, {**execution.payload, 'request': changed},
                expected_revision=execution.revision)
            tx.commit()
        with pytest.raises(RecognitionConflict):
            owner.validate(changed)
        with old.state.recognition_records.begin() as tx:
            execution = tx.read('v2_task_executions', identity)
            tx.put('v2_task_executions', identity, {**execution.payload, 'request': frozen},
                expected_revision=execution.revision)
            tx.commit()
        assert old.state.ai_runtime.accept_turn(frozen).status == 'accepted'
        assert owner.validate(frozen) is None
        replacement = request(old, frozen['scope']['project_id'], kind='project.task')
        replacement['session_id'] = frozen['session_id']
        with old.state.recognition_records.begin() as tx:
            execution = tx.read('v2_task_executions', identity)
            public = tx.read('v2_turns', identity)
            tx.put('v2_task_executions', identity, {**execution.payload, 'request': replacement},
                expected_revision=execution.revision)
            tx.put('v2_turns', identity, {**public.payload,
                'receipt': {'do': {'state': 'running', 'kernel_turn_id': replacement['turn_id']}}},
                expected_revision=public.revision)
            tx.commit()
        with pytest.raises(RecognitionConflict):
            owner.validate(replacement)
        restarted = ProjectConstraintInput(old, old.state.recognition_records, old.state.recognition_documents, root)
        with pytest.raises(RecognitionConflict):
            restarted.validate(replacement)


def test_actual_constraint_size_rejects_input_budget_without_truncating(application):
    owner, models = application.state.project_constraint_input, application.state.recognition_models
    original = '完整约束正文' * 100000
    messages = [{'role': 'system', 'content': original}]
    assert models.generation_budget_limits(expected_revision=models.public()['generation']['revision'], max_tokens=64)['window'] > 0
    with pytest.raises(RecognitionConflict, match='input budget'):
        owner.input_budget(models, messages, revision=models.public()['generation']['revision'], max_tokens=64)
    assert messages[0]['content'] == original
    with pytest.raises(RecognitionConflict, match='input budget'):
        owner.input_budget(models, [{'role': 'user', 'content': '实际问答预算'}],
            revision=models.public()['generation']['revision'], max_tokens=64, limit=1)


def test_normal_query_preparation_reserves_constraint_cost_and_detects_changes(application):
    scope = project()
    row = save(application, scope)
    query = application.state.workspace_domains.query
    plan = query.prepare_ask(scope, '请说明实际依据')
    assert plan['_project_constraints']['items'][0]['content'] == row['content']
    assert plan['prompt_overhead'] > 0
    save(application, scope, row['id'], content='修订后的实际要求', revision=row['revision'])
    with pytest.raises(RecognitionConflict):
        query.validate_ask_plan(plan)


def test_actual_expiry_without_a_write_invalidates_the_frozen_set(application):
    scope = project()
    end = datetime.now(timezone.utc) + timedelta(seconds=3)
    save(application, scope, valid_until=end.isoformat())
    frozen = request(application, scope)
    admit(application, frozen)
    assert application.state.project_constraint_input.validate(frozen)['items']
    time.sleep(max(0, (end - datetime.now(timezone.utc)).total_seconds()) + 0.02)
    with pytest.raises(RecognitionConflict):
        application.state.project_constraint_input.validate(frozen)


def test_wrong_database_root_and_changed_request_are_rejected(application, tmp_path):
    scope = project()
    frozen = request(application, scope)
    admit(application, frozen)
    owner = application.state.project_constraint_input
    foreign = SQLiteStructuredRecordStore(tmp_path / 'foreign.sqlite3')
    with foreign.begin_read() as reader, pytest.raises(RecognitionConflict):
        owner.validate(frozen, reader=reader)
    changed = deepcopy(frozen)
    changed['scope']['project_id'] = project()
    with pytest.raises(RecognitionConflict):
        owner.validate(changed)
    previous = application.state.recognition_runtime_root
    application.state.recognition_runtime_root = tmp_path
    try:
        with pytest.raises(RecognitionConflict):
            owner.validate(frozen)
    finally:
        application.state.recognition_runtime_root = previous


def test_actual_task_initial_keeps_the_original_style_rejection(application):
    app = application
    research = inspect.getclosurevars(app.state.workbench_task_observer).nonlocals['research']
    scope = project()
    row = save(app, scope)
    receipt, prepared = research.initial('turn-' + uuid4().hex, scope, '写一份有实际依据的说明', None)
    frozen = prepared['request']
    from core.ai_kernel.product_admission import bundle_from_payload
    binding = bundle_from_payload(prepared['admission_bundle'])
    assert app.state.ai_runtime.accept_turn(frozen, admission_bundle=binding).status == 'accepted'
    assert app.state.project_constraint_input.validate(frozen)['items'][0]['id'] == row['id']
    app.state.product_task_outcomes.validate(app.state.recognition_records, frozen)
    from backend.memory_app.v2.style_context import TASK_COLLECTION
    with app.state.recognition_records.begin() as reader:
        style = reader.read(TASK_COLLECTION, frozen['turn_id'])
        assert style is not None
        reader.put(TASK_COLLECTION, style.object_id, dict(style.payload), expected_revision=style.revision)
        with pytest.raises(RecognitionConflict, match='style'):
            app.state.product_task_outcomes.validate(TransactionRecords(reader), frozen)
        reader.rollback()


def test_actual_native_child_keeps_root_constraints_and_rejects_ancestry_changes(application):
    app, scope = application, project()
    row = save(app, scope, content='生成说明时保留实际单位和来源')
    frozen = request(app, scope, kind='project.task')
    bundle = prepare_admission(app, frozen, None, app.state.ai_turn_store)
    main = app.state.agent_runtime_composition.coordinator.accept_and_register_main(
        frozen, admission_bundle=bundle)
    child = app.state.agent_organization_runtime._prepare_steward(main)
    planner = app.state.ai_runtime.task_continuations.planner
    root = planner.profile_request(child.request)
    assert root == main.request
    app.state.product_task_guard(child.request, validate_constraints=False)
    app.state.product_task_guard(root)
    owner, store = app.state.project_constraint_input, app.state.ai_turn_store
    messages = owner.messages(root, [{'role': 'user', 'content': '准备说明'}])
    assert row['content'] in messages[0]['content']
    owner.input_budget(app.state.recognition_models, messages,
        revision=app.state.recognition_models.public_for_purpose('generation')['revision'], max_tokens=2000)
    ref = owner.record_input(root, child.request, messages, store, logical_input='真实后代准备')
    assert store.get(ref)['messages'] == messages
    assert store.get(ref)['root_turn_id'] == root['turn_id']
    assert not any(event['type'] in {'model.requested', 'model.completed'}
                   for event in store.events_after(child.request['turn_id']))
    for kind in ('body', 'binding'):
        changed = deepcopy(child.request)
        if kind == 'body':
            changed['input']['text'] += '篡改'
        else:
            changed['agent_binding']['parent_run_id'] = 'unbound-parent'
        with pytest.raises(ValueError, match='descendant request changed'):
            planner.profile_request(changed)
    save(app, scope, row['id'], content='修订后的完整约束', revision=row['revision'])
    with pytest.raises(RecognitionConflict):
        app.state.product_task_guard(root)
