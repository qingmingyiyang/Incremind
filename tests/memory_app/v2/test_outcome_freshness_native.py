from copy import deepcopy
import json
import os
from pathlib import Path
from shutil import copyfile
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.recognition import RecognitionConflict, WorkScope
from backend.memory_app.kernel.turn_requests import freeze_turn_request
from backend.memory_app.v2.outcome_freshness import OutcomeFreshness
from backend.memory_app.v2.outcome_freshness_routes import install_outcome_freshness_routes
from backend.memory_app.v2.outcome_source_preparation import read_model_context
from backend.memory_app.v2.profile import confirmed_profile, freeze_task_profile
from backend.memory_app.v2.recall_preferences import set_preference


@pytest.fixture(scope='module')
def environment():
    root = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) / 'native-case'
    root.mkdir()
    previous = os.environ['CHRIPTMAS_APP_ROOT']
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    settings = root / 'config/settings.toml'
    settings.parent.mkdir()
    copyfile(Path(__file__).parents[3] / 'config/settings.toml.example', settings)
    # 全局应用只在专属根已经解析并配置后导入。
    from backend.memory_app.app import create_app
    app = create_app(runtime_root=root)
    service = OutcomeFreshness(app)
    install_outcome_freshness_routes(app, service=service)
    try:
        with TestClient(app) as client:
            created = client.post('/api/v2/projects', json={'name': '成果依据隔离验证'})
            assert created.status_code == 200
            project = created.json()['id']
            assert app.state.ai_turn_store is app.state.ai_runtime._state
            yield app, client, service, project
    finally:
        os.environ['CHRIPTMAS_APP_ROOT'] = previous


def publish(app, project, text):
    scope = WorkScope('local-user', project)
    service = app.state.recognition_service
    experience = service.stage_experience(scope=scope, content='用户提供的合成依据')
    candidate = service.propose(scope=scope, content=text, source_experience_ids=[experience])
    return service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer='local-user')


def test_real_source_freeze_tracks_edits_and_manual_forgetting(environment):
    app, _, freshness, project = environment
    item = publish(app, project, '预算控制在十万元以内')
    material = {'type': 'recognition', 'id': item.id, 'revision': item.revision,
        'project_id': project, 'role': 'evidence', 'proof_ref': 'source-comparison'}
    owner = freshness._source_owner(project)
    frozen = freshness._freeze_material(owner, material)
    assert len(frozen) == 1 and frozen[0]['verified'] is True
    fact = {'sources': [{'evidence': 'crp://default/recognitions/' + item.id,
        'source': {'origin': frozen[0]['origin'], 'frozen': frozen[0]['frozen']}}]}
    with freshness._read_scope() as view:
        changed, unavailable, _ = freshness._compare(view, fact, '2026-11-05T00:00:00Z')
    assert changed == [] and unavailable == []
    set_preference(app.state.recognition_records, WorkScope('local-user', project), item.id,
        recognition_revision=item.revision, preference_revision=0, state='cooled')
    with freshness._read_scope() as view:
        changed, unavailable, _ = freshness._compare(view, fact, '2026-11-05T00:00:00Z')
    assert changed == [] and unavailable == []
    revised = app.state.recognition_service.revise(scope=WorkScope('local-user', project),
        recognition_id=item.id, expected_revision=item.revision, content='预算控制在八万元以内')
    with freshness._read_scope() as view:
        changed, unavailable, _ = freshness._compare(view, fact, '2026-11-05T00:00:00Z')
    assert len(changed) == 1 and changed[0]['reasons'] == ['edited'] and unavailable == []
    set_preference(app.state.recognition_records, WorkScope('local-user', project), item.id,
        recognition_revision=revised.revision, preference_revision=1, state='forgotten')
    with freshness._read_scope() as view:
        changed, unavailable, _ = freshness._compare(view, fact, '2026-11-05T00:00:00Z')
    assert changed[0]['reasons'] == ['edited', 'forgotten'] and unavailable == []
    with freshness._read_scope() as view:
        qualified = freshness._qualify_source_proofs(frozen, reader=view, local_only=True)
    assert qualified['complete'] is True and qualified['recognitions'] == []
    assert all(item['privacy_only'] for item in qualified['current_sources'])
    from backend.memory_app.v2.privacy import set_private_project
    set_private_project(app.state.recognition_records, project, True, 0)
    try:
        with freshness._read_scope() as view:
            local = freshness._qualify_source_proofs(frozen, reader=view, local_only=True)
        assert local['complete'] is True and local['privacy_ceiling']['effective_purposes'] == []
        with freshness._read_scope() as view, pytest.raises(RecognitionConflict, match='private_source_remote_blocked'):
            freshness._qualify_source_proofs(frozen, reader=view, local_only=False)
    finally:
        set_private_project(app.state.recognition_records, project, False, 1)


