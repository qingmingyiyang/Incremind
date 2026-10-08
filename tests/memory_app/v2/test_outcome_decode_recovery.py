"""真实 Main 的已付费用量、未知用量和运输及授权取消负控。"""
from contextlib import closing
from io import BytesIO
from pathlib import Path
import json
import sqlite3
from urllib.error import HTTPError

import pytest

from backend.memory_app.kernel.receipt_projection import kernel_call_groups
from backend.memory_app.v2.privacy import set_private_project
from tests.memory_app.v2.test_outcome_continuation import (
    BIRTH, test_actual_invalid_patch_retries_once_then_delivers_independent_new_root as original_invalid_patch)
from tests.memory_app.v2.test_outcome_continuation_boundaries import _continue, _is_main
from tests.memory_app.v2.test_outcome_redos import scenario, completed, wait_product, cancel_active_main
from tests.memory_app.v2.test_workbench_do import env as do_env


RAW_MARKER = 'R3_SYNTHETIC_RAW_BODY_MUST_NOT_APPEAR'
USAGE = {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}


def _receipts(values, kernel):
    path = Path(values.state.ai_turn_store._path).resolve()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        return [(kind, json.loads(raw)) for kind, raw in connection.execute(
            "SELECT kind,payload_json FROM ai_turn_payloads WHERE turn_id=? "
            "AND kind IN ('model-call-receipt','model-wire-attempt-receipt')", (kernel,))]


@pytest.mark.parametrize('unknown', [False, True])
def test_actual_decode_rejection_keeps_paid_or_unknown_usage_and_original_outcome_assertions(scenario, unknown):
    values = scenario
    if not unknown:
        # 原 True 的 JSON list、等待和全部业务断言直接执行，增加独立用量合同。
        original_invalid_patch(values, malformed=True)
        execution, = [row for row in values.records.list('v2_task_executions')
            if isinstance(row.payload.get('outcome_selection'), dict)
            and row.payload['outcome_selection'].get('mode') == 'continue']
        receipt = values.records.read('v2_turns', execution.object_id).payload['receipt']['do']
        attempts = 3
    else:
        original, delivered = completed(values, summary=BIRTH)
        previous = delivered['receipt']['do']['document_id']
        before = values.records.read('documents', previous)
        respond, main_calls = values.models.handler, []

        def external_response(messages, **options):
            if _is_main(messages):
                main_calls.append(messages)
                if len(main_calls) == 1:
                    return json.dumps([RAW_MARKER])
                return json.dumps({'type': 'complete', 'patches': [
                    {'kind': 'update', 'path': ['项目方案', '实施记录'], 'body': '真实新增正文。\n\n'}]},
                    ensure_ascii=False)
            return respond(messages, **options)

        values.models.handler = external_response
        transport = values.models._completion_fn

        def first_usage_unknown(**request):
            before_calls = len(main_calls)
            result = transport(**request)
            if before_calls == 0 and len(main_calls) == 1:
                def stream():
                    try:
                        for chunk in result:
                            yield {**chunk, 'usage': {}} if 'usage' in chunk else chunk
                    finally:
                        close = getattr(result, 'close', None)
                        if callable(close):
                            close()
                return stream()
            return result

        values.models._completion_fn = first_usage_unknown
        response = _continue(values, original, previous)
        receipt = wait_product(values, response)['receipt']['do']
        assert receipt['state'] == 'done' and len(main_calls) == 2, receipt
        assert receipt['fallback_new'] is False
        assert receipt['continues'] == {'document_id': previous, 'version': 2}
        assert values.records.read('documents', previous) == before
        assert values.documents.markdown(previous, revision=1) == BIRTH
        attempts = 2

    kernel = receipt['kernel_turn_id']
    payloads = _receipts(values, kernel)
    wires = [data for kind, data in payloads if kind == 'model-wire-attempt-receipt']
    assert len(wires) == len({wire['attempt_id'] for wire in wires}) == attempts
    assert {wire['attempt_number'] for wire in wires} == set(range(1, attempts + 1))
    identity, = {wire['model_request_id'] for wire in wires}
    assert all(wire['status'] == 'succeeded' for wire in wires)
    logical, = [data for kind, data in payloads
        if kind == 'model-call-receipt' and data['model_request_id'] == identity]
    call, = [call for group in kernel_call_groups(values.models.root, turn_id=kernel,
        project='project-a', remote_only=False, records=values.records) for call in group['calls']
        if call['model_request_id'] == identity]
    if unknown:
        assert sum(wire['usage'] is None for wire in wires) == 1
        assert next(wire for wire in wires if wire['attempt_number'] == 1)['usage_status'] == 'unavailable'
        assert next(wire for wire in wires if wire['attempt_number'] == 2)['usage'] == USAGE
        assert logical['usage'] is None and logical['usage_status'] == 'not_recorded'
        assert call['usage'] == USAGE and call['usage_status'] == 'partial' and call['cost'] is None
    else:
        assert all(wire['usage'] == USAGE and wire['usage_status'] == 'reported' for wire in wires)
        assert logical['usage'] == {'input_tokens': 12, 'output_tokens': 6, 'total_tokens': 18}
        assert logical['usage_status'] == 'recorded'
        assert call['usage'] == logical['usage'] and call['usage_status'] == 'recorded'
    assert values.state.ai_runtime.receipt_for(kernel).status == 'completed'
    assert RAW_MARKER not in json.dumps(payloads, ensure_ascii=False)


