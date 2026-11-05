from copy import deepcopy
from types import SimpleNamespace

from core.ai_kernel.contracts import validate_model_call_receipt, validate_turn_action
from core.ai_kernel.product_admission import admission_identity


PROTOCOL = 'answer-legacy-continuation-v1'


def _terminal(store, request, sequence):
    events = [event for event in store.events_after(request['turn_id'])
        if event['sequence'] <= sequence and event['type'] in {'model.completed', 'model.failed'}
        and event['data'].get('model_call_purpose') == 'primary']
    if not events:
        return None
    event = events[-1]
    model = event['correlation']['model_request_id']
    if event['data'].get('receipt_ref') is None:
        return None
    receipt = validate_model_call_receipt(store.get(event['data']['receipt_ref']))
    if (receipt['turn_id'] != request['turn_id'] or receipt['model_request_id'] != model
            or (receipt['status'] != 'completed' if event['type'] == 'model.completed'
                else receipt['status'] not in {'failed', 'cancelled', 'timed_out'})):
        raise ValueError('answer_legacy_terminal_changed')
    binding = store.get_immutable_payload(request['turn_id'], 'answer-invocation-binding-' + model)
    closed = store.get_immutable_payload(request['turn_id'], 'product-answer-closed-' + model)
    if binding is not None:
        value = binding[1]
        if value.get('phase') is not None:
            return None
        actual, logical = value.get('invocation_key'), value.get('logical_invocation')
        if (value.get('turn_id') != request['turn_id'] or value.get('project_id') != request['scope']['project_id']
                or value.get('model_request_id') != model or value.get('request_identity') != admission_identity(request)
                or value.get('tool_call_id') != event['correlation']['tool_call_id']):
            raise ValueError('answer_legacy_invocation_changed')
    elif closed is not None:
        value = closed[1]
        actual = logical = value.get('stage')
        if (value.get('turn_id') != request['turn_id'] or value.get('project_id') != request['scope']['project_id']
                or value.get('question') != request['input']['text'] or value.get('model_request_id') != model
                or value.get('invocation_id') != event['correlation']['tool_call_id']):
            raise ValueError('answer_legacy_close_owner_changed')
    else:
        return None
    if any(not isinstance(value, str) or not value for value in (actual, logical)):
        raise ValueError('answer_legacy_logical_unavailable')
    sent = store.get_immutable_payload(request['turn_id'], 'answer-model-input-' + actual)
    if (sent is None or sent[1].get('purpose') != 'primary' or sent[1].get('phase') is not None
            or binding is not None and binding[1].get('input_ref') != sent[0]):
        raise ValueError('answer_legacy_input_changed')
    inherited = store.get_immutable_payload(request['turn_id'], 'answer-legacy-input-' + actual)
    if inherited is not None:
        if (inherited[1].get('actual_invocation') != actual or inherited[1].get('input_ref') != sent[0]
                or inherited[1].get('request_identity') != admission_identity(request)):
            raise ValueError('answer_legacy_input_owner_changed')
        logical = inherited[1]['logical_invocation']
    return {'actual_invocation': actual, 'logical_invocation': logical, 'input_ref': sent[0],
        'model_request_id': model, 'tool_call_id': event['correlation']['tool_call_id']}


def freeze_legacy_source(store, action, row, paused):
    action = validate_turn_action(deepcopy(action))
    request = store.get_request(row.object_id)
    terminal = _terminal(store, request, action['expected_sequence'])
    if (action['type'] != 'resume' or action['turn_id'] != row.object_id or paused['phase'] is not None
            or terminal is None or terminal['actual_invocation'] != paused['source_invocation']
            or terminal['input_ref'] != paused['input'][0]):
        raise ValueError('answer_legacy_resume_source_unavailable')
    if store.get_immutable_payload(row.object_id, PROTOCOL) is None:
        store.get_or_create_immutable_payload(row.object_id, PROTOCOL,
            {'request_identity': admission_identity(request), 'start_sequence': action['expected_sequence']})
    return store.get_or_create_immutable_payload(row.object_id, 'answer-legacy-source-' + action['action_id'],
        {'request_identity': admission_identity(request), 'action': action, 'terminal': terminal,
         'paused': {'object_id': row.object_id, 'revision': row.revision, 'payload': deepcopy(row.payload)}})


