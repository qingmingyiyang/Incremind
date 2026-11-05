from copy import deepcopy
import json

from backend.recognition import RecognitionConflict, WorkScope
from backend.shared.llm.message_metadata import (
    _estimate_input_tokens, _build_json_mode_messages, _build_prompt_fallback_messages,
)
from core.ai_kernel.contracts import validate_model_wire_attempt_dispatch, validate_model_wire_attempt_receipt
from ..original_sources import original_content_identity
from ..source_egress import SourceEgressService
from ..turn_routing import SNAPSHOT_KIND, RecognitionRoutingSnapshot, _revision
from ..research_reads import turn_tree, _product_agent_request
from .part_context import (
    COLLECTION, ATTACHMENT_PREFIX, attachment_text, fit_attachment_context,
    validate_bound_context,
)


INPUT_KIND = 'product-task-attachment-input-v1-'
FRAMING = 'governed-validation-retries-0-v1'


def _envelopes(messages, response_model):
    # complete_governed 与流式降级均固定 retries=0；外层补丁重试重新准备。
    # 因此本接点只允许无 validation_error 的三种既有包装。
    envelopes = [deepcopy(messages)]
    if response_model is not None:
        envelopes.extend([
            _build_json_mode_messages(messages=messages, validation_error=None),
            _build_prompt_fallback_messages(messages=messages, response_model=response_model, validation_error=None),
        ])
    return envelopes


def input_tokens(messages, response_model=None):
    return max(_estimate_input_tokens(envelope) for envelope in _envelopes(messages, response_model))


def _budget(limits, agent_budget=None):
    if (not isinstance(limits, dict) or type(limits.get('window')) is not int
            or type(limits.get('reserve')) is not int
            or not 0 <= limits['reserve'] < limits['window']):
        raise RecognitionConflict('attachment input budget unavailable')
    budget = limits['window'] - limits['reserve']
    return min(budget, agent_budget['remaining_input_tokens']) if agent_budget is not None else budget


def project_attachment_messages(messages, accepted_texts, selected_text, *, require_context=True, root_input=None):
    admitted = set(accepted_texts)
    seen = 0

    def project(value, *, field=None):
        nonlocal seen
        if isinstance(value, dict):
            return {key: project(item, field=key) for key, item in value.items()}
        if isinstance(value, list):
            return [project(item) for item in value]
        if not isinstance(value, str):
            return value
        if value in admitted:
            seen += 1
            return selected_text
        if field == 'task' and value.endswith('（已截断）') and 'dependency_context' in value:
            prefix = value[:-len('（已截断）')]
            if not isinstance(root_input, str) or not prefix:
                raise RecognitionConflict('attachment steward root binding changed')
            projected = project(root_input)
            if not root_input.startswith(prefix) and not projected.startswith(prefix):
                raise RecognitionConflict('attachment steward root binding changed')
            # 沿实际 brief 已给出的长度重建相同裁剪，保持原 builder 的限制。
            return projected if len(projected) <= len(prefix) else projected[:len(prefix)] + '（已截断）'
        if field == 'dependency_context' or value.startswith(ATTACHMENT_PREFIX):
            raise RecognitionConflict('attachment message binding changed')
        # 原规划请求和事件可内嵌 JSON 字符串，逐层解析后仅投影绑定原文。
        if value.lstrip().startswith(('{', '[')):
            try:
                parsed = json.loads(value)
            except ValueError:
                return value
            before = seen
            changed = project(parsed)
            if changed != parsed:
                return json.dumps(changed, ensure_ascii=False, separators=(',', ':'))
            if seen > before:
                return value
        if any(text and text in value for text in admitted):
            raise RecognitionConflict('attachment message contains an unprojected binding')
        if ATTACHMENT_PREFIX.rstrip('\n') in value:
            raise RecognitionConflict('attachment message contains an unknown binding fragment')
        return value

    result = project(messages)
    if require_context and not seen:
        raise RecognitionConflict('attachment message binding unavailable')
    return result


def fit_task_attachment_messages(records, query, context, question, messages, limits,
                                 *, response_model=None, accepted_texts=(), root_input=None, agent_budget=None):
    budget = _budget(limits, agent_budget)
    texts = [context['text'], *accepted_texts]

    def fits(selected):
        projected = project_attachment_messages(messages, texts, selected['text'], root_input=root_input)
        return input_tokens(projected, response_model) <= budget

    selected = fit_attachment_context(records, query, context, question, fits)
    if (not selected or not selected.get('originals')
            or any(not row['content'].get('windows') for row in selected['originals'])):
        raise RecognitionConflict('attachment input budget has no complete source coverage')
    projected = project_attachment_messages(messages, texts, selected['text'], root_input=root_input)
    if input_tokens(projected, response_model) > budget:
        raise RecognitionConflict('attachment input exceeds planner budget')
    return selected, projected