def test_actual_profile_input_is_bound_without_claiming_a_wire_execution(environment):
    app, _, freshness, project = environment
    publish(app, 'me', '每次先列出验收条件')
    records, native, runtime = app.state.recognition_records, app.state.ai_turn_store, app.state.ai_runtime
    profile = confirmed_profile(records, app.state.recognition_service)
    privacy = app.state.workspace_domains.query.freeze_answer_privacy(project, local_only=True)
    privacy['material_refs'] = [{'type': 'recognition', 'id': item['id'], 'revision': item['revision'],
        'project_id': 'me'} for item in profile['items']]
    privacy['source_snapshots'] = [item['snapshot'] for item in profile['items']]
    identity = 'freshness-native-' + uuid4().hex
    request = freeze_turn_request('project.task', turn_id=identity, session_id='session-' + identity,
        operation_id='operation-' + uuid4().hex, idempotency_key='key-' + uuid4().hex,
        project_id=project, created_at='2026-11-05T00:00:00Z', text=json.dumps({'task': '合成任务'}, ensure_ascii=False),
        privacy=privacy, capabilities=['memory.recall'], template_version=1)
    request['capability_policy']['require_approval'] = ['memory.recall']
    request['approval_policy'] = {'mode': 'explicit', 'auto_approve_read_only': False}
    request['context_policy'].update(include_project_skill=False, include_memory=False, include_session_history=False)
    request['capability_request'] = {'mode': 'execute_exact_v1',
        'capability_id': 'memory.recall', 'arguments': {'query': '合成任务'}}
    accepted = runtime.submit_turn(request)
    assert accepted.status == 'waiting_approval'
    try:
        empty = read_model_context(native, request)
        assert empty['context_state'] == 'not_yet_consumed' and empty['recognitions'] == []
        with freshness._read_scope() as view:
            assert freshness.qualify_learning_context(empty, reader=view, local_only=True)['complete'] is True
        freeze_task_profile(records, request, profile)
        messages = [{'role': 'system', 'content': profile['text']}, {'role': 'user', 'content': request['input']['text']}]
        kind = 'product-task-model-input-v1-no-wire'
        prepared = freshness.prepare_input_basis(request=request, input_kind=kind, messages=messages)
        payload = {'messages': messages, 'outcome_source_preparation_ref': prepared,
            'context_sources': native.get(prepared)['context']['materials'],
            'context_proof_refs': native.get(prepared)['context']['proof_refs']}
        reference = native.get_or_create_immutable_payload(identity, kind, payload)
        basis = freshness.capture_input_basis(request=request, input_kind=kind, input_ref=reference)
        context = read_model_context(native, request, input_ref=reference)
        assert [row['id'] for row in context['recognitions']] == [row['id'] for row in profile['items']]
        assert all(row['project_id'] == 'me' and row['role'] == 'profile' for row in context['materials'])
        with pytest.raises(RecognitionConflict):
            freshness._consumed_basis(basis)
        with freshness._read_scope() as view, pytest.raises(RecognitionConflict):
            freshness.qualify_learning_context(context, reader=view, local_only=True)
        changed = deepcopy(payload)
        changed['messages'][-1]['content'] = '另一段实际输入'
        changed_ref = native.get_or_create_immutable_payload(identity, kind + '-changed', changed)
        with pytest.raises(RecognitionConflict, match='outcome_final_input_changed'):
            read_model_context(native, request, input_ref=changed_ref)
        source = records.read('recognitions', profile['items'][0]['id'])
        with records.begin() as tx:
            tx.put('recognitions', source.object_id, {**source.payload, 'content': '事务内另一画像'},
                expected_revision=source.revision)
            with pytest.raises(RecognitionConflict):
                freshness.validate_input_preparation(request=request, input_kind=kind,
                    preparation_ref=prepared, input_ref=reference, reader=tx)
            tx.rollback()
        assert records.read('recognitions', source.object_id) == source
        assert not [event for event in native.events_after(identity) if event['type'].startswith('model.')]
    finally:
        cancelled = runtime.apply_action({'schema_version': '1.0.0', 'action_id': 'cancel-' + uuid4().hex,
            'turn_id': identity, 'type': 'cancel', 'target_event_id': None, 'actor': 'user',
            'reason': '结束成果依据隔离验证', 'expected_sequence': len(tuple(native.events_after(identity))),
            'idempotency_key': 'cancel-' + uuid4().hex, 'created_at': '2026-11-05T00:01:00Z'})
        assert cancelled.status == 'cancelled'