def _sources(store, turn_id):
    protocol = store.get_immutable_payload(turn_id, PROTOCOL)
    if protocol is None:
        return []
    request, value = store.get_request(turn_id), protocol[1]
    if (set(value) != {'request_identity', 'start_sequence'} or value['request_identity'] != admission_identity(request)
            or type(value['start_sequence']) is not int or value['start_sequence'] < 1):
        raise ValueError('answer_legacy_protocol_changed')
    result = []
    for event in store.events_after(turn_id):
        if event['type'] != 'turn.resumed' or event['sequence'] <= value['start_sequence']:
            continue
        action = validate_turn_action(store.get(event['data']['payload_ref']))
        if action['type'] != 'resume':
            continue
        if action['turn_id'] != turn_id or event['sequence'] != action['expected_sequence'] + 1:
            raise ValueError('answer_legacy_accepted_action_changed')
        terminal = _terminal(store, request, action['expected_sequence'])
        source = store.get_immutable_payload(turn_id, 'answer-legacy-source-' + action['action_id'])
        if terminal is None:
            if source is not None:
                raise ValueError('answer_legacy_terminal_unavailable')
            continue
        if source is None:
            raise ValueError('answer_legacy_source_unavailable')
        body = source[1]
        if (set(body) != {'request_identity', 'action', 'terminal', 'paused'} or body['action'] != action
                or body['terminal'] != terminal or body['request_identity'] != admission_identity(request)):
            raise ValueError('answer_legacy_source_changed')
        result.append((event, source))
    return result


def read_legacy_input(store, turn_id, invocation, *, seen=()):
    from .answer_effective import resolve_paused_answer_input, project_answer_messages, validate_paused_answer_publication
    from .kernel.policy_runtime import retry_policy_for
    intent = store.get_immutable_payload(turn_id, 'answer-legacy-input-' + invocation)
    if intent is None:
        return None
    if invocation in seen:
        raise ValueError('answer_legacy_input_cycle')
    request, value = store.get_request(turn_id), intent[1]
    fields = {'request_identity', 'actual_invocation', 'logical_invocation', 'source_ref', 'input_ref', 'base_ref',
        'resume_sequence', 'run_owner_id', 'run_generation'}
    if (set(value) != fields or value['request_identity'] != admission_identity(request)
            or value['actual_invocation'] != invocation or not isinstance(value['run_owner_id'], str)
            or not value['run_owner_id'] or type(value['run_generation']) is not int or value['run_generation'] < 1):
        raise ValueError('answer_legacy_intent_changed')
    source = store.get(value['source_ref'])
    action = source['action']
    events = [(event, proof) for event, proof in _sources(store, turn_id) if proof[0] == value['source_ref']]
    if len(events) != 1 or events[0][0]['sequence'] != value['resume_sequence']:
        raise ValueError('answer_legacy_action_unaccepted')
    row = SimpleNamespace(**source['paused'])
    paused = resolve_paused_answer_input(store, turn_id, row, _seen=(*seen, invocation))
    validate_paused_answer_publication(paused, row)
    if (source['terminal']['actual_invocation'] != paused['source_invocation']
            or source['terminal']['input_ref'] != paused['input'][0]
            or value['logical_invocation'] != source['terminal']['logical_invocation']
            or value['base_ref'] != paused['validation_input'][0] or value['resume_sequence'] != action['expected_sequence'] + 1):
        raise ValueError('answer_legacy_paused_input_changed')
    answer = row.payload['result']
    answer = answer.get('receipt', {}).get('ask', answer)
    messages = retry_policy_for(request)({'kind': 'continue_messages',
        'messages': paused['input'][1]['messages'], 'partial': answer.get('partial', '')})
    expected = {**paused['input'][1], 'messages': project_answer_messages(store, request, messages)}
    actual = store.get_immutable_payload(turn_id, 'answer-model-input-' + invocation)
    if actual is None or actual[0] != value['input_ref'] or actual[1] != expected:
        raise ValueError('answer_legacy_resume_input_changed')
    return {'intent': intent, 'input': actual, 'validation_input': paused['validation_input'], 'source': source}


def prepare_legacy_input(store, request, invocation, messages, schema, *, lease, action, row, material_proof=None):
    from .answer_effective import resolve_paused_answer_input, validate_paused_answer_publication, project_answer_messages
    from .kernel.policy_runtime import retry_policy_for
    if lease is None or lease.turn_id != request['turn_id'] or store.assert_active_run_lease(lease) is None:
        raise ValueError('answer_legacy_owner_unavailable')
    sources = [(event, source) for event, source in _sources(store, request['turn_id']) if source[1]['action'] == action]
    if len(sources) != 1:
        raise ValueError('answer_legacy_action_unaccepted')
    event, source = sources[0]
    paused = resolve_paused_answer_input(store, request['turn_id'], row)
    validate_paused_answer_publication(paused, row)
    if (source[1]['paused'] != {'object_id': row.object_id, 'revision': row.revision, 'payload': row.payload}
            or paused['input'][1]['response_schema'] != schema or invocation == source[1]['terminal']['logical_invocation']
            or set(paused['input'][1]) not in ({'messages', 'purpose', 'response_schema'}, {'messages', 'purpose', 'response_schema', 'material_proof'})
            or paused['input'][1].get('material_proof') != material_proof
            or 'material_proof' in paused['input'][1] and not isinstance(material_proof, dict)):
        raise ValueError('answer_legacy_schema_changed')
    public = row.payload['result']
    public = public.get('receipt', {}).get('ask', public)
    expected = retry_policy_for(request)({'kind': 'continue_messages',
        'messages': paused['input'][1]['messages'], 'partial': public.get('partial', '')})
    if messages != project_answer_messages(store, request, expected):
        raise ValueError('answer_legacy_resume_messages_changed')
    input_ref = store.get_or_create_immutable_payload(request['turn_id'], 'answer-model-input-' + invocation,
        {**deepcopy(paused['input'][1]), 'messages': deepcopy(messages)})
    store.get_or_create_immutable_payload(request['turn_id'], 'answer-legacy-input-' + invocation,
        {'request_identity': admission_identity(request), 'actual_invocation': invocation,
         'logical_invocation': source[1]['terminal']['logical_invocation'], 'source_ref': source[0],
         'input_ref': input_ref, 'base_ref': paused['validation_input'][0], 'resume_sequence': event['sequence'],
         'run_owner_id': lease.owner_id, 'run_generation': lease.generation})
    return read_legacy_input(store, request['turn_id'], invocation)


