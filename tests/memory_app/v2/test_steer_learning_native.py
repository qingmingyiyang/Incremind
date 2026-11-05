from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
from shutil import copyfile
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.recognition import RecognitionConflict, WorkScope
from backend.memory_app.v2.policies import get, override, register
from backend.memory_app.v2.policies.consolidate_steer import v3
from backend.memory_app.v2.policies.steer import v1
from backend.memory_app.v2.steer_learning import ERRORS, TERMINALS
from backend.memory_app.v2.steer_learning_sources import CANDIDATES
from backend.memory_app.v2.turn_message_order import COLLECTION as MESSAGES


@pytest.fixture(scope='module')
def installed():
    root = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) / 'learning-native'
    root.mkdir()
    previous = os.environ['CHRIPTMAS_APP_ROOT']
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    config = root / 'config/settings.toml'
    config.parent.mkdir()
    copyfile(Path(__file__).parents[3] / 'config/settings.toml.example', config)
    try:
        get('consolidate', version='@3')
    except ValueError:
        register('consolidate', '@3')(v3)
    # 正常工厂仅在专属数据根已经解析并配置后导入。
    from backend.memory_app.app import create_app
    app = create_app(runtime_root=root)
    learning = app.state.product_steer_learning
    try:
        with TestClient(app) as client:
            response = client.post('/api/v2/projects', json={'name': '插话学习合成项目'})
            assert response.status_code == 200
            yield app, client, learning, response.json()['id']
    finally:
        os.environ['CHRIPTMAS_APP_ROOT'] = previous


@pytest.mark.parametrize('text,category', [
    ('以后请用列表回答', 'future'), ('每次都先列步骤', 'future'), ('今后不要使用术语', 'future'),
    ('下次请先问日期', 'future'), ('总是用简短标题', 'future'), ('往后请写出依据', 'future'),
    ('一直保持中文回答', 'future'), ('以后都标注适用条件', 'future'),
    ('不对，日期应为七月', 'correction'), ('不是这个项目', 'correction'), ('别用表格', 'correction'),
    ('太长了', 'correction'), ('换成中文', 'correction'), ('我要的是执行步骤', 'correction'),
    ('这里写错了', 'correction'), ('不用铺垫', 'correction'),
    ('顺便查一下价格', 'addition'), ('另外加上联系人', 'addition'), ('补充一个截止日期', 'addition'),
    ('预算是十万元', 'addition'), ('先不做统计', 'addition'), ('这次用表格', 'addition'),
    ('本次先列结论', 'addition'), ('提醒明天开会', 'addition'),
    ('继续', 'other'), ('快点', 'other'), ('好', 'other'), ('收到', 'other'), ('可以', 'other'),
    ('了解', 'other'), ('接着来', 'other'), ('辛苦', 'other'),
    ('别忘了顺便查价格', 'addition'), ('不要忘记联系同事', 'addition'), ('以后再讨论价格', 'addition'),
    ('今后再说', 'addition'), ('下次再看', 'addition'), ('这次不要铺垫', 'addition'),
    ('本次先不做', 'addition'), ('不是以后再说，是现在修改', 'correction'),
])
def test_actual_local_classifier_preserves_four_categories(text, category):
    classified = v1(text)
    assert classified['category'] == category
    assert classified['learnable'] == (category in {'future', 'correction'})


def test_consolidation_requires_same_direction_in_distinct_turns_and_independent_roots():
    def event(identity, turn, kind, direction):
        return {'event_id': identity, 'via': 'steer', 'turn_id': turn, 'type': kind, 'direction': direction}
    first = event('one', 'turn-one', 'correction', '不要表格')
    same_turn = event('two', 'turn-one', 'correction', '不要表格')
    opposite = event('three', 'turn-two', 'correction', '使用表格')
    second = event('four', 'turn-two', 'correction', '不要表格')
    future = event('five', 'turn-three', 'future', '以后请用列表')
    assert v3(operation='events', events=[first, same_turn, opposite, future]) == [future]
    assert v3(operation='events', events=[first, second, opposite, future]) == [first, second, future]
    assert v3(operation='accept', events=[first, second], event_ids=['one'], expected_ids=['one', 'four'], kind='correction') is False
    assert v3(operation='accept', events=[first, second], event_ids=['one', 'four'], expected_ids=['one', 'four'], kind='correction')
    assert v3(operation='accept', events=[future], event_ids=['five'], expected_ids=['five'], kind='correction')
    assert v3(operation='accept', events=[first, second], event_ids=['one', 'four'], expected_ids=['one', 'four'], kind='pattern', source_count=1) is False
    assert v3(operation='accept', events=[first, second], event_ids=['one', 'four'], expected_ids=['one', 'four'], kind='pattern', source_count=2)
    assert v1.followup('2026-11-05T00:00:00Z', '2026-11-05T00:10:00Z')
    assert v1.followup('2026-11-05T00:00:00Z', '2026-11-05T00:10:01Z') is False


@pytest.mark.parametrize('conditions,situation,expected', [
    ([], '面试准备时', ['面试准备时']), ([], None, ['原问题全文']),
    (['保留模型提出的条件'], '面试准备时', ['保留模型提出的条件']),
])
def test_learning_conditions_use_actual_situation_then_original_question(conditions, situation, expected):
    assert v3(operation='conditions', conditions=conditions, situation=situation, question='原问题全文') == expected


def test_real_message_order_is_atomic_idempotent_and_keeps_non_user_intermediates(installed):
    app, _, learning, project = installed
    records = app.state.recognition_records
    thread = 'thread-order-' + uuid4().hex
    at = datetime.now(timezone.utc).isoformat()
    with records.begin() as tx:
        tx.put('v2_threads', thread, {'project_id': project, 'created_at': at}, expected_revision=0)
        first = learning.record_message(tx, public_id='order-first', project_id=project, thread_id=thread,
            text='原问题', accepted_at=at)
        upload = learning.record_message(tx, public_id='order-upload', project_id=project, thread_id=thread,
            text='', accepted_at=at)
        second = learning.record_message(tx, public_id='order-middle', project_id=project, thread_id=thread,
            text='自动更新', accepted_at=at, origin='outcome_update')
        third = learning.record_message(tx, public_id='order-last', project_id=project, thread_id=thread,
            text='以后请用列表', accepted_at=at)
        assert [first['sequence'], upload['sequence'], second['sequence'], third['sequence']] == [1, 2, 3, 4]
        assert learning.order.pair(tx, 'order-middle')[0].payload['text'] == ''
        assert learning.order.pair(tx, 'order-last')[0].object_id == 'order-middle'
        assert learning.record_message(tx, public_id='order-last', project_id=project, thread_id=thread,
            text='以后请用列表', accepted_at=at) == third
        with pytest.raises(RecognitionConflict, match='turn_message_replay_changed'):
            learning.record_message(tx, public_id='order-last', project_id=project, thread_id=thread,
                text='其他文字', accepted_at=at)
        tx.commit()
    with records.begin() as tx:
        learning.record_message(tx, public_id='order-rolled-back', project_id=project, thread_id=thread,
            text='事务未提交', accepted_at=at)
    assert records.read(MESSAGES, 'order-rolled-back') is None


