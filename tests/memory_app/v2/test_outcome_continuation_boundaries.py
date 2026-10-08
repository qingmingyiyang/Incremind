"""真实续写网关、用量回执和交付事务的边界控制。"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import sqlite3
import time

import pytest

from backend.memory_app.kernel.receipt_projection import kernel_call_groups
from backend.memory_app.v2.privacy import set_private_project
from tests.memory_app.v2.test_outcome_continuation import BIRTH, CURRENT
from tests.memory_app.v2.test_outcome_redos import scenario, completed, wait_product
from tests.memory_app.v2.test_workbench_do import env as do_env


def _is_main(messages):
    context = json.loads(messages[-1]['content'])
    return any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', []))


def _continue(values, original, previous):
    response = values.client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'thread_id': original['thread_id'], 'intent': 'do',
        'text': '补充实施记录', 'continue_from': previous})
    assert response.status_code == 200, response.text
    return response.json()


def _payloads(values, kernel):
    with sqlite3.connect(values.state.ai_turn_store._path) as connection:
        return [(kind, json.loads(raw)) for kind, raw in connection.execute(
            "SELECT kind,payload_json FROM ai_turn_payloads WHERE turn_id=? "
            "AND kind IN ('model-call-receipt','model-wire-attempt-receipt','prompt-cache-receipt')", (kernel,))]


def _settled(values):
    deadline = time.monotonic() + 90
    while values.state.workbench_tasks and time.monotonic() < deadline:
        time.sleep(.02)
    assert not values.state.workbench_tasks


@pytest.mark.parametrize('attempts', [2, 3])
def test_actual_retry_wires_keep_complete_usage_cache_and_original_prices(scenario, attempts):
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    values.models.update_model_prices('generation', {
        'input_per_million': '2', 'output_per_million': '8', 'cache_read_per_million': '1'},
        expected_revision=0, expected_configuration_revision=values.models.public()['generation']['revision'])
    before = values.records.read('documents', previous)
    respond, main_calls = values.models.handler, []

    def external_response(messages, **options):
        if _is_main(messages):
            main_calls.append(messages)
            if len(main_calls) < attempts:
                return json.dumps({'type': 'complete', 'patches': [
                    {'kind': 'update', 'path': ['项目方案', '实施记录'], 'body': '# 非法标题'}]}, ensure_ascii=False)
            if attempts == 3:
                return json.dumps({'type': 'complete', 'summary': '# 独立新稿\n\n真实回退正文。'}, ensure_ascii=False)
            return json.dumps({'type': 'complete', 'patches': [
                {'kind': 'update', 'path': ['项目方案', '实施记录'], 'body': '新增实施正文。\n\n'}]}, ensure_ascii=False)
        return respond(messages, **options)

    values.models.handler = external_response
    transport = values.models._completion_fn

    def external_counters(**request):
        result = transport(**request)
        result['usage'].update(prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=4)
        return result

    values.models._completion_fn = external_counters
    response = _continue(values, original, previous)
    receipt = wait_product(values, response)['receipt']['do']
    assert receipt['state'] == 'done', receipt
    assert len(main_calls) == attempts
    payloads = _payloads(values, receipt['kernel_turn_id'])
    wires = [value for kind, value in payloads if kind == 'model-wire-attempt-receipt']
    assert len(wires) == attempts
    assert len({wire['attempt_id'] for wire in wires}) == attempts
    assert {wire['attempt_number'] for wire in wires} == set(range(1, attempts + 1))
    identity, = {wire['model_request_id'] for wire in wires}
    for wire in wires:
        assert wire['status'] == 'succeeded' and wire['usage_status'] == 'reported'
        assert wire['usage'] == {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}
        assert wire['cache_status'] == 'reported'
        assert wire['cache_metadata']['cache_read_input_tokens'] == 0
        assert wire['cache_metadata']['uncached_input_tokens'] == 4
        price = values.records.read('v2_model_wire_prices', wire['attempt_id'])
        assert price.revision == 1 and price.payload['price_revision'] == 1
        assert price.payload['configuration_revision'] == 1
        assert price.payload['model_request_id'] == identity
    logical, = [value for kind, value in payloads
                if kind == 'model-call-receipt' and value['model_request_id'] == identity]
    assert logical['usage_status'] == 'recorded'
    assert logical['usage'] == {'input_tokens': 4 * attempts, 'output_tokens': 2 * attempts,
                                'total_tokens': 6 * attempts}
    cache, = [value for kind, value in payloads
              if kind == 'prompt-cache-receipt' and value['model_request_id'] == identity]
    assert cache['cache_status'] == 'reported'
    assert cache['cache_read_input_tokens'] == 0 and cache['uncached_input_tokens'] == 4 * attempts
    calls = [call for group in kernel_call_groups(values.models.root, turn_id=receipt['kernel_turn_id'],
        project='project-a', remote_only=False, records=values.records) for call in group['calls']]
    main, = [call for call in calls if call['model_request_id'] == identity]
    assert main['usage'] == logical['usage'] and main['usage_status'] == 'recorded'
    assert main['cost']['currency'] == 'CNY'
    assert Decimal(main['cost']['amount']) == Decimal('0.000024') * attempts
    assert len(calls) == 3
    assert receipt['model_usage'] == {'input_tokens': 4 * (attempts + 2),
        'output_tokens': 2 * (attempts + 2), 'total_tokens': 6 * (attempts + 2)}
    assert receipt['model_cost']['currency'] == 'CNY'
    assert Decimal(receipt['model_cost']['amount']) == Decimal('0.000024') * (attempts + 2)
    assert values.records.read('documents', previous) == before
    assert receipt['fallback_new'] is (attempts == 3)


@pytest.mark.parametrize('changed', ['current_body', 'current_refs', 'private', 'generation_off'])
def test_actual_current_authority_change_before_wire_has_no_new_dispatch(scenario, changed):
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    item = asyncio.run(values.state.workspace_domains.intake.add_text({
        'project_id': 'project-a', 'text': '真实新增来源'})) if changed == 'current_refs' else None
    dispatches = len(values.models.calls)
    complete = values.models.complete_governed
    observed = []

    def before_original_complete(*args, **kwargs):
        if not observed:
            observed.append(changed)
            if changed == 'current_body':
                values.documents.save_user_edit(previous, markdown=CURRENT, expected_revision=1)
            elif changed == 'current_refs':
                values.documents.save_user_edit(previous, markdown=BIRTH, expected_revision=1,
                    source_refs=({'source_id': item['id'], 'locator': 'workspace://' + item['id']},))
            elif changed == 'private':
                set_private_project(values.records, 'project-a', True, 0)
            else:
                values.models.update('generation', {'allow_remote': False, 'expected_revision': 1})
        # 观察器只改变真实临时事实，所有资格与调用继续委托原方法。
        return complete(*args, **kwargs)

    values.models.complete_governed = before_original_complete
    try:
        response = _continue(values, original, previous)
        receipt = wait_product(values, response)['receipt']['do']
    finally:
        values.models.complete_governed = complete
    assert observed == [changed]
    assert len(values.models.calls) == dispatches
    assert receipt['state'] == 'failed' and receipt['document_id'] is None
    assert values.records.read('v2_task_draft_operations', 'deliver-' + receipt['kernel_turn_id']) is None
    assert {row.object_id for row in values.records.list('v2_outcome_lineage')} == {previous}
    assert values.documents.markdown(previous, revision=1) == BIRTH
    assert values.state.ai_turn_store.get_immutable_payload(receipt['kernel_turn_id'],
        'product-outcome-composition-v1') is None


def test_actual_configuration_change_after_paid_response_prevents_retry_dispatch(scenario):
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    respond, transport, main_calls = values.models.handler, values.models._completion_fn, []

    def external_response(messages, **options):
        if _is_main(messages):
            main_calls.append(messages)
            return json.dumps({'type': 'complete', 'patches': [
                {'kind': 'update', 'path': ['项目方案', '实施记录'], 'body': '# 非法标题'}]}, ensure_ascii=False)
        return respond(messages, **options)

    values.models.handler = external_response
    def after_paid_response(**request):
        before = len(main_calls)
        result = transport(**request)
        if before == 0 and len(main_calls) == 1:
            if request.get('stream') is True:
                def complete_then_change_configuration():
                    # 原 provider 的正文、终止帧和用量先被消费，再改变真实配置。
                    try:
                        for chunk in result:
                            yield chunk
                        values.models.update('generation', {'model': 'changed-model', 'expected_revision': 1})
                    finally:
                        close = getattr(result, 'close', None)
                        if callable(close):
                            close()
                return complete_then_change_configuration()
            values.models.update('generation', {'model': 'changed-model', 'expected_revision': 1})
        return result

    values.models._completion_fn = after_paid_response
    response = _continue(values, original, previous)
    receipt = wait_product(values, response)['receipt']['do']
    assert len(main_calls) == 1
    assert receipt['state'] == 'failed' and receipt['document_id'] is None
    wires = [value for kind, value in _payloads(values, receipt['kernel_turn_id'])
             if kind == 'model-wire-attempt-receipt']
    assert len(wires) == 1 and wires[0]['attempt_number'] == 1
    assert wires[0]['usage'] == {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}
    assert values.models.public()['generation']['revision'] == 2
    assert values.state.ai_turn_store.get_immutable_payload(receipt['kernel_turn_id'],
        'product-outcome-composition-v1') is None
    assert values.records.read('v2_task_draft_operations', 'deliver-' + receipt['kernel_turn_id']) is None
    assert values.documents.markdown(previous) == BIRTH


def test_actual_configuration_change_before_usage_never_claims_paid_completion(scenario):
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    respond, main_calls = values.models.handler, []

    def external_response(messages, **options):
        if _is_main(messages):
            main_calls.append(messages)
            # 保留原先提前失效的外 provider 时序，独立证明它不能声称已报告用量。
            values.models.update('generation', {'model': 'changed-model', 'expected_revision': 1})
            return json.dumps({'type': 'complete', 'patches': [
                {'kind': 'update', 'path': ['项目方案', '实施记录'], 'body': '# 非法标题'}]}, ensure_ascii=False)
        return respond(messages, **options)

    values.models.handler = external_response
    response = _continue(values, original, previous)
    receipt = wait_product(values, response)['receipt']['do']
    assert len(main_calls) == 1
    assert receipt['state'] == 'failed' and receipt['document_id'] is None
    wires = [value for kind, value in _payloads(values, receipt['kernel_turn_id'])
             if kind == 'model-wire-attempt-receipt']
    assert len(wires) == 1 and wires[0]['attempt_number'] == 1
    assert wires[0]['status'] == 'failed_transport'
    assert wires[0]['usage'] is None and wires[0]['usage_status'] == 'unavailable'
    assert values.models.public()['generation']['revision'] == 2
    assert values.state.ai_turn_store.get_immutable_payload(receipt['kernel_turn_id'],
        'product-outcome-composition-v1') is None
    assert values.records.read('v2_task_draft_operations', 'deliver-' + receipt['kernel_turn_id']) is None
    assert {row.object_id for row in values.records.list('v2_outcome_lineage')} == {previous}
    assert values.documents.markdown(previous) == BIRTH


def test_actual_final_sql_failure_rolls_back_delivery_and_replays_without_new_wire(scenario):
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    before = values.records.read('documents', previous)
    respond = values.models.handler

    def external_response(messages, **options):
        if _is_main(messages):
            return json.dumps({'type': 'complete', 'patches': [
                {'kind': 'update', 'path': ['项目方案', '实施记录'], 'body': '真实最终补丁。\n\n'}]}, ensure_ascii=False)
        return respond(messages, **options)

    values.models.handler = external_response
    with sqlite3.connect(values.records.database_path) as connection:
        connection.execute("CREATE TRIGGER reject_outcome_delivery BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='v2_outcome_lineage' AND json_extract(NEW.payload_json,'$.version')>1 "
            "BEGIN SELECT RAISE(ABORT, 'synthetic outcome delivery failure'); END")
    response = _continue(values, original, previous)
    _settled(values)
    identity = response['turn']['id']
    state = values.records.read('v2_task_executions', identity)
    kernel = state.payload['request']['turn_id']
    assert values.state.ai_runtime.receipt_for(kernel).status == 'completed'
    assert values.records.read('v2_turns', identity).payload['receipt']['do']['state'] == 'running'
    assert values.records.read('v2_turns', identity).payload['receipt']['do']['document_id'] is None
    assert values.records.read('v2_task_draft_operations', 'deliver-' + kernel) is None
    assert not [row for row in values.records.list('documents') if row.payload['type'] == 'agent-result-' + kernel]
    assert {row.object_id for row in values.records.list('v2_outcome_lineage')} == {previous}
    assert values.records.read('documents', previous) == before
    dispatches = len(values.models.calls)
    with sqlite3.connect(values.records.database_path) as connection:
        connection.execute('DROP TRIGGER reject_outcome_delivery')
    # 临时事实只让原持有者租期到期，恢复仍经原公开读取和真实完成事务。
    with values.records.begin() as tx:
        current = tx.read('v2_task_executions', identity)
        assert current == state and current.payload['owner'] is not None
        tx.put(current.collection, identity, {**current.payload,
            'claimed_at': (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()},
            expected_revision=current.revision)
        tx.commit()
    receipt = wait_product(values, response)['receipt']['do']
    assert receipt['state'] == 'done' and receipt['document_id'] != previous
    assert len(values.models.calls) == dispatches
    document = receipt['document_id']
    operation = values.records.read('v2_task_draft_operations', 'deliver-' + kernel)
    assert operation.payload['result']['document_id'] == document
    lineage = values.records.read('v2_outcome_lineage', document)
    assert lineage.payload['previous_id'] == previous and lineage.payload['version'] == 2
    assert receipt['continues'] == {'document_id': previous, 'version': 2}
    assert values.records.read('documents', previous) == before
