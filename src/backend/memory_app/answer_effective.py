from copy import deepcopy
import re
from types import SimpleNamespace
from urllib.parse import urlsplit

from core.ai_kernel.product_admission import admission_identity, MANIFEST_KIND
from core.ai_kernel.contracts import (
    validate_model_call_receipt, validate_model_wire_attempt_receipt, validate_model_wire_attempt_dispatch,
)
from .v2.policies import get


def execution_policy(steers):
    versions = {row['policy_version'] for row in steers}
    if len(versions) != 1 or not versions <= {'@2'}:
        raise ValueError('effective_answer_steer_policy_unavailable')
    return get('steer', version=next(iter(versions)))


def project_answer_messages(store, request, messages):
    manifest_ref = {'kind': 'session_event', 'object_id': request['turn_id'],
        'uri': f"crp://session/{request['turn_id']}/{MANIFEST_KIND}"}
    bundle = None
    if (store.get_immutable_payload(request['turn_id'], MANIFEST_KIND) is not None
            or manifest_ref in request.get('input', {}).get('refs', [])):
        bundle = store.restore_admission(request)
    markers = [ref for ref in request.get('input', {}).get('refs', []) if ref.get('kind') == 'session_event'
        and urlsplit(ref.get('uri', '')).path.startswith('/project-constraints/')]
    capsule = store.get_immutable_payload(request['turn_id'], 'project-constraints-v1')
    result = deepcopy(messages)
    if not markers and capsule is None:
        return result
    selected = [item for item in bundle.items if item.kind == 'project-constraints-v1'] if bundle is not None else []
    if (len(selected) != 1 or len(markers) != 1 or selected[0].ref != markers[0]
            or capsule is None or capsule[1] != selected[0].payload
            or set(capsule[1]) != {'native_turn_id', 'bundle'}
            or capsule[1]['native_turn_id'] != request['turn_id']):
        raise ValueError('effective_answer_constraints_unavailable')
    prompt = capsule[1]['bundle']['prompt']
    if not isinstance(prompt, str):
        raise ValueError('effective_answer_constraints_changed')
    if not prompt:
        return result
    block = {'role': 'system', 'content': prompt}
    prefix = get('constraint_scope', version='@1')().instruction_prefix
    existing = [row for row in result if row.get('role') == 'system'
        and isinstance(row.get('content'), str) and row['content'].startswith(prefix)]
    if existing:
        if existing != [block]:
            raise ValueError('effective_answer_constraints_changed')
        return result
    return [block, *result]


class NativeAnswerSnapshot:
    def __init__(self, snapshot):
        self.value = deepcopy(snapshot)
        self.turn_id = self.value['request']['turn_id']
        self.payloads = {reference: payload for reference, payload in self.value['immutable'].values()}
        if set(self.payloads) & set(self.value['payloads']):
            raise ValueError('effective_answer_payload_identity_ambiguous')
        self.payloads.update(self.value['payloads'])

    def get_request(self, turn_id):
        return deepcopy(self.value['request']) if turn_id == self.turn_id else None

    def get_immutable_payload(self, turn_id, kind):
        return deepcopy(self.value['immutable'].get(kind)) if turn_id == self.turn_id else None

    def get(self, reference):
        return deepcopy(self.payloads[reference])

    def events_after(self, turn_id, after_sequence=0):
        return tuple(deepcopy(event) for event in self.value['events']
            if event['turn_id'] == turn_id and event['sequence'] > after_sequence)

    def restore_admission(self, request):
        from core.ai_kernel.product_snapshot import restore_admission_snapshot
        if request != self.value['request']:
            raise ValueError('effective_answer_snapshot_request_changed')
        return restore_admission_snapshot(request, self.value['immutable'])

    def admission_base_refs(self, request):
        from core.ai_kernel.product_snapshot import admission_base_refs
        if request != self.value['request']:
            raise ValueError('effective_answer_snapshot_request_changed')
        return admission_base_refs(request, self.value['immutable'])

    def workbench_steers(self, public_turn_id):
        return tuple(deepcopy(row) for row in self.value['steers'] if row['payload']['public_turn_id'] == public_turn_id)

    def steer_model_grants(self, turn_id):
        return tuple(deepcopy(self.value['grants'])) if turn_id == self.turn_id else ()

    def model_attempt_bindings(self, turn_id):
        return tuple(deepcopy(self.value['attempts'])) if turn_id == self.turn_id else ()


def _completed_results(store, events, result):
    refs = result.get('metadata', {}).get('kernel_receipt_refs')
    if not isinstance(refs, list) or not refs or len(set(refs)) != len(refs):
        raise ValueError('effective_answer_result_receipts_unavailable')
    matches = [event for event in events if event['type'] == 'model.completed'
        and event['data'].get('receipt_ref') is not None and event['data'].get('evidence_refs') == refs]
    for event in matches:
        receipt = validate_model_call_receipt(store.get(event['data']['receipt_ref']))
        if (receipt.get('model_request_id') != event['correlation']['model_request_id']
                or receipt.get('turn_id') != event['turn_id'] or receipt.get('status') != 'completed'):
            raise ValueError('effective_answer_result_receipt_changed')
    return matches


def effective_answer(store, turn_id, invocation, *, _allow_missing=False):
    selected = store.get_immutable_payload(turn_id, 'answer-effective-' + invocation)
    if selected is None:
        if not _allow_missing:
            binding = store.get_immutable_payload(turn_id, 'workbench-steer-binding-v1')
            state = store.get_immutable_payload(turn_id, 'answer-steer-base-state-' + invocation)
            affected = state is not None
            if binding is not None:
                affected = affected or any(row['input_ref'] is not None and row['payload']['turn_id'] == turn_id
                    and store.get(row['input_ref']).get('origin_invocation') == invocation
                    for row in store.workbench_steers(binding[1]['public_turn_id']))
            if affected:
                raise ValueError('effective_answer_selector_unavailable')
        return None
    return _validate_effective(store, turn_id, invocation, selected)