def test_missing_and_foreign_candidate_sidecars_never_grant_native_provenance(installed):
    app, _, learning, project = installed
    service, records = app.state.recognition_service, app.state.recognition_records
    scope = WorkScope('local-user', project)
    experience = service.stage_experience(scope=scope, content='用户主动写下的合成内容')
    candidate = service.propose(scope=scope, content='手工待确认认识', source_experience_ids=[experience])
    with records.begin() as tx:
        row = tx.read('recognition_candidates', candidate.id)
        assert learning.sources.validate_candidate_origin(tx, scope, row) is None
        assert learning.sources.source_view(tx, scope, row) is None
        tx.put(CANDIDATES, candidate.id, {'owner': {'owner_user_id': 'other-user'}}, expected_revision=0)
        with pytest.raises(RecognitionConflict, match='steer_learning_candidate_owner_changed'):
            learning.sources.validate_candidate_origin(tx, scope, row)


def test_prepared_auxiliary_input_cannot_be_consumed_before_actual_native_acceptance(installed):
    app, _, learning, project = installed
    from backend.memory_app.v2.memory_turn import MemoryTurn
    from backend.memory_app.v2.steer_learning import SteerLearningOutput
    models, records = app.state.recognition_models, app.state.recognition_records
    models.update('generation', {'expected_revision': models.public()['generation']['revision'],
        'base_url': 'http://127.0.0.1:9/v1', 'model': 'gpt-4o-mini',
        'api_key': 'isolated-no-wire-placeholder', 'enabled': True, 'allow_remote': False})
    with override(consolidate='@3'):
        turn = MemoryTurn(records, models, kind='memory.consolidate', project=project,
            key=['steer-learning-v1', [], []], materials=[], validate=learning.freshness._validate)
        with records.begin() as tx:
            learning.sources.stage_auxiliary_input(tx, turn, [])
            assert learning.sources.validate_auxiliary_request(tx, turn.request, local_only=True)['feedback'] == []
            with pytest.raises(RecognitionConflict, match='steer_learning_auxiliary_native_unavailable'):
                learning.sources._generated(learning.freshness._reader(tx), turn.turn_id, settled=False)
            tx.commit()
        with pytest.raises(RecognitionConflict, match='steer_learning_model_input_owner_changed'):
            learning.sources.capture_model_input(owner=turn, request=turn.request, model_request_id='not-accepted',
                messages=[], response_schema=SteerLearningOutput.model_json_schema(),
                result_schema=SteerLearningOutput.model_json_schema(), governed=False)
        assert app.state.ai_turn_store.get_request(turn.turn_id) is None
        app.state.ai_turn_store.claim_turn(turn.request)
        from backend.memory_app.v2.turn_requests import validate_frozen_inputs
        from backend.memory_app.transaction_records import TransactionRecords
        with records.begin() as tx:
            prepared = tx.read('v2_steer_learning_inputs', turn.turn_id)
            tx.delete(prepared.collection, prepared.object_id, expected_revision=prepared.revision)
            with pytest.raises(RecognitionConflict, match='steer_learning_auxiliary_input_unavailable'):
                validate_frozen_inputs(TransactionRecords(tx), models, turn.request, source_owner=records)
        assert not any(event['type'].startswith('model.') for event in app.state.ai_turn_store.events_after(turn.turn_id))


def test_actual_no_match_followup_inspiration_uses_native_terminal_and_recovers_once(installed):
    app, client, learning, project = installed
    records = app.state.recognition_records
    models = app.state.recognition_models
    models.update('generation', {'expected_revision': models.public()['generation']['revision'],
        'base_url': 'https://127.0.0.2:9/v1', 'model': 'gpt-4o-mini',
        'api_key': 'isolated-no-wire-placeholder', 'enabled': True, 'allow_remote': False})
    with override(steer='@2', consolidate='@3'):
        first = client.post('/api/v2/workbench/turns', json={'project_id': project, 'intent': 'ask',
            'text': '没有任何匹配资料的合成问题'}, headers={'Idempotency-Key': 'learning-no-match-first'})
        assert first.status_code == 200
        original = first.json()['turn']
        assert original['receipt']['ask']['no_match'] is True
        original_request = app.state.ai_turn_store.get_request(original['id'])
        with records.begin() as tx:
            learning.record_message(tx, public_id=original['id'], project_id=project, thread_id=original['thread_id'],
                text=original['user_text'], accepted_at=learning.order.read(tx, original['id']).payload['accepted_at'],
                native_request=original_request)
            assert learning.stage_public_in_uow(tx, original['id']) == ()
            tx.commit()
        second = client.post('/api/v2/workbench/turns', json={'project_id': project, 'thread_id': original['thread_id'],
            'intent': 'inspiration', 'text': '#插话学习合成项目 以后请先列执行步骤'},
            headers={'Idempotency-Key': 'learning-no-match-second'})
        assert second.status_code == 200
        following = second.json()['turn']
        candidate = records.read('recognition_candidates', following['receipt']['inspiration']['insight']['id'])
        assert candidate.payload['scope'] == {'user_id': 'local-user', 'project_id': project}
        with records.begin() as tx:
            learning.record_message(tx, public_id=following['id'], project_id=project, thread_id=following['thread_id'],
                text=following['user_text'], accepted_at=learning.order.read(tx, following['id']).payload['accepted_at'],
                native_request=app.state.ai_turn_store.get_request(following['id']))
            facts = learning.stage_public_in_uow(tx, following['id'])
            assert len(facts) == 1 and facts[0]['via'] == 'followup' and facts[0]['category'] == 'future'
            assert facts[0]['target_turn_id'] == original['id'] and facts[0]['learnable'] is True
            assert all(context['materials'] == [] for context in facts[0]['before_contexts'])
            assert learning.stage_public_in_uow(tx, following['id']) == facts
            tx.commit()
        with learning.freshness._read_scope() as view:
            events = learning.consumer_events(project, reader=view, local_only=True)
            assert [event['event_id'] for event in events] == [facts[0]['id']]
            before = deepcopy(learning.countable_events(view))
            assert learning.countable_events(view) == before
        assert learning.refresh() == 0
        with sqlite3.connect(Path(app.state.ai_turn_store._path).resolve().as_uri() + '?mode=ro', uri=True) as connection:
            assert connection.execute("SELECT COUNT(*) FROM ai_turn_events WHERE json_extract(event_json,'$.type') = ?",
                ('model.attempt.dispatched',)).fetchone()[0] == 0


