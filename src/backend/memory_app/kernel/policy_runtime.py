"""Bind durable product decisions while the unchanged kernel executes a Turn."""
from contextlib import nullcontext

from core.ai_kernel import SynchronousAIRuntime, validate_turn_action
from core.ai_kernel.runtime import AIKernelRuntimeError
from core.ai_kernel.tool_invocation import ToolInvocationOutcome

from ..v2.policies import get, override
from ..v2.policies.pipelines import TURN_PIPELINES, interfaces_for_turn


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

    def _policy_scope(self, turn_id):
        request = self._state.get_request(turn_id)
        if request is None:
            return nullcontext()
        selections = request.get('policy_versions')
        if selections is None:
            recipe = TURN_PIPELINES.get(request.get('desired_outcome'))
            selections = {name: '@1' for name in interfaces_for_turn(request['desired_outcome'])} if recipe else {}
        elif request.get('desired_outcome') in TURN_PIPELINES:
            # Missing retry on an existing frozen recipe is historical @1,
            # never the ACTIVE selection at recovery time. Do not rewrite it.
            selections = {'retry': '@1', **selections}
        return override(**selections)

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
                if worker and not tasks.pure_text(error):
                    return super()._converge_active_planner_failure(turn_id, control, error)
                if tasks.pause(self, turn_id, control, error):
                    if worker and turn_id not in tasks.active:
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
        return self._receipt(intent.turn_id)

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
                tasks.admit(self, payload)
                return super()._apply_action(action)
        provider = self._answer_provider(payload['turn_id'])
        if payload['type'] != 'resume' or provider is None or self._state.get_action(payload['idempotency_key']) is not None:
            return super()._apply_action(action)
        row = provider.service.paused(payload['turn_id'])
        if row is None:
            return super()._apply_action(action)
        events = tuple(self._events.events_after(payload['turn_id']))
        if (not events or payload['expected_sequence'] != len(events) or events[-1]['type'] != 'tool.failed'
                or events[-1]['data']['status'] != 'waiting_approval'
                or events[-1]['correlation']['tool_call_id'] != row.payload['invocation_id']):
            raise AIKernelRuntimeError('partial answer action sequence conflict')
        with self._policy_scope(payload['turn_id']):
            provider.service.admit_continuation(payload, row)
            action_ref = self._payloads.put(payload['turn_id'], 'turn-action', payload)
            self._append(payload['turn_id'], 'turn.resumed', 'running', str(payload['reason']),
                actor='user', payload_ref=action_ref)
            receipt = self._run_accepted_turn(payload['turn_id'])
            self._state.save_action(payload, receipt)
            return receipt

    def _run(self, turn_id):
        with self._policy_scope(turn_id):
            return super()._run(turn_id)

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