def _accepted(turns, agents, request):
    accepted = turns.get_request(request['turn_id'])
    if accepted != request and accepted != _product_agent_request(
            turns, agents, request, request['scope']['project_id']):
        raise RecognitionConflict('attachment kernel request changed')
    return accepted


def _immutable_payload(turns, identity, kind):
    ref = turns.immutable_payload_reference(identity, kind)
    try:
        return ref, turns.get(ref)
    except KeyError:
        return None


def _agent_owner(turns, agents, request):
    accepted = _accepted(turns, agents, request)
    binding = accepted.get('agent_binding')
    found = agents.get_run_by_turn_id(accepted['turn_id'], project_id=accepted['scope']['project_id']) if agents else None
    if binding is None:
        if found is not None:
            raise RecognitionConflict('attachment Agent budget binding unavailable')
        return None
    if not isinstance(binding, dict) or found is None:
        raise RecognitionConflict('attachment Agent budget owner unavailable')
    run = found[0]
    expected = {key: getattr(run, key) for key in ('run_id', 'role', 'profile_id', 'profile_revision',
        'model_tier', 'depth', 'cancel_epoch', 'budget_snapshot_ref')}
    if (run.turn_id != accepted['turn_id'] or run.project_id != accepted['scope']['project_id']
            or agents.get_run(run.run_id) != run
            or any(binding.get(key) != value for key, value in expected.items())
            or run.parent_run_id is not None and binding.get('parent_run_id') != run.parent_run_id
            or not run.budget_snapshot_ref):
        raise RecognitionConflict('attachment Agent budget owner changed')
    return {'dimension': 'current_run_input_tokens', 'run_id': run.run_id, 'turn_id': run.turn_id,
        'project_id': run.project_id, 'profile_id': run.profile_id, 'profile_revision': run.profile_revision,
        'budget_snapshot_ref': run.budget_snapshot_ref, 'input_token_limit': run.budget_limit.input_tokens}


def remaining_agent_input(limit, reserved):
    if type(limit) is not int or type(reserved) is not int or not 0 <= reserved <= limit:
        raise RecognitionConflict('attachment Agent input budget exceeded')
    return limit - reserved


def agent_input_budget(turns, agents, request, *, frontier=None, exclude_attempt=None):
    owner = _agent_owner(turns, agents, request)
    if owner is None:
        return None
    events = tuple(turns.events_after(owner['turn_id'], after_sequence=0))
    latest = events[-1]['sequence'] if events else 0
    if frontier is None:
        frontier = latest
    if type(frontier) is not int or not 0 <= frontier <= latest:
        raise RecognitionConflict('attachment Agent budget frontier changed')
    events = [event for event in events if event['sequence'] <= frontier]
    terminals = {}
    for event in events:
        if event.get('type') != 'model.attempt.terminal':
            continue
        ref = event['data']['receipt_ref']
        terminal = validate_model_wire_attempt_receipt(turns.get(ref))
        if (terminal['turn_id'] != owner['turn_id'] or terminal['attempt_id'] in terminals
                or event['correlation']['model_request_id'] != terminal['model_request_id']):
            raise RecognitionConflict('attachment Agent attempt usage owner changed')
        terminals[terminal['attempt_id']] = (ref, terminal)
    reservations = []
    for event in events:
        if event.get('type') != 'model.attempt.dispatched':
            continue
        dispatch_ref = event['data']['payload_ref']
        dispatch = validate_model_wire_attempt_dispatch(turns.get(dispatch_ref))
        if dispatch['attempt_id'] == exclude_attempt:
            continue
        saved = _immutable_payload(turns, owner['turn_id'], INPUT_KIND + dispatch['attempt_id'])
        if (saved is None or dispatch['turn_id'] != owner['turn_id']
                or event['correlation']['model_request_id'] != dispatch['model_request_id']
                or saved[1].get('dispatch') != dispatch or saved[1].get('dispatch_ref') != dispatch_ref):
            raise RecognitionConflict('attachment Agent input reservation unavailable')
        estimate = saved[1]['estimated_input_tokens']
        if type(estimate) is not int or estimate <= 0:
            raise RecognitionConflict('attachment Agent input reservation changed')
        terminal_ref, terminal = terminals.get(dispatch['attempt_id'], (None, {}))
        if terminal and any(terminal[key] != dispatch[key] for key in
                ('turn_id', 'model_request_id', 'attempt_number', 'routing_snapshot_revision', 'provider_id', 'model_id')):
            raise RecognitionConflict('attachment Agent attempt usage changed')
        observed = (terminal.get('usage') or {}).get('input_tokens')
        # unknown 保持未知；估算只用于当前 Run admission 预留，不写入消费或计费。
        reserved = max(estimate, observed if observed is not None else 0)
        reservations.append({'attempt_id': dispatch['attempt_id'], 'dispatch_ref': dispatch_ref,
            'proof_ref': saved[0], 'terminal_ref': terminal_ref,
            'usage_status': 'observed' if observed is not None else 'unknown',
            'observed_input_tokens': observed, 'reserved_input_tokens': reserved})
    reserved = sum(row['reserved_input_tokens'] for row in reservations)
    return {**owner, 'event_frontier': frontier, 'attempt_reservations': reservations,
        'reserved_input_tokens': reserved,
        'remaining_input_tokens': remaining_agent_input(owner['input_token_limit'], reserved)}