def test_actual_remote_history_switch_refusal_retains_zero_dispatch_and_no_feedback(installed):
    app, client, learning, project = installed
    records, models = app.state.recognition_records, app.state.recognition_models
    models.update('generation', {'expected_revision': models.public()['generation']['revision'],
        'base_url': 'https://127.0.0.2:9/v1', 'model': 'gpt-4o-mini',
        'api_key': 'isolated-no-wire-placeholder', 'enabled': True, 'allow_remote': False})
    with override(steer='@2', consolidate='@3'):
        first = client.post('/api/v2/workbench/turns', json={'project_id': project, 'intent': 'ask',
            'text': '另一个没有匹配资料的合成问题'}, headers={'Idempotency-Key': 'learning-switch-first'})
        assert first.status_code == 200
        original = first.json()['turn']
        assert original['receipt']['ask']['no_match'] is True
        before = records.list('v2_turn_steer_feedback')
        second = client.post('/api/v2/workbench/turns', json={'project_id': project, 'thread_id': original['thread_id'],
            'intent': 'ask', 'text': '不对，请列具体步骤'}, headers={'Idempotency-Key': 'learning-switch-second'})
        assert second.status_code == 409 and second.json()['detail'] == 'source_changed_retry'
        assert records.list('v2_turn_steer_feedback') == before
        request = records.read('v2_turn_requests', 'learning-switch-second')
        assert request.payload['state'] == 'failed' and request.payload['error'] == 'source_changed_retry'
        with sqlite3.connect(Path(app.state.ai_turn_store._path).resolve().as_uri() + '?mode=ro', uri=True) as connection:
            assert connection.execute("SELECT COUNT(*) FROM ai_turn_events WHERE json_extract(event_json,'$.type') = ?",
                ('model.attempt.dispatched',)).fetchone()[0] == 0


def test_real_observer_keeps_source_failure_pending_and_processes_other_public_turns(installed):
    app, client, learning, project = installed
    records, models = app.state.recognition_records, app.state.recognition_models
    models.update('generation', {'expected_revision': models.public()['generation']['revision'],
        'base_url': 'https://127.0.0.2:9/v1', 'model': 'gpt-4o-mini',
        'api_key': 'isolated-no-wire-placeholder', 'enabled': True, 'allow_remote': False})
    with override(steer='@2', consolidate='@3'):
        first = client.post('/api/v2/workbench/turns', json={'project_id': project, 'intent': 'ask',
            'text': 'unmatched-' + uuid4().hex}, headers={'Idempotency-Key': 'observer-first'})
        assert first.status_code == 200
        original = first.json()['turn']
        assert original['receipt']['ask']['no_match'] is True
        with records.begin() as tx:
            learning.record_message(tx, public_id=original['id'], project_id=project, thread_id=original['thread_id'],
                text=original['user_text'], accepted_at=learning.order.read(tx, original['id']).payload['accepted_at'],
                native_request=app.state.ai_turn_store.get_request(original['id']))
            tx.commit()
        assert learning.observe_public(original['id'])['state'] == 'completed'
        following_response = client.post('/api/v2/workbench/turns', json={'project_id': project,
            'thread_id': original['thread_id'], 'intent': 'inspiration',
            'text': '#插话学习合成项目 以后请先列校验条件'}, headers={'Idempotency-Key': 'observer-next'})
        assert following_response.status_code == 200
        following = following_response.json()['turn']
        with records.begin() as tx:
            learning.record_message(tx, public_id=following['id'], project_id=project, thread_id=following['thread_id'],
                text=following['user_text'], accepted_at=learning.order.read(tx, following['id']).payload['accepted_at'])
            tx.commit()
        assert learning.observe_public(following['id'])['state'] == 'completed'
        saved = records.read('v2_turns', original['id'])
        with records.begin() as tx:
            tx.put('v2_turns', saved.object_id, {**saved.payload, 'user_text': saved.payload['user_text'] + 'changed'},
                expected_revision=saved.revision)
            tx.commit()
        refused = learning.observe_public(following['id'])
        assert refused == {'state': 'pending', 'staged': 0, 'error': 'public_native_answer_changed'}
        error = records.read(ERRORS, following['id'])
        assert error.payload['state'] == 'pending' and error.payload['owner'] == learning.freshness.owner
        third_response = client.post('/api/v2/workbench/turns', json={'project_id': project,
            'intent': 'inspiration', 'text': '#插话学习合成项目 当前补充信息'}, headers={'Idempotency-Key': 'observer-other'})
        assert third_response.status_code == 200
        third = third_response.json()['turn']
        with records.begin() as tx:
            learning.record_message(tx, public_id=third['id'], project_id=project, thread_id=third['thread_id'],
                text=third['user_text'], accepted_at=learning.order.read(tx, third['id']).payload['accepted_at'])
            tx.commit()
        learning.refresh()
        assert records.read(TERMINALS, third['id']) is not None
        assert records.read(ERRORS, following['id']).payload['state'] == 'pending'
        current = records.read('v2_turns', original['id'])
        with records.begin() as tx:
            tx.put('v2_turns', current.object_id, saved.payload, expected_revision=current.revision)
            tx.commit()
        assert learning.observe_public(following['id'])['state'] == 'completed'
        assert records.read(ERRORS, following['id']).payload['error'] is None
        assert records.read(ERRORS, following['id']).payload['state'] == 'completed'


