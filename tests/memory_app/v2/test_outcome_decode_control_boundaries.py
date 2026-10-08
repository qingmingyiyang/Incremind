"""真实新 Main 和同名普通异常保持原解码恢复边界。"""
import json
import time

from tests.memory_app.v2.test_outcome_continuation import BIRTH
from tests.memory_app.v2.test_outcome_continuation_boundaries import _continue, _is_main
from tests.memory_app.v2.test_outcome_decode_recovery import _receipts
from tests.memory_app.v2.test_outcome_redos import scenario, completed, wait_product
from tests.memory_app.v2.test_workbench_do import env as do_env


RAW_MARKER = 'CONTROL_SYNTHETIC_RAW_BODY_MUST_NOT_APPEAR'
USAGE = {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}


def _assert_no_delivery_or_composition(values, receipt):
    kernel = receipt['kernel_turn_id']
    assert receipt['state'] == 'failed' and receipt['document_id'] is None, receipt
    assert receipt['fallback_new'] is False and receipt['continues'] is None
    assert receipt['changes'] == []
    assert values.records.read('v2_task_draft_operations', 'deliver-' + kernel) is None
    assert values.state.ai_turn_store.get_immutable_payload(kernel,
        'product-outcome-composition-v1') is None
    return kernel


def test_actual_completed_decode_failure_without_previous_outcome_never_retries(scenario):
    values = scenario
    respond, main_calls = values.models.handler, []
    before_lineage = values.records.list('v2_outcome_lineage')

    def external_response(messages, **options):
        if _is_main(messages):
            main_calls.append(messages)
            # 完整非法对象仍经真实 decoder；普通 Main 保留原专家汇总完成路径。
            return json.dumps([RAW_MARKER])
        return respond(messages, **options)

    values.models.handler = external_response
    response = values.client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'intent': 'do', 'text': '准备一份新成果',
        'continue_from': None})
    assert response.status_code == 200, response.text
    data = response.json()
    receipt = wait_product(values, data)['receipt']['do']
    execution = values.records.read('v2_task_executions', data['turn']['id'])
    frozen = json.loads(execution.payload['request']['input']['text'])
    assert frozen['outcome_input']['document_id'] is None
    assert 'outcome_selection' not in frozen
    assert len(main_calls) == 1
    assert receipt['state'] == 'done' and isinstance(receipt['document_id'], str), receipt
    assert receipt['fallback_new'] is False and receipt['continues'] is None
    assert receipt['changes'] == []
    kernel, document = receipt['kernel_turn_id'], receipt['document_id']
    payloads = _receipts(values, kernel)
    wire, = [value for kind, value in payloads if kind == 'model-wire-attempt-receipt']
    assert wire['attempt_number'] == 1 and wire['status'] == 'succeeded'
    assert wire['usage'] == USAGE and wire['usage_status'] == 'reported'
    context = json.loads(main_calls[0][-1]['content'])
    topology, = [event['resolved_payload'] for event in context['events']
        if event['type'] == 'tool.completed'
        and event['data']['capability_id'] == 'agent.list']
    expert, = [child for child in topology['children'] if child['profile_id'] == 'subagent.worker']
    assert expert['status'] == 'completed'
    role = expert.get('organization_role') or expert['profile_id']
    assert values.documents.markdown(document) == f'专家结论：\n\n【{role}】组成部分\n组成正文'
    operation = values.records.read('v2_task_draft_operations', 'deliver-' + kernel)
    assert operation is not None and operation.payload['result']['document_id'] == document
    assert values.state.ai_turn_store.get_immutable_payload(kernel,
        'product-outcome-composition-v1') is None
    lineage = values.records.read('v2_outcome_lineage', document)
    assert lineage.payload['root_id'] == document and lineage.payload['previous_id'] is None
    assert lineage.payload['version'] == 1 and lineage.payload['turn_id'] == data['turn']['id']
    assert {row.object_id for row in values.records.list('v2_outcome_lineage')} == (
        {row.object_id for row in before_lineage} | {document})
    assert RAW_MARKER not in json.dumps(payloads, ensure_ascii=False)
    assert RAW_MARKER not in json.dumps(receipt, ensure_ascii=False)
    assert RAW_MARKER not in json.dumps(tuple(values.state.ai_turn_store.events_after(kernel)),
        ensure_ascii=False)


def test_actual_provider_value_error_prefix_never_grants_completed_decode_recovery(scenario):
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    before = values.records.read('documents', previous)
    before_lineage = values.records.list('v2_outcome_lineage')
    respond, main_calls = values.models.handler, []

    def external_response(messages, **options):
        if _is_main(messages):
            main_calls.append(messages)
            # 同名文本由普通 ValueError 提供，不构造专用 decoder 错误或完成标记。
            raise ValueError('json_object_required')
        return respond(messages, **options)

    values.models.handler = external_response
    response = _continue(values, original, previous)
    receipt = wait_product(values, response)['receipt']['do']
    assert len(main_calls) == 1
    kernel = _assert_no_delivery_or_composition(values, receipt)
    payloads = _receipts(values, kernel)
    wire, = [value for kind, value in payloads if kind == 'model-wire-attempt-receipt']
    assert wire['attempt_number'] == 1 and wire['status'] == 'failed_transport'
    assert wire['usage'] is None and wire['usage_status'] == 'unavailable'
    assert values.records.read('documents', previous) == before
    assert values.documents.markdown(previous, revision=1) == BIRTH
    assert values.records.list('v2_outcome_lineage') == before_lineage
    assert 'json_object_required' not in json.dumps(payloads, ensure_ascii=False)
    assert 'json_object_required' not in json.dumps(receipt, ensure_ascii=False)
    assert 'json_object_required' not in json.dumps(
        tuple(values.state.ai_turn_store.events_after(kernel)), ensure_ascii=False)


