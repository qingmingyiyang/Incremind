"""Bind domain question orchestration to the application's existing Turn runner."""
from core.storage_provider.connection_scope import with_connection_scope
import asyncio
from copy import deepcopy
from contextvars import ContextVar
from dataclasses import replace
from hashlib import sha256
import json
from threading import RLock
from time import monotonic
from types import SimpleNamespace

from fastapi import HTTPException

from core.ai_kernel import CapabilityDefinition, validate_turn_request
from .turn_requests import freeze_turn_request
from core.ai_tooling import tool_from_capability
from ..workspace_contracts import _now
from .ai_execution_control import begin_nested_model_call, execution_control_from
from .policy_runtime import retry_policy_for


ACTIVE_ANSWER = ContextVar("product_answer_execution", default=None)
RESULT_KIND = "product-answer-result-v2"


class _AnswerHandles(dict):
    def __init__(self):
        super().__init__()
        self.invocation_lock = RLock()


class AnswerInterrupted(RuntimeError):
    def __init__(self, result):
        self.result = result
        super().__init__('answer_interrupted')


class _AnswerCall:
    """First durable terminal wins even when a timed-out transport returns."""
    def __init__(self, handle, runtime, turn_id):
        self.handle, self.runtime, self.turn_id = handle, runtime, turn_id
        self.lock = RLock()
        self.terminal = None
        self.refs = ()
        self.discarded = False

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def finalize(self, *, error_code):
        with self.lock:
            if self.terminal is None:
                self.refs = self.handle.finalize(error_code=error_code)
                self.terminal = "completed" if error_code is None else "failed"
            return self.refs

    def discard(self):
        with self.lock:
            if not self.discarded:
                self.runtime._append(self.turn_id, "model.result.discarded", "running",
                    "Late answer model result discarded", model_request_id=self.model_request_id,
                    model_call_purpose=self.handle.control.purpose)
                self.discarded = True


def abandon_answer_model(invocation_key):
    active = ACTIVE_ANSWER.get()
    if active is not None:
        call = active[3].get(invocation_key)
        if call is not None:
            call.finalize(error_code="ai.answer_aux_timeout")


def answer_definition():
    """One read tool owns at most two rewrites and one answer, without retries."""
    definition = CapabilityDefinition("workbench.answer.execute", 1, "read", False, "read_only",
        "crp://default/contracts/workbench-question-request.schema.json",
        "crp://default/contracts/workbench-question-presentation.schema.json")
    tool = tool_from_capability(definition)
    return replace(definition, tool_definition=replace(tool,
        nested_model_handle_budget=3, timeout_ms=240_000, idempotency="never_retry",
        retry_policy=replace(tool.retry_policy, max_attempts=1, retryable_error_codes=())))


class ProductAnswerCapability:
    def __init__(self, service, store):
        self.service, self.store = service, store

    def paused_invocation(self, intent):
        with self.service.lock:
            execution = self.service.active.get(intent.turn_id)
        if (execution is None or execution.get('paused_invocation') != intent.invocation_id
                or intent.capability_id != 'workbench.answer.execute'):
            return None
        return self.service.paused(intent.turn_id, require_bundle=False)

    @with_connection_scope
    def invoke(self, request):
        frozen = self.store.get_request(request["turn_id"])
        if not frozen or frozen.get("execution_policy", {}).get("template_version") != 2:
            raise ValueError("product answer template is unavailable")
        if frozen["desired_outcome"] != "project.answer":
            raise ValueError("product answer kind changed")
        identity = request["turn_id"]
        saved = self.store.get_immutable_payload(identity, RESULT_KIND)
        if saved is None:
            with self.service.lock:
                execution = self.service.active.get(identity)
            if execution is None:
                # Recovery owns uncertain attempts. A lost HTTP closure never
                # authorizes issuing a fresh model request.
                raise ValueError("product answer requires recovery review")
            handles = _AnswerHandles()
            token = ACTIVE_ANSWER.set((request, self.store, self.service.query, handles))
            try:
                from backend.shared.llm.model_transport import ModelInterrupted
                try:
                    result = asyncio.run(execution["operation"]())
                except ModelInterrupted as interrupted:
                    if interrupted.interruption != 'stopped':
                        raise
                    result = {'receipt': {'ask': {'answer': None, 'partial': '', 'interruption': 'stopped',
                        'citations': [], 'layers': dict.fromkeys(('insight', 'summary', 'note', 'source', 'persona'), 0),
                        'trace': [], 'egress_receipt_id': None, 'no_match': False, 'context': None,
                        'model_usage': {}, 'excluded_sources': []}}, 'chosen': []}
                answer = result.get('receipt', {}).get('ask', result)
                if answer.get('interruption') != 'stopped':
                    execution_control_from(request).checkpoint()
                if 'interruption' in answer:
                    self.service.preserve_interruption(request, execution, result, handles)
                    raise AnswerInterrupted(result)
                ref = self.store.get_or_create_immutable_payload(identity, RESULT_KIND, result)
            except BaseException as error:
                execution["error"] = error
                if not isinstance(error, AnswerInterrupted):
                    execution_control_from(request).checkpoint()
                raise ValueError("product answer execution failed") from None
            finally:
                ACTIVE_ANSWER.reset(token)
        else:
            ref = saved[0]
        return {"summary": "Question completed", "payload_ref": ref, "evidence_refs": []}