def _revision_feedback(installed, project, name, key, text='以后请先列编号'):
    app, client, learning, _ = installed
    records = app.state.recognition_records
    first = client.post('/api/v2/workbench/turns', json={'project_id': project, 'intent': 'ask',
        'text': 'unmatched-' + uuid4().hex}, headers={'Idempotency-Key': key + '-ask'})
    assert first.status_code == 200
    original = first.json()['turn']
    assert original['receipt']['ask']['no_match'] is True
    following_response = client.post('/api/v2/workbench/turns', json={'project_id': project,
        'thread_id': original['thread_id'], 'intent': 'inspiration', 'text': '#' + name + ' ' + text},
        headers={'Idempotency-Key': key + '-next'})
    assert following_response.status_code == 200
    following = following_response.json()['turn']
    with records.begin() as tx:
        for turn in (original, following):
            if tx.read(MESSAGES, turn['id']) is None:
                learning.record_message(tx, public_id=turn['id'], project_id=project, thread_id=turn['thread_id'],
                    text=turn['user_text'], accepted_at=turn['created_at'],
                    native_request=app.state.ai_turn_store.get_request(turn['id']))
        tx.commit()
    assert learning.observe_public(following['id'])['state'] == 'completed'
    feedback = records.read('v2_turn_steer_feedback', 'followup-' + following['id'])
    assert feedback is not None
    return original, following, feedback


def _invalidate_revision_source(records, original):
    current = records.read('v2_turns', original['id'])
    with records.begin() as tx:
        tx.put('v2_turns', current.object_id, {**current.payload, 'user_text': current.payload['user_text'] + '-invalidated'},
            expected_revision=current.revision)
        tx.commit()


def test_revision_counts_isolate_seen_and_invalid_sources_across_real_projects(installed):
    from backend.memory_app.v2.learning_events import checkpoint, complete_in_transaction
    from backend.memory_app.v2.steer_learning import EVENT_ERRORS
    app, client, learning, _ = installed
    records, models = app.state.recognition_records, app.state.recognition_models
    models.update('generation', {'expected_revision': models.public()['generation']['revision'],
        'base_url': 'https://127.0.0.2:9/v1', 'model': 'gpt-4o-mini',
        'api_key': 'isolated-no-wire-placeholder', 'enabled': True, 'allow_remote': False})
    names = ['count-a-' + uuid4().hex[:8], 'count-b-' + uuid4().hex[:8]]
    projects = []
    for name in names:
        created = client.post('/api/v2/projects', json={'name': name})
        assert created.status_code == 200
        projects.append(created.json()['id'])
    with override(steer='@2', consolidate='@3'):
        seen = _revision_feedback(installed, projects[0], names[0], 'seen-' + uuid4().hex)
        assert checkpoint(records, 0, steer_learning=learning)[projects[0]]['score'] == 1
        unseen = _revision_feedback(installed, projects[0], names[0], 'unseen-' + uuid4().hex)
        addition = _revision_feedback(installed, projects[0], names[0], 'addition-' + uuid4().hex, '顺便查一下价格')
        assert addition[2].payload['learnable'] is False
        for item in (seen, unseen, addition):
            _invalidate_revision_source(records, item[0])
        legitimate = _revision_feedback(installed, projects[1], names[1], 'legitimate-' + uuid4().hex)
        state = checkpoint(records, 0, steer_learning=learning)
        assert state[projects[0]]['score'] == 1 and state[projects[1]]['score'] == 1
        assert records.read(EVENT_ERRORS, unseen[2].object_id).payload['state'] == 'pending'
        assert records.read(EVENT_ERRORS, seen[2].object_id) is None
        assert records.read(EVENT_ERRORS, addition[2].object_id) is None
        before_errors = records.list(EVENT_ERRORS)
        with learning.freshness._read_scope() as view:
            assert learning.consumer_events(projects[0], reader=view, local_only=True) == []
            assert [event['event_id'] for event in learning.consumer_events(projects[1], reader=view, local_only=True)] == [legitimate[2].object_id]
        assert records.list(EVENT_ERRORS) == before_errors
        with records.begin() as tx:
            complete_in_transaction(tx, projects[1], steer_learning=learning)
            tx.commit()
        assert records.read('v2_learning_accumulation', projects[1]).payload['score'] == 0
        assert checkpoint(records, 0, steer_learning=learning)[projects[1]]['score'] == 0
        learning.observe_events(projects[0], local_only=True)
        assert records.read(EVENT_ERRORS, seen[2].object_id).payload['state'] == 'pending'
        assert records.read(EVENT_ERRORS, addition[2].object_id) is None


def test_revision_learning_birth_markers_require_proofs_for_candidates_and_publications(installed):
    from backend.memory_app.v2.steer_learning_sources import learning_candidate_identity, learning_recognition_identity
    from backend.memory_app.source_egress import SourceEgressService
    from core.storage_provider import SQLiteStructuredRecordStore
    app, _, learning, project = installed
    records, service = app.state.recognition_records, app.state.recognition_service
    scope = WorkScope('local-user', project)
    original = service.stage_experience(scope=scope, content='人工录入的身份拒绝检查')
    candidate = service.propose(scope=scope, content='人工确认的普通认识', source_experience_ids=[original])
    recognition = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer='local-user')
    with records.begin() as tx:
        current = tx.read('recognition_candidates', candidate.id)
        published = tx.read('recognitions', recognition.id)
        assert learning.sources.is_learning_candidate(tx, scope, current) is False
        assert learning.sources.is_learning_recognition(tx, scope, published) is False
        assert learning_candidate_identity(records, tx, scope, current) is False
        assert learning_recognition_identity(records, tx, scope, published) is False
        references = [{'type': 'recognition', 'id': recognition.id, 'revision': recognition.revision}]
        baseline = SourceEgressService(records, reader=tx).snapshot(scope, references)
        assert next(node for node in baseline['nodes'] if node['id'] == recognition.id
            and node['type'] == 'recognition')['effective_purposes'] == ['embedding', 'generation', 'rerank']
        # 损坏输入中的学习身份没有对应执行证明，不能退回普通无父来源。
        changed = tx.put('recognition_candidates', current.object_id, {**current.payload,
            'generation': {'id': str(uuid4()), 'step_version': 'candidate-from-steer-v1'}}, expected_revision=current.revision)
        assert learning.sources.is_learning_candidate(tx, scope, changed)
        assert learning.sources.is_learning_recognition(tx, scope, published)
        assert learning_candidate_identity(records, tx, scope, changed)
        assert learning_recognition_identity(records, tx, scope, published)
        with pytest.raises(RecognitionConflict, match='candidate_proof_unavailable'):
            learning.sources.validate_candidate_origin(tx, scope, changed)
        with pytest.raises(RecognitionConflict, match='publication_proof_unavailable'):
            learning.sources.recognition_authority(records, tx, scope, published)
        with pytest.raises(RecognitionConflict, match='candidate_proof_unavailable'):
            learning.sources.stage_published_origin(tx, scope, changed, published)
        with pytest.raises(RecognitionConflict, match='publication_proof_unavailable'):
            SourceEgressService(records, reader=tx).snapshot(scope, references)
        detached_owner = SQLiteStructuredRecordStore(records.database_path)
        with pytest.raises(RecognitionConflict, match='recognition_actual_owner_unavailable'):
            SourceEgressService(detached_owner, reader=tx).snapshot(scope, references)


