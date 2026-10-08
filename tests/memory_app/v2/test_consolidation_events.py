"""New policies consume real corrections through the existing memory Turn."""
import json
from datetime import datetime, timezone, timedelta

import pytest

from backend.memory_app.v2.consolidation import Consolidation
from backend.memory_app.v2.policies import override, get
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import RecognitionError, WorkScope
from tests.memory_app.v2.test_consolidation import PatternModel
from tests.memory_app.v2.test_workbench_ask import env as _env, add_document, publish
from tests.memory_app.v2.kernel_receipts import requests

env = _env


class CorrectionModel(PatternModel):
    def __init__(self, *, event_ids=None, kind='correction'):
        super().__init__()
        self.event_ids, self.kind = event_ids, kind

    def complete(self, messages, *, max_tokens, validate_current, wire_attempt_sink=None):
        self.before()
        validate_current()
        attempt = wire_attempt_sink.begin_model_wire_attempt() if wire_attempt_sink else None
        def wire():
            self.calls += 1
            self.messages = messages
            self.history = [*getattr(self, 'history', []), messages]
            value = {'text': '送礼前逐项核对售后政策', 'conditions': ['挑礼物时']}
            if 'event_id' in messages[-1]['content']:
                import re
                value.update(event_ids=self.event_ids if self.event_ids is not None else
                    re.findall(r'"event_id":\s*"([^"]+)"', messages[-1]['content']), kind=self.kind)
            if any('最多300字' in message['content'] for message in messages):
                value = {'text': '合成概览'}
            if attempt:
                attempt.succeeded(usage={'prompt_tokens': 8, 'completion_tokens': 4, 'total_tokens': 12}, cache_observation=None)
            return json.dumps(value, ensure_ascii=False)
        result = attempt.invoke_wire(wire) if attempt else wire()
        self.after()
        validate_current()
        return result, {'model': 'fake', 'configuration_revision': 1}


def corrected(env):
    document = add_document(env, summary='礼物售后', body='保留换货机会', original='合成礼物资料')[0]
    recognition, _ = publish(env, '先比较实用程度', doc=document)
    revised = env.service.revise(scope=WorkScope('local-user', 'alpha'), recognition_id=recognition.id,
        expected_revision=recognition.revision, content='下次先核实售后期限')
    return document, revised, env.records.list('v2_correction_events')[0]


def job(env, model):
    return Consolidation(env.records, env.service, env.documents, model,
        now=lambda: datetime.now(timezone.utc) + timedelta(days=1))


def test_one_correction_is_frozen_with_source_and_proposed_without_pattern(env):
    document, recognition, event = corrected(env)
    model = CorrectionModel()
    conso = job(env, model)
    with override(trigger='@2', consolidate='@2'):
        outcome = conso.run('alpha')
    assert outcome['new_suggestions'] == 1
    request = next(row for row in requests(env.records) if row['desired_outcome'] == 'memory.consolidate')
    assert event.object_id in request['input']['text']
    assert '先比较实用程度' in request['input']['text'] and '下次先核实售后期限' in request['input']['text']
    assert any(messages[-1]['content'] == request['input']['text'] for messages in model.history)
    saved = env.records.list('v2_consolidation_inputs')[0]
    assert saved.payload['event_ids'] == [event.object_id]
    candidates = [row for row in env.records.list('recognition_candidates') if row.payload['state'] == 'pending']
    assert len(candidates) == 1 and candidates[0].payload['source_experience_ids']
    assert env.records.read('v2_insight_patterns', candidates[0].object_id).payload['kind'] == 'correction'
    assert len(env.records.list('recognitions')) == 1
    old_calls = model.calls
    with override(trigger='@2', consolidate='@2'):
        assert conso.run('alpha')['replayed'] is True
    assert model.calls == old_calls
    assert next(row for row in requests(env.records) if row['desired_outcome'] == 'memory.consolidate') == request