def test_missing_fact_routes_report_unknown_and_keep_original_document(environment):
    app, client, freshness, project = environment
    from core.document_engine.ports import DocumentDraft
    original_item = app.state.workspace_domains.items.create(project, 'text', '本机合成原件', '合成正文')
    document = app.state.recognition_documents.create(DocumentDraft(title='本机合成稿',
        document_type='note', markdown='合成正文', source_refs=({'source_id': original_item['id'],
            'locator': 'workspace://' + original_item['id']},), project_id=project))
    identity = document['id']
    original = app.state.recognition_records.read('documents', identity)
    with freshness._read_scope() as view:
        assert freshness._fresh_candidates(view, {'project_id': project, 'document_id': identity,
            'scope_origin': freshness._origin(freshness._source_owner(project))}) == []
    response = client.get('/api/v2/library/outcomes/' + identity + '/changes', params={'project_id': project})
    assert response.status_code == 200, response.json()
    unknown = response.json()
    assert unknown['verified'] is False and unknown['fresh_count'] is None and unknown['observation'] is None
    for action in ('ack', 'update'):
        failed = client.post('/api/v2/library/outcomes/' + identity + '/' + action,
            json={'project_id': project, 'expected_revision': 0, 'observation': {}},
            headers={'Idempotency-Key': 'missing-history-' + action})
        assert failed.status_code == 409, failed.json()
    assert freshness.records.read('documents', identity) == original
    assert freshness.records.read('v2_outcome_ack', identity) is None


def test_actual_document_edits_and_verification_keep_distinct_change_semantics(environment):
    app, _, freshness, project = environment
    from core.document_engine.ports import DocumentDraft
    from backend.memory_app.v2.layers import mark_verified
    item = app.state.workspace_domains.items.create(project, 'text', '编辑依据原件', '交付日期为六月')
    documents = app.state.recognition_documents
    document = documents.create(DocumentDraft(title='待核对整理稿', document_type='note', markdown='交付日期为六月',
        source_refs=({'source_id': item['id'], 'locator': 'workspace://' + item['id']},), project_id=project))
    owner = freshness._source_owner(project)
    with freshness._read_scope() as view:
        before = freshness._source(owner, view, 'document', document['id'])
        original = freshness._source(owner, view, 'original_item', item['id'])
    mark_verified(app.state.recognition_records, document['id'], 1)
    with freshness._read_scope() as view:
        verified = freshness._source(owner, view, 'document', document['id'])
    from backend.memory_app.v2.policies import get
    assert get('stale', version='@1').changed(before, verified, reference=freshness.now())['reasons'] == []
    documents.save_user_edit(document['id'], markdown='交付日期为七月', expected_revision=1)
    app.state.workspace_domains.items.update(item['id'], project, {'staged'}, source_text='原件重新转写为七月交付')
    with freshness._read_scope() as view:
        edited = freshness._source(owner, view, 'document', document['id'])
        transcribed = freshness._source(owner, view, 'original_item', item['id'])
    assert get('stale', version='@1').changed(before, edited, reference=freshness.now())['reasons'] == ['edited']
    assert get('stale', version='@1').changed(original, transcribed, reference=freshness.now())['reasons'] == ['edited']


