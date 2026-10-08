"""A correctly-protocolled resumed Main failure cannot become a completion."""
import json
import time
from urllib.error import HTTPError

from tests.memory_app.v2.test_workbench_do import env


def test_closed_task_resume_prebody_transport_failure_keeps_partial_without_false_done(env):
    client, models = env
    runtime, store = client.app.state.ai_runtime, client.app.state.ai_turn_store
    records = client.app.state.recognition_service.records
    calls, closed = [], []
    prefix = '原任务完整段落。\n\n'
    main_role = client.app.state.agent_runtime_composition.profiles.get('main.orchestrator').organization_role

    def provider(**request):
        # A continuation's final user message is ordinary prose. Discover the
        # original decision packet, rather than parsing that prose as JSON.
        packets = []
        for message in request['messages']:
            if message['role'] != 'user':
                continue
            try:
                packet = json.loads(message['content'])
            except json.JSONDecodeError:
                continue
            if isinstance(packet, dict) and ('output' in packet or 'decision_contract' in packet):
                packets.append(packet)
        assert packets
        packet = packets[-1]
        role = ('steward' if 'output' in packet else 'main'
                if packet['role']['organization_role'] == main_role else 'worker')
        calls.append(role)
        if role == 'main' and calls.count('main') == 2:
            assert request['messages'][-1]['role'] == 'user'
            assert not request['messages'][-1]['content'].startswith('{')
            assert any(message == {'role': 'assistant', 'content': prefix}
                       for message in request['messages'])
            # A real HTTPError at the external provider boundary, before any
            # response/stream is returned. Retry-After exceeds this request's
            # remaining execution budget, so the unchanged policy cannot retry.
            raise HTTPError('https://example.test/v1/chat/completions', 503,
                            'synthetic unavailable before resumed output',
                            {'Retry-After': '600'}, None)
        text = json.dumps({'mode': 'main_only', 'assignments': []} if role == 'steward' else
                          {'type': 'complete', 'summary': prefix + '尚未闭合的尾部' if role == 'main'
                           else '完成的工作单元。'}, ensure_ascii=False)
        if not request.get('stream'):
            return {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 6, 'completion_tokens': 4}}

        def stream():
            try:
                yield {'choices': [{'delta': {'content': text[:-2] if role == 'main' else text},
                                    'finish_reason': None}],
                       'usage': {'prompt_tokens': 6, 'completion_tokens': 4}}
                if role == 'main':
                    raise ConnectionError('synthetic first body interruption')
                yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 6, 'completion_tokens': 4}}
            finally:
                closed.append(role)
        return stream()

    models._completion_fn = provider
    response = client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'intent': 'do', 'text': '保留任务成果并如实报告续写失败'})
    assert response.status_code == 200, response.text
    product = response.json()['turn']['id']
    execution = records.read('v2_task_executions', product)
    identity = execution.payload['request']['turn_id']
    deadline = time.monotonic() + 25
    while True:
        ready_events = tuple(store.events_after(identity))
        ready_execution = records.read('v2_task_executions', product)
        ready_product = records.read('v2_turns', product)
        ready_receipt = ready_product.payload['receipt']['do']
        active = identity in client.app.state.ai_turn_runner.active_turn_ids
        observed = {'turn_id': identity, 'kernel_last_event': ready_events[-1]['type'] if ready_events else None,
                    'active': active, 'owner': ready_execution.payload['owner'],
                    'execution_revision': ready_execution.revision, 'product_revision': ready_product.revision,
                    'product_state': ready_receipt['state'], 'has_expected_partial': ready_receipt.get('partial') == prefix}
        if (ready_events and not active and ready_execution.payload['owner'] is None
                and ready_receipt['state'] == 'interrupted' and ready_receipt.get('partial') == prefix):
            break
        assert time.monotonic() < deadline, observed
        time.sleep(.05)
    print('PREBODY_READINESS=' + json.dumps(observed, ensure_ascii=False))
    assert runtime.receipt_for(identity).status == 'waiting_approval'
    binding = runtime.task_continuations.paused(identity, 'project-a')
    assert binding is not None and binding[1]['partial'] == prefix
    assert records.read('v2_turns', product).payload['receipt']['do']['partial'] == prefix
    original_request = store.get_request(identity)
    original_events = tuple(store.events_after(identity))
    original_tools = [event for event in original_events if event['type'] == 'tool.intent.recorded']
    original_terminals = [store.get(event['data']['receipt_ref']) for event in original_events
                          if event['type'] == 'model.attempt.terminal']
    assert len(original_terminals) == 1 and original_terminals[0]['status'] == 'failed_transport'
    original_failed = store.get(binding[1]['model_receipt_ref'])
    assert original_failed['status'] == 'failed'
    assert runtime._effect_runner.log.get(original_terminals[0]['attempt_id']).state.value == 'UNKNOWN'
    assert closed.count('main') == 1 and calls.count('main') == 1
    assert not records.list('v2_task_draft_operations')
    usage_before = {name: records.list(name) for name in ('v2_usage_document', 'v2_usage_insight')}

    resumed = client.post(f'/api/v2/workbench/turns/{product}/continue',
        json={'project_id': 'project-a'}, headers={'Idempotency-Key': 'prebody-task-resume-only'})
    assert resumed.status_code == 200, resumed.text
    final = resumed.json()['receipt']['do']
    events = tuple(store.events_after(identity))
    terminals = [store.get(event['data']['receipt_ref']) for event in events
                 if event['type'] == 'model.attempt.terminal']
    print('PREBODY_CONTINUATION_OBSERVATION=' + json.dumps({
        'turn_id': identity, 'status': runtime.receipt_for(identity).status,
        'product_state': final['state'], 'partial': final.get('partial'),
        'main_wires': calls.count('main'), 'main_stream_closes': closed.count('main'),
        'events': [{'type': event['type'], 'error_code': event['data'].get('error_code'),
                    'model_request_id': event['correlation']['model_request_id']}
                   for event in events[len(original_events):]],
        'terminals': [{'attempt_id': row['attempt_id'], 'model_request_id': row['model_request_id'],
                       'status': row['status'], 'error_code': row.get('error_code'),
                       'usage': row.get('usage')} for row in terminals]}, ensure_ascii=False))
    assert store.get_request(identity) == original_request
    assert [event for event in events if event['type'] == 'tool.intent.recorded'] == original_tools
    assert len(terminals) == 2 and len({row['model_request_id'] for row in terminals}) == 2
    assert terminals[0] == original_terminals[0] and terminals[1]['status'] == 'failed_transport'
    assert store.get(binding[1]['model_receipt_ref']) == original_failed
    assert all(runtime._effect_runner.log.get(row['attempt_id']).state.value == 'UNKNOWN'
               for row in terminals)
    assert calls.count('main') == 2 and closed.count('main') == 1
    assert {name: records.list(name) for name in usage_before} == usage_before
    assert final['state'] not in {'done', 'partial'}
    assert final['kernel_turn_id'] == identity and final['partial'] == prefix
    assert runtime.receipt_for(identity).status != 'completed'
    assert not any(event['type'] == 'turn.completed' for event in events[len(original_events):])
    assert not records.list('v2_task_draft_operations') and not final.get('document_id')
    assert final['state'] == 'failed' and runtime.receipt_for(identity).status == 'failed'
    new_failed = [event for event in events[len(original_events):] if event['type'] == 'model.failed']
    assert len(new_failed) == 1
    failed = store.get(new_failed[0]['data']['receipt_ref'])
    assert failed['status'] == 'failed' and failed['model_request_id'] == terminals[1]['model_request_id']
    assert failed['error_code'] == 'ai.model_call_failed'
    assert terminals[1]['usage'] is None
    assert runtime.task_continuations.paused(identity, 'project-a') is None
    before_rejected = tuple(store.events_after(identity))
    rejected = client.post(f'/api/v2/workbench/turns/{product}/continue',
        json={'project_id': 'project-a'}, headers={'Idempotency-Key': 'failed-task-cannot-continue'})
    assert rejected.status_code == 409, rejected.text
    assert tuple(store.events_after(identity)) == before_rejected
    assert calls.count('main') == 2 and closed.count('main') == 1
    assert not records.list('v2_task_draft_operations')
    assert {name: records.list(name) for name in usage_before} == usage_before