@pytest.mark.parametrize('change', ['private', 'forgotten', 'cross_project', 'missing_owner'])
def test_ineligible_correction_is_not_sent_to_wire(env, change):
    document, recognition, event = corrected(env)
    if change == 'private':
        set_private_project(env.records, 'alpha', True, 0)
    else:
        with env.records.begin() as tx:
            if change == 'forgotten':
                tx.put('v2_document_recall', document, {'state': 'forgotten', 'by': 'user'}, expected_revision=0)
            else:
                tx.put('v2_correction_events', event.object_id,
                    {**event.payload, **({'project_id': 'beta'} if change == 'cross_project' else {'object_id': 'absent-owner'})},
                    expected_revision=event.revision)
            tx.commit()
    model = CorrectionModel()
    with override(trigger='@2', consolidate='@2'):
        job(env, model).run('alpha')
    assert not any(row['desired_outcome'] == 'memory.consolidate' for row in requests(env.records))


def test_correction_policy_rejects_single_turn_and_single_source_pattern():
    policy = get('consolidate', version='@2')
    transient = {'event_id': 'e1', 'type': 'correction', 'before': '甲', 'after': '改成乙', 'turn_id': 'one'}
    assert policy(None, operation='events', events=[transient]) == []
    permanent = {**transient, 'after': '以后改成乙'}
    assert policy(None, operation='events', events=[permanent]) == [permanent]
    assert policy(None, operation='accept', event_ids=['e1'], expected_ids=['e1'], kind='pattern', source_count=1) is False
    assert policy(None, operation='accept', event_ids=['e1'], expected_ids=['e1'], kind='correction', source_count=1) is True
    assert policy(None, operation='accept', event_ids=['wrong'], expected_ids=['e1'], kind='correction', source_count=2) is False


def test_decide_uses_only_cited_events_and_distinguishes_repeated_turns():
    policy = get('consolidate', version='@2')
    events = [{'event_id': f'e{i}', 'type': 'correction', 'before': '甲', 'after': '改成乙', 'turn_id': f't{i}'} for i in range(2)]
    assert policy(None, operation='events', events=events) == events
    assert policy(None, operation='accept', events=events, event_ids=['e0'], expected_ids=['e0', 'e1'], kind='correction', source_count=2) is False
    assert policy(None, operation='accept', events=events, event_ids=['e0', 'e1'], expected_ids=['e0', 'e1'], kind='pattern', source_count=2) is True
    assert policy(None, operation='accept', events=[{'event_id':'edit', 'type':'edit'}], event_ids=['edit'], expected_ids=['edit'], kind='pattern', source_count=2) is False
    assert policy(None, operation='accept', events=[{'event_id':'edit', 'type':'edit'}, {'event_id':'other', 'type':'edit'}], event_ids=['edit'], expected_ids=['edit', 'other'], kind='correction', source_count=2) is True


@pytest.mark.parametrize('selection,kind,accepted', [((0, 1), 'pattern', False), ((0, 2), 'pattern', True), ((0,), 'correction', True)])
def test_pattern_sources_come_only_from_its_selected_real_correction_events(env, selection, kind, accepted):
    from backend.memory_app.v2.consolidation_events import corrections, original_count
    _, first, event1 = corrected(env)
    env.service.revise(scope=WorkScope('local-user', 'alpha'), recognition_id=first.id,
                       expected_revision=first.revision, content='再次修改同一原件的判断')
    event2 = next(row for row in env.records.list('v2_correction_events') if row.object_id != event1.object_id)
    document2 = add_document(env, summary='另一原件事实', body='独立来源判断', original='合成另一独立原件')[0]
    other, _ = publish(env, '另一独立来源的旧判断', doc=document2)
    env.service.revise(scope=WorkScope('local-user', 'alpha'), recognition_id=other.id,
                       expected_revision=1, content='另一来源的新判断')
    event3 = next(row for row in env.records.list('v2_correction_events') if row.object_id not in {event1.object_id, event2.object_id})
    all_ids = [event1.object_id, event2.object_id, event3.object_id]
    selected = [all_ids[index] for index in selection]
    model = CorrectionModel(event_ids=selected, kind=kind)
    conso = job(env, model)
    with override(trigger='@2', consolidate='@2'):
        qualified = corrections(env.records, 'alpha')
        assert {event['event_id'] for event in qualified} == set(all_ids)
        assert original_count(env.records, qualified[0]['_snapshot']) == 1
        if accepted:
            assert conso._pattern('alpha', [], events=qualified, use_corrections=True) == 1
        else:
            with pytest.raises(RecognitionError, match='consolidation_learning_evidence_invalid'):
                conso._pattern('alpha', [], events=qualified, use_corrections=True)
    frozen = next(request for request in requests(env.records) if request['desired_outcome'] == 'memory.consolidate')
    assert all(identity in frozen['input']['text'] for identity in all_ids)
    candidates = [row for row in env.records.list('recognition_candidates') if row.payload['state'] == 'pending']
    assert len(candidates) == int(accepted)
    assert len(env.records.list('recognitions')) == 2
    if accepted:
        pattern = env.records.read('v2_insight_patterns', candidates[0].object_id)
        assert pattern.payload['event_ids'] == selected and pattern.payload['kind'] == kind