def test_actual_confirmed_replacement_and_validity_end_are_visible(environment):
    app, _, freshness, project = environment
    from backend.memory_app.v2.links import InsightLinks
    old = publish(app, project, '原有执行日期为六月')
    material = {'type': 'recognition', 'id': old.id, 'revision': old.revision,
        'project_id': project, 'role': 'evidence', 'proof_ref': 'replacement-comparison'}
    frozen = freshness._freeze_material(freshness._source_owner(project), material)[0]
    replacement = publish(app, project, '用户已确认执行日期改为七月')
    links = InsightLinks(app.state.recognition_records, app.state.recognition_service)
    proposal = links.propose(project, replacement.id, old.id, 'supersedes', '用户确认执行日期更新')
    links.review(project, proposal['id'], proposal['revision'], True)
    with freshness._read_scope() as view:
        current = freshness._source(freshness._source_owner(project), view, 'recognition', old.id)
    from backend.memory_app.v2.policies import get
    reasons = get('stale', version='@1').changed(frozen['frozen'], current, reference=freshness.now())['reasons']
    assert 'superseded' in reasons and 'expired' in reasons


def test_actual_failed_read_cannot_grant_material_and_its_payload_is_bound(environment):
    app, _, freshness, project = environment
    from core.ai_kernel.model_planner import _materialize_event
    publish(app, project, '执行日期为七月')
    native = app.state.ai_turn_store
    identity = 'freshness-read-' + uuid4().hex
    request = freeze_turn_request('project.task', turn_id=identity, session_id='session-' + identity,
        operation_id='operation-' + uuid4().hex, idempotency_key='key-' + uuid4().hex,
        project_id=project, created_at=freshness.now(), text=json.dumps({'task': '查阅执行日期'}, ensure_ascii=False),
        privacy=app.state.workspace_domains.query.freeze_answer_privacy(project, local_only=True),
        capabilities=['memory.recall'], template_version=1)
    request['context_policy'].update(include_project_skill=False, include_memory=False, include_session_history=False)
    request['capability_request'] = {'mode': 'execute_exact_v1',
        'capability_id': 'memory.recall', 'arguments': {'query': '执行日期'}}
    runner = app.state.ai_turn_runner
    runner.accept_and_submit(request)
    completed = runner.wait_for_terminal(identity, timeout_seconds=30)
    assert completed is not None and completed.status == 'failed'
    events = [_materialize_event(event, native) for event in native.events_after(identity)]
    assert any(event['type'] == 'tool.outcome.recorded' for event in events)
    assert next(event for event in events if event['type'] == 'tool.outcome.recorded')['resolved_payload']['status'] == 'failed'
    frame = {'goal': request['desired_outcome'], 'scope': request['scope'], 'input': request['input'], 'events': events,
        'capabilities': [], 'context': [], 'decision_contract': {'complete': {'evidence_refs': []}}}
    kind = 'product-task-model-input-v1-read-only'
    reference = freshness.prepare_input_basis(request=request, input_kind=kind,
        messages=[{'role': 'user', 'content': json.dumps(frame, ensure_ascii=False)}])
    context = native.get(reference)['context']
    assert context['materials'] == [] and context['proof_refs'] == []
    changed = deepcopy(frame)
    outcome = next(event for event in changed['events'] if event['type'] == 'tool.outcome.recorded')
    outcome['resolved_payload'] = {'status': 'completed', 'result': '另一段正文'}
    with pytest.raises(RecognitionConflict, match='outcome_planner_result_changed'):
        freshness.prepare_input_basis(request=request, input_kind=kind + '-changed',
            messages=[{'role': 'user', 'content': json.dumps(changed, ensure_ascii=False)}])
    assert not [event for event in native.events_after(identity) if event['type'].startswith('model.')]