def resolve_answer_invocation(store, turn_id, invocation, *, _seen=(), _phase_alias=True):
    if invocation in _seen:
        raise ValueError('answer_inheritance_cycle')
    if _phase_alias:
        from .answer_legacy import resolve_legacy_answer
        legacy = resolve_legacy_answer(store, turn_id, invocation, seen=_seen)
        if legacy is not None:
            return legacy
    known_input = store.get_immutable_payload(turn_id, 'answer-model-input-' + invocation)
    if (known_input is not None and known_input[1].get('phase') is not None
            and store.get_immutable_payload(turn_id, 'answer-phase-protocol-v1') is None):
        raise ValueError('answer_phase_protocol_unavailable')
    resumed = resolve_answer_phase(store, turn_id, invocation, _seen=_seen) if _phase_alias else None
    if resumed is not None:
        return resumed
    inherited = _answer_inheritance(store, turn_id, invocation, seen=(*_seen, invocation))
    selected = effective_answer(store, turn_id, invocation)
    if selected is not None:
        return {'input': selected['input'], 'result': selected['result'],
            'validation_input': inherited['validation_base'] if inherited is not None else selected['validation_input'],
            'effective': selected['proof'], 'metadata': aggregate_invocation_metadata(store, turn_id,
                selected['proof']['model_request_ids'], selected['result'][1]['metadata'])}
    actual = store.get_immutable_payload(turn_id, 'answer-model-input-' + invocation)
    result = store.get_immutable_payload(turn_id, 'answer-model-result-' + invocation)
    if inherited is not None and result is not None:
        completed = _completed_results(store, tuple(store.events_after(turn_id)), result[1])
        if len(completed) != 1:
            raise ValueError('answer_inheritance_result_unproven')
        model = completed[0]['correlation']['model_request_id']
        call = store.get_immutable_payload(turn_id, 'answer-invocation-binding-' + model)
        if (call is None or call[1].get('input_ref') != actual[0]
                or call[1].get('logical_invocation') != invocation or call[1].get('invocation_key') != invocation
                or call[1].get('phase') != actual[1].get('phase')
                or call[1].get('phase_logical') != actual[1].get('phase_logical')
                or call[1].get('run_owner_id') != inherited['proof']['run_owner_id']
                or call[1].get('run_generation') != inherited['proof']['run_generation']
                or call[1].get('request_identity') != admission_identity(store.get_request(turn_id))):
            raise ValueError('answer_inheritance_invocation_changed')
    return {'input': actual, 'result': result,
        'validation_input': inherited['validation_base'] if inherited is not None else actual,
        'effective': None, 'metadata': result[1].get('metadata') if result is not None else None}


def _answer_inheritance(store, turn_id, invocation, *, seen):
    capsule = store.get_immutable_payload(turn_id, 'answer-inherited-input-' + invocation)
    if capsule is None:
        if store.get_immutable_payload(turn_id, 'answer-inherited-base-' + invocation) is not None:
            raise ValueError('answer_inheritance_capsule_unavailable')
        return None
    value, request = capsule[1], store.get_request(turn_id)
    binding = store.get_immutable_payload(turn_id, 'workbench-steer-binding-v1')
    if request is None or binding is None:
        raise ValueError('answer_inheritance_owner_unavailable')
    store.restore_admission(request)
    fields = {'turn_id', 'project_id', 'public_turn_id', 'logical_invocation', 'request_identity', 'run_owner_id',
        'run_generation', 'boundary_sequence', 'base_ref', 'input_ref', 'sources', 'steer_refs', 'continuation'}
    if (set(value) != fields or value['turn_id'] != turn_id or value['project_id'] != request['scope']['project_id']
            or value['public_turn_id'] != binding[1]['public_turn_id'] or value['logical_invocation'] != invocation
            or value['request_identity'] != admission_identity(request)
            or not isinstance(value['run_owner_id'], str) or not value['run_owner_id']
            or type(value['run_generation']) is not int or value['run_generation'] < 1
            or type(value['boundary_sequence']) is not int or value['boundary_sequence'] < 1):
        raise ValueError('answer_inheritance_identity_changed')
    base = store.get_immutable_payload(turn_id, 'answer-inherited-base-' + invocation)
    actual = store.get_immutable_payload(turn_id, 'answer-model-input-' + invocation)
    if base is None or actual is None or base[0] != value['base_ref'] or actual[0] != value['input_ref']:
        raise ValueError('answer_inheritance_input_unavailable')
    if base[1].get('purpose') != 'primary' or project_answer_messages(store, request, base[1]['messages']) != base[1]['messages']:
        raise ValueError('answer_inheritance_base_changed')
    rows = {row['payload_ref']: row for row in store.workbench_steers(value['public_turn_id'])}
    allowed, origins = set(), set()
    if not isinstance(value['sources'], list):
        raise ValueError('answer_inheritance_sources_changed')
    for source in value['sources']:
        if (not isinstance(source, dict) or set(source) != {'origin_invocation', 'selector_ref', 'input_ref', 'result_ref', 'steer_refs'}
                or source['origin_invocation'] in origins):
            raise ValueError('answer_inheritance_source_changed')
        origin = source['origin_invocation']
        origins.add(origin)
        previous = resolve_answer_invocation(store, turn_id, origin, _seen=seen, _phase_alias=False)
        selector = store.get_immutable_payload(turn_id, 'answer-effective-' + origin)
        proof = previous['effective']
        if (proof is None or selector is None or source['selector_ref'] != selector[0]
                or previous['input'] is None or previous['result'] is None
                or source['input_ref'] != previous['input'][0] or source['result_ref'] != previous['result'][0]
                or source['steer_refs'] != [ref for batch in proof['batches'] for ref in batch]
                or proof['run_generation'] != value['run_generation'] or proof['run_owner_id'] != value['run_owner_id']):
            raise ValueError('answer_inheritance_source_proof_changed')
        completed = _completed_results(store, tuple(store.events_after(turn_id)), previous['result'][1])
        if len(completed) != 1 or completed[0]['sequence'] > value['boundary_sequence']:
            raise ValueError('answer_inheritance_source_order_changed')
        allowed.update(source['steer_refs'])
    continuation, paused = value['continuation'], None
    if continuation is not None:
        if (not isinstance(continuation, dict) or set(continuation) != {'paused', 'source_invocation', 'source_input_ref', 'resume'}
                or type(continuation['resume']) is not bool):
            raise ValueError('answer_inheritance_continuation_changed')
        frozen_row = continuation['paused']
        if (not isinstance(frozen_row, dict) or set(frozen_row) != {'object_id', 'revision', 'payload'}
                or type(frozen_row['revision']) is not int or frozen_row['revision'] < 1):
            raise ValueError('answer_inheritance_continuation_changed')
        paused = resolve_paused_answer_input(store, turn_id, SimpleNamespace(**frozen_row), _seen=seen)
        if paused['input'][0] != continuation['source_input_ref'] or paused['source_invocation'] != continuation['source_invocation']:
            raise ValueError('answer_inheritance_continuation_input_changed')
        allowed.update(paused.get('steer_refs', []))
    references = value['steer_refs']
    if (not isinstance(references, list) or (not references and continuation is None)
            or len(references) != len(set(references)) or set(references) != allowed
            or references != [ref for ref in rows if ref in allowed]):
        raise ValueError('answer_inheritance_steer_order_changed')
    facts = []
    for reference in references:
        row = rows.get(reference)
        if (row is None or row['payload']['turn_id'] != turn_id or row['input_ref'] is None
                or row['state'] not in {'injected', 'released', 'amended'}):
            raise ValueError('answer_inheritance_steer_unadopted')
        facts.append(row['payload'])
    if continuation is not None and continuation['resume']:
        from .kernel.policy_runtime import retry_policy_for
        public = continuation['paused']['payload']['result']
        public = public.get('receipt', {}).get('ask', public)
        partial = public.get('partial')
        if not isinstance(partial, str):
            raise ValueError('answer_inheritance_partial_unavailable')
        expected = retry_policy_for(request)({'kind': 'continue_messages', 'messages': paused['input'][1]['messages'], 'partial': partial})
        expected = project_answer_messages(store, request, expected)
        if (base[1]['messages'] != expected or base[1]['response_schema'] != paused['input'][1]['response_schema']
                or base[1].get('phase') != paused['phase'] or base[1].get('phase_logical') != paused['source_logical']):
            raise ValueError('answer_inheritance_resume_messages_changed')
        derived = deepcopy(base[1]['messages'])
    else:
        derived = execution_policy(facts).answer_messages(base[1]['messages'], facts)
    if actual[1] != {**base[1], 'messages': derived}:
        raise ValueError('answer_inheritance_messages_changed')
    return {'proof': value, 'base': base, 'input': actual, 'steer_refs': references,
        'validation_base': paused['validation_input'] if continuation is not None and continuation['resume'] else base}


