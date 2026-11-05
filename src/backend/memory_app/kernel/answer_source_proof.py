import json

from backend.recognition import RecognitionConflict
from backend.recognition.external_turn_facts import completed_answer_facts
from core.ai_kernel.contracts import validate_model_call_receipt
from ..answer_proof import read_answer_input
from ..answer_effective import NativeAnswerSnapshot
from core.ai_kernel.product_snapshot import read_product_turn_snapshot


def qualify_no_match(reader, facts):
    from backend.recognition.external_turn_facts import _readonly, source_store
    from .route_source_snapshot import _closed_answer_fact
    request = facts['request']
    if facts['answer'].get('no_match') is not True:
        raise RecognitionConflict('saved_answer_no_match_unproven')
    with _readonly(source_store(reader).root / 'ai-turns.sqlite3') as connection:
        snapshot = read_product_turn_snapshot(connection, request['turn_id'])
        if (snapshot['request'] != request or snapshot['immutable'] != facts['immutable']
                or _closed_answer_fact(reader, request, connection) != 'completed'):
            raise RecognitionConflict('saved_answer_no_match_unproven')
        discarded_auxiliary_attempts(reader, request, connection, snapshot['events'], snapshot['attempts'])
    return True


def _payload(connection, identity, reference):
    row = connection.execute('SELECT payload_json FROM ai_turn_payloads WHERE turn_id=? AND payload_ref=?',
        (identity, reference)).fetchone()
    if row is None:
        raise RecognitionConflict('route_correction_native_unverifiable')
    return json.loads(row[0])


def _completed_product(records, request, connection, events, immutable):
    identity = request['turn_id']
    facts = completed_answer_facts(records, project_id=request['scope']['project_id'], turn_id=identity)
    if facts['request'] != request or facts['immutable'] != immutable:
        raise RecognitionConflict('route_correction_native_unverifiable')
    completed = [event for event in events if event['type'] == 'tool.completed'
        and event['data'].get('capability_id') == 'workbench.answer.execute'
        and event['data'].get('payload_ref') == facts['result_ref']]
    if len(completed) != 1:
        raise RecognitionConflict('route_correction_native_unverifiable')
    invocation = completed[0]['correlation']['tool_call_id']
    outcomes = [event for event in events if event['type'] == 'tool.outcome.recorded'
        and event['correlation']['tool_call_id'] == invocation]
    if len(outcomes) != 1:
        raise RecognitionConflict('route_correction_native_unverifiable')
    outcome = _payload(connection, identity, outcomes[0]['data']['payload_ref'])
    effect = connection.execute('SELECT turn_id,state,result_ref FROM effect WHERE operation_id=?',
        (invocation,)).fetchone()
    if (outcome.get('turn_id') != identity or outcome.get('invocation_id') != invocation
            or outcome.get('capability_id') != 'workbench.answer.execute' or outcome.get('status') != 'completed'
            or outcome.get('payload_ref') != facts['result_ref']
            or effect is None or tuple(effect) != (identity, 'SETTLED_OK', outcomes[0]['data']['payload_ref'])
            or not outcomes[0]['sequence'] < completed[0]['sequence'] < events[-1]['sequence']):
        raise RecognitionConflict('route_correction_native_unverifiable')
    return facts['result'], invocation


def _completed_model_outputs(request, connection, events, immutable):
    identity, consumed, retained, inputs = request['turn_id'], set(), set(), {}
    for kind, (_, output) in immutable.items():
        if not kind.startswith('answer-model-result-'):
            continue
        references = output.get('metadata', {}).get('kernel_receipt_refs')
        if not isinstance(references, list) or not references or len(set(references)) != len(references):
            raise RecognitionConflict('route_correction_native_unverifiable')
        matches = [event for event in events if event['type'] == 'model.completed'
            and event['data'].get('evidence_refs') == references]
        if len(matches) != 1:
            raise RecognitionConflict('route_correction_native_unverifiable')
        event = matches[0]
        receipt = validate_model_call_receipt(_payload(connection, identity, event['data']['receipt_ref']))
        if (receipt['turn_id'] != identity or receipt['model_request_id'] != event['correlation']['model_request_id']
                or receipt['status'] != 'completed'):
            raise RecognitionConflict('route_correction_native_unverifiable')
        consumed.add(receipt['model_request_id'])
        retained.update(references)
        prepared = immutable.get('answer-model-input-' + kind.removeprefix('answer-model-result-'))
        if prepared is not None:
            if prepared[1].get('purpose') != receipt.get('model_call_purpose'):
                raise RecognitionConflict('route_correction_native_unverifiable')
            inputs[prepared[0]] = receipt.get('model_call_purpose')
    return consumed, retained, inputs