def test_actual_selected_task_continuation_decode_rejection_never_restarts_patch_recovery(scenario):
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    before = values.records.read('documents', previous)
    before_lineage = values.records.list('v2_outcome_lineage')
    transport = values.models._completion_fn
    main_calls, closed, admissions = [], [], []
    runtime, store = values.state.ai_runtime, values.state.ai_turn_store
    service = runtime.task_continuations
    identity, capsule = None, None

    def provider(**request):
        context = json.loads(next(message['content'] for message in reversed(request['messages'])
            if message['role'] == 'user' and message['content'].startswith('{')))
        if not any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])):
            return transport(**request)
        main_calls.append(request)
        number = len(main_calls)
        if number == 2:
            # 只读取原 admit/take 留下的真实执行事实，不写 active 或构造胶囊。
            active = service.active.get(identity)
            admissions.append((active is not None and active.get('used') is True,
                active is not None and active.get('runtime') is runtime,
                active is not None and active['binding'][1] == capsule))
        if not request.get('stream'):
            return {'choices': [{'message': {'content': json.dumps([RAW_MARKER])},
                'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 8, 'completion_tokens': 5}}
        def stream():
            try:
                if number == 1:
                    # 原中断测试的真实部分流与 close，资格由原持有者产生。
                    yield {'choices': [{'delta': {'content': '{"type":"complete","summary":"完整成果段落。\\n\\n未完成'},
                        'finish_reason': None}], 'usage': {'prompt_tokens': 6, 'completion_tokens': 4}}
                    raise ConnectionError('synthetic summary body disconnect')
                yield {'choices': [{'delta': {'content': json.dumps([RAW_MARKER])}, 'finish_reason': None}]}
                yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 8, 'completion_tokens': 5}}
            finally:
                closed.append(number)
        return stream()

    values.models._completion_fn = provider
    response = _continue(values, original, previous)
    turn_id = response['turn']['id']
    execution = values.records.read('v2_task_executions', turn_id)
    identity = execution.payload['request']['turn_id']
    frozen = json.loads(execution.payload['request']['input']['text'])
    assert frozen['outcome_input']['document_id'] == previous
    deadline = time.monotonic() + 25
    while not store.events_after(identity) or runtime.receipt_for(identity).status != 'waiting_approval':
        assert time.monotonic() < deadline
        time.sleep(.05)
    paused_response = values.client.get('/api/v2/workbench/threads/' + response['thread_id'],
        params={'project_id': 'project-a'})
    assert paused_response.status_code == 200, paused_response.text
    paused_turn, = [row for row in paused_response.json()['turns'] if row['id'] == turn_id]
    assert paused_turn['receipt']['do']['state'] == 'interrupted'
    assert paused_turn['receipt']['do']['interruption'] == 'connection'
    assert paused_turn['receipt']['do']['partial'] == '完整成果段落。\n\n'
    binding = service.paused(identity, 'project-a')
    assert binding is not None
    descriptor, capsule = binding
    assert descriptor.payload['state'] == 'paused' and store.get(descriptor.payload['capsule_ref']) == capsule
    request_before = store.get_request(identity)
    assert capsule['request'] == request_before and capsule['product_request'] == execution.payload['request']
    assert capsule['partial'] == '完整成果段落。\n\n' and capsule['interruption'] == 'connection'
    events_before = tuple(store.events_after(identity))
    tools_before = [event for event in events_before if event['type'] == 'tool.intent.recorded']
    assert tools_before
    assert len(main_calls) == 1 and main_calls[0].get('stream') is True and closed == [1]
    resume_key = 'decode-task-continuation-once'
    resumed = values.client.post('/api/v2/workbench/turns/' + turn_id + '/continue',
        json={'project_id': 'project-a'}, headers={'Idempotency-Key': resume_key})
    assert resumed.status_code == 200, resumed.text
    receipt = wait_product(values, response)['receipt']['do']
    assert admissions == [(True, True, True)]
    assert len(main_calls) == 2 and closed == [1, 2]
    assert runtime.receipt_for(identity).status == 'failed'
    assert receipt['kernel_turn_id'] == identity
    assert _assert_no_delivery_or_composition(values, receipt) == identity
    assert store.get_request(identity) == request_before
    assert [event for event in store.events_after(identity) if event['type'] == 'tool.intent.recorded'] == tools_before
    assert service.active.get(identity) is None
    action, _ = store.get_action(resume_key)
    assert action['turn_id'] == identity and action['type'] == 'resume'
    payloads = _receipts(values, identity)
    wires = [value for kind, value in payloads if kind == 'model-wire-attempt-receipt']
    assert len(wires) == len({wire['model_request_id'] for wire in wires}) == 2
    assert all(wire['attempt_number'] == 1 for wire in wires)
    first, = [wire for wire in wires if wire['model_request_id'] == capsule['model_request_id']]
    second, = [wire for wire in wires if wire['model_request_id'] != capsule['model_request_id']]
    assert first['status'] == 'failed_transport' and second['status'] == 'succeeded'
    assert second['usage'] == {'input_tokens': 8, 'output_tokens': 5, 'total_tokens': 13}
    assert second['usage_status'] == 'reported'
    assert values.records.read('documents', previous) == before
    assert values.documents.markdown(previous, revision=1) == BIRTH
    assert values.records.list('v2_outcome_lineage') == before_lineage
    assert RAW_MARKER not in json.dumps(payloads, ensure_ascii=False)
    assert RAW_MARKER not in json.dumps(receipt, ensure_ascii=False)
    assert RAW_MARKER not in json.dumps(tuple(store.events_after(identity)), ensure_ascii=False)