def resolve_legacy_answer(store, turn_id, logical, *, seen=()):
    from .answer_effective import resolve_answer_invocation, _completed_results
    sources = [(event, source) for event, source in _sources(store, turn_id) if source[1]['terminal']['logical_invocation'] == logical]
    if not sources:
        return None
    event, source = sources[-1]
    selected = store.get_immutable_payload(turn_id, 'answer-legacy-result-' + source[1]['action']['action_id'])
    if selected is None or set(selected[1]) != {'intent_ref', 'input_ref', 'result_ref'}:
        raise ValueError('answer_legacy_result_unavailable')
    intent = store.get(selected[1]['intent_ref'])
    actual_key = intent['actual_invocation']
    frozen = read_legacy_input(store, turn_id, actual_key, seen=seen)
    if frozen is None or frozen['intent'][0] != selected[1]['intent_ref'] or frozen['intent'][1]['source_ref'] != source[0]:
        raise ValueError('answer_legacy_result_intent_changed')
    actual = resolve_answer_invocation(store, turn_id, actual_key, _seen=seen, _phase_alias=False)
    if (actual['input'] is None or actual['result'] is None or actual['input'][0] != selected[1]['input_ref']
            or actual['result'][0] != selected[1]['result_ref']):
        raise ValueError('answer_legacy_result_changed')
    completed = _completed_results(store, tuple(store.events_after(turn_id)), actual['result'][1])
    if len(completed) != 1 or completed[0]['sequence'] <= event['sequence']:
        raise ValueError('answer_legacy_result_unproven')
    binding = store.get_immutable_payload(turn_id, 'answer-invocation-binding-' + completed[0]['correlation']['model_request_id'])
    final_key = actual['effective']['final_invocation'] if actual.get('effective') is not None else actual_key
    if (binding is None or binding[1].get('input_ref') != actual['input'][0]
            or binding[1].get('invocation_key') != final_key or binding[1].get('logical_invocation') != actual_key
            or binding[1].get('phase') is not None
            or binding[1].get('run_owner_id') != intent['run_owner_id'] or binding[1].get('run_generation') != intent['run_generation']
            or binding[1].get('request_identity') != admission_identity(store.get_request(turn_id))):
        raise ValueError('answer_legacy_result_owner_changed')
    return {**actual, 'validation_input': frozen['validation_input'], 'legacy_selection': selected,
        'metadata': {**actual['metadata'], 'legacy_continuation': True}}


def complete_legacy_answer(store, turn_id, invocation, *, lease, action):
    from .answer_effective import resolve_answer_invocation, _completed_results
    frozen = read_legacy_input(store, turn_id, invocation)
    if (frozen is None or frozen['source']['action'] != action or lease is None or lease.turn_id != turn_id
            or store.assert_active_run_lease(lease) is None or frozen['intent'][1]['run_owner_id'] != lease.owner_id
            or frozen['intent'][1]['run_generation'] != lease.generation):
        raise ValueError('answer_legacy_completion_owner_changed')
    actual = resolve_answer_invocation(store, turn_id, invocation, _phase_alias=False)
    if actual['result'] is None or len(_completed_results(store, tuple(store.events_after(turn_id)), actual['result'][1])) != 1:
        raise ValueError('answer_legacy_completion_unproven')
    result = store.get_or_create_immutable_payload(turn_id, 'answer-legacy-result-' + action['action_id'],
        {'intent_ref': frozen['intent'][0], 'input_ref': actual['input'][0], 'result_ref': actual['result'][0]})
    resolve_legacy_answer(store, turn_id, frozen['intent'][1]['logical_invocation'])
    return result