class ProductAnswerTurns:
    def __init__(self, application, root, query):
        self.application, self.root, self.query = application, root, query
        self.active, self.lock = {}, RLock()

    def _runtime(self):
        from .ai_runtime import get_or_build_ai_runtime
        with self.lock:
            return get_or_build_ai_runtime(SimpleNamespace(app=self.application), SimpleNamespace(root_dir=self.root))

    def boundary_tools(self, identity):
        from core.effect_log import EffectState
        store = self.application.state.ai_turn_store
        events = tuple(store.events_after(identity))
        result = []
        for event in events:
            if event['type'] != 'tool.intent.recorded':
                continue
            invocation = event['correlation']['tool_call_id']
            intent = store.get(event['data']['payload_ref'])
            outcomes = [value for value in events if value['type'] == 'tool.outcome.recorded'
                and value['correlation']['tool_call_id'] == invocation]
            if len(outcomes) != 1:
                return None
            outcome = store.get(outcomes[0]['data']['payload_ref'])
            effect = store.effect_runner.log.get(invocation)
            if (effect is None or effect.turn_id != identity or effect.intent_ref != event['data']['payload_ref']
                    or intent['turn_id'] != identity or outcome['turn_id'] != identity
                    or outcome['invocation_id'] != invocation or outcome['capability_id'] != intent['capability_id']
                    or intent['capability_id'] != 'workbench.answer.execute'
                    or outcome['effect_certainty'] != 'confirmed_none'
                    or (outcome['status'], effect.state) not in {('completed', EffectState.SETTLED_OK),
                        ('failed', EffectState.SETTLED_ERR), ('cancelled', EffectState.SETTLED_ERR)}):
                return None
            result.append({'invocation_id': invocation, 'intent_ref': event['data']['payload_ref'],
                'outcome_ref': outcomes[0]['data']['payload_ref'], 'effect_state': effect.state.value})
        return result

    def pause_boundary(self, identity):
        from .answer_continuations import COLLECTION, plan_binding
        runtime, store = self.application.state.ai_runtime, self.application.state.ai_turn_store
        owner, lease = runtime.product_stops, runtime._run_lease_context.get()
        if not owner.requested(identity) or lease is None or lease.turn_id != identity or store.assert_active_run_lease(lease) is None:
            return None
        row = self.query.records.read(COLLECTION, identity)
        if row is None:
            return None
        original = plan_binding(self.query, row)
        attempts, tools = owner.settled_attempts(identity), self.boundary_tools(identity)
        if attempts is None or not tools:
            return None
        owner.ensure_cancel_event(identity)
        old = row.payload.get('result', {}).get('receipt', {}).get('ask', {})
        partial = old.get('partial', '')
        from ..v2.turn_frames import FRAMES, STREAMS, frame_text
        stream, frames = self.query.records.read(STREAMS, identity), self.query.records.read(FRAMES, identity)
        if (stream is not None and frames is not None and stream.payload.get('binding') == {
                'kernel_turn_id': identity, 'request': original['request']}
                and frames.payload.get('project_id') == original['project_id']):
            text = frame_text(frames.payload)
            if text is not None:
                partial = retry_policy_for(original['request'])({'kind': 'partial_output', 'text': text,
                    'wall_elapsed': 0, 'monotonic_elapsed': 0})['partial']
        result = {'receipt': {'ask': {**old, 'answer': None, 'partial': partial, 'interruption': 'stopped',
            'citations': [], 'layers': old.get('layers', {}), 'trace': old.get('trace', []),
            'egress_receipt_id': old.get('egress_receipt_id'), 'no_match': False,
            'context': old.get('context'), 'model_usage': old.get('model_usage', {}),
            'excluded_sources': old.get('excluded_sources', [])}}, 'chosen': row.payload.get('result', {}).get('chosen', [])}
        stop = owner.current(identity)
        kind = 'product-answer-stop-boundary-' + str(stop.payload['generation'])
        event = runtime._new_event(identity, 'turn.cancel.requested', 'waiting_approval',
            'user stopped answer at settled boundary', actor='user')
        capsule = {'turn_id': identity, 'project_id': original['project_id'], 'request': original['request'],
            'plan_ref': row.payload['plan_ref'], 'lease': {'owner_id': lease.owner_id, 'generation': lease.generation},
            'settled_attempts': attempts, 'tools': tools, 'result': result, 'kind': kind}
        committed = store.append_event_with_immutable_payload(event, expected_sequence=len(tuple(store.events_after(identity))),
            immutable_kind=kind, immutable_payload=capsule, run_lease=lease)
        with self.query.records.begin() as tx:
            current = tx.read(COLLECTION, identity)
            if current != row:
                raise ValueError('answer_boundary_changed')
            tx.put(COLLECTION, identity, {**row.payload, 'state': 'paused', 'result': result,
                'invocation_id': tools[-1]['invocation_id'], 'boundary_ref': committed.immutable_payload_ref,
                'boundary_event_id': committed.event['event_id']}, expected_revision=row.revision)
            tx.commit()
        owner.pause(identity, committed.event, committed.immutable_payload_ref)
        return committed.event

    def preserve_interruption(self, request, execution, result, handles):
        from backend.shared.llm.model_transport import is_provider_closed_witness
        from .answer_continuations import COLLECTION, plan_binding
        from .receipt_projection import closed_model_attempt_binding
        calls = [(key, value) for key, value in handles.items()
            if getattr(value, 'interruption', None) == 'stopped']
        invocation, call = calls[-1] if calls else (execution.get('invocation_key', 'answer'),
            handles.get(execution.get('invocation_key', 'answer')))
        if call is None or not is_provider_closed_witness(getattr(call, 'close_witness', None)):
            return
        lease = call.runtime._run_lease_context.get()
        if lease is None or lease.turn_id != request['turn_id']:
            return
        frozen = self.store_request(request['turn_id'])
        owner = {'owner_id': lease.owner_id, 'generation': lease.generation}
        if self.application.state.ai_turn_store.assert_active_run_lease(lease) is None:
            return
        attempts = closed_model_attempt_binding(self.root, request['turn_id'], frozen['scope']['project_id'],
            question=frozen['input']['text'], model_request_id=call.model_request_id, lease=owner,
            stopped=getattr(call, 'interruption', None) == 'stopped')
        if attempts is None:
            return
        prepared = self.query.records.read(COLLECTION, request['turn_id'])
        if prepared is None:
            return
        plan_binding(self.query, prepared)
        binding = {'turn_id': request['turn_id'], 'project_id': frozen['scope']['project_id'],
            'question': frozen['input']['text'], 'invocation_id': request['tool_call_id'],
            'model_request_id': call.model_request_id, 'lease': owner, 'attempts': attempts,
            'plan_ref': prepared.payload['plan_ref'], 'result': result}
        store = self.application.state.ai_turn_store
        auxiliary_refs = []
        # 已结束的辅助失败沿原分类保留，只有真实传输关闭证明可参与整轮暂停。
        for stage, auxiliary in handles.items():
            if (auxiliary is call or auxiliary.terminal != 'failed'
                    or not getattr(auxiliary, 'auxiliary_binding', None)
                    or not is_provider_closed_witness(getattr(auxiliary, 'close_witness', None))
                    or getattr(auxiliary, 'interruption', None) is not None):
                continue
            failures = [event for event in store.events_after(request['turn_id'])
                if event['type'] == 'model.failed' and event['correlation']['model_request_id'] == auxiliary.model_request_id
                and event['correlation']['tool_call_id'] == request['tool_call_id'] and event['data']['receipt_ref']]
            auxiliary_attempts = closed_model_attempt_binding(self.root, request['turn_id'], binding['project_id'],
                question=binding['question'], model_request_id=auxiliary.model_request_id, lease=owner)
            if len(failures) != 1 or auxiliary_attempts is None:
                continue
            kind, auxiliary_binding = auxiliary.auxiliary_binding
            proof = {'turn_id': request['turn_id'], 'project_id': binding['project_id'], 'question': binding['question'],
                'invocation_id': request['tool_call_id'], 'stage': stage, 'model_request_id': auxiliary.model_request_id,
                'lease': owner, 'attempts': auxiliary_attempts, 'binding_kind': kind, 'binding_ref': auxiliary_binding,
                'failure_ref': failures[0]['data']['receipt_ref']}
            reference = store.get_or_create_immutable_payload(request['turn_id'],
                'product-answer-aux-closed-' + auxiliary.model_request_id, proof)
            if self.closed_auxiliary(request['turn_id'], reference) != proof:
                return
            auxiliary_refs.append(reference)
        if auxiliary_refs:
            binding['auxiliary_close_refs'] = auxiliary_refs
        if getattr(call, 'interruption', None) == 'stopped':
            binding.update(stage=invocation, internal_partial=getattr(call, 'internal_partial', ''))
        ref = store.get_or_create_immutable_payload(request['turn_id'],
            'product-answer-closed-' + call.model_request_id, binding)
        if store.assert_active_run_lease(lease) is None:
            return
        with self.query.records.begin() as tx:
            row = tx.read(COLLECTION, request['turn_id'])
            if (row is None or row.payload.get('project_id') != binding['project_id']
                    or row.payload.get('question') != binding['question']
                    or row.payload.get('state') not in {'prepared', 'continuing'}):
                return
            previous = {key: value for key, value in row.payload.items() if key not in {'boundary_ref', 'boundary_event_id'}}
            tx.put(COLLECTION, row.object_id, {**previous, 'state': 'paused', 'result': result,
                'invocation_id': binding['invocation_id'],
                'close_refs': [*row.payload.get('close_refs', []), ref]}, expected_revision=row.revision)
            tx.commit()
        execution['paused_invocation'] = binding['invocation_id']

    def closed_auxiliary(self, identity, reference):
        from .receipt_projection import closed_model_attempt_binding
        store = self.application.state.ai_turn_store
        try:
            value = store.get(reference)
            frozen = store.get_request(identity)
            if (type(value) is not dict or set(value) != {'turn_id', 'project_id', 'question', 'invocation_id',
                    'stage', 'model_request_id', 'lease', 'attempts', 'binding_kind', 'binding_ref', 'failure_ref'}
                    or value['turn_id'] != identity or value['project_id'] != frozen['scope']['project_id']
                    or value['question'] != frozen['input']['text'] or frozen['desired_outcome'] != 'project.answer'
                    or store.get_immutable_payload(identity, 'product-answer-aux-closed-' + value['model_request_id']) != (reference, value)):
                return None
            base = 'answer-gap-binding-' + value['stage'] + '-v1'
            if value['binding_kind'] != base and not value['binding_kind'].startswith(base + '-'):
                return None
            binding = store.get_immutable_payload(identity, value['binding_kind'])
            inputs = store.get_immutable_payload(identity, 'answer-gap-input-' + value['stage'] + '-v1')
            if (binding is None or binding[0] != value['binding_ref'] or inputs is None
                    or binding[1]['turn_id'] != identity or binding[1]['invocation_key'] != value['stage']
                    or binding[1]['purpose'] != 'aux' or binding[1]['model_request_id'] != value['model_request_id']
                    or binding[1]['input_ref'] != inputs[0] or inputs[1]['purpose'] != 'aux'):
                return None
            failures = [event for event in store.events_after(identity) if event['type'] == 'model.failed'
                and event['correlation']['model_request_id'] == value['model_request_id']
                and event['correlation']['tool_call_id'] == value['invocation_id']
                and event['data']['receipt_ref'] == value['failure_ref'] and event['data']['model_call_purpose'] == 'aux']
            current = closed_model_attempt_binding(self.root, identity, value['project_id'], question=value['question'],
                model_request_id=value['model_request_id'], lease=value['lease'])
            if len(failures) != 1 or current is None or current != value['attempts']:
                return None
            return value
        except (ValueError, TypeError, KeyError):
            return None

    def store_request(self, identity):
        return self.application.state.ai_turn_store.get_request(identity)

    def suspended_without_executor(self, identity):
        from .answer_continuations import COLLECTION
        with self.lock:
            executing = identity in self.active
        return not executing and self.query.records.read(COLLECTION, identity) is not None

    def admit_continuation(self, action, row):
        from .answer_continuations import COLLECTION
        with self.lock:
            execution = self.active.get(row.object_id)
        if execution is None or execution.get('continuation') != row:
            raise ValueError('answer_continuation_requires_explicit_executor')
        execution['validate_current'](row)
        if execution.get('stop_generation') is not None:
            self.application.state.ai_runtime.product_stops.require_generation(row.object_id, execution['stop_generation'])
        with self.query.records.begin() as tx:
            current = tx.read(COLLECTION, row.object_id)
            if current != row:
                raise ValueError('answer_continuation_revision_changed')
            if execution.get('stop_generation') is not None:
                from .task_continuations import STOPS
                stop = tx.read(STOPS, row.object_id)
                if stop is None or stop.payload['generation'] != execution['stop_generation']:
                    raise ValueError('product_stop_changed')
            tx.put(COLLECTION, row.object_id, {**row.payload, 'state': 'continuing',
                'action_id': action['action_id']}, expected_revision=row.revision)
            tx.commit()

    def paused(self, identity, *, require_bundle=True):
        from .answer_continuations import COLLECTION, plan_binding, plan_reference
        from .receipt_projection import closed_model_attempt_binding
        store = self.application.state.ai_turn_store
        frozen = store.get_request(identity)
        row = self.query.records.read(COLLECTION, identity)
        if (frozen is None or row is None or row.payload.get('state') != 'paused'
                or row.payload.get('project_id') != frozen['scope']['project_id']
                or row.payload.get('question') != frozen['input']['text']
                or frozen.get('desired_outcome') != 'project.answer'):
            return None
        if row.payload.get('boundary_ref'):
            capsule = store.get(row.payload['boundary_ref'])
            owner = self.application.state.ai_runtime.product_stops
            event = next((value for value in store.events_after(identity)
                if value['event_id'] == row.payload['boundary_event_id']), None)
            plan = plan_binding(self.query, row)
            if (capsule['request'] != frozen or capsule['project_id'] != frozen['scope']['project_id']
                    or capsule['plan_ref'] != row.payload['plan_ref'] or capsule['request'] != plan['request']
                    or capsule['result'] != row.payload['result'] or event is None
                    or event['type'] != 'turn.cancel.requested' or event['data']['status'] != 'waiting_approval'
                    or event['data']['evidence_refs'] != [row.payload['boundary_ref']]
                    or store.get_immutable_payload(identity, capsule['kind']) != (row.payload['boundary_ref'], capsule)
                    or self.boundary_tools(identity) != capsule['tools']
                    or owner.settled_attempts(identity) != capsule['settled_attempts']):
                return None
            return row
        refs = row.payload.get('close_refs')
        if not isinstance(refs, list) or not refs:
            return None
        try:
            original = plan_binding(self.query, row)
        except (ValueError, TypeError, KeyError):
            return None
        events = tuple(store.events_after(identity))
        requests, auxiliary_requests = set(), set()
        for ref in refs:
            try:
                if not isinstance(ref, str):
                    return None
                value = store.get(ref)
                stopped = value.get('result', {}).get('receipt', {}).get('ask', value.get('result', {})).get('interruption') == 'stopped'
                if (type(value) is not dict or set(value) != {'turn_id', 'project_id', 'question', 'invocation_id',
                        'model_request_id', 'lease', 'attempts', 'plan_ref', 'result'} | ({'stage', 'internal_partial'} if stopped else set())
                        | ({'auxiliary_close_refs'} if 'auxiliary_close_refs' in value else set())
                        or value['turn_id'] != identity or value['project_id'] != row.payload['project_id']
                        or value['question'] != row.payload['question']
                        or value['model_request_id'] in requests):
                    return None
                reference = plan_reference(self.query, identity, value['plan_ref'])[1]
                if reference['request'] != frozen or reference['project_id'] != value['project_id']:
                    return None
                immutable = store.get_immutable_payload(identity, 'product-answer-closed-' + value['model_request_id'])
                if immutable is None or immutable != (ref, value):
                    return None
                current = closed_model_attempt_binding(self.root, identity, value['project_id'], question=value['question'],
                    model_request_id=value['model_request_id'], lease=value['lease'], stopped=stopped)
                if current is None or current != value['attempts']:
                    return None
                if require_bundle:
                    bundles = [event for event in events if event['type'] == 'tool.outcome.recorded'
                        and event['correlation']['tool_call_id'] == value['invocation_id']
                        and set(event['data']['evidence_refs']) == {ref, value['plan_ref']}]
                    if len(bundles) != 1:
                        return None
                    outcome = store.get(bundles[0]['data']['payload_ref'])
                    if (outcome['invocation_id'] != value['invocation_id'] or outcome['turn_id'] != identity
                            or outcome['capability_id'] != 'workbench.answer.execute' or outcome['status'] != 'failed'
                            or outcome['effect_certainty'] != 'confirmed_none'):
                        return None
                if not requests and value['lease'] != reference['lease']:
                    return None
                auxiliary_refs = value.get('auxiliary_close_refs', [])
                if type(auxiliary_refs) is not list:
                    return None
                for auxiliary_ref in auxiliary_refs:
                    auxiliary = self.closed_auxiliary(identity, auxiliary_ref)
                    if (auxiliary is None or auxiliary['lease'] != value['lease']
                            or auxiliary['invocation_id'] != value['invocation_id']):
                        return None
                    auxiliary_requests.add(auxiliary['model_request_id'])
                requests.add(value['model_request_id'])
            except (ValueError, TypeError, KeyError):
                return None
        if (value['invocation_id'] != row.payload.get('invocation_id')
                or value['result'] != row.payload.get('result')):
            return None
        dispatched = {event['data']['payload_ref'] for event in events if event['type'] == 'model.attempt.dispatched'}
        terminals = [store.get(event['data']['receipt_ref']) for event in events if event['type'] == 'model.attempt.terminal']
        if len(dispatched) != len(terminals) or any(value['status'] == 'consumer_cancelled'
                and value['model_request_id'] not in requests for value in terminals):
            return None
        completed = {event['correlation']['model_request_id'] for event in events if event['type'] == 'model.completed'}
        if any(value['status'] != 'succeeded' and value['model_request_id'] not in requests | completed | auxiliary_requests for value in terminals):
            return None
        return row

    def _clear_prepared_plan(self, identity):
        from .answer_continuations import COLLECTION
        with self.query.records.begin() as tx:
            row = tx.read(COLLECTION, identity)
            if row is not None and row.payload.get('state') in {'prepared', 'continuing'}:
                tx.delete(COLLECTION, identity, expected_revision=row.revision)
                tx.commit()

    @with_connection_scope
    async def continue_on_request(self, *, identity, project, key, operation, validate_current):
        from uuid import uuid4
        from .answer_continuations import COLLECTION
        from core.ai_kernel import validate_turn_action
        runtime = await asyncio.to_thread(self._runtime)
        state = self.application.state
        store, runner = state.ai_turn_store, state.ai_turn_runner
        frozen = store.get_request(identity)
        if frozen is None or frozen['scope']['project_id'] != project:
            raise HTTPException(404, 'workbench_not_found')
        prior = store.get_action(key)
        if prior is not None:
            payload, _ = prior
            if payload['turn_id'] != identity or payload['type'] != 'resume':
                raise HTTPException(409, 'idempotency_key_conflict')
            result = store.get_immutable_payload(identity, RESULT_KIND)
            if result is not None:
                return result[1]
            row = self.paused(identity)
            if row is not None:
                return row.payload['result']
            raise HTTPException(409, 'turn_interrupted')
        row = self.paused(identity)
        if row is None or runtime.receipt_for(identity).status != 'waiting_approval':
            raise HTTPException(409, 'turn_interrupted')
        validate_current(row)
        action = validate_turn_action({'schema_version': '1.0.0', 'action_id': 'action-' + uuid4().hex,
            'turn_id': identity, 'type': 'resume', 'target_event_id': None, 'reason': 'continue partial answer',
            'actor': 'user', 'expected_sequence': len(tuple(store.events_after(identity))),
            'idempotency_key': key, 'created_at': _now()})
        invocation = 'answer-continue-' + action['action_id']
        execution = {'operation': lambda: operation(row, invocation), 'invocation_key': invocation,
            'continuation': row, 'validate_current': validate_current, 'action': action,
            'stop_generation': (runtime.product_stops.continuation_generation(identity)
                if row.payload['result'].get('receipt', {}).get('ask', row.payload['result']).get('interruption') == 'stopped'
                else None)}
        with self.lock:
            if identity in self.active:
                raise HTTPException(409, 'turn_in_progress')
            self.active[identity] = execution
        try:
            await asyncio.to_thread(runner.apply_action_and_wait, action)
            stopped = self.paused(identity)
            if stopped is not None and runtime.receipt_for(identity).status == 'waiting_approval':
                return stopped.payload['result']
            if isinstance(execution.get('error'), AnswerInterrupted):
                return execution['error'].result
            if 'error' in execution:
                raise execution['error']
            result = store.get_immutable_payload(identity, RESULT_KIND)
            if result is None or runtime.receipt_for(identity).status != 'completed':
                raise HTTPException(502, 'answer_generation_failed')
            with self.query.records.begin() as tx:
                current = tx.read(COLLECTION, identity)
                tx.delete(COLLECTION, identity, expected_revision=current.revision)
                tx.commit()
            return result[1]
        finally:
            with self.lock:
                self.active.pop(identity, None)

    @with_connection_scope
    async def run(self, *, turn_id, project, question, operation, policy_versions=None,
                  situation=None, part_context=None):
        if ACTIVE_ANSWER.get() is not None:
            return await operation()
        runtime = await asyncio.to_thread(self._runtime)
        state = self.application.state
        store, runner = state.ai_turn_store, state.ai_turn_runner
        existing = store.get_request(turn_id)
        if existing is not None:
            if existing["scope"]["project_id"] != project or existing["input"]["text"] != question:
                raise HTTPException(409, "idempotency_key_conflict")
            result = store.get_immutable_payload(turn_id, RESULT_KIND)
            if result is not None and runtime.receipt_for(turn_id).status == "completed":
                return result[1]
            raise HTTPException(409, "turn_interrupted")
        target = self.query.ask_target()
        privacy = self.query.freeze_answer_privacy(project, local_only=target["execution_location"] == "local")
        request = freeze_turn_request("project.answer", turn_id=turn_id, session_id="session-" + turn_id,
            operation_id="answer-" + turn_id, idempotency_key="answer-" + turn_id,
            project_id=project, created_at=_now(), text=question, privacy=privacy,
            capabilities=["workbench.answer.execute"], template_version=2, situation=situation)
        if policy_versions is not None:
            request['policy_versions'] = dict(policy_versions)
        request["context_policy"].update(include_memory=False)
        from ..part_context_binding import bind_context
        bind_context(self.query.records, request, part_context)
        request["capability_request"] = {"mode": "execute_exact_v1",
            "capability_id": "workbench.answer.execute", "arguments": {"query": question}}
        validate_turn_request(request)
        execution = {"operation": operation}
        with self.lock:
            if turn_id in self.active:
                raise HTTPException(409, "turn_in_progress")
            self.active[turn_id] = execution
        try:
            await asyncio.to_thread(runner.accept_and_submit, request)
            from .answer_contract import answer_lifecycle
            deadline = monotonic() + answer_lifecycle(request)['wait_timeout_seconds']
            receipt = None
            while monotonic() < deadline:
                receipt = await asyncio.to_thread(runner.wait_for_terminal, turn_id, timeout_seconds=.05)
                if receipt is not None:
                    break
                if self.paused(turn_id) is not None and runtime.receipt_for(turn_id).status == 'waiting_approval':
                    break
            stopped = self.paused(turn_id)
            if stopped is not None and runtime.receipt_for(turn_id).status == 'waiting_approval':
                return stopped.payload['result']
            if "error" in execution:
                if isinstance(execution['error'], AnswerInterrupted):
                    return execution['error'].result
                raise execution["error"]
            result = store.get_immutable_payload(turn_id, RESULT_KIND)
            if receipt is None or receipt.status != "completed" or result is None:
                raise HTTPException(502, "answer_generation_failed")
            return result[1]
        finally:
            if runtime.receipt_for(turn_id).status in {'completed', 'failed', 'cancelled'}:
                self._clear_prepared_plan(turn_id)
            with self.lock:
                self.active.pop(turn_id, None)