def prepare_answer_inheritance(store, request, invocation, base, *, lease, adopted, continuation=None, resume=False, action=None):
    if lease is None or lease.turn_id != request['turn_id'] or store.assert_active_run_lease(lease) is None:
        raise ValueError('answer_inheritance_run_owner_unavailable')
    prior = store.get_immutable_payload(request['turn_id'], 'answer-inherited-input-' + invocation)
    if prior is not None:
        restored = _answer_inheritance(store, request['turn_id'], invocation, seen=(invocation,))
        if restored['base'][1] != base:
            raise ValueError('answer_inheritance_base_changed')
        if resume and base.get('phase') is not None:
            _freeze_phase_intent(store, request, invocation, restored, lease=lease, action=action)
        return restored['input'][1]
    sources, origins, references = [], set(), set()
    for row in adopted:
        source = store.get(row['input_ref'])
        origin = source.get('origin_invocation')
        if origin == invocation:
            raise ValueError('answer_inheritance_same_invocation')
        if not isinstance(origin, str):
            raise ValueError('answer_inheritance_source_unavailable')
        if origin in origins:
            continue
        origins.add(origin)
        previous = resolve_answer_invocation(store, request['turn_id'], origin, _phase_alias=False)
        selector = store.get_immutable_payload(request['turn_id'], 'answer-effective-' + origin)
        if previous['effective'] is None or selector is None or previous['result'] is None:
            raise ValueError('answer_inheritance_source_unavailable')
        refs = [ref for batch in previous['effective']['batches'] for ref in batch]
        sources.append({'origin_invocation': origin, 'selector_ref': selector[0],
            'input_ref': previous['input'][0], 'result_ref': previous['result'][0], 'steer_refs': refs})
        references.update(refs)
    continued = None
    if continuation is not None:
        paused = resolve_paused_answer_input(store, request['turn_id'], continuation)
        references.update(paused.get('steer_refs', []))
        if references or resume:
            continued = {'paused': {'object_id': continuation.object_id, 'revision': continuation.revision,
                'payload': deepcopy(continuation.payload)}, 'source_invocation': paused['source_invocation'],
                'source_input_ref': paused['input'][0], 'resume': resume}
    if resume and continued is None and references:
        raise ValueError('answer_inheritance_resume_source_unavailable')
    if not references and not resume:
        return deepcopy(base)
    rows = store.workbench_steers(store.get_immutable_payload(request['turn_id'], 'workbench-steer-binding-v1')[1]['public_turn_id'])
    ordered = [row for row in rows if row['payload_ref'] in references]
    messages = deepcopy(base['messages']) if resume else execution_policy([row['payload'] for row in ordered]).answer_messages(base['messages'], [row['payload'] for row in ordered])
    base_ref = store.get_or_create_immutable_payload(request['turn_id'], 'answer-inherited-base-' + invocation, base)
    actual = {**deepcopy(base), 'messages': messages}
    input_ref = store.get_or_create_immutable_payload(request['turn_id'], 'answer-model-input-' + invocation, actual)
    binding = store.get_immutable_payload(request['turn_id'], 'workbench-steer-binding-v1')[1]
    store.get_or_create_immutable_payload(request['turn_id'], 'answer-inherited-input-' + invocation,
        {'turn_id': request['turn_id'], 'project_id': request['scope']['project_id'],
         'public_turn_id': binding['public_turn_id'], 'logical_invocation': invocation,
         'request_identity': admission_identity(request), 'run_owner_id': lease.owner_id, 'run_generation': lease.generation,
         'boundary_sequence': tuple(store.events_after(request['turn_id']))[-1]['sequence'],
         'base_ref': base_ref, 'input_ref': input_ref, 'sources': sources,
         'steer_refs': [row['payload_ref'] for row in ordered], 'continuation': continued})
    restored = _answer_inheritance(store, request['turn_id'], invocation, seen=(invocation,))
    if resume and base.get('phase') is not None:
        _freeze_phase_intent(store, request, invocation, restored, lease=lease, action=action)
    return restored['input'][1]


def _phase_primary_binding(store, request, sequence):
    terminals = [event for event in store.events_after(request['turn_id'])
        if event['sequence'] <= sequence and event['type'] in {'model.completed', 'model.failed'}
        and event['data'].get('model_call_purpose') == 'primary']
    if not terminals:
        return None
    event = terminals[-1]
    model = event['correlation']['model_request_id']
    receipt = validate_model_call_receipt(store.get(event['data']['receipt_ref']))
    if (receipt['turn_id'] != request['turn_id'] or receipt['model_request_id'] != model
            or (receipt['status'] != 'completed' if event['type'] == 'model.completed'
                else receipt['status'] not in {'failed', 'cancelled', 'timed_out'})):
        raise ValueError('answer_phase_terminal_owner_changed')
    binding = store.get_immutable_payload(request['turn_id'], 'answer-invocation-binding-' + model)
    if binding is None or binding[1].get('phase') is None:
        return None
    value = binding[1]
    actual = store.get_immutable_payload(request['turn_id'], 'answer-model-input-' + value['invocation_key'])
    if (value.get('turn_id') != request['turn_id'] or value.get('project_id') != request['scope']['project_id']
            or value.get('model_request_id') != model or value.get('tool_call_id') != event['correlation']['tool_call_id']
            or value.get('request_identity') != admission_identity(request) or actual is None
            or actual[0] != value.get('input_ref') or value['phase'] not in {'single', 'first', 'final-review'}
            or actual[1].get('phase') != value['phase'] or actual[1].get('phase_logical') != value.get('phase_logical')
            or not isinstance(value.get('phase_logical'), str) or not value['phase_logical']):
        raise ValueError('answer_phase_terminal_binding_changed')
    return value


