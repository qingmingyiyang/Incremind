"""B 的真实下游消费者；公开生产者完成记录为合成输入，不证明 Runner 或 Host。"""
from datetime import datetime, timedelta, timezone
import json
import re
from types import SimpleNamespace

from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.source_graph import SourceGraph
from backend.memory_app.v2.consolidation_events import consumer_outcomes
from backend.memory_app.v2.inspirations import freeze_task_turn
from backend.memory_app.v2.learning_events import events
from backend.memory_app.v2.outcome_corrections import _root, _time
from backend.memory_app.v2.task_drafts import TaskDrafts
from backend.recognition import WorkScope
from backend.recognition.product_draft_dependencies import product_draft_source
from core.ai_kernel import validate_turn_request
from tests.memory_app.v2.test_skill_exports import draft
from tests.memory_app.v2.test_skill_exports_full_factory import (
    BASE, original_factory, post, source,
)


PROJECT = 'alpha'
SCOPE = WorkScope('local-user', PROJECT)
BIRTH = '先交付未经核对的结果。\n\n保留适用范围。'
EDITED = '以后交付前核对限制和来源。\n\n保留适用范围。'
METHOD = '交付前核对限制与来源，纠正后保留适用条件'
CONDITIONS = ['准备成果时']


def synthetic_published_output(actual, *, known_birth):
    """原写入者生成草稿，原内核只接收请求；完成 owner 事实明确为合成前提。"""
    documents = actual.app.state.recognition_documents
    parent = source(actual)
    suffix = 'known' if known_birth else 'unknown'
    kernel_id, product_id = 'turn-synthetic-b-' + suffix, 'turn-synthetic-product-' + suffix
    created = datetime.now(timezone.utc).isoformat()
    material = {'type': 'recognition', 'id': parent.id, 'revision': parent.revision, 'project_id': PROJECT}
    # 与 TaskDo.initial 使用同一冻结入口，完整字段由原契约与真实 Source 装配。
    request = freeze_task_turn(records=actual.records, models=actual.models, project_id=PROJECT,
        load_text=lambda item: '', text=json.dumps({'task': '准备一份成果',
            'division_examples': [], 'division_override': None}, ensure_ascii=False), materials=[material],
        turn_id=kernel_id, session_id='session-' + product_id, operation_id='op-' + kernel_id,
        idempotency_key='task-' + product_id, created_at=created)
    assert validate_turn_request(request) == request
    assert request['desired_outcome'] == 'project.task' and request['execution_policy']['purpose'] == 'primary'
    assert request['privacy']['material_refs'] == [material]
    snapshot = SourceEgressService(actual.records).snapshot(SCOPE,
        [{'type': 'recognition', 'id': parent.id, 'revision': parent.revision}])
    assert request['privacy']['source_snapshots'] == [snapshot]
    assert actual.store.claim_turn(request) == (kernel_id, True)
    assert actual.store.get_request(kernel_id) == request
    # 没有执行生产者；不伪造完成事件、模型尝试或外部执行回执。
    assert tuple(actual.store.events_after(kernel_id)) == ()
    output = TaskDrafts(actual.records, documents).create(turn_id=kernel_id, project=PROJECT,
        operation='deliver-' + kernel_id, title='合成生产者成果', markdown=BIRTH)
    with actual.records.begin() as tx:
        if known_birth:
            # 字段来自 TaskDo.initial/advance 与 product_draft_source；值是合成输入。
            tx.put('v2_task_executions', product_id,
                {'project_id': PROJECT, 'started': True, 'request': request}, expected_revision=0)
        # 原可见性要求公开交付。此 done 值也是合成输入，不能签真实生产者完成。
        tx.put('v2_turns', product_id, {'project_id': PROJECT, 'intent': 'do', 'receipt': {'do': {
            'state': 'done', 'document_id': output['document_id'], 'kernel_turn_id': kernel_id,
            'title': '合成生产者成果'}}}, expected_revision=0)
        tx.commit()
    return SimpleNamespace(documents=documents, parent=parent, output=output,
        kernel_id=kernel_id, product_id=product_id, request=request)


def save_edit(actual, output, revision, *, status=200):
    response = actual.client.patch('/api/recognition/documents/' + output['document_id'],
        json={'project_id': PROJECT, 'expected_revision': revision, 'markdown': EDITED})
    assert response.status_code == status, response.text
    return response