def _proof_context(records, root_request):
    row = records.read(COLLECTION, root_request['turn_id'])
    marker = {'kind': 'session_event', 'object_id': root_request['turn_id'],
              'uri': 'crp://default/' + COLLECTION + '/' + root_request['turn_id']}
    if row is None:
        if marker in root_request['input']['refs']:
            raise RecognitionConflict('attachment root binding unavailable')
        return None
    context = row.payload.get('context')
    product = json.loads(root_request['input']['text'])
    if (row.revision != 1 or row.payload.get('project_id') != root_request['scope']['project_id']
            or row.payload.get('input_refs') != root_request['input']['refs']
            or marker not in root_request['input']['refs']
            or not isinstance(context, dict)
            or context.get('project_id') != root_request['scope']['project_id']
            or not isinstance(product, dict) or product.get('dependency_context') != context['text']):
        raise RecognitionConflict('attachment root binding changed')
    if 'attachment_titles' not in context:
        if context['text'].startswith(ATTACHMENT_PREFIX):
            raise RecognitionConflict('attachment root evidence binding changed')
        return None
    if attachment_text(context) != context['text']:
        raise RecognitionConflict('attachment root text binding changed')
    return row


def _validate_windows(records, context, selected):
    expected = deepcopy(context)
    if len(selected.get('originals', [])) != len(expected['originals']):
        raise RecognitionConflict('attachment source coverage changed')
    for original, chosen in zip(expected['originals'], selected['originals']):
        windows = chosen.get('content', {}).get('windows')
        if not windows:
            raise RecognitionConflict('attachment source coverage changed')
        current = original_content_identity(records, WorkScope(**original['scope']), original['id'], windows=windows)
        expected_original = deepcopy(original)
        expected_original['content'] = current
        if expected_original != chosen:
            raise RecognitionConflict('attachment source identity changed')
        original['content'] = current
    expected['text'] = attachment_text(expected)
    if expected != selected:
        raise RecognitionConflict('attachment selected context changed')


def _window_refs(selected):
    return [{'id': original['id'], 'windows': deepcopy(original['content']['windows'])}
            for original in selected['originals']]


def _restore_selection(records, context, selections):
    selected = deepcopy(context)
    if not isinstance(selections, list) or len(selections) != len(selected['originals']):
        raise RecognitionConflict('attachment input source coverage changed')
    for original, selection in zip(selected['originals'], selections):
        if (not isinstance(selection, dict) or set(selection) != {'id', 'windows'}
                or selection['id'] != original['id']):
            raise RecognitionConflict('attachment input source identity changed')
        original['content']['windows'] = deepcopy(selection['windows'])
    selected['text'] = attachment_text(selected)
    _validate_windows(records, context, selected)
    return selected


def _prefixes(messages, response_model):
    return [envelope[:-len(messages)] for envelope in _envelopes(messages, response_model)]