def freeze_answer_phase_source(store, action, row):
    from core.ai_kernel.contracts import validate_turn_action
    action = validate_turn_action(deepcopy(action))
    if action['type'] != 'resume' or action['turn_id'] != row.object_id:
        raise ValueError('answer_phase_action_changed')
    source = resolve_paused_answer_input(store, row.object_id, row)
    validate_paused_answer_publication(source, row)
    if source['phase'] is None:
        from .answer_legacy import freeze_legacy_source
        return freeze_legacy_source(store, action, row, source)
    request = store.get_request(row.object_id)
    if store.get_immutable_payload(row.object_id, 'workbench-steer-binding-v1') is None:
        raise ValueError('answer_phase_native_identity_unavailable')
    store.restore_admission(request)
    binding = _phase_primary_binding(store, request, action['expected_sequence'])
    if (binding is None or binding['phase'] != source['phase'] or binding['phase_logical'] != source['source_logical']
            or binding['input_ref'] != source['input'][0]):
        raise ValueError('answer_phase_paused_binding_changed')
    return store.get_or_create_immutable_payload(row.object_id, 'answer-phase-source-' + action['action_id'],
        {'request_identity': admission_identity(request), 'action': action,
         'paused': {'object_id': row.object_id, 'revision': row.revision, 'payload': deepcopy(row.payload)},
         'phase': source['phase'], 'phase_logical': source['source_logical'],
         'source_invocation': source['source_invocation'], 'source_input_ref': source['input'][0],
         'model_request_id': binding['model_request_id']})


def _answer_phase_sources(store, turn_id):
    from core.ai_kernel.contracts import validate_turn_action
    request = store.get_request(turn_id)
    protocol = store.get_immutable_payload(turn_id, 'answer-phase-protocol-v1')
    if protocol is None:
        return []
    if protocol[1] != {'schema_version': '1.0.0', 'turn_id': turn_id, 'request_identity': admission_identity(request)}:
        raise ValueError('answer_phase_protocol_changed')
    result = []
    for event in store.events_after(turn_id):
        if event['type'] != 'turn.resumed':
            continue
        action = validate_turn_action(store.get(event['data']['payload_ref']))
        if action['turn_id'] != turn_id:
            raise ValueError('answer_phase_accepted_action_changed')
        if action['type'] != 'resume':
            continue
        if event['sequence'] != action['expected_sequence'] + 1:
            raise ValueError('answer_phase_accepted_action_changed')
        binding = _phase_primary_binding(store, request, action['expected_sequence'])
        source = store.get_immutable_payload(turn_id, 'answer-phase-source-' + action['action_id'])
        if binding is None:
            if source is not None:
                raise ValueError('answer_phase_source_binding_unavailable')
            continue
        if source is None:
            raise ValueError('answer_phase_source_unavailable')
        if store.get_immutable_payload(turn_id, 'workbench-steer-binding-v1') is None:
            raise ValueError('answer_phase_native_identity_unavailable')
        store.restore_admission(request)
        value = source[1]
        fields = {'request_identity', 'action', 'paused', 'phase', 'phase_logical', 'source_invocation', 'source_input_ref', 'model_request_id'}
        if (set(value) != fields or value['request_identity'] != admission_identity(request) or value['action'] != action
                or value['phase'] != binding['phase'] or value['phase_logical'] != binding['phase_logical']
                or value['model_request_id'] != binding['model_request_id'] or value['source_input_ref'] != binding['input_ref']):
            raise ValueError('answer_phase_source_changed')
        paused = resolve_paused_answer_input(store, turn_id, SimpleNamespace(**value['paused']))
        if (paused['phase'] != value['phase'] or paused['source_logical'] != value['phase_logical']
                or paused['source_invocation'] != value['source_invocation'] or paused['input'][0] != value['source_input_ref']):
            raise ValueError('answer_phase_source_input_changed')
        result.append((event, source))
    return result


def _freeze_phase_intent(store, request, invocation, inherited, *, lease, action):
    if action is None:
        raise ValueError('answer_phase_resume_action_unavailable')
    sources = [(event, source) for event, source in _answer_phase_sources(store, request['turn_id']) if source[1]['action'] == action]
    if len(sources) != 1:
        raise ValueError('answer_phase_action_not_accepted')
    event, source = sources[0]
    value = source[1]
    if (inherited['proof']['continuation'] is None or inherited['proof']['continuation']['paused'] != value['paused']
            or inherited['input'][1].get('phase') != value['phase']
            or inherited['input'][1].get('phase_logical') != value['phase_logical'] or invocation == value['phase_logical']):
        raise ValueError('answer_phase_resume_input_changed')
    return store.get_or_create_immutable_payload(request['turn_id'], 'answer-phase-intent-' + action['action_id'],
        {'request_identity': admission_identity(request), 'action_id': action['action_id'], 'source_ref': source[0],
         'resume_sequence': event['sequence'], 'phase': value['phase'], 'phase_logical': value['phase_logical'],
         'actual_invocation': invocation, 'input_ref': inherited['input'][0], 'base_ref': inherited['validation_base'][0],
         'run_owner_id': lease.owner_id, 'run_generation': lease.generation})


def resolve_answer_phase(store, turn_id, logical, *, _seen=()):
    sources = [(event, source) for event, source in _answer_phase_sources(store, turn_id) if source[1]['phase_logical'] == logical]
    if not sources:
        return None
    event, source = sources[-1]
    action = source[1]['action']
    intent = store.get_immutable_payload(turn_id, 'answer-phase-intent-' + action['action_id'])
    selected = store.get_immutable_payload(turn_id, 'answer-phase-result-' + action['action_id'])
    if intent is None or selected is None:
        raise ValueError('answer_phase_result_unavailable')
    value = intent[1]
    fields = {'request_identity', 'action_id', 'source_ref', 'resume_sequence', 'phase', 'phase_logical',
        'actual_invocation', 'input_ref', 'base_ref', 'run_owner_id', 'run_generation'}
    if (set(value) != fields or value.get('source_ref') != source[0] or value.get('action_id') != action['action_id']
            or value.get('resume_sequence') != event['sequence'] or value.get('phase_logical') != logical
            or value.get('phase') != source[1]['phase'] or value.get('actual_invocation') == logical
            or not isinstance(value.get('actual_invocation'), str) or not value['actual_invocation']
            or value.get('request_identity') != admission_identity(store.get_request(turn_id))):
        raise ValueError('answer_phase_intent_changed')
    actual = resolve_answer_invocation(store, turn_id, value['actual_invocation'], _seen=_seen, _phase_alias=False)
    inherited = _answer_inheritance(store, turn_id, value['actual_invocation'], seen=(*_seen, value['actual_invocation']))
    if (inherited is None or inherited['proof']['continuation'] is None
            or inherited['proof']['continuation']['paused'] != source[1]['paused']
            or inherited['input'][0] != value.get('input_ref') or inherited['validation_base'][0] != value.get('base_ref')
            or inherited['proof']['run_owner_id'] != value.get('run_owner_id')
            or inherited['proof']['run_generation'] != value.get('run_generation')
            or actual['result'] is None or actual['input'] is None
            or selected[1] != {'intent_ref': intent[0], 'input_ref': actual['input'][0], 'result_ref': actual['result'][0]}):
        raise ValueError('answer_phase_result_changed')
    completed = _completed_results(store, tuple(store.events_after(turn_id)), actual['result'][1])
    if len(completed) != 1 or completed[0]['sequence'] <= event['sequence']:
        raise ValueError('answer_phase_result_unproven')
    return {**actual, 'validation_input': inherited['validation_base'], 'phase_selection': selected}