def test_original_factory_unknown_draft_edit_preserves_history_without_correction(original_factory):
    actual = original_factory
    setup = synthetic_published_output(actual, known_birth=False)
    identity = setup.output['document_id']
    assert actual.records.read('v2_task_executions', setup.product_id) is None
    assert _root(actual.records, SCOPE, identity) is None
    before = actual.records.read('documents', identity)
    save_edit(actual, setup.output, 2, status=409)
    assert actual.records.read('documents', identity) == before
    saved = save_edit(actual, setup.output, 1).json()
    assert saved['revision'] == 2 and saved['markdown'] == EDITED
    assert setup.documents.markdown(identity, revision=1) == BIRTH
    assert setup.documents.markdown(identity, revision=2) == EDITED
    assert actual.records.read('v2_task_draft_operations', 'deliver-' + setup.kernel_id).payload['inputs']['markdown'] == BIRTH
    assert _root(actual.records, SCOPE, identity) is None
    assert actual.records.list('v2_outcome_corrections') == ()
    assert not any(item.startswith('outcome:') for item in events(actual.records).get(PROJECT, set()))
    assert consumer_outcomes(actual.records, PROJECT) == []
    assert actual.records.list('v2_memory_turn_keys') == () and actual.wire['calls'] == 0
    assert tuple(actual.store.events_after(setup.kernel_id)) == ()
    assert [row['id'] for row in actual.client.get(BASE + '/methods').json()['items']] == [setup.parent.id]