def test_corrected_recognition_private_policy_prevents_correction_wire(env):
    _, recognition, _ = corrected(env)
    SourceEgressService(env.records).set_policy(WorkScope('local-user', 'alpha'), 'recognition', recognition.id,
        recognition.revision, 0, [])
    model = CorrectionModel()
    with override(trigger='@2', consolidate='@2'):
        job(env, model).run('alpha')
    assert not any(row['desired_outcome'] == 'memory.consolidate' for row in requests(env.records))


def test_only_rejected_candidate_project_still_consolidates_its_authorized_correction(env):
    from backend.memory_app.document_recognition import ensure_document_experience
    document = add_document(env, summary='旧资料', body='保留条件', original='合成原件')[0]
    experience = ensure_document_experience(env.documents, env.service, 'alpha', document)[0]
    scope = WorkScope('local-user', 'alpha')
    candidate = env.service.propose(scope=scope, content='应核实的旧判断', source_experience_ids=[experience])
    env.service.reject_candidate(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer='local-user')
    model = CorrectionModel()
    conso = Consolidation(env.records, env.service, env.documents, model,
        now=lambda: datetime.now(timezone.utc) + timedelta(days=8))
    with override(trigger='@2', consolidate='@2'):
        assert conso._collect('alpha') == []
        result = conso.run('alpha')
    assert result['new_suggestions'] == 1
    request = next(row for row in requests(env.records) if row['desired_outcome'] == 'memory.consolidate')
    assert '应核实的旧判断' in request['input']['text']


def test_two_documents_derived_from_the_same_real_original_are_one_pattern_source(env):
    from backend.memory_app.v2.task_drafts import TaskDrafts
    from backend.memory_app.original_sources import source_store
    from core.ai_kernel import SQLiteAITurnStore
    document = add_document(env, summary='缓存按需加载减少重复请求', body='缓存按需加载减少重复请求', original='合成同一原件')[0]
    recognition, _ = publish(env, '缓存按需加载减少重复请求', doc=document)
    material = {'type': 'recognition', 'id': recognition.id, 'revision': recognition.revision, 'project_id': 'alpha'}
    snapshot = SourceEgressService(env.records).snapshot(WorkScope('local-user', 'alpha'),
        [{key: material[key] for key in ('type', 'id', 'revision')}])
    env.documents.now = datetime.now(timezone.utc).isoformat()
    derived = []
    for index in range(2):
        turn = f'turn-derived-{index}'
        output = TaskDrafts(env.records, env.documents).create(turn_id=turn, project='alpha',
            operation='deliver-' + turn, title=f'同源转述{index}', markdown='缓存按需加载减少重复请求')
        request = {'turn_id': turn, 'session_id': 'session-' + turn, 'operation_id': 'op-' + turn,
            'idempotency_key': 'key-' + turn, 'scope': {'kind': 'project', 'project_id': 'alpha', 'series_id': None},
            'desired_outcome': 'project.task', 'privacy': {'material_refs': [material], 'source_snapshots': [snapshot]},
            'input': {'refs': [{'kind': 'atom', 'object_id': recognition.id, 'uri': 'crp://default/recognitions/' + recognition.id}]}}
        with env.records.begin() as tx:
            tx.put('v2_task_executions', 'product-' + turn, {'project_id': 'alpha', 'started': True, 'request': request}, expected_revision=0)
            tx.put('v2_turns', 'product-' + turn, {'project_id': 'alpha', 'intent': 'do', 'receipt': {'do': {
                'state': 'done', 'document_id': output['document_id'], 'kernel_turn_id': turn, 'title': f'同源转述{index}'}}}, expected_revision=0)
            tx.commit()
        SQLiteAITurnStore(source_store(env.records).root / 'ai-turns.sqlite3').claim_turn(request)
        derived.append(output['document_id'])
    model = PatternModel()
    conso = job(env, model)
    with override(trigger='@2', consolidate='@2'):
        group = [row for row in conso._collect('alpha') if row.kind == 'document' and row.object_id in derived]
        assert {row.object_id for row in group} == set(derived)
        assert conso._pattern('alpha', group, use_corrections=True) == 0
    assert model.calls == 0
    with override(consolidate='@1'):
        assert conso._pattern('alpha', group) == 1
    assert model.calls == 1