def _answer_failure_classification(error):
    """Only finite local facts cross the diagnostic boundary; never error text."""
    from backend.recognition import RecognitionError
    from backend.shared.llm.model_transport import ModelInterrupted
    from core.ai_kernel.event_store import RunLeaseRevoked
    from ..model_config import ModelConfigurationError, ModelResponseDecodeError

    if (type(error) is ModelResponseDecodeError and error.completed_wire is True
            and type(error.decode_code) is str
            and error.decode_code in {'json_empty', 'json_object_required', 'json_invalid', 'schema_invalid'}):
        return 'decode', error.decode_code, True
    if isinstance(error, RecognitionError):
        return 'source', 'recognition_error', None
    if isinstance(error, RunLeaseRevoked):
        return 'control', 'lease_revoked', None
    if isinstance(error, ModelInterrupted):
        return 'control', 'interrupted', None
    if isinstance(error, asyncio.CancelledError):
        return 'control', 'cancelled', None
    if isinstance(error, TimeoutError):
        return 'deadline', 'timeout', None
    if (type(error) is ModelConfigurationError and len(error.args) == 1
            and type(error.args[0]) is str):
        code = error.args[0]
        if code in {'model_output_incomplete', 'model_output_missing_content', 'model_response_invalid'}:
            return 'provider_output', code, None
        if code in {'model_configuration_changed_before_request', 'model_configuration_changed_during_request',
                    'model_egress_not_configured', 'model_egress_endpoint_invalid', 'model_egress_remote_not_consented'}:
            return 'configuration', code, None
        if code == 'model_request_failed: check endpoint, model and credentials':
            # This wrapper can also hide an older non-stream decode rejection.
            return 'model', 'model_request_failed', None
    return 'other', 'unclassified_failure', None