def test_revision_proposal_requires_actual_settlement_and_receipt_binding(installed):
    import time
    from backend.memory_app.kernel.memory_turn import proposal_intent
    from backend.memory_app.v2.memory_turn import MemoryTurn
    from core.effect_log import EffectHandlerDeferred, EffectState
    app, _, learning, project = installed
    models, records = app.state.recognition_models, app.state.recognition_records
    models.update('generation', {'expected_revision': models.public()['generation']['revision'],
        'base_url': 'http://127.0.0.1:9/v1', 'model': 'gpt-4o-mini',
        'api_key': 'isolated-no-wire-placeholder', 'enabled': True, 'allow_remote': False})
    with override(consolidate='@3'):
        turn = MemoryTurn(records, models, kind='memory.consolidate', project=project,
            key=['proposal-contract', uuid4().hex], materials=[], validate=learning.freshness._validate)
    runtime, native = app.state.ai_runtime, app.state.ai_turn_store
    runtime.accept_turn(turn.request)
    scope = WorkScope('local-user', project)
    experience = app.state.recognition_service.stage_experience(scope=scope, content='真实领域提案效果检查')
    candidate_id = 'manual-proposal-' + uuid4().hex
    intent = proposal_intent(turn.request, 'steer-learning')
    runner = native.effect_runner
    def committed_then_deferred(effect):
        assert effect.operation_id == intent.operation_id
        app.state.recognition_service.propose(scope=scope, content='人工提供的效果契约输入',
            source_experience_ids=[experience], candidate_id=candidate_id)
        native.get_or_create_immutable_payload(turn.turn_id, 'memory-proposal-steer-learning',
            {'committed': True, 'key': 'steer-learning'})
        raise EffectHandlerDeferred('crp://session/' + turn.turn_id + '/deferred-domain-proposal')
    def existing():
        row = records.read('recognition_candidates', candidate_id)
        return row.object_id if row is not None else None
    try:
        effect = runner.execute(intent, committed_then_deferred, now=int(time.time()))
        assert effect.state == EffectState.PLANNED and existing() == candidate_id
        with learning.freshness._read_scope() as view:
            with pytest.raises(RecognitionConflict, match='proposal_unsettled'):
                learning.sources.validate_proposal_effect(view, turn.request)
        assert turn.propose(key='steer-learning', write=existing, existing=existing) == candidate_id
        assert runner.log.get(intent.operation_id).state == EffectState.SETTLED_OK
        with learning.freshness._read_scope() as view:
            assert learning.sources.validate_proposal_effect(view, turn.request) == intent.operation_id
        with sqlite3.connect(native._path) as connection:
            connection.execute('DELETE FROM effect_receipt WHERE operation_id=?', (intent.operation_id,))
            connection.commit()
        with learning.freshness._read_scope() as view:
            with pytest.raises(RecognitionConflict, match='proposal_unsettled'):
                learning.sources.validate_proposal_effect(view, turn.request)
        assert not any(event['type'].startswith('model.') for event in native.events_after(turn.turn_id))
    finally:
        runtime.apply_action({'schema_version': '1.0.0', 'action_id': 'cancel-' + uuid4().hex,
            'turn_id': turn.turn_id, 'type': 'cancel', 'target_event_id': None, 'actor': 'user',
            'reason': '结束领域效果契约检查', 'expected_sequence': len(tuple(native.events_after(turn.turn_id))),
            'idempotency_key': 'cancel-' + uuid4().hex, 'created_at': datetime.now(timezone.utc).isoformat()})


def test_revision_identity_without_learning_service_or_native_database(tmp_path):
    from backend.recognition import RecognitionService
    from backend.memory_app.v2.steer_learning_sources import learning_candidate_identity, learning_recognition_identity
    from core.storage_provider import SQLiteStructuredRecordStore
    records = SQLiteStructuredRecordStore(tmp_path / 'recognition.sqlite3')
    service, scope = RecognitionService(records), WorkScope('local-user', 'identity-check')
    original = service.stage_experience(scope=scope, content='独立身份检查原件')
    candidate = service.propose(scope=scope, content='普通人工提案', source_experience_ids=[original])
    reserved = service.propose(scope=scope, content='保留身份的拒绝检查', source_experience_ids=[original],
        candidate_id='steer-memory-' + uuid4().hex)
    recognition = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer='local-user')
    assert not (tmp_path / '.rebuild-data/ai-turns.sqlite3').exists()
    assert not (tmp_path / 'ai-turns.sqlite3').exists()
    with records.begin() as tx:
        published = tx.read('recognitions', recognition.id)
        current = tx.read('recognition_candidates', candidate.id)
        assert learning_candidate_identity(records, tx, scope, current) is False
        assert learning_recognition_identity(records, tx, scope, published) is False
        assert learning_candidate_identity(records, tx, scope, tx.read('recognition_candidates', reserved.id))
        # 领域出生标记仅要求更多证明，不授予来源资格。
        changed = tx.put('recognition_candidates', current.object_id, {**current.payload,
            'generation': {'id': str(uuid4()), 'step_version': 'candidate-from-steer-v1'}}, expected_revision=current.revision)
        assert learning_candidate_identity(records, tx, scope, changed)
        assert learning_recognition_identity(records, tx, scope, published)
    assert not (tmp_path / '.rebuild-data/ai-turns.sqlite3').exists()
    assert not (tmp_path / 'ai-turns.sqlite3').exists()


