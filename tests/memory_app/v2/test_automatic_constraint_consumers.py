from contextlib import contextmanager
import inspect
import os
from pathlib import Path
import shutil
import sys
import traceback
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.memory_app.v2.automatic_constraints import PREPARED
from backend.memory_app.v2.automatic_constraint_input import automatic_bundle
from backend.memory_app.v2.memory_turn import MemoryTurn
from backend.memory_app.v2.projects import assign_scene
from backend.recognition import RecognitionConflict, WorkScope
from core.ai_kernel.sqlite_store import SQLiteAITurnStore


@pytest.fixture(scope='module')
def application(tmp_path_factory):
    root = tmp_path_factory.mktemp('g15c').resolve(strict=True)
    assert root.is_absolute() and root.is_dir()
    assert not any(path.name.casefold() == 'runtime' for path in (root, *root.parents))
    configuration = root / 'config/settings.toml'
    configuration.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', configuration)
    prior = os.environ.get('CHRIPTMAS_APP_ROOT')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    try:
        assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) == root
        from backend.memory_app.app import create_app
        app = create_app(runtime_root=root)
        assert Path(app.state.recognition_runtime_root).resolve(strict=True) == root
        models = app.state.recognition_models
        models.update('generation', {'expected_revision': models.public()['generation']['revision'],
            'base_url': 'http://127.0.0.1:9/v1', 'model': 'gpt-4o-mini', 'api_key': 'synthetic-unused',
            'enabled': True, 'allow_remote': True})
        with TestClient(app):
            yield app
            with app.state.ai_turn_store._connect() as connection:
                assert connection.execute('SELECT COUNT(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0
                assert connection.execute("SELECT COUNT(*) FROM ai_turn_events WHERE json_extract(event_json, '$.type') IN ('model.attempt.dispatched', 'model.completed', 'turn.completed')").fetchone()[0] == 0
    finally:
        if prior is None:
            os.environ.pop('CHRIPTMAS_APP_ROOT', None)
        else:
            os.environ['CHRIPTMAS_APP_ROOT'] = prior


@contextmanager
def no_network():
    # 审计真实 Python 网络事件，不替换传输、模型或原有业务函数。
    events, active = [], [True]
    def observe(event, arguments):
        if active[0] and event in {'socket.connect', 'socket.getaddrinfo'}:
            caller = [(Path(frame.filename).name, frame.name) for frame in traceback.extract_stack(limit=7)[:-1]]
            address = arguments[1] if event == 'socket.connect' else None
            self_pipe = (event == 'socket.connect' and isinstance(address, tuple) and address[0] == '127.0.0.1'
                and type(address[1]) is int and address[1] > 1024
                and ('socket.py', '_fallback_socketpair') in caller
                and ('proactor_events.py', '_make_self_pipe') in caller)
            # Windows 事件循环通过 stdlib socketpair 建立唤醒管道，按实际调用链区分模型运输。
            if not self_pipe:
                events.append({'event': event, 'address': str(address), 'caller': caller})
    sys.addaudithook(observe)
    try:
        yield
    finally:
        active[0] = False
        assert events == []


def domain(app, *, large=False):
    records, models = app.state.recognition_records, app.state.recognition_models
    project = 'auto-consumer-' + uuid4().hex
    with records.begin() as tx:
        tx.put('v2_projects', project, {'name': '自动内容约束验收', 'scenes': ['会议', '预算'],
            'private': False, 'builtin': None}, expected_revision=0)
        tx.commit()
    owner = app.state.product_automatic_constraints
    owner.scopes.upsert(WorkScope('local-user', project), project + '-rule', 0,
        '保留每条适用条件。' * 9000 if large else '保留每条适用条件。',
        expected_scope_revision=0, expected_project_revision=1, mode='always')
    item = app.state.workspace_domains.items.create(project, 'text', '合成原件', '人工输入的会议记录')
    document = user_document(app, project, '会议记录')
    assign_scene(records, 'document', document['id'], project, '会议')
    return records, models, owner, project, item, document


def user_document(app, project, title):
    from backend.api.workbench_review_intent_runtime import ReviewIntentAdmission, ReviewIntentJobRepository
    from core.ingestion_core import ObjectStoreSourceRegistrar
    from core.product_core.workbench_source_intake import CaptureWorkbenchTextSource
    review = app.state.workspace_domains.review.legacy_reviews
    jobs = ReviewIntentJobRepository(review.jobs, ReviewIntentAdmission(review.records, review.object_store))
    capture = CaptureWorkbenchTextSource(source_registrar=ObjectStoreSourceRegistrar(review.object_store,
        namespace_id=app.state.recognition_documents.namespace_id, project_id=project), job_repository=jobs)
    markdown = '# ' + title + '-' + project + '\n\n## 摘要\n\n逐条核实工作条件。\n\n## 正文\n\n人工输入的会议记录。'
    captured = capture.execute(title=title + '-' + project, content=markdown)
    view = review.get(captured.source_id, project)
    assert view['status'] == 'ready' and view['draft_markdown'] == markdown
    confirmed = review.confirm(captured.source_id, project, expected_revision=view['revision'],
        expected_document_basis=view['document_basis'], expected_markdown=markdown)
    assert confirmed['status'] == 'confirmed'
    return app.state.recognition_documents.read(confirmed['document_id'])


def exports_from_factory(app):
    routers = [app.router]
    for router in routers:
        for route in router.routes:
            if hasattr(route, 'original_router'):
                routers.append(route.original_router)
            elif (getattr(route, 'path', None) == '/api/v2/projects/{project_id}/skill-exports'
                    and 'POST' in getattr(route, 'methods', ())):
                return inspect.getclosurevars(route.endpoint).nonlocals['exports']
    raise AssertionError('actual_skill_export_route_missing')


def test_actual_factory_injects_the_same_owner_into_methods_and_every_producer(application):
    app = application
    owner = app.state.product_automatic_constraints
    assert app.state.recognition_models.product_automatic_constraints is owner
    exports = exports_from_factory(app)
    assert exports.records is owner.records
    assert exports.generation_validator.__self__ is owner
    records, models, _, project, item, document = domain(app)
    with no_network():
        for producer, kind, mode, anchors, scene in (
            ('item-organize', 'memory.organize', 'object', [{'type': 'item', 'id': item['id']}], None),
            ('document-insights', 'memory.propose_insights', 'object', [{'type': 'document', 'id': document['id']}], None),
            ('inspirations', 'memory.propose_insights', 'scope', [{'type': 'document', 'id': document['id']}], '会议'),
            ('overview', 'memory.overview', 'scope', [{'type': 'document', 'id': document['id']}], '会议'),
            ('consolidation', 'memory.consolidate', 'project', [], None),
        ):
            turn = MemoryTurn(records, models, kind=kind, project=project, key=uuid4().hex, materials=[],
                validate=lambda: owner.scopes.list(WorkScope('local-user', project)),
                automatic={'producer': producer, 'mode': mode, 'scene': scene, 'anchors': anchors})
            assert turn.admission.owner is owner
            assert records.read(PREPARED, turn.turn_id).revision == 1
            admitted = turn.admission.admission(turn.request, turn.store)
            with turn.store.admission_bundle(turn.request, admitted):
                assert turn.store.claim_turn(turn.request) == (turn.turn_id, True)
            reopened = SQLiteAITurnStore(turn.store._path)
            assert reopened.restore_admission(turn.request) == admitted
            if producer == 'inspirations':
                from backend.memory_app.v2.inspiration_extraction import _attempt_input
                assert _attempt_input(records, turn.request) == turn.base_request()['input']
            with records.begin() as tx:
                turn.validate_commit(tx)
            assert automatic_bundle(turn.request, admitted)['text'] == ''
        excluded = MemoryTurn(records, models, kind='memory.link_suggest', project=project, key=uuid4().hex,
            materials=[], validate=lambda: None)
        assert excluded.admission is None


def test_real_organize_retry_replaces_preclaim_checkpoint_and_never_backfills_old_turn(application):
    from backend.memory_app.v2.organize_turns import OrganizeTurns, STEPS
    records, models, owner, project, item, _ = domain(application)
    messages = [{'role': 'user', 'content': '逐条整理原文'}]
    def make():
        turn = OrganizeTurns(root=Path(application.state.recognition_runtime_root), records=records,
            models=models, item_id=item['id'], project_id=project, source='人工输入的会议记录',
            validate_current=lambda: owner.scopes.list(WorkScope('local-user', project)))
        turn.input_limit = 1
        return turn
    with no_network():
        first = make()
        with pytest.raises(ValueError, match='material_analysis_budget_insufficient'):
            first.complete(messages, max_tokens=1000, stage='contract', validate_current=first.validate_current)
        before = records.read(STEPS, 'organize-' + item['id'] + '-contract')
        first_proof = records.read(PREPARED, before.payload['turn_id'])
        assert first_proof is not None and first.store.get_request(before.payload['turn_id']) is None
        second = make()
        with pytest.raises(ValueError, match='material_analysis_budget_insufficient'):
            second.complete(messages, max_tokens=1000, stage='contract', validate_current=second.validate_current)
        after = records.read(STEPS, before.object_id)
        assert after.revision == before.revision + 1 and after.payload['turn_id'] != before.payload['turn_id']
        assert records.read(PREPARED, before.payload['turn_id']) == first_proof
        staged = records.read(PREPARED, after.payload['turn_id'])
        request = staged.payload['request']
        admission = second.constraint_admission.admission(request, second.store)
        descriptor = admission.items[0].payload['automatic_descriptor']
        assert descriptor['producer_previous']['revision'] == before.revision
        assert descriptor['producer_previous']['payload'] == before.payload
        with second.store.admission_bundle(request, admission):
            assert second.store.claim_turn(request) == (request['turn_id'], True)
        assert second.constraint_admission.validate_current(request, SQLiteAITurnStore(second.store._path))['text'] == ''
        with records.begin() as tx:
            with pytest.raises(ValueError, match='producer_changed'):
                first.constraint_admission.validate_commit(first_proof.payload['request'], first.store, tx)


def test_material_analysis_and_transcript_language_budgets_reserve_the_complete_rule_block(application):
    from backend.memory_app.v2.material_analysis import _limits
    from backend.memory_app.v2.organize_turns import OrganizeTurns
    from backend.memory_app.v2.policies import get
    records, models, owner, project, item, _ = domain(application, large=True)
    organize = OrganizeTurns(root=Path(application.state.recognition_runtime_root), records=records,
        models=models, item_id=item['id'], project_id=project, source='人工输入的会议记录',
        validate_current=lambda: owner.scopes.list(WorkScope('local-user', project)))
    with no_network():
        with pytest.raises(ValueError, match='material_analysis_budget_insufficient'):
            _limits(organize)
        limits = models.generation_budget_limits(expected_revision=models.public()['generation']['revision'])
        available, _ = get('transcript_correction', version='@1').budget(limits)
        original_limit = organize.input_limit
        with pytest.raises(ValueError, match='transcript_organization_budget_insufficient'):
            organize.reserve_input_limit(available, error_code='transcript_organization_budget_insufficient', bind_limit=False)
        assert organize.input_limit == original_limit
    assert not any(row.object_id.startswith('organize-' + item['id'] + '-')
        for row in records.list('workspace_organize_steps'))


@pytest.mark.parametrize('producer', ['document-insights', 'inspirations', 'overview', 'consolidation', 'skill-export'])
def test_actual_content_owners_reject_complete_rule_prompt_before_any_provider(application, producer):
    records, models, owner, project, item, document = domain(application, large=True)
    documents, service = application.state.recognition_documents, application.state.recognition_service
    other = user_document(application, project, '第二份会议记录') if producer == 'consolidation' else None
    published = None
    if producer in {'consolidation', 'skill-export'}:
        from backend.memory_app.document_recognition import ensure_document_experience
        evidence, _ = ensure_document_experience(documents, service, project, document['id'])
        if other is not None:
            ensure_document_experience(documents, service, project, other['id'])
        else:
            scope = WorkScope('local-user', project)
            candidate = service.propose(scope=scope, content='办理前核对条件', conditions=['会议期间'], source_experience_ids=[evidence])
            published = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer='local-user')
    with no_network():
        if producer == 'document-insights':
            from backend.memory_app.v2.insight_generation import generate_insights
            errors = []
            assert generate_insights(models, service, documents, project, document['id'], on_error=errors.append) == []
            assert errors == ['insight_generation_failed']
        elif producer == 'inspirations':
            from backend.memory_app.v2.inspiration_extraction import extract_inspirations, InspirationExtractionError
            with pytest.raises(InspirationExtractionError, match='budget_insufficient'):
                extract_inspirations(models, service, documents, project=project, scene='会议',
                    selected=[{'document_id': document['id'], 'expected_revision': document['revision']}])
        elif producer == 'overview':
            from backend.memory_app.v2.overviews import ScopeOverviews
            with pytest.raises(RecognitionConflict, match='project constraints exceed input budget'):
                ScopeOverviews(records, documents, models).update(project, '会议')
        elif producer == 'consolidation':
            from backend.memory_app.v2.consolidation import Consolidation, Memory
            group = [Memory(doc['id'], doc['revision'], doc, 'document', (), (doc['id'],), ())
                for doc in (document, other)]
            with pytest.raises(RecognitionConflict, match='project constraints exceed input budget'):
                Consolidation(records, service, documents, models)._pattern(project, group)
        else:
            from backend.memory_app.v2.skill_generation import generate_skill
            with pytest.raises(RecognitionConflict, match='project constraints exceed input budget'):
                generate_skill(models, exports_from_factory(application), project,
                    sources=[{'id': published.id, 'revision': published.revision}], scene='会议')
        keys = [row for row in records.list('v2_memory_turn_keys') if row.payload['identity']['project'] == project]
        assert len(keys) == 1
        saved = records.read(PREPARED, keys[0].object_id)
        assert saved is not None and saved.revision == 1
        from core.ai_kernel.product_admission import bundle_from_payload
        admitted = bundle_from_payload(saved.payload['bundle'])
        snapshot = automatic_bundle(saved.payload['request'], admitted)
        assert snapshot['text'] == '' and len(snapshot['items']) == 1
        descriptor = admitted.items[0].payload['automatic_descriptor']
        assert descriptor['spec']['producer'] == producer
        assert records.list('v2_insight_patterns') == ()
        assert not any(row.payload.get('project_id') == project for row in records.list('v2_skill_exports'))
        native = MemoryTurn.store_for(records)
        if native.get_request(keys[0].object_id) is not None:
            assert native.get_immutable_payload(keys[0].object_id, 'memory-generation-output-v1') is None
            errors = [event for event in native.events_after(keys[0].object_id) if event['type'] == 'turn.failed']
            assert len(errors) == 1
            requested = [event for event in native.events_after(keys[0].object_id) if event['type'] == 'model.requested']
            assert len(requested) == 1
            binding = owner.bind(records, models, kind=saved.payload['request']['desired_outcome'], project=project,
                spec=descriptor['spec'])
            with pytest.raises(ValueError, match='model_request_unavailable'):
                binding.record_input(saved.payload['request'], native,
                    model_request_id=requested[0]['correlation']['model_request_id'],
                    messages=binding.messages(saved.payload['request'], native, []), response_model=None, max_tokens=1000)


