from copy import deepcopy

from backend.recognition import RecognitionConflict
from core.ai_kernel.product_admission import admission_identity
from core.ai_kernel.contracts import validate_governed_payload
from ..answer_effective import project_answer_messages, resolve_answer_invocation, _completed_results
from ..answer_proof import REVIEW_KEY, RAW_ANSWER_KEY, EVIDENCE_ANSWER_KEY, quote_answer_messages, review_messages
from .policies import get, override
from .policies.compose import evidence_contract_version


class PrimaryWireProjection:
    def __init__(self, store, request, plan_loader, *, execution=None):
        self.store, self.request, self.plan_loader = store, request, plan_loader
        self.execution = execution

    def canonical(self, logical, *, seen=()):
        if logical in seen:
            raise RecognitionConflict('material_origin_message_projection_cycle')
        plan = self.plan_loader(logical)
        with override(**self.request['policy_versions']):
            if logical in {RAW_ANSWER_KEY, EVIDENCE_ANSWER_KEY}:
                if logical == RAW_ANSWER_KEY:
                    from .raw_answer import messages, preflight
                else:
                    from .evidence_answer import messages, preflight
                contract = preflight(plan)
                if contract is None:
                    raise RecognitionConflict('material_origin_message_plan_changed')
                return messages(plan, contract)
            version = evidence_contract_version(plan['policy_versions']['compose'])
            partitioned = version in {'@18', '@19', '@20', '@21', '@22', '@23', '@24', '@25', '@26',
                '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}
            if partitioned:
                from .answer_evidence import build_partition_messages
                arguments = {'history': plan.get('history', ''), 'profile': plan.get('profile', {}),
                    'part_context': plan.get('part_context'), 'compose_version': version}
                if logical == REVIEW_KEY:
                    first = self.first_output(seen=(*seen, logical))
                    arguments['first_output' if version in {'@24', '@25', '@26', '@27', '@28', '@29',
                        '@30', '@31', '@32', '@33', '@34', '@35', '@36'} else 'partition'] = first
                return build_partition_messages(plan['chosen'], plan['question'], **arguments)
            from .budget import ask_instruction, source_texts, user_text
            from .profile import profile_messages
            original = profile_messages(plan.get('profile', {}), [
                {'role': 'system', 'content': get('compose')(ask_instruction, plan['chosen'])},
                {'role': 'user', 'content': get('compose')(user_text, source_texts(plan['chosen']),
                    plan['question'], plan.get('history', ''))}])
            if plan.get('part_context'):
                original.insert(1 if plan.get('profile', {}).get('text') else 0,
                    {'role': 'user', 'content': plan['part_context']['text']})
            if logical != REVIEW_KEY:
                return original
            first = self.first_output(seen=(*seen, logical))
            return (quote_answer_messages(original, first, plan['chosen'], compose_version=version,
                question=plan['question']) if version in {'@15', '@16', '@17'} else
                review_messages(original, first, get('compose')(None, operation='review_instruction')))

    def first_output(self, *, seen):
        first = resolve_answer_invocation(self.store, self.request['turn_id'], 'answer')
        if (first['input'] is None or first['result'] is None
                or len(_completed_results(self.store, tuple(self.store.events_after(self.request['turn_id'])),
                    first['result'][1])) != 1):
            raise RecognitionConflict('material_origin_message_first_output_unavailable')
        self.validate_bound(first['input'][0], 'answer', seen=seen)
        return deepcopy(first['result'][1]['output'])

    def binding(self, input_ref):
        selected = []
        for event in self.store.events_after(self.request['turn_id']):
            if event['type'] != 'model.requested' or event['data'].get('model_call_purpose') != 'primary':
                continue
            identity = event['correlation']['model_request_id']
            saved = self.store.get_immutable_payload(self.request['turn_id'], 'answer-invocation-binding-' + identity)
            if saved is not None and saved[1].get('input_ref') == input_ref:
                binding = saved[1]
                fields = {'turn_id', 'project_id', 'model_request_id', 'invocation_key', 'logical_invocation',
                    'tool_call_id', 'input_ref', 'run_owner_id', 'run_generation', 'request_identity'}
                if (set(binding) not in (fields, fields | {'phase', 'phase_logical'})
                        or binding['turn_id'] != self.request['turn_id']
                        or binding['project_id'] != self.request['scope']['project_id']
                        or binding['model_request_id'] != identity
                        or binding['tool_call_id'] != event['correlation']['tool_call_id']
                        or binding['request_identity'] != admission_identity(self.request)
                        or not isinstance(binding['run_owner_id'], str) or not binding['run_owner_id']
                        or type(binding['run_generation']) is not int or binding['run_generation'] < 1):
                    raise RecognitionConflict('material_origin_message_binding_changed')
                actual = self.store.get_immutable_payload(self.request['turn_id'],
                    'answer-model-input-' + saved[1]['invocation_key'])
                if actual is None or actual[0] != input_ref:
                    raise RecognitionConflict('material_origin_message_binding_changed')
                value = actual[1]
                fields = {'messages', 'purpose', 'response_schema', 'material_proof'}
                if (set(value) != fields | ({'phase', 'phase_logical'} if 'phase' in binding else set())
                        or value.get('purpose') != 'primary' or not isinstance(value.get('response_schema'), dict)
                        or value.get('phase') != binding.get('phase')
                        or value.get('phase_logical') != binding.get('phase_logical')):
                    raise RecognitionConflict('material_origin_message_binding_changed')
                selected.append((saved[1], actual[1]))
        if len(selected) != 1:
            raise RecognitionConflict('material_origin_message_binding_unavailable')
        return selected[0]

    def validate_bound(self, input_ref, logical, *, seen=()):
        binding, value = self.binding(input_ref)
        self.validate(logical, value['messages'], invocation_key=binding['invocation_key'],
            require_projected=True, seen=seen)

    def logical(self, logical, invocation_key):
        supported = {'answer', REVIEW_KEY, RAW_ANSWER_KEY, EVIDENCE_ANSWER_KEY}
        if logical in supported:
            return logical
        from ..answer_effective import _answer_phase_sources
        from ..answer_legacy import read_legacy_input, _sources
        legacy = read_legacy_input(self.store, self.request['turn_id'], invocation_key)
        if legacy is not None:
            selected = legacy['intent'][1]['logical_invocation']
            if selected in supported:
                return selected
        for _, source in _answer_phase_sources(self.store, self.request['turn_id']):
            action = source[1]['action']
            intent = self.store.get_immutable_payload(self.request['turn_id'], 'answer-phase-intent-' + action['action_id'])
            current = self.execution
            if ((intent is not None and intent[1].get('actual_invocation') == invocation_key)
                    or current is not None and current.get('invocation_key') == invocation_key
                    and current.get('action') == action):
                selected = source[1]['phase_logical']
                if selected in supported:
                    return selected
        if self.execution is not None and self.execution.get('invocation_key') == invocation_key:
            for _, source in _sources(self.store, self.request['turn_id']):
                if source[1]['action'] == self.execution.get('action'):
                    selected = source[1]['terminal']['logical_invocation']
                    if selected in supported:
                        return selected
        raise RecognitionConflict('material_origin_message_invocation_changed')

    def _inherited(self, invocation_key, logical, *, seen):
        from ..answer_effective import _answer_inheritance
        inherited = _answer_inheritance(self.store, self.request['turn_id'], invocation_key, seen=(invocation_key,))
        if inherited is None:
            return None
        raw = self.canonical(logical, seen=seen)
        expected = project_answer_messages(self.store, self.request, raw)
        if inherited['validation_base'][1]['messages'] != expected:
            raise RecognitionConflict('material_origin_message_base_changed')
        continuation = inherited['proof']['continuation']
        if continuation is not None:
            self.validate_bound(continuation['source_input_ref'], logical, seen=seen)
        for source in inherited['proof']['sources']:
            prior = resolve_answer_invocation(self.store, self.request['turn_id'], source['origin_invocation'],
                _phase_alias=False)
            if prior['input'] is None:
                raise RecognitionConflict('material_origin_message_source_unavailable')
            bound, _ = self.binding(prior['input'][0])
            prior_logical = bound.get('phase_logical', bound['logical_invocation'])
            self.validate_bound(prior['input'][0], self.logical(prior_logical, bound['invocation_key']), seen=seen)
        return inherited

    def _legacy(self, invocation_key, logical, *, seen):
        from ..answer_legacy import read_legacy_input
        legacy = read_legacy_input(self.store, self.request['turn_id'], invocation_key)
        if legacy is None:
            return None
        expected = project_answer_messages(self.store, self.request, self.canonical(logical, seen=seen))
        if legacy['validation_input'][1]['messages'] != expected:
            raise RecognitionConflict('material_origin_message_base_changed')
        self.validate_bound(legacy['source'][1]['terminal']['input_ref'], logical, seen=seen)
        return legacy['input'][1]['messages']

    def _continued(self, invocation_key, logical, *, seen):
        from ..answer_effective import resolve_paused_answer_input, validate_paused_answer_publication, _answer_phase_sources
        from ..answer_legacy import _sources
        from ..kernel.policy_runtime import retry_policy_for
        execution = self.execution
        if (execution is None or execution.get('invocation_key') != invocation_key
                or execution.get('continuation') is None or execution.get('action') is None):
            return None
        row, action = execution['continuation'], execution['action']
        sources = [source for _, source in (*_answer_phase_sources(self.store, self.request['turn_id']),
            *_sources(self.store, self.request['turn_id'])) if source[1]['action'] == action]
        if (len(sources) != 1 or sources[0][1]['paused'] != {
                'object_id': row.object_id, 'revision': row.revision, 'payload': row.payload}):
            raise RecognitionConflict('material_origin_message_continuation_changed')
        paused = resolve_paused_answer_input(self.store, self.request['turn_id'], row)
        validate_paused_answer_publication(paused, row)
        self.validate_bound(paused['input'][0], logical, seen=seen)
        public = row.payload['result'].get('receipt', {}).get('ask', row.payload['result'])
        messages = retry_policy_for(self.request)({'kind': 'continue_messages',
            'messages': paused['input'][1]['messages'], 'partial': public.get('partial', '')})
        return project_answer_messages(self.store, self.request, messages)

    def _steered(self, invocation_key, logical, *, seen):
        from ..answer_effective import execution_policy
        capsule = self.store.get_immutable_payload(self.request['turn_id'], 'turn-steer-input-' + invocation_key)
        if capsule is None:
            return None
        value = capsule[1]
        origin = value.get('origin_invocation')
        base = self.store.get_immutable_payload(self.request['turn_id'], 'answer-base-input-' + str(origin))
        if (base is None or value.get('base_ref') != base[0] or value.get('invocation') != invocation_key
                or value.get('request_identity') != admission_identity(self.request)
                or value.get('turn_id') != self.request['turn_id']
                or value.get('project_id') != self.request['scope']['project_id']):
            raise RecognitionConflict('material_origin_message_steer_base_changed')
        self.validate(logical, base[1]['messages'], invocation_key=origin, require_projected=True, seen=seen)
        identity = self.store.get_immutable_payload(self.request['turn_id'], 'workbench-steer-binding-v1')
        if identity is None:
            raise RecognitionConflict('material_origin_message_steer_identity_unavailable')
        rows = {row['payload_ref']: row for row in self.store.workbench_steers(identity[1]['public_turn_id'])}
        events = tuple(self.store.events_after(self.request['turn_id']))
        batches = value.get('batches')
        if not isinstance(batches, list) or not batches or value.get('steer_refs') != batches[-1]:
            raise RecognitionConflict('material_origin_message_steer_batches_changed')
        messages, adopted = deepcopy(base[1]['messages']), set()
        for index, batch in enumerate(batches):
            if not isinstance(batch, list) or not batch or len(set(batch)) != len(batch):
                raise RecognitionConflict('material_origin_message_steer_batches_changed')
            facts, inputs = [], set()
            for reference in batch:
                row = rows.get(reference)
                if (reference in adopted or row is None or row['payload']['turn_id'] != self.request['turn_id']
                        or row['state'] not in {'injected', 'released', 'amended'} or row['input_ref'] is None
                        or row['payload']['lease'] != {'owner_id': value.get('run_owner_id'),
                            'generation': value.get('run_generation')}
                        or sum(event['type'] == 'turn.steer.applied' and row['input_ref'] in
                            event['data'].get('evidence_refs', []) for event in events) != 1):
                    raise RecognitionConflict('material_origin_message_steer_source_changed')
                payload = row['payload']
                validate_governed_payload(payload)
                original = self.store.get_immutable_payload(self.request['turn_id'],
                    'turn-steer-received-' + payload['submission_key'])
                if (original != (reference, payload) or payload.get('binding_ref') != identity[0]
                        or any(payload.get(field) != identity[1].get(field) for field in ('public_turn_id',
                            'target_turn_id', 'project_id', 'scene', 'thread_id', 'owner_user_id', 'namespace_id',
                            'origin_kind', 'part', 'part_turn_ids'))
                        or sum(event['type'] == 'turn.steer.received' and reference in
                            event['data'].get('evidence_refs', []) for event in events) != 1):
                    raise RecognitionConflict('material_origin_message_steer_source_changed')
                facts.append(row['payload'])
                inputs.add(row['input_ref'])
                adopted.add(reference)
            if len(inputs) != 1:
                raise RecognitionConflict('material_origin_message_steer_source_changed')
            reference = inputs.pop()
            source = self.store.get(reference)
            frozen = self.store.get_immutable_payload(self.request['turn_id'],
                'turn-steer-input-' + source.get('invocation', ''))
            messages = execution_policy(facts).answer_messages(messages, facts)
            if (frozen != (reference, source) or source.get('batches') != batches[:index + 1]
                    or source.get('steer_refs') != batch or source.get('base_ref') != base[0]
                    or source.get('origin_invocation') != origin or source.get('messages') != messages
                    or source.get('request_identity') != admission_identity(self.request)
                    or source.get('run_owner_id') != value.get('run_owner_id')
                    or source.get('run_generation') != value.get('run_generation')):
                raise RecognitionConflict('material_origin_message_steer_projection_changed')
        return messages

    def validate(self, logical, messages, *, invocation_key=None, require_projected=False, seen=()):
        invocation_key = logical if invocation_key is None else invocation_key
        token = (logical, invocation_key)
        if token in seen:
            raise RecognitionConflict('material_origin_message_projection_cycle')
        # 控制输入只从已接纳的原生来源投影；调用方消息不参与选择来源。
        logical = self.logical(logical, invocation_key)
        steered = self._steered(invocation_key, logical, seen=(*seen, token))
        if steered is not None:
            if messages != steered:
                raise RecognitionConflict('material_origin_model_messages_changed')
            return
        inherited = self._inherited(invocation_key, logical, seen=(*seen, token))
        if inherited is not None:
            if messages != inherited['input'][1]['messages']:
                raise RecognitionConflict('material_origin_model_messages_changed')
            return
        legacy = self._legacy(invocation_key, logical, seen=(*seen, token))
        if legacy is not None:
            if messages != legacy:
                raise RecognitionConflict('material_origin_model_messages_changed')
            return
        continued = self._continued(invocation_key, logical, seen=(*seen, token))
        if continued is not None:
            if messages != continued:
                raise RecognitionConflict('material_origin_model_messages_changed')
            return
        if invocation_key != logical:
            raise RecognitionConflict('material_origin_message_invocation_changed')
        raw = self.canonical(logical, seen=seen)
        projected = project_answer_messages(self.store, self.request, raw)
        if messages == projected or not require_projected and messages == raw:
            return
        raise RecognitionConflict('material_origin_model_messages_changed')