def _adopted_intervention_attempts(snapshot, selected, consumed):
    from ..answer_effective import closed_steer_attempt_receipts, effective_answer
    if not snapshot['grants']:
        return set()
    if selected is None or selected[1].get('purpose') != 'primary':
        raise RecognitionConflict('route_correction_source_unfinished')
    native = NativeAnswerSnapshot(snapshot)
    identity = snapshot['request']['turn_id']
    closed = set(closed_steer_attempt_receipts(native, identity))
    rows = {row['payload_ref']: row for row in snapshot['steers']}
    adopted = set()
    for grant in snapshot['grants']:
        row = rows.get(grant['steer_ref'])
        source = native.get(grant['input_ref'])
        effective = effective_answer(native, identity, source['origin_invocation'])
        if row is None or row['state'] != 'released' or effective is None:
            raise RecognitionConflict('route_correction_source_unfinished')
        proof = effective['proof']
        inputs = [rows[batch[0]]['input_ref'] for batch in proof['batches']]
        if (grant['input_ref'] not in proof['continuation_inputs']
                or proof['model_request_ids'][-1] not in consumed
                or grant['model_request_id'] not in proof['model_request_ids'][1:]):
            raise RecognitionConflict('route_correction_source_unfinished')
        decision = native.get_immutable_payload(identity, 'turn-steer-answer-decision-'
            + proof['final_invocation'] + '-' + str(inputs.index(grant['input_ref'])))
        if decision != (row['decision_ref'], {'turn_id': identity, 'steer_input_ref': grant['input_ref'],
                'result_ref': effective['result'][0], 'model_request_id': proof['model_request_ids'][-1]}):
            raise RecognitionConflict('route_correction_source_unfinished')
        interruption = native.get(grant['interruption_ref'])
        # 仅对实际采用且已产出完整回答的续行保留旧尝试的未知账目。
        for attempt in snapshot['attempts']:
            if (attempt['model_request_id'] == interruption['model_request_id']
                    and attempt['terminal_ref'] in closed and attempt['effect_state'] == 'UNKNOWN'):
                adopted.add(attempt['attempt_id'])
    return adopted


def discarded_auxiliary_attempts(records, request, connection, events, attempts):
    # 只判断旧辅助请求是否进入已完成正文，未知效果与消费记录继续由原内核保留。
    identity = request['turn_id']
    if events[-1]['type'] != 'turn.completed':
        return set()
    try:
        immutable = {kind: (reference, json.loads(body)) for kind, reference, body in connection.execute(
            'SELECT kind,payload_ref,payload_json FROM ai_turn_immutable_payloads WHERE turn_id=?', (identity,))}
        product, invocation = _completed_product(records, request, connection, events, immutable)
        consumed, retained, inputs = _completed_model_outputs(request, connection, events, immutable)
        snapshot = read_product_turn_snapshot(connection, identity)
        if snapshot['request'] != request or snapshot['immutable'] != immutable or snapshot['events'] != list(events):
            raise RecognitionConflict('route_correction_native_unverifiable')
        selected = read_answer_input(NativeAnswerSnapshot(snapshot), identity)
        ask = product['receipt']['ask']
        if ask.get('no_match') is True:
            if (selected is not None or product.get('chosen') != [] or ask.get('citations') != []
                    or ask.get('answer') != '当前项目中没有匹配且可用于回答的资料。'
                    or any(event['type'] == 'model.requested'
                        and event['data'].get('model_call_purpose') == 'primary' for event in events)):
                raise RecognitionConflict('route_correction_native_unverifiable')
        elif (selected is None or selected[1].get('purpose') != 'primary' or inputs.get(selected[0]) != 'primary'
                or not consumed or selected[0] not in {reference for kind, (reference, _) in immutable.items()
                    if kind.startswith('answer-model-input-')}):
            raise RecognitionConflict('route_correction_native_unverifiable')
        discarded = _adopted_intervention_attempts(snapshot, selected, consumed)
        for attempt in attempts:
            if attempt['effect_state'] != 'UNKNOWN' or attempt['attempt_id'] in discarded:
                continue
            terminal = attempt['terminal']
            model = attempt['model_request_id']
            calls = [event for event in events if event['correlation'].get('model_request_id') == model]
            requested = [event for event in calls if event['type'] == 'model.requested']
            failed = [event for event in calls if event['type'] == 'model.failed']
            dispatched = [event for event in calls if event['type'] == 'model.attempt.dispatched'
                and event['data'].get('payload_ref') == attempt['dispatch_ref']]
            closed = [event for event in calls if event['type'] == 'model.attempt.terminal'
                and event['data'].get('receipt_ref') == attempt['terminal_ref']]
            if (terminal is None or terminal.get('status') != 'failed_transport' or model in consumed
                    or attempt['terminal_ref'] in retained
                    or len(requested) != 1 or requested[0]['data'].get('model_call_purpose') != 'aux'
                    or requested[0]['data'].get('capability_id') != 'workbench.answer.execute'
                    or requested[0]['correlation']['tool_call_id'] != invocation
                    or len(failed) != 1 or failed[0]['data'].get('model_call_purpose') != 'aux'
                    or failed[0]['correlation']['tool_call_id'] != invocation
                    or any(event['type'] == 'model.completed' for event in calls)
                    or len(dispatched) != 1 or len(closed) != 1
                    or not requested[0]['sequence'] < dispatched[0]['sequence'] < closed[0]['sequence']
                        < failed[0]['sequence'] < events[-1]['sequence']
                    or attempt['terminal_ref'] not in failed[0]['data'].get('evidence_refs', [])):
                raise RecognitionConflict('route_correction_source_unfinished')
            receipt = validate_model_call_receipt(_payload(connection, identity, failed[0]['data']['receipt_ref']))
            if (receipt['turn_id'] != identity or receipt['model_request_id'] != model
                    or receipt['status'] != 'failed' or receipt.get('model_call_purpose') != 'aux'):
                raise RecognitionConflict('route_correction_native_unverifiable')
            discarded.add(attempt['attempt_id'])
        return discarded
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
        raise RecognitionConflict('route_correction_native_unverifiable') from error
