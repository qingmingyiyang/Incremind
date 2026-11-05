from copy import deepcopy
import json
import socket

import pytest
from pydantic import BaseModel

from backend.recognition import RecognitionConflict, WorkScope
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.part_context import attachment_text
from backend.memory_app.v2.privacy import privacy_revision
from core.storage_provider import SQLiteStructuredRecordStore


class Decision(BaseModel):
    summary: str


@pytest.fixture
def originals(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    scope = WorkScope('local-user', 'alpha')
    bodies = ['预算十万元。\n\n' + '交付时间五月。\n\n' * 120,
              '预算二十万元。\n\n' + '交付时间六月。\n\n' * 120]
    with records.begin() as tx:
        tx.put('v2_turns', 'parent', {'project_id': 'alpha', 'intent': 'multi',
            'part_turn_ids': ['native'], 'attachment_item_ids': ['original-0', 'original-1']}, expected_revision=0)
        for index, body in enumerate(bodies):
            identity = 'original-' + str(index)
            tx.put('workspace_items', identity, {'id': identity, 'project_id': 'alpha',
                'input_kind': 'file', 'created_at': '2026-10-25', 'status': 'ready',
                'title': '访谈' + str(index), 'source_text': body}, expected_revision=0)
        tx.commit()
    authority = SourceEgressService(records)
    snapshots = [authority.snapshot_original_content(scope, 'original-' + str(i)) for i in range(2)]
    context = {'project_id': 'alpha', 'parent': 'parent', 'originals': snapshots,
        'answers': [], 'privacy_revision': privacy_revision(records), 'target': {},
        'refs': [{'kind': 'source', 'object_id': row['id'],
                  'uri': 'crp://default/workspace/' + row['id']} for row in snapshots],
        'attachment_titles': ['访谈0', '访谈1']}
    context['text'] = attachment_text(context)
    return records, context


def planner_messages(context):
    task = json.dumps({'task': '预算分别多少', 'dependency_context': context['text']}, ensure_ascii=False)
    return [{'role': 'system', 'content': '保留实际规划指令'}, {'role': 'user', 'content': json.dumps({
        'input': {'text': task, 'refs': context['refs']}, 'events': [{'resolved_payload': {'input': {'text': task}}}],
        'capabilities': [{'capability_id': 'document.draft.propose'}],
        'decision_contract': {'complete': {'summary': 'string'}}, 'context': []}, ensure_ascii=False)},
        {'role': 'user', 'content': context['text']}]


def test_actual_nested_planner_messages_fit_every_original_without_rewriting_authority(originals):
    from backend.memory_app.v2.task_attachment_input import fit_task_attachment_messages, input_tokens
    records, context = originals
    messages = planner_messages(context)
    before = deepcopy((context, messages))
    selected, projected = fit_task_attachment_messages(records, None, context, '预算分别多少', messages,
        {'window': 2600, 'reserve': 200}, response_model=Decision)
    assert input_tokens(projected, Decision) <= 2400
    assert all(row['content']['windows'] for row in selected['originals'])
    assert (context, messages) == before
    assert projected[0] == messages[0]
    nested = json.loads(projected[1]['content'])
    assert json.loads(nested['input']['text'])['dependency_context'] == selected['text']
    assert json.loads(nested['events'][0]['resolved_payload']['input']['text'])['dependency_context'] == selected['text']
    assert projected[2]['content'] == selected['text']
    for original in selected['originals']:
        for window in original['content']['windows']:
            body = original['content']['identity']['source_text']
            assert body[window['start']:window['end']] == window['text']


def test_fixed_planner_input_too_large_rejects_before_any_attempt(originals):
    from backend.memory_app.v2.task_attachment_input import fit_task_attachment_messages
    records, context = originals
    messages = [{'role': 'system', 'content': '固定指令。' * 500}, *planner_messages(context)]
    with pytest.raises(RecognitionConflict, match='budget'):
        fit_task_attachment_messages(records, None, context, '预算分别多少', messages,
            {'window': 1000, 'reserve': 200}, response_model=Decision)


def test_dependency_projection_rejects_unbound_text_and_keeps_unrelated_text(originals):
    from backend.memory_app.v2.task_attachment_input import project_attachment_messages
    _, context = originals
    unchanged = [{'role': 'user', 'content': json.dumps({'task': '完整用户任务'}, ensure_ascii=False)}]
    assert project_attachment_messages(unchanged, [context['text']], '选中原文', require_context=False) == unchanged
    forged = [{'role': 'user', 'content': json.dumps({'dependency_context': '未经绑定的原文'}, ensure_ascii=False)}]
    with pytest.raises(RecognitionConflict, match='binding'):
        project_attachment_messages(forged, [context['text']], '选中原文')


def test_missing_budget_refuses_to_freeze_a_projection(originals):
    from backend.memory_app.v2.task_attachment_input import fit_task_attachment_messages
    records, context = originals
    with pytest.raises(RecognitionConflict, match='budget'):
        fit_task_attachment_messages(records, None, context, '预算分别多少', planner_messages(context), None)


def native_request(records, context):
    from core.ai_kernel.turn_kinds import freeze_turn_request
    from backend.memory_app.part_context_binding import bind_context
    request = freeze_turn_request('project.task', turn_id='task-native', session_id='session-native',
        operation_id='op-native', idempotency_key='key-native', project_id='alpha', created_at='2026-10-25T00:00:00Z',
        text=json.dumps({'task': '预算分别多少', 'dependency_context': context['text']}, ensure_ascii=False),
        privacy={'mode': 'local_only', 'allow_remote': False, 'pii': 'possible',
                 'consent_refs': [], 'retention': 'session'}, capabilities=[], template_version=3)
    bind_context(records, request, context)
    return request


@pytest.mark.parametrize('change', ['none', 'messages', 'private'])
def test_native_handle_binds_distinct_attempts_and_blocks_changed_input_without_http(originals, tmp_path, change):
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.kernel.product_routing import ProductGenerationRouting
    from backend.memory_app.v2.task_attachment_input import (
        INPUT_KIND, PreparedAttachmentInput, fit_task_attachment_messages, validate_task_attachment_inputs,
    )
    from core.ai_kernel import SQLiteAITurnStore, ScopedCapabilityRegistry, SynchronousAIRuntime
    from core.effect_log import EffectLog, EffectRunner

    records, context = originals
    request = native_request(records, context)
    models = ModelConfiguration(records, tmp_path)
    models.update('generation', {'provider': 'openai', 'base_url': 'http://127.0.0.1:9/v1',
        'model': 'qwen2.5-1.5b-instruct', 'enabled': True, 'allow_remote': False, 'expected_revision': 0})
    turns = SQLiteAITurnStore(tmp_path / 'ai-turns.sqlite3')
    seen, invoked, timed_out = [], [], []
    authority = SourceEgressService(records)
    receiver, peer = socket.socketpair()
    receiver.settimeout(0.2)

    def validate():
        for original in context['originals']:
            authority.validate_original_content(WorkScope(**original['scope']), original, 'generation')

    class DomainBoundary:
        def plan(self, accepted, events, capabilities, payloads, execution_control):
            route = ProductGenerationRouting(models, turns).acquire(request=accepted,
                project_id='alpha', project_profile_id='project-native', project_profile_revision=1,
                boundary_profile_id='boundary-native', boundary_profile_revision=1, capability_ids=[])
            configuration = route.payload['configuration']
            execution_control.model_call_routed(snapshot_ref=route.payload_ref, snapshot_revision=route.revision,
                prompt_cache_scope_identity=route.payload['prompt_cache_scope_identity'],
                provider=configuration['provider'], model=configuration['model'], execution_location='local_loopback')
            execution_control.model_call_started(provider=configuration['provider'], model=configuration['model'])
            for number in range(2 if change == 'none' else 1):
                messages = planner_messages(context)
                selected, projected = fit_task_attachment_messages(records, None, context, '预算分别多少',
                    messages, {'window': 2600, 'reserve': 200}, response_model=Decision)
                prepared = PreparedAttachmentInput(records=records, turns=turns, root_request=accepted,
                    request=accepted, context_row=records.read('v2_part_contexts', accepted['turn_id']),
                    selected=selected, messages=projected, routing_snapshot=route.generation_binding(),
                    limits={'window': 2600, 'reserve': 200}, response_model=Decision,
                    execution_control=execution_control, validate=validate)
                handle = prepared.wire_attempt_sink.begin_model_wire_attempt()
                seen.append(handle.dispatch)
                assert handle.durable_commit_witness is handle.handle.durable_commit_witness
                assert handle.dispatch['attempt_number'] == number + 1
                proof = turns.get_immutable_payload(accepted['turn_id'], INPUT_KIND + handle.dispatch['attempt_id'])
                assert proof[1]['messages'] == projected
                if change == 'messages':
                    prepared.messages.append({'role': 'user', 'content': '未经绑定的附加消息'})
                elif change == 'private':
                    item = records.read('workspace_items', 'original-0')
                    authority.set_policy(WorkScope('local-user', 'alpha'), 'original_item', item.object_id,
                        item.revision, 0, [])

                def reject_without_http():
                    invoked.append(handle.dispatch['attempt_id'])
                    try:
                        receiver.recv(1)
                    except TimeoutError:
                        timed_out.append(handle.dispatch['attempt_id'])
                        handle.failed_transport(error_code='ai.domain_receive_timeout')
                        raise
                    pytest.fail('保持开放的对端未产生接收超时')

                if change == 'none':
                    # 对端保持连接且不发送数据，真实接收超时沿原 Handle 完成失败终态。
                    with pytest.raises(TimeoutError):
                        handle.invoke_wire(reject_without_http)
                else:
                    handle.invoke_wire(reject_without_http)
            raise RecognitionConflict('domain budget probe stopped')

    runtime = SynchronousAIRuntime(planner=DomainBoundary(), registry=ScopedCapabilityRegistry(),
        events=turns, payloads=turns, state=turns,
        effect_runner=EffectRunner(EffectLog(tmp_path / 'effects.sqlite3'), owner_id='budget-native'))
    try:
        assert runtime.submit_turn(request).status == 'failed'
    finally:
        receiver.close()
        peer.close()
    assert len(seen) == (2 if change == 'none' else 1)
    assert len({row['attempt_id'] for row in seen}) == len(seen)
    assert len({row['model_request_id'] for row in seen}) == 1
    assert len(invoked) == (2 if change == 'none' else 0)
    assert timed_out == invoked
    proofs = validate_task_attachment_inputs(records, turns, None, request)
    assert len(proofs) == len(seen)
    for proof in proofs:
        assert not {'root_request', 'request', 'context', 'selected', 'source_messages'} & set(proof)
        changed = deepcopy(proof)
        changed['estimated_input_tokens'] += 1
        with pytest.raises(ValueError, match='immutable payload identity conflict'):
            turns.get_or_create_immutable_payload(request['turn_id'], INPUT_KIND + proof['dispatch']['attempt_id'], changed)
    if change == 'none':
        print('attachment-proof-size', json.dumps({'root_context_bytes': len(context['text'].encode()),
            'attempt_bytes': [len(json.dumps(proof, ensure_ascii=False).encode()) for proof in proofs]}))
        row = records.read('workspace_items', 'original-0')
        with records.begin() as tx:
            tx.put('workspace_items', row.object_id, {**row.payload, 'source_text': '修改后的真实原件。'},
                expected_revision=row.revision)
            tx.commit()
        with pytest.raises(RecognitionConflict, match='coordinates|identity'):
            validate_task_attachment_inputs(records, turns, None, request)
    terminals = [turns.get(row['data']['receipt_ref']) for row in turns.events_after(request['turn_id'])
                 if row['type'] == 'model.attempt.terminal']
    assert len(terminals) == len(seen)
    assert all(row['status'] == 'failed_transport' for row in terminals)
    assert all(row['usage_status'] == 'unavailable' for row in terminals)


def test_native_no_wire_completion_does_not_invent_attachment_attempts(originals, tmp_path):
    from backend.memory_app.v2.task_attachment_input import validate_task_attachment_inputs
    from core.ai_kernel import SQLiteAITurnStore, ScopedCapabilityRegistry, SynchronousAIRuntime
    records, context = originals
    request = native_request(records, context)
    turns = SQLiteAITurnStore(tmp_path / 'ai-turns.sqlite3')

    class LocalCompletion:
        def plan(self, request, events, capabilities, payloads, execution_control):
            return {'type': 'complete', 'summary': '本机领域检查完成'}

    runtime = SynchronousAIRuntime(planner=LocalCompletion(), registry=ScopedCapabilityRegistry(),
        events=turns, payloads=turns, state=turns)
    assert runtime.submit_turn(request).status == 'completed'
    assert validate_task_attachment_inputs(records, turns, None, request) == []
    assert not any(row['type'] == 'model.attempt.dispatched' for row in turns.events_after(request['turn_id']))