@pytest.mark.parametrize('change', ['event_cas', 'owner_revision', 'source_revoked', 'forgotten'])
def test_changes_at_real_dispatch_fence_block_the_correction_wire(env, change):
    document, recognition, event = corrected(env)
    model = CorrectionModel()
    def mutate_once():
        model.before = lambda: None
        if change == 'source_revoked':
            ref = event.payload['source_refs'][0]
            SourceEgressService(env.records).set_policy(WorkScope('local-user', 'alpha'), ref['type'], ref['id'], ref['revision'], 0, [])
        elif change == 'owner_revision':
            env.service.revise(scope=WorkScope('local-user', 'alpha'), recognition_id=recognition.id,
                expected_revision=recognition.revision, content='后来再次纠正')
        else:
            with env.records.begin() as tx:
                if change == 'event_cas':
                    tx.put('v2_correction_events', event.object_id, {**event.payload, 'after': 'event changed'}, expected_revision=event.revision)
                else:
                    tx.put('v2_document_recall', document, {'state': 'forgotten', 'by': 'user'}, expected_revision=0)
                tx.commit()
    model.before = mutate_once
    with override(trigger='@2', consolidate='@2'):
        result = job(env, model).run('alpha')
    assert result['failed_groups'] >= 1
    assert not any('event_id' in messages[-1]['content'] for messages in getattr(model, 'history', []))
    assert not env.records.list('v2_consolidation_inputs')
    from backend.memory_app.v2.memory_turn import MemoryTurn
    store = MemoryTurn.store_for(env.records)
    for request in requests(env.records):
        if request['desired_outcome'] == 'memory.consolidate':
            assert not any(event['type'] == 'model.attempt.dispatched' for event in store.events_after(request['turn_id']))


def test_manual_real_project_run_resets_score_and_runtime_and_keeps_other_project(env):
    from backend.memory_app.v2.daily import DailyJobs
    _, _, _ = corrected(env)
    add_document(env, project='beta', summary='别项目资料', body='别项目资料', original='合成别项目原件')
    model = CorrectionModel()
    conso = job(env, model)
    callbacks, clock = [], [0.0]
    with override(trigger='@2', consolidate='@2'):
        daily = DailyJobs(records=env.records, clock=lambda: clock[0])
        before = daily.checkpoint()
        assert before['alpha']['score'] == 3 and before['beta']['score'] == 1
        clock[0] = 60
        daily.checkpoint()
        conso.request('alpha', callbacks.append)
        callbacks[0]()
    states = {row.object_id: row.payload for row in env.records.list('v2_learning_accumulation')}
    assert states['alpha']['score'] == 0 and states['alpha']['run_seconds'] == 0
    assert states['beta']['score'] == 1 and states['beta']['run_seconds'] == 60
    assert not any('合成别项目原件' in message['content'] for messages in model.history for message in messages)