def complete_answer_phase(store, turn_id, invocation, *, lease, action):
    if action is None:
        return None
    intent = store.get_immutable_payload(turn_id, 'answer-phase-intent-' + action['action_id'])
    if intent is None:
        return None
    if lease is None or lease.turn_id != turn_id or store.assert_active_run_lease(lease) is None:
        raise ValueError('answer_phase_completion_owner_unavailable')
    if intent[1]['actual_invocation'] != invocation:
        return None
    actual = resolve_answer_invocation(store, turn_id, invocation, _phase_alias=False)
    if actual['input'] is None or actual['result'] is None:
        raise ValueError('answer_phase_completion_unavailable')
    completed = _completed_results(store, tuple(store.events_after(turn_id)), actual['result'][1])
    if len(completed) != 1:
        raise ValueError('answer_phase_completion_unproven')
    reference = store.get_or_create_immutable_payload(turn_id, 'answer-phase-result-' + action['action_id'],
        {'intent_ref': intent[0], 'input_ref': actual['input'][0], 'result_ref': actual['result'][0]})
    resolve_answer_phase(store, turn_id, intent[1]['phase_logical'])
    return reference


def validate_paused_answer_publication(source, row):
    from .structured_generation import AskOutput
    answer = row.payload['result']
    answer = answer.get('receipt', {}).get('ask', answer)
    partial = answer.get('partial', '')
    if not isinstance(partial, str):
        raise ValueError('answer_publication_partial_changed')
    if partial and source['input'][1]['response_schema'] != AskOutput.model_json_schema():
        raise ValueError('answer_publication_partial_schema_unsupported')


def project_answer_output(store, turn_id, logical, output):
    from .structured_generation import AskOutput
    actual = resolve_answer_invocation(store, turn_id, logical)
    if actual['result'] is None or actual['input'] is None or actual['result'][1].get('output') != output:
        raise ValueError('answer_publication_actual_output_changed')
    selected = actual.get('phase_selection') or actual.get('legacy_selection')
    if selected is None:
        return deepcopy(output)
    intent = store.get(selected[1]['intent_ref'])
    source = store.get(intent['source_ref'])
    paused = resolve_paused_answer_input(store, turn_id, SimpleNamespace(**source['paused']))
    answer = source['paused']['payload']['result']
    answer = answer.get('receipt', {}).get('ask', answer)
    partial = answer.get('partial', '')
    if not isinstance(partial, str):
        raise ValueError('answer_publication_partial_changed')
    if not partial:
        return deepcopy(output)
    if (actual['input'][1]['response_schema'] != AskOutput.model_json_schema()
            or paused['input'][1]['response_schema'] != AskOutput.model_json_schema()):
        raise ValueError('answer_publication_partial_schema_unsupported')
    suffix = AskOutput.model_validate(output)
    if suffix.model_dump() != output:
        raise ValueError('answer_publication_suffix_changed')
    from .answer_proof import _quote_plan
    capsule = store.get_immutable_payload(turn_id, 'answer-continuation-plan-v1')
    plan = _quote_plan(store.get_request(turn_id), capsule[1] if capsule else None)
    prefix_numbers = [int(left or right) for left, right in re.findall(r'\[(\d+)\]|【(\d+)】', partial)]
    numbers = list(dict.fromkeys([*prefix_numbers, *suffix.citations]))
    if any(type(number) is not int or not 1 <= number <= len(plan['chosen']) for number in numbers):
        raise ValueError('answer_publication_partial_citation_changed')
    return {'answer': partial + suffix.answer, 'citations': numbers}


def aggregate_invocation_metadata(store, turn_id, request_ids, metadata):
    from .v2.followup import sum_usage
    events = [event for event in store.events_after(turn_id) if event['correlation']['model_request_id'] in request_ids]
    dispatch = [event for event in events if event['type'] == 'model.attempt.dispatched']
    terminals = [event['data']['receipt_ref'] for event in events if event['type'] == 'model.attempt.terminal']
    observations = [(validate_model_wire_attempt_receipt(store.get(reference)).get('usage') or {}) for reference in terminals]
    usage = sum_usage(*observations)
    if (not observations or len(dispatch) != len(terminals)
            or any(any(type(value.get(key)) is not int or value[key] < 0
                for key in ('input_tokens', 'output_tokens', 'total_tokens')) for value in observations)):
        usage['observed_only'] = True
    refs = [event['data']['receipt_ref'] for event in events
        if event['type'] in {'model.attempt.terminal', 'model.completed', 'model.failed'} and event['data']['receipt_ref']]
    return {**metadata, 'usage': usage,
        'kernel_receipt_refs': list(dict.fromkeys([*refs, *metadata.get('kernel_receipt_refs', [])]))}


def native_answer_usage(store, turn_id, *, purpose='primary'):
    from .v2.followup import sum_usage
    request = store.get_request(turn_id)
    if request is None or request['desired_outcome'] != 'project.answer' or purpose not in {'primary', 'aux'}:
        raise ValueError('answer_usage_native_owner_changed')
    events = tuple(store.events_after(turn_id))
    identities = [event['correlation']['model_request_id'] for event in events
        if event['type'] == 'model.requested' and event['data'].get('model_call_purpose') == purpose]
    if len(identities) != len(set(identities)):
        raise ValueError('answer_usage_request_identity_duplicated')
    calls = [event for event in events if event['correlation']['model_request_id'] in identities]
    dispatches = [event for event in calls if event['type'] == 'model.attempt.dispatched']
    references = [event['data']['receipt_ref'] for event in calls if event['type'] == 'model.attempt.terminal']
    if len(references) != len(set(references)):
        raise ValueError('answer_usage_wire_receipt_duplicated')
    receipts = [validate_model_wire_attempt_receipt(store.get(reference)) for reference in references]
    if (any(receipt['turn_id'] != turn_id or receipt['model_request_id'] not in identities for receipt in receipts)
            or len({receipt['attempt_id'] for receipt in receipts}) != len(receipts)):
        raise ValueError('answer_usage_wire_owner_changed')
    terminal_models = {event['correlation']['model_request_id'] for event in calls
        if event['type'] in {'model.completed', 'model.failed'}}
    usage = sum_usage(*(receipt.get('usage') or {} for receipt in receipts))
    complete = not identities or (terminal_models == set(identities) and bool(dispatches) and len(dispatches) == len(receipts)
        and all(all(type((receipt.get('usage') or {}).get(key)) is int and receipt['usage'][key] >= 0
            for key in ('input_tokens', 'output_tokens', 'total_tokens')) for receipt in receipts))
    if not complete:
        usage['observed_only'] = True
    return {'usage': usage, 'complete': complete, 'model_request_ids': identities, 'receipt_ids': references}