def validate_task_attachment_inputs(records, turns, agents, request):
    bound = _proof_context(records, request)
    if bound is None:
        return []
    root = _accepted(turns, agents, request)
    proofs = []
    for identity in turn_tree(turns, agents, request['turn_id'], request['scope']['project_id']):
        for event in turns.events_after(identity, after_sequence=0):
            if event.get('type') != 'model.attempt.dispatched':
                continue
            dispatch_ref = event.get('data', {}).get('payload_ref')
            dispatch = validate_model_wire_attempt_dispatch(turns.get(dispatch_ref))
            saved = _immutable_payload(turns, identity, INPUT_KIND + dispatch['attempt_id'])
            if saved is None:
                raise RecognitionConflict('attachment attempt input proof unavailable')
            proof = saved[1]
            route = _immutable_payload(turns, identity, SNAPSHOT_KIND)
            if (proof.get('version') != 1 or proof.get('framing') != FRAMING
                    or proof.get('root_turn_id') != root['turn_id'] or proof.get('turn_id') != identity
                    or proof.get('context_revision') != bound.revision
                    or proof.get('dispatch') != dispatch or proof.get('dispatch_ref') != dispatch_ref
                    or dispatch['turn_id'] != identity
                    or event.get('correlation', {}).get('model_request_id') != dispatch['model_request_id']
                    or not route or proof.get('routing_snapshot') != RecognitionRoutingSnapshot(
                        route[0], _revision(route[1]), route[1]).generation_binding()):
                raise RecognitionConflict('attachment attempt input proof changed')
            agent_budget = proof.get('agent_budget')
            budget = agent_input_budget(turns, agents, turns.get_request(identity),
                frontier=agent_budget['event_frontier'] if agent_budget is not None else event['sequence'] - 1)
            if (budget != agent_budget or budget is not None and budget['event_frontier'] >= event['sequence']
                    or proof['estimated_input_tokens'] > _budget(proof['limits'], budget)):
                raise RecognitionConflict('attachment Agent input budget proof changed')
            selected = _restore_selection(records, bound.payload['context'], proof['selections'])
            projected = project_attachment_messages(proof['messages'], [bound.payload['context']['text'], selected['text']],
                selected['text'], root_input=root['input']['text'])
            prefixes = proof['framing_prefixes']
            if (not isinstance(prefixes, list) or len(prefixes) not in {1, 3} or prefixes[0] != []
                    or any(not isinstance(prefix, list) for prefix in prefixes)):
                raise RecognitionConflict('attachment framing proof changed')
            envelopes = [[*prefix, *proof['messages']] for prefix in prefixes]
            if (projected != proof['messages']
                    or max(_estimate_input_tokens(envelope) for envelope in envelopes) != proof['estimated_input_tokens']
                    or proof['estimated_input_tokens'] > _budget(proof['limits'])):
                raise RecognitionConflict('attachment planner input proof changed')
            proofs.append(proof)
        # 返回、交付及成果回读均复验当前总额，保留真实 usage 超过估算后的拒绝。
        agent_input_budget(turns, agents, turns.get_request(identity))
    return proofs


class PreparedAttachmentInput:
    def __init__(self, *, records, turns, root_request, request, context_row, selected,
                 messages, routing_snapshot, limits,
                 response_model, execution_control, validate, agents=None):
        self.records, self.turns = records, turns
        self.agents, self.request = agents, deepcopy(request)
        self.messages = deepcopy(messages)
        self._validate = validate
        self._context = deepcopy(context_row.payload['context'])
        self._selected = deepcopy(selected)
        # 根正文只保留在原授权记录；逐尝试仅保存受预算约束的窗口和消息。
        self._payload = deepcopy({'version': 1, 'framing': FRAMING,
            'root_turn_id': root_request['turn_id'], 'turn_id': request['turn_id'],
            'context_revision': context_row.revision, 'selections': _window_refs(selected),
            'messages': messages, 'routing_snapshot': routing_snapshot,
            'limits': limits, 'framing_prefixes': _prefixes(messages, response_model),
            'estimated_input_tokens': input_tokens(messages, response_model)})
        self.wire_attempt_sink = _AttachmentWireSink(execution_control, self)

    def validate(self):
        self._validate()
        _agent_owner(self.turns, self.agents, self.request)
        if self.messages != self._payload['messages']:
            raise RecognitionConflict('attachment planner messages changed')
        _validate_windows(self.records, self._context, self._selected)
        authority = SourceEgressService(self.records)
        for original in self._selected['originals']:
            authority.validate_original_content(WorkScope(**original['scope']), original, 'generation')

    def attempt_payload(self):
        self.validate()
        budget = agent_input_budget(self.turns, self.agents, self.request)
        if self._payload['estimated_input_tokens'] > _budget(self._payload['limits'], budget):
            raise RecognitionConflict('attachment Agent input budget exhausted')
        return {**self._payload, 'agent_budget': budget}

    def validate_attempt(self, payload):
        self.validate()
        current = agent_input_budget(self.turns, self.agents, self.request,
            exclude_attempt=payload['dispatch']['attempt_id'])
        frozen = payload['agent_budget']
        without_frontier = lambda value: {key: item for key, item in value.items() if key != 'event_frontier'} if value else value
        if without_frontier(current) != without_frontier(frozen):
            raise RecognitionConflict('attachment Agent input reservations changed')
        if payload['estimated_input_tokens'] > _budget(payload['limits'], current):
            raise RecognitionConflict('attachment Agent input budget exhausted')