def test_revision_shared_identity_preserves_actual_owner_and_membership(tmp_path):
    from backend.recognition import RecognitionService
    from backend.memory_app.transaction_records import TransactionRecords
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.material_origins import SharedProjectLease
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.shared_projects import SharedProjects, SharedProjectError
    from backend.memory_app.v2.shared_runtime import SharedDataRoots, SharedWriteRecords
    from backend.memory_app.v2.steer_learning_sources import learning_candidate_identity, learning_recognition_identity
    from core.storage_provider import SQLiteStructuredRecordStore
    root = tmp_path / 'shared-identity'
    root.mkdir()
    devices = DeviceRegistry(root / 'server')
    pair = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='身份检查管理员')
    admin = devices.authenticate(pair['key'])
    users = ServerUsers(root, records=devices.records)
    user = users.create(admin, name='身份检查成员')
    pair = devices.exchange(devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)['code'], name='身份检查设备')
    member = devices.authenticate(pair['key'])
    registry = SharedProjects(users, SharedDataRoots(root))
    project = registry.create(admin, project_name='真实共享身份检查', operation_id='create-' + uuid4().hex)['project']
    registry.invite(admin, project['id'], user_id=member.user_id, role='member', authority=project['authority'],
        operation_id='invite-' + uuid4().hex)
    invitation = registry.invitations(member)[0]
    registry.accept(member, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-' + uuid4().hex)
    member_project = registry.detail(member, project['id'])['project']
    writer = SharedWriteRecords(registry, member, project['id'], authority=member_project['authority'],
        action='identity-check', object_kind='recognition', object_id='identity-check', object_title='身份检查')
    scope = WorkScope('local-user', project['id'])
    with writer.begin() as tx:
        source = RecognitionService(TransactionRecords(tx))
        original = source.stage_experience(scope=scope, content='共享身份检查人工原件')
        candidate = source.propose(scope=scope, content='共享普通认识', source_experience_ids=[original])
        recognition = source.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision,
            reviewer=member.user_id)
        tx.commit()
    member_lease = SharedProjectLease.acquire(registry, member, project['id'])
    admin_lease = SharedProjectLease.acquire(registry, admin, project['id'])
    unrelated = SQLiteStructuredRecordStore(root / 'unrelated.sqlite3')
    with member_lease.current(registry, member) as owner, admin_lease.current(registry, admin) as wrong_owner:
        with writer.begin() as tx:
            current = tx.read('recognition_candidates', candidate.id)
            published = tx.read('recognitions', recognition.id)
            for actual in (writer, owner):
                assert learning_candidate_identity(actual, tx, scope, current) is False
                assert learning_recognition_identity(actual, tx, scope, published) is False
            with pytest.raises(RecognitionConflict, match='source reader owner changed'):
                learning_candidate_identity(wrong_owner, tx, scope, current)
            with unrelated.begin() as foreign:
                with pytest.raises(SharedProjectError, match='shared_read_capability_expired'):
                    learning_recognition_identity(owner, foreign, scope, published)
            # 已存在的学习出生标记只触发严格证明要求，共享来源不借用操作人原生库。
            changed = tx.put('recognition_candidates', current.object_id, {**current.payload,
                'generation': {'id': str(uuid4()), 'step_version': 'candidate-from-steer-v1'}}, expected_revision=current.revision)
            for actual in (writer, owner):
                assert learning_candidate_identity(actual, tx, scope, changed)
                assert learning_recognition_identity(actual, tx, scope, published)
    shared_root = registry.roots.path(project['id'])
    assert not (shared_root / '.rebuild-data/ai-turns.sqlite3').exists()
    assert not (shared_root / 'ai-turns.sqlite3').exists()
    detail = registry.detail(admin, project['id'])
    membership = next(row for row in detail['members'] if row['user_id'] == member.user_id)
    registry.change_member(admin, project['id'], user_id=member.user_id, action='remove',
        expected_revision=membership['revision'], authority=detail['project']['authority'], operation_id='remove-' + uuid4().hex)
    with pytest.raises(SharedProjectError, match='shared_forbidden'):
        with writer.begin():
            raise AssertionError('removed_member_was_accepted')
    with pytest.raises(SharedProjectError, match='shared_forbidden'):
        member_lease.validate(registry, member)


def _manual_restructured_recognition(installed, operation):
    from backend.recognition.restructuring import RestructureProposalService
    app, _, _, project = installed
    source = app.state.recognition_service
    scope = WorkScope('local-user', project)
    original = source.stage_experience(scope=scope, content='重组来源检查原件-' + uuid4().hex)
    candidate = source.propose(scope=scope, content='重组前的人工认识', source_experience_ids=[original])
    parent = source.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer='local-user')
    if operation == 'split':
        child = source.split(scope=scope, recognition_id=parent.id, expected_revision=parent.revision,
            parts=['拆分后的第一项', '拆分后的第二项'])[0]
    elif operation == 'merge':
        other_candidate = source.propose(scope=scope, content='合并中的普通认识', source_experience_ids=[original])
        other = source.publish(scope=scope, candidate_id=other_candidate.id,
            expected_revision=other_candidate.revision, reviewer='local-user')
        child = source.merge(scope=scope, recognition_ids=[parent.id, other.id],
            expected_revisions={parent.id: parent.revision, other.id: other.revision}, content='合并后的人工认识')
    else:
        authority = RestructureProposalService(source)
        snapshot = authority.capture(scope=scope, recognition_ids=[parent.id], expected_revisions={parent.id: parent.revision})
        child_id = 'recognition-' + uuid4().hex
        proposal = authority.save(scope=scope, proposal_id='proposal-' + uuid4().hex, snapshot=snapshot,
            operation='supersede', outputs=[{'recognition_id': child_id, 'content': '取代后的人工认识',
                'conditions': [], 'source_experience_ids': [original], 'source_recognition_ids': []}], reason='人工取代检查')
        authority.review(scope=scope, proposal_id=proposal['id'], expected_revision=proposal['revision'],
            decision='approve', reviewer='local-user')
        child = source.get_recognition(scope=scope, recognition_id=child_id)
        child = source.revise(scope=scope, recognition_id=child.id, expected_revision=child.revision,
            content='取代后再次人工修订的认识')
    return scope, candidate, parent, child