def test_original_factory_published_draft_edit_consumed_once_as_pending_method(original_factory, monkeypatch):
    actual = original_factory
    config = actual.models.public()['generation']
    actual.models.update('generation', {'enabled': True, 'expected_revision': config['revision']})
    config = actual.models.public()['generation']
    assert config['configured'] is True and config['enabled'] is True and config['allow_remote'] is True
    assert config['base_url'] == 'https://example.invalid/v1' and config['model'] == 'synthetic-skill'
    setup = synthetic_published_output(actual, known_birth=True)
    identity = setup.output['document_id']
    bound = product_draft_source(actual.records, SCOPE, identity, 1)
    assert bound.revisions['task_execution_id'] == setup.product_id
    assert bound.revisions['product_turn_id'] == setup.kernel_id and bound.revisions['document_revision'] == 1
    assert bound.roots == ((PROJECT, 'recognition', setup.parent.id, setup.parent.revision),)
    assert bound.request == actual.store.get_request(setup.kernel_id) == setup.request
    publication = actual.records.read('v2_turns', setup.product_id)
    execution = actual.records.read('v2_task_executions', setup.product_id)
    before = actual.records.read('documents', identity)
    save_edit(actual, setup.output, 2, status=409)
    assert actual.records.read('documents', identity) == before
    assert actual.records.list('v2_outcome_corrections') == ()
    save_edit(actual, setup.output, 1)
    fact, = actual.records.list('v2_outcome_corrections')
    assert fact.payload['kind'] == 'outcome_edit' and fact.payload['turn_id'] == setup.product_id
    assert fact.payload['document_id'] == identity and fact.payload['birth_revision'] == 1
    assert fact.payload['from_revision'] == 1 and fact.payload['to_revision'] == 2
    assert fact.payload['before'] == BIRTH.split('\n\n')[0] and fact.payload['after'] == EDITED.split('\n\n')[0]
    assert setup.documents.markdown(identity, revision=1) == BIRTH
    assert setup.documents.markdown(identity, revision=2) == EDITED
    assert actual.records.read('v2_turns', setup.product_id) == publication
    assert actual.records.read('v2_task_executions', setup.product_id) == execution
    last = _time(fact.payload['last_saved_at'])
    assert last is not None and fact.payload['net_change'] is True
    event_id = 'outcome:' + fact.object_id
    boundary, mature = last + timedelta(seconds=600), last + timedelta(seconds=601)
    assert consumer_outcomes(actual.records, PROJECT, now=boundary.isoformat()) == []
    assert event_id not in events(actual.records, now=boundary.isoformat()).get(PROJECT, set())
    qualified, = consumer_outcomes(actual.records, PROJECT, now=mature.isoformat())
    assert qualified['event_id'] == event_id and qualified['_roots'] == [{'id': identity, 'revision': 1}]
    assert qualified['_refs'] == [{'type': 'recognition', 'id': setup.parent.id,
        'revision': setup.parent.revision, 'project_id': PROJECT}]
    assert event_id in events(actual.records, now=mature.isoformat()).get(PROJECT, set())

    def completion(**request):
        actual.wire['calls'] += 1
        actual.wire['messages'] = request['messages']
        assert not request.get('stream')
        text = '\n'.join(message['content'] for message in request['messages'])
        assert '"outcomes"' in text and fact.payload['before'] in text and fact.payload['after'] in text
        assert re.findall(r'"event_id":\s*"([^"]+)"', text) == [event_id]
        output = {'text': METHOD, 'conditions': CONDITIONS, 'event_ids': [event_id], 'kind': 'correction'}
        return {'choices': [{'message': {'content': json.dumps(output, ensure_ascii=False)},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 11, 'completion_tokens': 7}}

    # 仅提供方响应和公开 now 墙钟注入；原 factory consumer、Source、Turn 与 CAS 不替换。
    monkeypatch.setattr(actual.models, '_completion_fn', completion)
    job = actual.app.state.memory_consolidation
    assert job.records is actual.records and job.service is actual.service and job.models is actual.models
    monkeypatch.setattr(job, 'now', lambda: mature)
    result = job.run(PROJECT)
    assert result['new_suggestions'] == 1 and result['failed_groups'] == 0
    assert actual.wire['calls'] == 1
    pattern, = actual.records.list('v2_insight_patterns')
    assert pattern.payload['kind'] == 'correction' and pattern.payload['event_ids'] == [event_id]
    candidate = actual.records.read('recognition_candidates', pattern.object_id)
    assert candidate.payload['state'] == 'pending' and candidate.payload['conditions'] == CONDITIONS
    assert candidate.payload['content'] == METHOD
    experience_id, = candidate.payload['source_experience_ids']
    artifact = actual.records.read('recognition_experiences', experience_id)
    assert artifact.payload['content'] == BIRTH
    assert artifact.payload['provenance']['kind'] == 'model_generated_artifact'
    assert artifact.payload['provenance']['source_refs'] == [
        {'type': 'turn', 'id': setup.kernel_id}, {'type': 'document', 'id': identity, 'revision': 1}]
    graph = SourceGraph()
    graph.snapshot(SourceEgressService(actual.records).snapshot(SCOPE,
        [{'type': 'experience', 'id': experience_id, 'revision': artifact.revision}]))
    assert any(node['kind'] == 'material' and node['type'] == 'recognition'
        and node['id'] == setup.parent.id and node['scope'] == {'user_id': 'local-user', 'project_id': PROJECT}
        for node in graph.result()['nodes'])
    consumed, = actual.records.list('v2_consolidation_inputs')
    assert consumed.payload['documents'] == [[identity, 1]] and consumed.payload['event_ids'] == [event_id]
    assert consumer_outcomes(actual.records, PROJECT, now=mature.isoformat()) == []
    replay = job.run(PROJECT)
    assert replay == {'processed': 0, 'new_suggestions': 0, 'replayed': True}
    assert actual.records.read('recognition_candidates', candidate.object_id) == candidate
    assert actual.records.list('v2_consolidation_inputs') == (consumed,) and actual.wire['calls'] == 1
    # 合成生产者完成值不变，原内核仍无生产者执行事件；只验证真实下游消费。
    assert actual.records.read('v2_turns', setup.product_id) == publication
    assert actual.records.read('v2_task_executions', setup.product_id) == execution
    assert tuple(actual.store.events_after(setup.kernel_id)) == ()
    turn, = actual.records.list('v2_memory_turn_keys')
    request = actual.store.get_request(turn.object_id)
    assert request == turn.payload['request'] and request['desired_outcome'] == 'memory.consolidate'
    assert request['execution_policy']['purpose'] == 'aux'
    assert request['privacy']['material_refs'] == [{'type': 'experience', 'id': experience_id,
        'revision': artifact.revision, 'project_id': PROJECT}]
    completed = tuple(actual.store.events_after(turn.object_id))
    assert completed[-1]['type'] == 'turn.completed'
    assert len([event for event in completed if event['type'] == 'model.completed']) == 1
    terminals = [event for event in completed if event['type'] == 'model.attempt.terminal']
    assert len(terminals) == 1 and actual.store.get(terminals[0]['data']['receipt_ref'])['status'] == 'succeeded'
    assert actual.records.read('recognitions', candidate.object_id) is None
    assert [row['id'] for row in actual.client.get(BASE + '/methods').json()['items']] == [setup.parent.id]
    refused = post(actual, BASE, {'sources': [{'id': candidate.object_id, 'revision': candidate.revision}],
        'document': draft()}, 409)
    assert refused.json()['detail'] == 'skill_source_unavailable'
    post(actual, '/api/v2/library/insights/' + candidate.object_id + '/confirm',
        {'project_id': PROJECT, 'expected_revision': candidate.revision + 1}, 409)
    assert actual.records.read('recognition_candidates', candidate.object_id) == candidate
    assert setup.documents.markdown(identity, revision=1) == BIRTH
    assert setup.documents.markdown(identity, revision=2) == EDITED