def closed_steer_attempt_receipts(store, turn_id):
    request = store.get_request(turn_id)
    binding = store.get_immutable_payload(turn_id, 'workbench-steer-binding-v1')
    if request is None or binding is None:
        return ()
    store.restore_admission(request)
    rows = {row['payload_ref']: row for row in store.workbench_steers(binding[1]['public_turn_id'])}
    events, closed = tuple(store.events_after(turn_id)), []
    for grant in store.steer_model_grants(turn_id):
        row = rows.get(grant['steer_ref'])
        source, proof = store.get(grant['input_ref']), store.get(grant['interruption_ref'])
        if (row is None or row['payload']['turn_id'] != turn_id or row['input_ref'] != grant['input_ref']
                or row['state'] not in {'injected', 'released', 'amended'}
                or grant['public_turn_id'] != binding[1]['public_turn_id']
                or source.get('turn_id') != turn_id or source.get('project_id') != request['scope']['project_id']
                or source.get('interruption_ref') != grant['interruption_ref']
                or source.get('model_invocation_key') != grant['invocation_key']
                or grant['steer_ref'] not in source.get('steer_refs', [])
                or proof.get('turn_id') != turn_id or proof.get('provider_closed') is not True
                or source.get('run_generation') != proof.get('run_generation')
                or source.get('run_owner_id') != proof.get('run_owner_id')
                or row['payload']['lease'] != {'owner_id': proof.get('run_owner_id'), 'generation': proof.get('run_generation')}
                or type(proof.get('transport_generation')) is not int or proof['transport_generation'] < 1):
            raise ValueError('steer_closed_attempt_binding_changed')
        if (store.get_immutable_payload(turn_id, 'turn-steer-interruption-' + proof['model_request_id']) != (grant['interruption_ref'], proof)
                or store.get_immutable_payload(turn_id, 'turn-steer-input-' + source['invocation']) != (grant['input_ref'], source)):
            raise ValueError('steer_closed_attempt_capsule_changed')
        if sum(event['type'] == 'turn.steer.applied' and grant['input_ref'] in event['data'].get('evidence_refs', []) for event in events) != 1:
            raise ValueError('steer_closed_attempt_input_unadopted')
        calls = [event for event in events if event['correlation']['model_request_id'] == proof['model_request_id']]
        requested = [event for event in calls if event['type'] == 'model.requested']
        failed = [event for event in calls if event['type'] == 'model.failed']
        if (len(requested) != 1 or requested[0]['data'].get('model_call_purpose') != 'primary'
                or requested[0]['correlation']['tool_call_id'] != grant['tool_invocation_id'] or len(failed) != 1):
            raise ValueError('steer_closed_attempt_owner_changed')
        receipts = [event['data']['receipt_ref'] for event in calls if event['type'] == 'model.attempt.terminal']
        if (len(receipts) != sum(event['type'] == 'model.attempt.dispatched' for event in calls)
                or receipts != proof.get('wire_receipt_refs')):
            raise ValueError('steer_closed_attempt_settlement_changed')
        attempts = [value for value in store.model_attempt_bindings(turn_id) if value['model_request_id'] == proof['model_request_id']]
        if len(attempts) != len(receipts) or not attempts:
            raise ValueError('steer_closed_attempt_reservations_unavailable')
        for number, attempt in enumerate(attempts, 1):
            if (attempt['attempt_number'] != number or attempt['status'] != 'terminal'
                    or attempt['terminal_ref'] != receipts[number - 1]
                    or attempt['lease_owner_id'] != proof['run_owner_id'] or attempt['lease_generation'] != proof['run_generation']):
                raise ValueError('steer_closed_attempt_lease_changed')
            dispatched = validate_model_wire_attempt_dispatch(attempt['dispatch'])
            terminal = validate_model_wire_attempt_receipt(attempt['terminal'])
            expected_effect = 'SETTLED_OK' if terminal['status'] == 'succeeded' else 'UNKNOWN'
            if (attempt['effect_state'] != expected_effect or terminal['attempt_id'] != attempt['attempt_id']
                    or terminal['model_request_id'] != proof['model_request_id'] or terminal['turn_id'] != turn_id
                    or terminal['attempt_number'] != number or any(dispatched.get(key) != terminal.get(key) for key in
                        ('attempt_id', 'attempt_number', 'turn_id', 'model_request_id', 'routing_snapshot_revision', 'provider_id', 'model_id'))):
                raise ValueError('steer_closed_attempt_effect_changed')
        for reference in receipts:
            receipt = validate_model_wire_attempt_receipt(store.get(reference))
            if receipt['turn_id'] != turn_id or receipt['model_request_id'] != proof['model_request_id']:
                raise ValueError('steer_closed_attempt_receipt_changed')
            closed.append(reference)
    return tuple(dict.fromkeys(closed))