@pytest.mark.parametrize('operation', ['split', 'merge', 'supersede'])
def test_revision_restructured_descendants_preserve_learning_identity(installed, operation):
    from backend.memory_app.source_egress import _MAX_NODES
    from backend.memory_app.v2.steer_learning_lineage import recognition_family, validate_family
    from backend.memory_app.v2.steer_learning_sources import learning_recognition_identity
    app, _, learning, _ = installed
    records = app.state.recognition_records
    scope, candidate, parent, child = _manual_restructured_recognition(installed, operation)
    with records.begin() as tx:
        current = tx.read('recognitions', child.id)
        if operation == 'supersede':
            assert current.revision == 2 and current.payload['content'] == '取代后再次人工修订的认识'
        assert learning_recognition_identity(records, tx, scope, current) is False
        assert tx.read('recognitions', parent.id).payload['state'] == 'superseded'
        family = recognition_family(tx, scope, current, max_nodes=_MAX_NODES)
        nodes = validate_family(tx, scope, current, family)
        by_id = {node['id']: node for node in nodes}
        assert set(by_id) == set(family)
        assert by_id[child.id]['birth'] != by_id[parent.id]['birth']
        assert by_id[child.id]['birth']['object_id'] == child.id
        assert by_id[child.id]['transitions'][0]['parent_id'] in current.payload['parent_ids']
        assert all(family[item].payload['state'] == 'superseded' for item in family if item != child.id)
        marker = tx.read('recognition_candidates', candidate.id)
        # 标记损坏输入要求补全真实证明，不把人工认识描述为模型生成。
        tx.put('recognition_candidates', marker.object_id, {**marker.payload,
            'generation': {'id': str(uuid4()), 'step_version': 'candidate-from-steer-v1'}}, expected_revision=marker.revision)
        assert learning_recognition_identity(records, tx, scope, current)
        with pytest.raises(RecognitionConflict, match='publication_proof_unavailable'):
            learning.sources.recognition_authority(records, tx, scope, current)


@pytest.mark.parametrize('change,code', [
    ('missing_version', 'lineage_version_unavailable'), ('missing_parent', 'lineage_scope_unavailable'),
    ('cycle', 'lineage_cycle'), ('cross_scope', 'lineage_scope_unavailable'),
    ('missing_birth', 'outcome_source_birth_unavailable'), ('missing_transition', 'lineage_version_unavailable'),
    ('changed_parents', 'lineage_birth_changed'), ('reborn_child', 'lineage_birth_unavailable'),
])
def test_revision_learning_descendant_rejects_incomplete_domain_lineage(installed, change, code):
    from core.storage_provider.record_lineage import HEAD_COLLECTIONS
    app, _, learning, _ = installed
    records = app.state.recognition_records
    scope, candidate, parent, child = _manual_restructured_recognition(installed, 'split')
    with records.begin() as tx:
        marker = tx.read('recognition_candidates', candidate.id)
        tx.put('recognition_candidates', marker.object_id, {**marker.payload,
            'generation': {'id': str(uuid4()), 'step_version': 'candidate-from-steer-v1'}}, expected_revision=marker.revision)
        current = tx.read('recognitions', child.id)
        retained = tx.read('recognitions', parent.id)
        if change in {'missing_version', 'missing_transition'}:
            identity = child.id + '~v1' if change == 'missing_version' else parent.id + '~v' + str(retained.payload['version'])
            version = tx.read('recognition_versions', identity)
            tx.delete('recognition_versions', identity, expected_revision=version.revision)
        elif change == 'missing_parent':
            tx.delete('recognitions', parent.id, expected_revision=retained.revision)
        elif change == 'missing_birth':
            head = tx.read(HEAD_COLLECTIONS['recognitions'], child.id)
            tx.delete(HEAD_COLLECTIONS['recognitions'], child.id, expected_revision=head.revision)
        elif change == 'cross_scope':
            tx.put('recognitions', parent.id, {**retained.payload, 'project_id': 'other-project',
                'scope': {'user_id': scope.user_id, 'project_id': 'other-project'}}, expected_revision=retained.revision)
        elif change == 'reborn_child':
            tx.delete('recognitions', current.object_id, expected_revision=current.revision)
            current = tx.put('recognitions', current.object_id, dict(current.payload), expected_revision=0)
        else:
            current = tx.put('recognitions', child.id, {**current.payload,
                'parent_ids': [child.id] if change == 'cycle' else []}, expected_revision=current.revision)
        with pytest.raises(RecognitionConflict, match=code):
            learning.sources.recognition_authority(records, tx, scope, current)


def test_revision_learning_identity_keeps_multiple_restructure_generations_and_ordinary_roots(installed):
    from backend.memory_app.source_egress import _MAX_NODES
    from backend.memory_app.v2.steer_learning_lineage import recognition_family, validate_family
    from backend.memory_app.v2.steer_learning_sources import learning_recognition_identity
    app, _, learning, _ = installed
    records, source = app.state.recognition_records, app.state.recognition_service
    scope, candidate, parent, first = _manual_restructured_recognition(installed, 'split')
    roots = records.read('recognitions', first.id).payload['source_experience_ids']
    ordinary = source.propose(scope=scope, content='后续合并中的普通认识', source_experience_ids=roots)
    ordinary = source.publish(scope=scope, candidate_id=ordinary.id, expected_revision=ordinary.revision, reviewer='local-user')
    child = source.merge(scope=scope, recognition_ids=[first.id, ordinary.id],
        expected_revisions={first.id: first.revision, ordinary.id: ordinary.revision}, content='两次重组后的人工认识')
    with records.begin() as tx:
        current = tx.read('recognitions', child.id)
        family = recognition_family(tx, scope, current, max_nodes=_MAX_NODES)
        nodes = validate_family(tx, scope, current, family)
        assert set(family) == {parent.id, first.id, ordinary.id, child.id}
        by_id = {node['id']: node for node in nodes}
        assert by_id[first.id]['transitions'][0]['parent_id'] == parent.id
        assert {item['parent_id'] for item in by_id[child.id]['transitions']} == {first.id, ordinary.id}
        assert learning_recognition_identity(records, tx, scope, current) is False
        marker = tx.read('recognition_candidates', candidate.id)
        tx.put('recognition_candidates', marker.object_id, {**marker.payload,
            'generation': {'id': str(uuid4()), 'step_version': 'candidate-from-steer-v1'}}, expected_revision=marker.revision)
        assert learning_recognition_identity(records, tx, scope, current)
        with pytest.raises(RecognitionConflict, match='publication_proof_unavailable'):
            learning.sources.recognition_authority(records, tx, scope, current)


def test_revision_published_learning_proof_rejects_json_and_unissued_objects():
    from backend.memory_app.v2.steer_learning_publication import PublishedLearningProof, require_published_learning_context
    with pytest.raises(RecognitionConflict, match='proof_not_issued'):
        PublishedLearningProof()
    for value in ({'publication_ref': 'unissued'}, object(), object.__new__(PublishedLearningProof)):
        with pytest.raises(RecognitionConflict, match='proof_not_issued'):
            require_published_learning_context(value, freshness=None, reader=None, context={})


