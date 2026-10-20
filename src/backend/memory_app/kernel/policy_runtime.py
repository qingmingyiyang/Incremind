"""Bind durable product decisions while the unchanged kernel executes a Turn."""
from contextlib import contextmanager

from core.ai_kernel import SynchronousAIRuntime, validate_turn_action
from core.ai_kernel.runtime import AIKernelRuntimeError
from core.ai_kernel.tool_invocation import ToolInvocationOutcome

from ..v2.policies import get, override
from ..v2.policies.pipelines import TURN_PIPELINES, interfaces_for_turn
from .answer_contract import answer_contract_scope


def retry_policy_for(request=None):
    """Resolve through the existing product-policy binding boundary."""
    if request is None:
        return get('retry')
    selections = request.get('policy_versions')
    if selections is None:
        selections = {}
    return get('retry', version=selections.get('retry', '@1'))


class ProductPolicyRuntime(SynchronousAIRuntime):
    def run_accepted_turn(self, turn_id, run_lease=None):
        return super().run_accepted_turn(turn_id, run_lease=run_lease)

    def apply_action(self, action, run_lease=None):
        receipt = super().apply_action(action, run_lease=run_lease)
        owner = getattr(self, 'product_stops', None)
        if owner is not None:
            owner.release_closed_action_lease(receipt.turn_id, run_lease, receipt)
        return receipt

    def _check_tool_call_budget(self, turn_id, invocation_ids):
        invocation_ids = tuple(invocation_ids)
        if self._continued_answer_tool(turn_id, invocation_ids):
            return
        return super()._check_tool_call_budget(turn_id, invocation_ids)

    def _continued_answer_tool(self, turn_id, invocation_ids):
        provider, owner = self._answer_provider(turn_id), getattr(self, 'product_stops', None)
        if provider is None or owner is None or len(invocation_ids) != 1:
            return False
        request = self._state.get_request(turn_id)
        exact = {'mode': 'execute_exact_v1', 'capability_id': 'workbench.answer.execute',
            'arguments': {'query': request['input']['text']}}
        if (request.get('agent_binding') is not None or request.get('capability_request') != exact
                or request['capability_policy']['allowed'] != ['workbench.answer.execute']):
            return False
        execution = provider.service.active.get(turn_id)
        continuation = execution.get('continuation') if execution is not None else None
        action = execution.get('action') if execution is not None else None
        if (continuation is None or continuation.object_id != turn_id or continuation.payload.get('state') != 'paused'
                or action is None or action['type'] != 'resume' or action['turn_id'] != turn_id
                or execution.get('stop_generation') is None):
            return False
        lease = self._run_lease_context.get()
        row = owner.current(turn_id)
        if lease is None or lease.turn_id != turn_id or row is None or self._state.assert_active_run_lease(lease) is None:
            return False
        owner.acknowledged_cancels(turn_id)
        accepted = [value for _, value in row.payload['resumes'] if value['action'] == action
            and value['stop_generation'] == execution['stop_generation']
            and value['lease'] == {'owner_id': lease.owner_id, 'generation': lease.generation}]
        if len(accepted) != 1:
            return False
        references = set(continuation.payload.get('close_refs', []))
        references.add(continuation.payload.get('boundary_ref'))
        if self._payloads.get(accepted[0]['proof_ref'])['proof_ref'] not in references:
            return False
        closed = set()
        for _, value in row.payload['resumes']:
            if value['action']['turn_id'] != turn_id:
                continue
            proof = self._payloads.get(value['proof_ref'])
            if (self._state.get_immutable_payload(turn_id, 'product-user-stop-' + str(proof['generation']))
                    != (value['proof_ref'], proof) or proof['request'] != request):
                return False
            if proof['proof_ref'] not in references:
                continue
            capsule = self._payloads.get(proof['proof_ref'])
            if capsule['result']['receipt']['ask'].get('interruption') != 'stopped':
                return False
            if 'invocation_id' in capsule:
                closed.add(capsule['invocation_id'])
            closed.update(tool['invocation_id'] for tool in capsule.get('tools', []))
        claimed = execution.get('continued_tool_call_id')
        if claimed is not None and invocation_ids != (claimed,):
            return False
        events = [event for event in self._events.events_after(turn_id)
            if event['type'] in {'tool.requested', 'tool.intent.recorded'}]
        historical = {event['correlation']['tool_call_id'] for event in events} - {claimed}
        if not historical or not historical <= closed or invocation_ids[0] in closed:
            return False
        for event in events:
            if event['data']['capability_id'] != exact['capability_id']:
                return False
            if event['type'] == 'tool.intent.recorded':
                intent = self._payloads.get(event['data']['payload_ref'])
                if intent['turn_id'] != turn_id or intent['arguments'] != exact['arguments']:
                    return False
        # 严格接纳的继续只领取同一只读动作的一个新物理身份，原预算保持。
        execution['continued_tool_call_id'] = invocation_ids[0]
        return True

    def request_background_cancel(self, turn_id, run_lease=None, *, reason=None):
        owner = getattr(self, 'product_stops', None)
        if reason == 'user stopped product turn':
            if owner is None:
                return False
        elif owner is None or not owner.requested(turn_id):
            return super().request_background_cancel(turn_id, run_lease)
        with self._planner_controls_lock, owner.lock:
            if not (owner.cancellation_requested(turn_id) if reason == 'user stopped product turn' else owner.requested(turn_id)):
                return False
            scope = self._bind_run_lease(turn_id, run_lease)
            try:
                owner.ensure_cancel_event(turn_id)
                return super().request_background_cancel(turn_id, run_lease)
            finally:
                self._run_lease_context.reset(scope)

    def _begin_planner_control(self, turn_id, **kwargs):
        with self._planner_controls_lock:
            control = super()._begin_planner_control(turn_id, **kwargs)
            owner = getattr(self, 'product_stops', None)
            if owner is not None and owner.requested(turn_id):
                owner.ensure_cancel_event(turn_id)
                self._request_planner_cancel(turn_id, reason='user stopped product turn', payload_ref=None)
            return control

    def _append_turn_terminal(self, turn_id, event_type, status, summary, **kwargs):
        tasks = getattr(self, 'task_continuations', None)
        if event_type == 'turn.cancelled':
            provider = self._answer_provider(turn_id)
            event = (provider.service.pause_boundary(turn_id) if provider is not None else
                tasks.pause_boundary(self, turn_id) if tasks is not None else None)
            if event is not None:
                return event
        return super()._append_turn_terminal(turn_id, event_type, status, summary, **kwargs)

    @contextmanager
    def _policy_scope(self, turn_id):
        request = self._state.get_request(turn_id)
        if request is None:
            with answer_contract_scope(None):
                yield
            return
        selections = request.get('policy_versions')
        if selections is None:
            recipe = TURN_PIPELINES.get(request.get('desired_outcome'))
            selections = {name: '@1' for name in interfaces_for_turn(request['desired_outcome'])} if recipe else {}
        elif request.get('desired_outcome') in TURN_PIPELINES:
            # Missing retry on an existing frozen recipe is historical @1,
            # never the ACTIVE selection at recovery time. Do not rewrite it.
            selections = {'retry': '@1', **selections}
        with override(**selections), answer_contract_scope(request):
            yield

    def _run_accepted_turn(self, turn_id):
        with self._policy_scope(turn_id):
            tasks = getattr(self, 'task_continuations', None)
            if tasks is not None and tasks.suspended(turn_id):
                return self._receipt(turn_id)
            provider = self._answer_provider(turn_id)
            if provider is not None and provider.service.suspended_without_executor(turn_id):
                return self._receipt(turn_id)
            return super()._run_accepted_turn(turn_id)

    def _converge_active_planner_failure(self, turn_id, control, error):
        tasks = getattr(self, 'task_continuations', None)
        if tasks is not None:
            with self._planner_controls_lock:
                worker = tasks.worker(turn_id)
                stopped = (getattr(error, 'interruption', None) == 'stopped' and tasks.stops is not None
                    and tasks.stops.requested(turn_id))
                if worker and not stopped and not tasks.pure_text(error):
                    return super()._converge_active_planner_failure(turn_id, control, error)
                if tasks.pause(self, turn_id, control, error):
                    if worker and not stopped and turn_id not in tasks.active:
                        maximum = retry_policy_for(tasks.planner.profile_request(error.request))(
                            {'kind': 'partial_limits'})['text_continuations']
                        receipt = self._receipt(turn_id)
                        for _ in range(maximum):
                            current = tasks.latest_interruption.get(turn_id)
                            if not tasks.pure_text(current):
                                return receipt
                            binding = tasks.paused(turn_id, current.request['scope']['project_id'])
                            if binding is None:
                                return receipt
                            tasks.validate_current(binding[1])
                            lease = self._run_lease_context.get()
                            if lease is None or self._state.assert_active_run_lease(lease) is None:
                                return receipt
                            with tasks.executing(turn_id, None, binding):
                                self._append(turn_id, 'turn.resumed', 'running', 'continue closed pure text worker', actor='system')
                                receipt = self._run(turn_id)
                            if receipt.status != 'waiting_approval':
                                return receipt
                        return self._fail(turn_id, error)
                    return self._receipt(turn_id)
        return super()._converge_active_planner_failure(turn_id, control, error)

    def _answer_provider(self, turn_id):
        request = self._state.get_request(turn_id)
        if (request is None or request.get('desired_outcome') != 'project.answer'
                or request.get('execution_policy', {}).get('template_version') != 2):
            return None
        resolved = self._registry.resolve('workbench.answer.execute')
        if resolved is None or not callable(getattr(resolved[1], 'paused_invocation', None)):
            return None
        return resolved[1]

    def _record_failed_tool_outcome(self, intent, *, attempt, error_code, effect_certainty):
        provider = self._answer_provider(intent.turn_id)
        row = provider.paused_invocation(intent) if provider is not None else None
        if (row is None or intent.capability_id != 'workbench.answer.execute'
                or effect_certainty != 'confirmed_none'):
            return super()._record_failed_tool_outcome(intent, attempt=attempt,
                error_code=error_code, effect_certainty=effect_certainty)
        outcome = ToolInvocationOutcome(invocation_id=intent.invocation_id, turn_id=intent.turn_id,
            capability_id=intent.capability_id, attempt=max(attempt, 1), status='failed',
            effect_certainty=effect_certainty, payload_ref=None, receipt_ref=None, evidence_refs=(),
            error_code=error_code, retryable=False)
        self._append_tool_outcome_bundle(outcome, intent.turn_id, summary='tool failure outcome recorded',
            capability_id=intent.capability_id, error_code=error_code,
            step_id=intent.step_id, tool_call_id=intent.invocation_id,
            evidence_refs=(row.payload['plan_ref'], row.payload['close_refs'][-1]))
        # This internal worker-stop status grants no approval or egress fact.
        self._append(intent.turn_id, 'tool.failed', 'waiting_approval', 'answer interrupted; explicit continuation required',
            capability_id=intent.capability_id, error_code=error_code,
            step_id=intent.step_id, tool_call_id=intent.invocation_id)
        result = row.payload['result']
        answer = result.get('receipt', {}).get('ask', result)
        if answer.get('interruption') == 'stopped':
            self.product_stops.pause(intent.turn_id, tuple(self._events.events_after(intent.turn_id))[-1],
                row.payload['close_refs'][-1])
        return self._receipt(intent.turn_id)

    def _record_cancelled_tool_outcome(self, intent, *, error_code, timed_out=False):
        provider = self._answer_provider(intent.turn_id)
        row = provider.paused_invocation(intent) if provider is not None else None
        if (not timed_out and row is not None and self.product_stops.requested(intent.turn_id)):
            return self._record_failed_tool_outcome(intent, attempt=max(self._tool_attempt_count(
                intent.turn_id, intent.invocation_id), 1), error_code=error_code, effect_certainty='confirmed_none')
        return super()._record_cancelled_tool_outcome(intent, error_code=error_code, timed_out=timed_out)

    def recover_accepted_turn(self, turn_id, run_lease):
        provider = self._answer_provider(turn_id)
        tasks = getattr(self, 'task_continuations', None)
        if ((provider is not None and provider.service.suspended_without_executor(turn_id))
                or (tasks is not None and tasks.suspended(turn_id))):
            scope = self._bind_run_lease(turn_id, run_lease)
            try:
                return self._receipt(turn_id)
            finally:
                self._run_lease_context.reset(scope)
        return super().recover_accepted_turn(turn_id, run_lease)

    def _apply_action(self, action):
        payload = validate_turn_action(action)
        tasks = getattr(self, 'task_continuations', None)
        if (tasks is not None and tasks.suspended(payload['turn_id']) and payload['type'] in {'resume', 'approve'}
                and self._state.get_action(payload['idempotency_key']) is None):
            if payload['type'] != 'resume':
                raise AIKernelRuntimeError('task continuation requires an explicit product executor')
            with self._policy_scope(payload['turn_id']):
                try:
                    tasks.admit(self, payload)
                    return super()._apply_action(action)
                except (AIKernelRuntimeError, ValueError) as error:
                    execution = tasks.active[payload['turn_id']]
                    stop = self.product_stops.current(payload['turn_id'])
                    start = stop.payload['members'].get(payload['turn_id']) if stop is not None else None
                    added = tuple(self._events.events_after(payload['turn_id']))[start:] if start is not None else ()
                    conflict = (type(error) is ValueError and str(error) == 'product_stop_changed'
                        or isinstance(error, AIKernelRuntimeError) and str(error) == 'turn action expected sequence conflict' and added)
                    if (conflict and execution['action'] == payload
                            and execution.get('stop_generation') is not None and stop is not None
                            and stop.payload['generation'] > execution['stop_generation']
                            and stop.payload['state'] in {'requested', 'paused'}
                            and all(event['type'] == 'turn.cancel.requested' for event in added)
                            and start is not None and start <= payload['expected_sequence']
                            and tasks.pause_boundary(self, payload['turn_id']) is not None):
                        # 原继续尚未接纳；新的真实停止保持等待，不改动作序列或记录成功。
                        return self._receipt(payload['turn_id'])
                    raise
        provider = self._answer_provider(payload['turn_id'])
        if payload['type'] != 'resume' or provider is None or self._state.get_action(payload['idempotency_key']) is not None:
            return super()._apply_action(action)
        row = provider.service.paused(payload['turn_id'])
        if row is None:
            return super()._apply_action(action)
        events = tuple(self._events.events_after(payload['turn_id']))
        boundary = row.payload.get('boundary_event_id')
        if (not events or payload['expected_sequence'] != len(events)
                or events[-1]['data']['status'] != 'waiting_approval'
                or (events[-1]['event_id'] != boundary if boundary else
                    events[-1]['type'] != 'tool.failed'
                    or events[-1]['correlation']['tool_call_id'] != row.payload['invocation_id'])):
            raise AIKernelRuntimeError('partial answer action sequence conflict')
        with self._policy_scope(payload['turn_id']):
            provider.service.admit_continuation(payload, row)
            action_ref = self._payloads.put(payload['turn_id'], 'turn-action', payload)
            self._append(payload['turn_id'], 'turn.resumed', 'running', str(payload['reason']),
                actor='user', payload_ref=action_ref)
            acknowledged = self.product_stops.acknowledge(payload['turn_id'], payload,
                generation=provider.service.active[payload['turn_id']].get('stop_generation'))
            if acknowledged is False:
                if provider.service.pause_boundary(payload['turn_id']) is None:
                    raise AIKernelRuntimeError('new user stop has unsettled answer work')
                receipt = self._receipt(payload['turn_id'])
                self._state.save_action(payload, receipt)
                return receipt
            receipt = self._run_accepted_turn(payload['turn_id'])
            self._state.save_action(payload, receipt)
            return receipt

    def _run(self, turn_id):
        with self._policy_scope(turn_id):
            tasks = getattr(self, 'task_continuations', None)
            execution = tasks.active.get(turn_id) if tasks is not None else None
            if execution is not None and execution['action'] is not None:
                acknowledged = self.product_stops.acknowledge(turn_id, execution['action'],
                    generation=execution['stop_generation'])
                if acknowledged is False and tasks.pause_boundary(self, turn_id) is not None:
                    return self._receipt(turn_id)
            return super()._run(turn_id)

    def _turn_cancel_requested(self, turn_id):
        owner = getattr(self, 'product_stops', None)
        if owner is not None and owner.requested(turn_id):
            owner.ensure_cancel_event(turn_id)
            return True
        acknowledged = owner.acknowledged_cancels(turn_id) if owner is not None else set()
        if not acknowledged:
            return super()._turn_cancel_requested(turn_id)
        events = tuple(self._events.events_after(turn_id))
        requested = max((event['sequence'] for event in events if event['type'] == 'turn.cancel.requested'
            and event['event_id'] not in acknowledged), default=0)
        outcome = max((event['sequence'] for event in events if event['type'] == 'tool.outcome.recorded'), default=0)
        return requested > 0 and outcome > requested

    def _batch_cancel_requested(self, turn_id):
        owner = getattr(self, 'product_stops', None)
        if owner is not None and owner.requested(turn_id):
            owner.ensure_cancel_event(turn_id)
            return True
        acknowledged = owner.acknowledged_cancels(turn_id) if owner is not None else set()
        if not acknowledged:
            return super()._batch_cancel_requested(turn_id)
        return any(event['type'] == 'turn.cancel.requested' and event['event_id'] not in acknowledged
            for event in self._events.events_after(turn_id))

    def _invoke_tool(self, turn_id, decision):
        with self._policy_scope(turn_id):
            return super()._invoke_tool(turn_id, decision)

    def _resume_incomplete_tool(self, turn_id):
        with self._policy_scope(turn_id):
            return super()._resume_incomplete_tool(turn_id)

    def _resume_tool_batch(self, turn_id):
        with self._policy_scope(turn_id):
            return super()._resume_tool_batch(turn_id)

    def _continue_mcp_request_state(self, turn_id, pending):
        with self._policy_scope(turn_id):
            return super()._continue_mcp_request_state(turn_id, pending)