def _validate_effective(store, turn_id, invocation, selected, *, paused_request=None):
    value, request = selected[1], store.get_request(turn_id)
    if request is None:
        raise ValueError('effective_answer_request_unavailable')
    store.restore_admission(request)
    binding = store.get_immutable_payload(turn_id, 'workbench-steer-binding-v1')
    if (binding is None or value.get('turn_id') != turn_id or value.get('origin_invocation') != invocation
            or value.get('public_turn_id') != binding[1]['public_turn_id'] or value.get('request_identity') != admission_identity(request)):
        raise ValueError('effective_answer_binding_changed')
    base = store.get_immutable_payload(turn_id, 'answer-base-input-' + invocation)
    actual = store.get_immutable_payload(turn_id, 'answer-model-input-' + value['final_invocation'])
    result = store.get_immutable_payload(turn_id, 'answer-model-result-' + value['final_invocation'])
    if (base is None or actual is None or (paused_request is None and result is None) or value.get('base_ref') != base[0]
            or value.get('input_ref') != actual[0] or (paused_request is None and value.get('result_ref') != result[0])):
        raise ValueError('effective_answer_proof_unavailable')
    rows = {row['payload_ref']: row for row in store.workbench_steers(binding[1]['public_turn_id'])}
    events = tuple(store.events_after(turn_id))
    applied = {reference for event in events if event['type'] == 'turn.steer.applied' for reference in event['data'].get('evidence_refs', [])}
    messages, seen, flat, derived = deepcopy(base[1]['messages']), set(), [], {}
    batches = value.get('batches')
    if not isinstance(batches, list) or not batches:
        raise ValueError('effective_answer_applied_chain_unavailable')
    for batch in batches:
        if not isinstance(batch, list) or not batch or len(set(batch)) != len(batch):
            raise ValueError('effective_answer_applied_chain_changed')
        facts = []
        for reference in batch:
            row = rows.get(reference)
            if (reference in seen or row is None or row['state'] not in {'injected', 'released', 'amended'}
                    or row['input_ref'] not in applied or row['payload']['turn_id'] != turn_id
                    or row['payload']['lease'] != {'owner_id': value.get('run_owner_id'), 'generation': value.get('run_generation')}
                    or sum(event['type'] == 'turn.steer.applied' and row['input_ref'] in event['data'].get('evidence_refs', []) for event in events) != 1):
                raise ValueError('effective_answer_applied_chain_changed')
            adopted = store.get(row['input_ref'])
            if (not isinstance(adopted, dict) or adopted.get('turn_id') != turn_id
                    or adopted.get('project_id') != request['scope']['project_id']
                    or adopted.get('run_generation') != value.get('run_generation')
                    or adopted.get('run_owner_id') != value.get('run_owner_id')
                    or adopted.get('steer_refs', []).count(reference) != 1):
                raise ValueError('effective_answer_adopted_input_changed')
            facts.append(row['payload'])
            seen.add(reference)
            flat.append(reference)
        messages = execution_policy(facts).answer_messages(messages, facts)
        derived[tuple(batch)] = deepcopy(messages)
    if flat != [reference for reference in rows if reference in seen]:
        raise ValueError('effective_answer_adoption_order_changed')
    if actual[1] != {**base[1], 'messages': messages}:
        raise ValueError('effective_answer_input_changed')
    if project_answer_messages(store, request, base[1]['messages']) != base[1]['messages']:
        raise ValueError('effective_answer_base_constraints_changed')
    requests = value.get('model_request_ids')
    if (not isinstance(requests, list) or not requests or len(set(requests)) != len(requests)
            or (paused_request is None and [event['correlation']['model_request_id'] for event in _completed_results(store, events, result[1])] != [requests[-1]])
            or (paused_request is not None and requests[-1] != paused_request)):
        raise ValueError('effective_answer_result_unproven')
    requested = []
    for identity in requests:
        calls = [event for event in events if event['type'] == 'model.requested' and event['correlation']['model_request_id'] == identity]
        if len(calls) != 1 or calls[0]['data'].get('model_call_purpose') != 'primary':
            raise ValueError('effective_answer_invocation_chain_changed')
        requested.append(calls[0]['correlation']['tool_call_id'])
    if len(set(requested)) != 1:
        raise ValueError('effective_answer_invocation_owner_changed')
    input_binding = store.get_immutable_payload(turn_id, 'answer-invocation-binding-' + requests[-1])
    phase = actual[1].get('phase')
    if phase is not None and phase not in {'single', 'first', 'final-review'}:
        raise ValueError('effective_answer_phase_changed')
    if phase is not None and (not isinstance(actual[1].get('phase_logical'), str) or not actual[1]['phase_logical']):
        raise ValueError('effective_answer_phase_logical_changed')
    if (input_binding is None or input_binding[1] != {'turn_id': turn_id, 'project_id': request['scope']['project_id'],
            'model_request_id': requests[-1], 'invocation_key': value['final_invocation'], 'logical_invocation': invocation,
            'tool_call_id': requested[-1], 'input_ref': actual[0], 'run_owner_id': value['run_owner_id'],
            'run_generation': value['run_generation'], 'request_identity': admission_identity(request),
            **({'phase': phase, 'phase_logical': actual[1]['phase_logical']} if phase is not None else {})}):
        raise ValueError('effective_answer_actual_invocation_input_changed')
    continuation = value.get('continuation_inputs')
    if not isinstance(continuation, list) or len(continuation) != len(requests) - 1:
        raise ValueError('effective_answer_continuation_chain_changed')
    grants = store.steer_model_grants(turn_id)
    qualified_closed = set(closed_steer_attempt_receipts(store, turn_id))
    for index, reference in enumerate(continuation):
        consumed = store.get(reference)
        if (not isinstance(consumed, dict) or consumed.get('origin_invocation') != invocation
                or consumed.get('messages') != derived.get(tuple(consumed.get('steer_refs', [])))):
            raise ValueError('effective_answer_continuation_input_changed')
        matches = [grant for grant in grants if grant['input_ref'] == reference and grant['tool_invocation_id'] == requested[0]
            and grant['public_turn_id'] == value['public_turn_id'] and grant['interruption_ref'] == consumed.get('interruption_ref')
            and grant['invocation_key'] == consumed.get('model_invocation_key') and grant['model_request_id'] == requests[index + 1]
            and grant['steer_ref'] in consumed.get('steer_refs', [])]
        if len(matches) != 1:
            raise ValueError('effective_answer_continuation_grant_unavailable')
        closed = store.get(matches[0]['interruption_ref'])
        if (not isinstance(closed, dict) or closed.get('model_request_id') != requests[index]
                or closed.get('provider_closed') is not True or closed.get('run_generation') != value['run_generation']
                or closed.get('run_owner_id') != value['run_owner_id']):
            raise ValueError('effective_answer_continuation_close_unproven')
        calls = [event for event in events if event['correlation']['model_request_id'] == requests[index]]
        receipts = [event['data']['receipt_ref'] for event in calls if event['type'] == 'model.attempt.terminal']
        if (not any(event['type'] == 'model.failed' for event in calls)
                or len(receipts) != sum(event['type'] == 'model.attempt.dispatched' for event in calls)
                or receipts != closed.get('wire_receipt_refs') or not set(receipts) <= qualified_closed):
            raise ValueError('effective_answer_continuation_settlement_unproven')
        for receipt_ref in receipts:
            receipt = validate_model_wire_attempt_receipt(store.get(receipt_ref))
            if receipt['turn_id'] != turn_id or receipt['model_request_id'] != requests[index]:
                raise ValueError('effective_answer_wire_receipt_owner_changed')
    return {'selector': selected, 'input': actual, 'result': result, 'validation_input': base, 'proof': value}


def recover_effective_answer(store, turn_id, invocation):
    existing = effective_answer(store, turn_id, invocation, _allow_missing=True)
    if existing is not None:
        return existing
    binding = store.get_immutable_payload(turn_id, 'workbench-steer-binding-v1')
    if binding is None:
        return None
    candidates = {}
    for row in store.workbench_steers(binding[1]['public_turn_id']):
        if row['payload']['turn_id'] == turn_id and row['input_ref'] is not None:
            value = store.get(row['input_ref'])
            if isinstance(value, dict) and value.get('origin_invocation') == invocation:
                candidates[row['input_ref']] = value
    state = store.get_immutable_payload(turn_id, 'answer-steer-base-state-' + invocation)
    if candidates:
        input_ref, frozen = next(reversed(candidates.items()))
        continuation = [*frozen['prior_inputs'], *([] if frozen.get('preflight') else [input_ref])]
        final_key = frozen['invocation']
    elif state is not None:
        frozen = state[1]
        continuation, final_key = [], invocation
    else:
        return None
    actual = store.get_immutable_payload(turn_id, 'answer-model-input-' + final_key)
    result = store.get_immutable_payload(turn_id, 'answer-model-result-' + final_key)
    if actual is None or result is None:
        raise ValueError('steer_recovery_requires_terminal_result')
    completed = _completed_results(store, tuple(store.events_after(turn_id)), result[1])
    if len(completed) != 1:
        raise ValueError('steer_recovery_requires_terminal_result')
    proof = {'turn_id': turn_id, 'public_turn_id': binding[1]['public_turn_id'],
        'origin_invocation': invocation, 'final_invocation': final_key, 'base_ref': frozen['base_ref'],
        'input_ref': actual[0], 'result_ref': result[0], 'batches': frozen['batches'],
        'run_generation': frozen['run_generation'], 'run_owner_id': frozen['run_owner_id'],
        'request_identity': frozen['request_identity'],
        'model_request_ids': [*frozen['prior_request_ids'], completed[0]['correlation']['model_request_id']],
        'continuation_inputs': continuation}
    _validate_effective(store, turn_id, invocation, (None, proof))
    store.get_or_create_immutable_payload(turn_id, 'answer-effective-' + invocation, proof)
    return effective_answer(store, turn_id, invocation)