def test_revision_retained_recognition_reads_actual_birth_and_historical_versions_only(installed):
    from backend.memory_app.source_egress import SourceEgressService
    from backend.memory_app.v2.outcome_freshness import _lineage
    from backend.memory_app.v2.steer_learning_lineage import retained_recognition_record
    from core.storage_provider import SQLiteStructuredRecordStore
    app, _, _, project = installed
    records, service = app.state.recognition_records, app.state.recognition_service
    scope = WorkScope('local-user', project)
    original = service.stage_experience(scope=scope, content='真实保留版本原件')
    candidate = service.propose(scope=scope, content='首版人工认识', source_experience_ids=[original])
    recognition = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer='local-user')
    with records.begin() as tx:
        birth = _lineage(tx, 'recognitions', recognition.id)
    changed = service.revise(scope=scope, recognition_id=recognition.id, expected_revision=recognition.revision, content='人工修订的第二版')
    service.split(scope=scope, recognition_id=changed.id, expected_revision=changed.revision, parts=['第一项', '第二项'])
    with records.begin() as tx:
        for revision in (1, 2, 3):
            actual, current_birth = retained_recognition_record(records, tx, scope, recognition.id,
                expected_birth=birth, expected_revision=revision)
            assert actual.revision == 3 and actual.payload['state'] == 'superseded'
            assert actual.payload['content'] == '人工修订的第二版' and current_birth == birth
        assert tx.read('recognition_versions', recognition.id + '~v1').payload['snapshot']['content'] == '首版人工认识'
        assert service._qualified_recognition(tx, actual).authorized is False
        with pytest.raises(RecognitionConflict):
            SourceEgressService(records, reader=tx).snapshot(scope, [{'type': 'recognition', 'id': actual.object_id, 'revision': actual.revision}])
        with pytest.raises(RecognitionConflict, match='retained_birth_changed'):
            retained_recognition_record(records, tx, scope, recognition.id, expected_birth={**birth, 'fact_id': uuid4().hex})
        with pytest.raises(RecognitionConflict, match='retained_revision_unavailable'):
            retained_recognition_record(records, tx, scope, recognition.id, expected_revision=4)
        with pytest.raises(RecognitionConflict, match='lineage_row_changed'):
            retained_recognition_record(records, tx, WorkScope('local-user', 'another-project'), recognition.id)
        unrelated = SQLiteStructuredRecordStore(app.state.recognition_runtime_root / 'unrelated-retained.sqlite3')
        with pytest.raises(RecognitionConflict, match='source reader owner changed'):
            retained_recognition_record(unrelated, tx, scope, recognition.id)
        version = tx.read('recognition_versions', recognition.id + '~v2')
        tx.delete('recognition_versions', version.object_id, expected_revision=version.revision)
        with pytest.raises(RecognitionConflict, match='retained_revision_unavailable'):
            retained_recognition_record(records, tx, scope, recognition.id, expected_revision=2)


def test_published_privacy_reader_keeps_real_retained_sources_out_of_normal_materials(installed):
    from backend.memory_app.source_egress import SourceEgressService
    from backend.memory_app.v2.outcome_freshness import _lineage
    from backend.memory_app.v2.steer_learning_privacy import _PublishedPrivacySources, _permit
    from core.storage_provider import SQLiteStructuredRecordStore
    app, _, _, project = installed
    records, service = app.state.recognition_records, app.state.recognition_service
    scope = WorkScope('local-user', project)
    experience = service.stage_experience(scope=scope, content='隐私遍历中的人工原件')
    candidate = service.propose(scope=scope, content='后续新增的私密父源', source_experience_ids=[experience])
    parent = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer='local-user')
    candidate = service.propose(scope=scope, content='保留的人工认识', source_experience_ids=[experience])
    retained = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer='local-user')
    authority = SourceEgressService(records)
    authority.set_policy(scope, 'recognition', parent.id, parent.revision, 0, [])
    retained = service.revise(scope=scope, recognition_id=retained.id, expected_revision=retained.revision,
        content='含有新增父源的人工认识', source_recognition_ids=[parent.id])
    service.split(scope=scope, recognition_id=retained.id, expected_revision=retained.revision, parts=['人工分项甲', '人工分项乙'])
    with records.begin() as tx:
        row = tx.read('recognitions', retained.id)
        references = [{'type': 'recognition', 'id': row.object_id, 'revision': row.revision}]
        with pytest.raises(RecognitionConflict):
            SourceEgressService(records, reader=tx).snapshot(scope, references)
        permits = {}
        birth = _lineage(tx, 'recognitions', row.object_id)
        _permit(permits, scope, row.object_id, 1, birth)
        private = _PublishedPrivacySources(records, tx, permits)
        snapshot = private.snapshot(scope, references)
        nodes = {(node['type'], node['id']): node for node in snapshot['nodes']}
        assert nodes[('recognition', row.object_id)]['effective_purposes'] == []
        assert nodes[('recognition', parent.id)]['effective_purposes'] == []
        assert tx.read('recognitions', row.object_id) == row and row.payload['state'] == 'superseded'
        assert private._walk_key('snapshot', scope, references) == (None, None)
        with pytest.raises(RecognitionConflict):
            SourceEgressService(records, reader=tx).snapshot(scope, references)
        with pytest.raises(RecognitionConflict):
            SourceEgressService(records).snapshot(scope, references)
        unrelated = SQLiteStructuredRecordStore(app.state.recognition_runtime_root / 'wrong-privacy-owner.sqlite3')
        with pytest.raises(RecognitionConflict, match='source reader owner changed'):
            _PublishedPrivacySources(unrelated, tx, permits)
        permits[(scope.user_id, scope.project_id, row.object_id)]['birth'] = {**birth, 'fact_id': uuid4().hex}
        with pytest.raises(RecognitionConflict, match='retained_birth_changed'):
            _PublishedPrivacySources(records, tx, permits).snapshot(scope, references)


def test_publication_qualifier_rejects_unissued_context_before_reading_materials(installed):
    app, _, learning, _ = installed
    with app.state.recognition_records.begin() as tx:
        for proof in (None, {}, object()):
            with pytest.raises(RecognitionConflict, match='publication_proof_not_issued'):
                learning.freshness.qualify_published_learning_context({}, reader=tx,
                    publication_proof=proof, local_only=True)