def _answer_failure_usage(value):
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in ('input_tokens', 'output_tokens', 'total_tokens')
            if type(value.get(key)) is int and value[key] >= 0}


def _record_answer_failure(store, request, invocation, handle, input_ref, schema, error):
    """A diagnostic snapshot, never a generated result or answer evidence.

    A pending attempt keeps its dispatch reference.  Later terminal/usage facts
    remain in the normal attempt ledger; this immutable snapshot is not revised.
    """
    category, code, completed = _answer_failure_classification(error)
    observed = answer_observation(invocation)
    payload = {'version': 'answer-model-failure-v1', 'turn_id': request['turn_id'],
        'invocation': invocation, 'model_request_id': handle.model_request_id, 'input_ref': input_ref,
        'response_schema_sha256': sha256(json.dumps(schema, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode('utf-8')).hexdigest(),
        'category': category, 'code': code, 'completed_wire': completed,
        'usage': _answer_failure_usage(error.usage) if completed is True else {},
        'observations': [_answer_failure_usage(item) for item in observed['observations']],
        'receipt_ids': list(observed['receipt_ids']), 'usage_complete': observed['complete'] is True}
    store.get_or_create_immutable_payload(request['turn_id'], 'answer-model-failure-' + invocation, payload)


def generate_answer(models, messages, *, response_model, max_tokens, validate_current,
                    on_delta=None, purpose="primary", invocation_key="answer", timeout_seconds=None, gap_plan=None, on_retry=None,
                    failure_diagnostics=False, **unused):
    """Use the tool-owned model lifecycle; domain code never owns a wire call."""
    active = ACTIVE_ANSWER.get()
    if active is None:
        raise RuntimeError("answer model call requires an active kernel Turn")
    if gap_plan is not None:
        rag = gap_plan.get('_rag')
        expected = ('rag-decision-' + str(rag['round'])) if rag is not None else 'gap-drilldown'
        if purpose != 'aux' or invocation_key != expected:
            raise ValueError('gap_invocation_changed')
        return _generate_gap_answer(models, messages, response_model=response_model, max_tokens=max_tokens,
            validate_current=validate_current, on_delta=on_delta, timeout_seconds=timeout_seconds, plan=gap_plan,
            invocation=invocation_key)
    request, store, query, handles = active
    def validate_wire():
        validate_current()
        query.validate_answer_request(models, store.get_request(request["turn_id"]))
    validate_wire()
    kind = "answer-model-result-" + invocation_key
    existing = store.get_immutable_payload(request["turn_id"], kind)
    if existing is not None:
        payload = existing[1]
        return response_model.model_validate(payload["output"]), payload["metadata"]
    schema = response_model.model_json_schema()
    input_ref = store.get_or_create_immutable_payload(request["turn_id"], "answer-model-input-" + invocation_key,
        {"messages": messages, "purpose": purpose, "response_schema": schema})
    execution = query.answer_turns.active[request['turn_id']]
    call_key = (invocation_key + '-' + execution['invocation_key']
        if execution.get('continuation') is not None else invocation_key)
    from ..turn_routing import RecognitionRoutingSnapshot, SNAPSHOT_KIND, _revision
    frozen = store.get_immutable_payload(request["turn_id"], SNAPSHOT_KIND)
    if frozen is None:
        raise RuntimeError("answer model routing is unavailable")
    route = RecognitionRoutingSnapshot(frozen[0], _revision(frozen[1]), frozen[1]).generation_binding()
    main_models = models
    if purpose == 'aux':
        from .aux_routing import auxiliary_route
        models, route = auxiliary_route(models, store, request['turn_id'], request['scope']['project_id'], route)
    activation = None
    if purpose == 'primary':
        from .provider_store_binding import main_provider_store_activation
        activation = main_provider_store_activation(models, query.records,
            store.get_request(request['turn_id']), frozen[1])
    elif purpose == 'aux':
        from .provider_store_binding import auxiliary_provider_store_activation
        activation = auxiliary_provider_store_activation(main_models, models, query.records,
            store, store.get_request(request['turn_id']), frozen[1], route)
    inner = begin_nested_model_call(request, invocation_key=call_key, purpose=purpose)
    handle = _AnswerCall(inner, inner.runtime, request["turn_id"])
    handles[invocation_key] = handle
    try:
        output, metadata = models.complete_governed(messages, routing_snapshot=route,
            execution_control=execution_control_from(request), metadata_sink=handle, wire_attempt_sink=handle,
            response_model=response_model, max_tokens=max_tokens, validate_current=validate_wire,
            on_delta=on_delta, purpose=purpose, timeout_seconds=timeout_seconds, retry_policy=retry_policy_for(),
            stop_requested=lambda: handle.runtime.product_stops.checkpoint(request['turn_id']),
            **({'provider_store_activation': activation} if activation is not None else {}),
            **({'on_retry': on_retry} if on_retry is not None else {}))
        validate_wire()
        with handle.lock:
            if handle.terminal == "failed":
                handle.discard()
                raise TimeoutError("answer model result arrived after terminal")
            metadata = {**metadata, "kernel_receipt_refs": list(handle.finalize(error_code=None))}
            store.get_or_create_immutable_payload(request["turn_id"], kind,
                {"output": output.model_dump(), "metadata": metadata})
            return output, metadata
    except BaseException as error:
        from backend.shared.llm.model_transport import ModelInterrupted
        if isinstance(error, ModelInterrupted):
            handle.close_witness = error.close_witness
            handle.interruption = error.interruption
            handle.internal_partial = getattr(error, 'internal_partial', '')
        if handle.terminal is not None:
            handle.discard()
        handle.finalize(error_code="ai.answer_model_failed")
        if failure_diagnostics is True:
            try:
                _record_answer_failure(store, request, invocation_key, handle, input_ref, schema, error)
            except Exception:
                # Optional archival must never replace the original failure or
                # trigger a different model retry/fallback decision.
                pass
        raise


def gap_answer_input(models, plan):
    """Prepare proof with the fixed original auxiliary route; allocate no call."""
    active = ACTIVE_ANSWER.get()
    if active is None:
        raise RuntimeError('gap model call requires an active kernel Turn')
    request, store, query, _ = active
    frozen_request = store.get_request(request['turn_id'])
    if (plan['project_id'] != request['scope']['project_id'] or plan['question'] != frozen_request['input']['text']
            or ('_drilldown' not in plan and '_rag' not in plan)
            or plan['policy_versions'] != frozen_request['policy_versions']):
        raise ValueError('gap_turn_identity_changed')
    if '_rag' in plan:
        from ..v2.policies import get
        policy = get('retrieve', version=frozen_request['policy_versions']['retrieve'])
        if (not getattr(policy, 'agentic', False) or type(plan['_rag']['round']) is not int
                or not 1 <= plan['_rag']['round'] <= policy(None, operation='agentic_limits')['decisions']):
            raise ValueError('gap_turn_identity_changed')
    query.validate_answer_request(models, frozen_request)
    from ..turn_routing import RecognitionRoutingSnapshot, SNAPSHOT_KIND, _revision
    from .aux_routing import auxiliary_route
    frozen = store.get_immutable_payload(request['turn_id'], SNAPSHOT_KIND)
    if frozen is None:
        raise RuntimeError('answer model routing is unavailable')
    primary = RecognitionRoutingSnapshot(frozen[0], _revision(frozen[1]), frozen[1]).generation_binding()
    selected, route = auxiliary_route(models, store, request['turn_id'], request['scope']['project_id'], primary)
    materials = query.gap_materials(plan, selected, local_only=route['execution_location'] == 'local_loopback')
    proof = {key: materials[key] for key in ('privacy', 'selection')}
    return selected, route, {'messages': materials['messages'], 'material_proof': proof, 'policy_versions': dict(plan['policy_versions'])}


def _generate_gap_answer(models, messages, *, response_model, max_tokens, validate_current, on_delta, timeout_seconds, plan, invocation='gap-drilldown'):
    request, store, query, handles = ACTIVE_ANSWER.get()
    turn_id = request['turn_id']
    input_kind, binding_kind = 'answer-gap-input-' + invocation + '-v1', 'answer-gap-binding-' + invocation + '-v1'
    result_kind = 'answer-model-result-' + invocation
    validate_current()
    selected, route, prepared = gap_answer_input(models, plan)
    if messages != prepared['messages']:
        raise ValueError('gap_messages_changed')
    payload = deepcopy({**prepared, 'purpose': 'aux', 'response_schema': response_model.model_json_schema(),
        'max_tokens': max_tokens, 'route_ref': route['payload_ref'], 'route_revision': route['revision']})

    def validate_wire():
        validate_current()
        _, current_route, current = gap_answer_input(models, plan)
        if (messages != payload['messages'] or current != {key: payload[key] for key in prepared}
                or current_route != route):
            raise ValueError('gap_inputs_changed')

    with handles.invocation_lock:
        validate_wire()
        saved_input = store.get_immutable_payload(turn_id, input_kind)
        binding = store.get_immutable_payload(turn_id, binding_kind)
        result = store.get_immutable_payload(turn_id, result_kind)
        if saved_input is not None or binding is not None or result is not None:
            if saved_input is None or saved_input[1] != payload or binding is None:
                raise ValueError('gap_invocation_unbound_or_changed')
            expected = {'version': 1, 'turn_id': turn_id, 'invocation_key': invocation, 'purpose': 'aux',
                'input_ref': saved_input[0], 'route_ref': route['payload_ref'], 'route_revision': route['revision']}
            fact = binding[1]
            if (not isinstance(fact, dict) or set(fact) != {*expected, 'model_request_id'}
                    or type(fact['version']) is not int
                    or any(fact[key] != value for key, value in expected.items())
                    or not isinstance(fact['model_request_id'], str) or not fact['model_request_id']):
                raise ValueError('gap_invocation_binding_invalid')
            events = [event for event in store.events_after(turn_id)
                if event.get('correlation', {}).get('model_request_id') == fact['model_request_id']]
            if not any(event['type'] == 'model.requested' and event['data'].get('model_call_purpose') == 'aux'
                    for event in events):
                raise ValueError('gap_invocation_call_unavailable')
            if result is not None:
                receipts = result[1]['metadata'].get('kernel_receipt_refs', [])
                if not any(event['type'] == 'model.completed' and event['data']['receipt_ref'] in receipts
                        for event in store.events_after(turn_id)):
                    raise ValueError('gap_invocation_result_unbound')
                return response_model.model_validate(result[1]['output']), result[1]['metadata']
            execution = query.answer_turns.active[turn_id]
            row = execution.get('continuation')
            if row is None or not any(store.get(ref).get('stage') == invocation
                    and store.get(ref)['model_request_id'] == fact['model_request_id']
                    for ref in row.payload['close_refs']):
                raise ValueError('gap_invocation_interrupted')
            binding_kind += '-' + execution['invocation_key']
        input_ref = store.get_or_create_immutable_payload(turn_id, input_kind, payload)
        execution = query.answer_turns.active[turn_id]
        call_key = invocation + '-' + execution['invocation_key'] if execution.get('continuation') is not None else invocation
        inner = begin_nested_model_call(request, invocation_key=call_key, purpose='aux')
        handle = _AnswerCall(inner, inner.runtime, turn_id)
        handles[invocation] = handle
        try:
            binding_ref = store.get_or_create_immutable_payload(turn_id, binding_kind,
                {'version': 1, 'turn_id': turn_id, 'invocation_key': invocation, 'purpose': 'aux',
                 'model_request_id': handle.model_request_id, 'input_ref': input_ref,
                 'route_ref': route['payload_ref'], 'route_revision': route['revision']})
            handle.auxiliary_binding = binding_kind, binding_ref
        except BaseException:
            handle.finalize(error_code='ai.answer_gap_binding_failed')
            raise
    try:
        output, metadata = selected.complete_governed(messages, routing_snapshot=route,
            execution_control=execution_control_from(request), metadata_sink=handle, wire_attempt_sink=handle,
            response_model=response_model, max_tokens=max_tokens, validate_current=validate_wire,
            on_delta=on_delta, purpose='aux', timeout_seconds=timeout_seconds, retry_policy=retry_policy_for(),
            stop_requested=lambda: handle.runtime.product_stops.checkpoint(turn_id))
        validate_wire()
        with handle.lock:
            if handle.terminal == 'failed':
                handle.discard()
                raise TimeoutError('answer model result arrived after terminal')
            metadata = {**metadata, 'kernel_receipt_refs': list(handle.finalize(error_code=None))}
            store.get_or_create_immutable_payload(turn_id, result_kind,
                {'output': output.model_dump(), 'metadata': metadata})
            return output, metadata
    except BaseException as error:
        from backend.shared.llm.model_transport import ModelInterrupted
        if isinstance(error, ModelInterrupted):
            handle.close_witness = error.close_witness
            handle.interruption = error.interruption
            handle.internal_partial = getattr(error, 'internal_partial', '')
        elif getattr(error, 'close_witness', None) is not None:
            handle.close_witness = error.close_witness
        if handle.terminal is not None:
            handle.discard()
        handle.finalize(error_code='ai.answer_model_failed')
        raise


def auxiliary_answer_models(models):
    active = ACTIVE_ANSWER.get()
    if active is None:
        return models
    request, store, _, _ = active
    from .aux_routing import auxiliary_models
    return auxiliary_models(models, store, request['turn_id'])


def answer_observation(invocation_key):
    """Read durable wire facts even when output validation or a deadline failed."""
    active = ACTIVE_ANSWER.get()
    if active is None:
        return {"receipt_ids": [], "observations": [], "complete": False}
    request, store, _, handles = active
    handle = handles.get(invocation_key)
    if handle is None:
        saved = store.get_immutable_payload(request['turn_id'], 'answer-model-result-' + invocation_key)
        refs = saved[1]['metadata'].get('kernel_receipt_refs', []) if saved is not None else []
        completed = [event for event in store.events_after(request['turn_id'])
            if event['type'] == 'model.completed' and event['data']['receipt_ref'] in refs]
        if len(completed) != 1:
            return {'receipt_ids': [], 'observations': [], 'complete': False}
        identity = completed[0]['correlation']['model_request_id']
    else:
        identity = handle.model_request_id
    events = [event for event in store.events_after(request["turn_id"])
              if event.get("correlation", {}).get("model_request_id") == identity]
    dispatched = [event for event in events if event["type"] == "model.attempt.dispatched"]
    terminals = [event for event in events if event["type"] == "model.attempt.terminal"]
    refs = [event["data"]["receipt_ref"] for event in terminals]
    usage = [store.get(ref).get("usage") or {} for ref in refs]
    # An inflight attempt has only its immutable dispatch reference. The
    # eventual receipt can be located by its model_request_id/attempt_id.
    if len(terminals) < len(dispatched):
        refs.extend(event["data"]["payload_ref"] for event in dispatched[len(terminals):])
    return {"receipt_ids": refs, "observations": usage,
            "complete": bool(dispatched) and len(terminals) == len(dispatched) and all(usage)}