def resolve_paused_answer_input(store, turn_id, row, *, _seen=()):
    request = store.get_request(turn_id)
    if (request is None or row.object_id != turn_id or row.payload.get('project_id') != request['scope']['project_id']
            or row.payload.get('question') != request['input']['text']):
        raise ValueError('paused_answer_input_scope_changed')
    refs = row.payload.get('close_refs', [])
    closure = store.get(refs[-1]) if refs else None
    model_id = None
    if closure is not None:
        model_id = closure['model_request_id']
        if (closure.get('turn_id') != turn_id or closure.get('project_id') != row.payload['project_id']
                or closure.get('question') != row.payload['question'] or closure.get('result') != row.payload['result']
                or store.get_immutable_payload(turn_id, 'product-answer-closed-' + model_id) != (refs[-1], closure)):
            raise ValueError('paused_answer_close_proof_changed')
    elif row.payload.get('boundary_ref'):
        boundary = store.get(row.payload['boundary_ref'])
        if (boundary.get('request') != request or boundary.get('result') != row.payload['result']
                or store.get_immutable_payload(turn_id, boundary['kind']) != (row.payload['boundary_ref'], boundary)):
            raise ValueError('paused_answer_boundary_changed')
        settled = {validate_model_wire_attempt_receipt(store.get(reference))['model_request_id']
            for reference in boundary.get('settled_attempts', {}).get('terminals', [])}
        primary = [event['correlation']['model_request_id'] for event in store.events_after(turn_id)
            if event['type'] in {'model.completed', 'model.failed'} and event['data'].get('model_call_purpose') == 'primary'
            and event['correlation']['model_request_id'] in settled]
        model_id = primary[-1] if primary else None
    native_binding = store.get_immutable_payload(turn_id, 'answer-invocation-binding-' + model_id) if model_id else None
    metadata = native_binding[1] if native_binding is not None else None
    stage = metadata['invocation_key'] if metadata is not None else closure.get('stage', 'answer') if closure is not None else 'answer'
    actual = store.get_immutable_payload(turn_id, 'answer-model-input-' + stage)
    if actual is None or actual[1].get('purpose') != 'primary':
        raise ValueError('paused_answer_input_unavailable')
    if project_answer_messages(store, request, actual[1]['messages']) != actual[1]['messages']:
        raise ValueError('paused_answer_constraints_changed')
    identity = store.get_immutable_payload(turn_id, 'workbench-steer-binding-v1')
    rows = store.workbench_steers(identity[1]['public_turn_id']) if identity is not None else ()
    adopted = [value for value in rows if value['payload']['turn_id'] == turn_id and value['input_ref'] is not None]
    phase = actual[1].get('phase')
    if (phase is not None and phase not in {'single', 'first', 'final-review'}
            or metadata is not None and metadata.get('phase') != phase
            or metadata is not None and metadata.get('phase_logical') != actual[1].get('phase_logical')
            or phase is not None and (not isinstance(actual[1].get('phase_logical'), str) or not actual[1]['phase_logical'])
            or phase is not None and store.get_immutable_payload(turn_id, 'answer-phase-protocol-v1') is None
            or phase is not None and metadata is None):
        raise ValueError('paused_answer_phase_binding_changed')
    if metadata is None:
        if adopted:
            raise ValueError('paused_answer_actual_invocation_unavailable')
        return {'input': actual, 'validation_input': actual, 'source_invocation': stage, 'source_logical': None,
            'phase': None, 'steer_refs': [], 'legacy_logical': closure.get('stage') if closure is not None else None}
    if (metadata.get('turn_id') != turn_id or metadata.get('project_id') != request['scope']['project_id']
            or metadata.get('model_request_id') != model_id or metadata.get('input_ref') != actual[0]
            or metadata.get('request_identity') != admission_identity(request)
            or closure is not None and (metadata.get('tool_call_id') != closure['invocation_id']
                or {'owner_id': metadata.get('run_owner_id'), 'generation': metadata.get('run_generation')} != closure['lease'])):
        raise ValueError('paused_answer_actual_input_binding_changed')
    origin = metadata['logical_invocation']
    if origin in _seen:
        raise ValueError('answer_inheritance_continuation_cycle')
    inherited = _answer_inheritance(store, turn_id, origin, seen=(*_seen, origin))
    if inherited is not None and (metadata['run_owner_id'] != inherited['proof']['run_owner_id']
            or metadata['run_generation'] != inherited['proof']['run_generation']):
        raise ValueError('paused_answer_inheritance_owner_changed')
    state = store.get_immutable_payload(turn_id, 'answer-steer-base-state-' + origin)
    sources = [(value['input_ref'], store.get(value['input_ref'])) for value in adopted]
    sources = [(reference, value) for reference, value in sources if value.get('invocation') == stage]
    if sources:
        reference, frozen = sources[-1]
        continuation = [*frozen['prior_inputs'], *([] if frozen.get('preflight') else [reference])]
    elif state is not None:
        frozen, continuation = state[1], []
    else:
        from .answer_legacy import read_legacy_input
        legacy = read_legacy_input(store, turn_id, origin, seen=_seen) if phase is None else None
        return {'input': actual,
            'validation_input': inherited['validation_base'] if inherited is not None else legacy['validation_input'] if legacy is not None else actual,
            'source_invocation': stage, 'source_logical': metadata.get('phase_logical') if phase is not None else None, 'phase': phase,
            'legacy_logical': (legacy['intent'][1]['logical_invocation'] if legacy is not None else metadata.get('logical_invocation')) if phase is None else None,
            'steer_refs': inherited['steer_refs'] if inherited is not None else []}
    proof = {'turn_id': turn_id, 'public_turn_id': identity[1]['public_turn_id'], 'origin_invocation': origin,
        'final_invocation': stage, 'base_ref': frozen['base_ref'], 'input_ref': actual[0], 'result_ref': None,
        'run_generation': frozen['run_generation'], 'run_owner_id': frozen['run_owner_id'],
        'request_identity': frozen['request_identity'], 'batches': frozen['batches'],
        'continuation_inputs': continuation, 'model_request_ids': [*frozen['prior_request_ids'], model_id]}
    selected = _validate_effective(store, turn_id, origin, (None, proof), paused_request=model_id)
    refs = {ref for batch in proof['batches'] for ref in batch}
    if inherited is not None:
        refs.update(inherited['steer_refs'])
    return {'input': selected['input'], 'validation_input': inherited['validation_base'] if inherited is not None else selected['validation_input'],
        'source_invocation': stage, 'source_logical': metadata.get('phase_logical') if phase is not None else None,
        'phase': phase, 'legacy_logical': metadata.get('logical_invocation') if phase is None else None,
        'steer_refs': [value['payload_ref'] for value in rows if value['payload_ref'] in refs]}