class _AttachmentWireSink:
    def __init__(self, sink, prepared):
        self.sink, self.prepared = sink, prepared

    def __getattr__(self, name):
        return getattr(self.sink, name)

    def begin_model_wire_attempt(self):
        prepared_payload = self.prepared.attempt_payload()
        handle = self.sink.begin_model_wire_attempt()
        try:
            dispatch = validate_model_wire_attempt_dispatch(handle.dispatch)
            if (dispatch['turn_id'] != self.prepared._payload['turn_id']
                    or dispatch['model_request_id'] != self.sink.model_request_id):
                raise RecognitionConflict('attachment wire owner changed')
            payload = {**prepared_payload, 'dispatch': dict(dispatch), 'dispatch_ref': handle.dispatch_ref}
            self.prepared.turns.get_or_create_immutable_payload(dispatch['turn_id'], INPUT_KIND + dispatch['attempt_id'], payload)
        except BaseException:
            def reject():
                handle.failed_transport(error_code='ai.attachment_input_proof_failed')
            handle.invoke_wire(reject)
            raise
        return _AttachmentWireHandle(handle, self.prepared, payload)


class _AttachmentWireHandle:
    def __init__(self, handle, prepared, payload):
        self.handle, self.prepared, self.payload = handle, prepared, payload

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def invoke_wire(self, handler):
        def guarded():
            try:
                self.prepared.validate_attempt(self.payload)
            except BaseException:
                self.handle.failed_transport(error_code='ai.attachment_input_changed')
                raise
            return handler()
        return self.handle.invoke_wire(guarded)


class TaskAttachmentInput:
    def __init__(self, records, models, query):
        self.records, self.models, self.query = records, models, query

    def validate(self, root_request, turns, agents):
        bound = _proof_context(self.records, root_request)
        if bound is None:
            return []
        validate_bound_context(self.records, self.models, root_request, self.query)
        return validate_task_attachment_inputs(self.records, turns, agents, root_request)

    def __call__(self, *, root_request, request, messages, routing_snapshot,
                 execution_control, response_model, max_tokens, retry_policy, turns, agents):
        bound = _proof_context(self.records, root_request)
        if bound is None:
            return None
        if not callable(retry_policy):
            raise RecognitionConflict('attachment governed retry contract unavailable')
        proofs = self.validate(root_request, turns, agents)
        limits = self.models.generation_budget_limits(
            expected_revision=routing_snapshot['configuration']['revision'], max_tokens=max_tokens)
        agent_budget = agent_input_budget(turns, agents, request)
        context = bound.payload['context']
        accepted_texts = list(dict.fromkeys([context['text'], *(
            _restore_selection(self.records, context, proof['selections'])['text'] for proof in proofs)]))
        question = json.loads(root_request['input']['text'])['task']
        selected, projected = fit_task_attachment_messages(self.records, self.query, context,
            question, messages, limits, response_model=response_model, accepted_texts=accepted_texts,
            root_input=root_request['input']['text'], agent_budget=agent_budget)
        def validate():
            self.validate(root_request, turns, agents)
            if self.models.generation_budget_limits(
                    expected_revision=routing_snapshot['configuration']['revision'], max_tokens=max_tokens) != limits:
                raise RecognitionConflict('attachment input budget changed')
        return PreparedAttachmentInput(records=self.records, turns=turns, root_request=root_request,
            request=request, context_row=bound, selected=selected,
            messages=projected, routing_snapshot=routing_snapshot,
            limits=limits, response_model=response_model, execution_control=execution_control,
            validate=validate, agents=agents)