def test_update_policy_preserves_historical_text_and_separates_reference_namespace():
    from backend.memory_app.v2.policies.outcome_update import v1
    original = {'document_id': 'retained-original',
        'current_markdown': '原有判断 [1] crp://default/recognitions/old。\n\n用户编辑保留。',
        'source_changes': [{'id': 'old-source', 'reasons': ['forgotten']}],
        'current_sources': [{'id': 'current-source', 'revision': 2}]}
    before = deepcopy(original)
    messages = v1('messages', original)
    history = json.loads(messages[1]['content'])
    assert history['citation_namespace'] == 'previous/retained-original'
    assert history['markdown'] == original['current_markdown'].replace('crp://', 'previous/retained-original/crp/')
    assert 'crp://' not in history['markdown']
    assert history['current_sources'] == [{'id': 'current-source', 'revision': 2}]
    assert original == before
    assert v1('current_context', {'reasons': ['forgotten']}) is False


def test_update_planner_projection_uses_real_current_text_and_same_payload_projection():
    from backend.memory_app.v2.policies.outcome_update import v1
    old, current = 'crp://default/recognitions/old', 'crp://default/recognitions/current'
    basis = {'preparation_id': 'prepared-update', 'document_id': 'retained-original'}
    original_input = {'text': json.dumps({'task': '更新计划', 'outcome_input': {
        'continuation_policy': '@1', 'document_id': 'retained-original', 'document_revision': 2,
        'update_basis': basis, 'current_markdown': '原判断 ' + old, 'source_refs': [old]}}),
        'refs': [{'uri': old}, {'uri': current}], 'project_id': 'alpha'}
    original_payload = {'nested': original_input, 'other': '未改动'}
    frame = {'input': original_input, 'scope': {'project_id': 'alpha'},
        'events': [{'resolved_payload': original_payload}], 'task': original_input['text'],
        'capabilities': ['memory.recall'], 'context': [{'id': 'original-proof'}]}
    history = {'basis': basis, 'document_id': 'retained-original',
        'current_markdown': '用户编辑保留 ' + old, 'birth_ai_markdown': '原始生成 ' + old,
        'source_changes': [{'id': 'old', 'reasons': ['forgotten']}],
        'current_sources': [{'text': '当前实际正文', 'aliases': [current], 'material': {'id': 'current'}}],
        'privacy_ceiling': {'sources': [{'aliases': [old]}]}}
    before = deepcopy((original_input, frame, history))
    projected_text = v1('planner_input', {'text': json.dumps(frame, ensure_ascii=False),
        'original_input': original_input, 'history': history})
    projected = json.loads(projected_text)
    assert old not in projected_text and current in projected_text
    assert '当前实际正文' in projected_text and '用户编辑保留' in projected_text
    assert 'previous/retained-original/crp/' in projected_text
    assert projected['input']['refs'] == [{'uri': current}]
    assert projected['scope'] == frame['scope'] and projected['context'] == frame['context']
    assert projected['events'][0]['resolved_payload'] == v1('payload', {
        'value': original_payload, 'original_input': original_input, 'history': history})
    assert projected['task'] == projected['input']['text']
    assert (original_input, frame, history) == before
    changed = deepcopy(frame)
    changed['input']['project_id'] = 'other'
    with pytest.raises(ValueError, match='outcome_update_planner_input_changed'):
        v1('planner_input', {'text': json.dumps(changed), 'original_input': original_input, 'history': history})