def test_actual_inspiration_budget_retries_keep_each_birth_and_compare_original_material_input(application):
    from backend.memory_app.v2.inspiration_extraction import (
        InspirationExtractionError, _key, _validate_attempt_key, extract_inspirations,
    )
    records, models, owner, project, _, document = domain(application, large=True)
    selected = [{'document_id': document['id'], 'expected_revision': document['revision']}]
    previous, turns = None, []
    with no_network():
        for attempt in range(3):
            if attempt:
                current = records.read('project_constraints', project + '-rule')
                owner.scopes.upsert(WorkScope('local-user', project), current.object_id, current.revision,
                    '办理前逐条核实条件。' * 8000 + str(attempt), expected_scope_revision=0,
                    expected_project_revision=1, mode='always')
            with pytest.raises(InspirationExtractionError, match='budget_insufficient') as error:
                extract_inspirations(models, application.state.recognition_service,
                    application.state.recognition_documents, project=project, scene='会议', selected=selected,
                    retry_token=previous)
            identity = error.value.detail['turn_id']
            assert identity not in turns and error.value.detail['retry_token'] == identity
            turns.append(identity)
            previous = identity
            staged = records.read(PREPARED, identity)
            assert staged is not None and staged.revision == 1
            _validate_attempt_key(records, records.read('v2_memory_turn_keys', identity),
                _key(project, '会议', selected), staged.payload['request'])
            assert MemoryTurn.store_for(records).get_request(identity) is None
    assert len(turns) == 3 and all(records.read(PREPARED, identity) is not None for identity in turns)


def test_g11_learning_projection_reads_frozen_rules_after_current_rule_change(application):
    records, models, owner, project, _, _ = domain(application)
    turn = MemoryTurn(records, models, kind='memory.consolidate', project=project, key=uuid4().hex,
        materials=[], validate=lambda: None,
        automatic={'producer': 'consolidation', 'mode': 'project', 'scene': None, 'anchors': []})
    admitted = turn.admission.admission(turn.request, turn.store)
    with no_network(), turn.store.admission_bundle(turn.request, admitted):
        assert turn.store.claim_turn(turn.request) == (turn.turn_id, True)
        messages = application.state.product_steer_learning.sources._model_messages(turn.request)
        assert messages[0]['content'].endswith('保留每条适用条件。')
        rule = records.read('project_constraints', project + '-rule')
        owner.scopes.upsert(WorkScope('local-user', project), rule.object_id, rule.revision, '当前新增的条件。',
            expected_scope_revision=0, expected_project_revision=1, mode='always')
        assert application.state.product_steer_learning.sources._model_messages(turn.request) == messages
        with records.begin() as tx:
            with pytest.raises(ValueError, match='bundle_changed'):
                turn.validate_commit(tx)