@pytest.mark.parametrize('failure', ['transport', 'private', 'configuration', 'cancel'])
def test_actual_decode_recovery_never_masks_provider_authority_or_cancel_failure(scenario, failure):
    values = scenario
    original, delivered = completed(values, summary=BIRTH)
    previous = delivered['receipt']['do']['document_id']
    before = values.records.read('documents', previous)
    respond, main_calls = values.models.handler, []
    if failure == 'cancel':
        values.block = True

    def external_response(messages, **options):
        if _is_main(messages):
            main_calls.append(messages)
            if failure == 'transport':
                raise HTTPError('https://example.test/v1/chat/completions', 403, RAW_MARKER,
                    {}, BytesIO(RAW_MARKER.encode()))
            if failure == 'private':
                set_private_project(values.records, 'project-a', True, 0)
            elif failure == 'configuration':
                values.models.update('generation', {'model': 'changed-model', 'expected_revision': 1})
            elif failure == 'cancel':
                respond(messages, **options)
            return json.dumps([RAW_MARKER])
        return respond(messages, **options)

    values.models.handler = external_response
    response = _continue(values, original, previous)
    turn = cancel_active_main(values, response) if failure == 'cancel' else wait_product(values, response)
    receipt = turn['receipt']['do']
    assert len(main_calls) == 1
    assert receipt['state'] == 'failed' and receipt['document_id'] is None
    assert receipt['fallback_new'] is False
    assert values.records.read('documents', previous) == before
    assert values.documents.markdown(previous, revision=1) == BIRTH
    assert {row.object_id for row in values.records.list('v2_outcome_lineage')} == {previous}
    kernel = receipt['kernel_turn_id']
    assert values.records.read('v2_task_draft_operations', 'deliver-' + kernel) is None
    assert values.state.ai_turn_store.get_immutable_payload(kernel, 'product-outcome-composition-v1') is None
    payloads = _receipts(values, kernel)
    wires = [data for kind, data in payloads if kind == 'model-wire-attempt-receipt']
    assert len(wires) == 1 and wires[0]['attempt_number'] == 1
    assert wires[0]['status'] == 'failed_transport'
    assert RAW_MARKER not in json.dumps(payloads, ensure_ascii=False)
    assert RAW_MARKER not in json.dumps(receipt, ensure_ascii=False)
    assert RAW_MARKER not in json.dumps(tuple(values.state.ai_turn_store.events_after(kernel)), ensure_ascii=False)
    if failure == 'cancel':
        assert values.state.ai_runtime.receipt_for(kernel).status == 'cancelled'
    elif failure == 'configuration':
        assert values.models.public()['generation']['revision'] == 2
