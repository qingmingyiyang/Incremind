"""Publish only closed product planner interruptions under their original lease."""
from copy import deepcopy
from contextlib import contextmanager
from threading import RLock

from backend.shared.llm.model_transport import ModelInterrupted, is_provider_closed_witness
from core.effect_log import EffectState
from core.ai_kernel import RunLeaseRevoked
from .receipt_projection import closed_task_model_attempt_binding

COLLECTION = 'v2_task_continuations'
PREFIX = 'product-task-closed-'


class TaskModelInterrupted(ModelInterrupted):
    def __init__(self, error, *, owner, request, control, messages, route, validate, raw_prefix=None):
        super().__init__(partial=error.partial, interruption=error.interruption,
            close_witness=error.close_witness)
        self.owner, self.request, self.control = owner, request, control
        self.messages, self.route, self.validate = deepcopy(messages), route, validate
        self.raw_prefix = raw_prefix


class TaskContinuations:
    def __init__(self, planner, records, root, *, read_control_type=None):
        self.planner, self.records, self.store, self.root = planner, records, planner.store, root
        self.read_control_type = read_control_type
        self.active, self.lock = {}, RLock()
        self.latest_interruption = {}

    def worker(self, identity):
        request = self.store.get_request(identity)
        binding = request.get('agent_binding', {}) if request else {}
        run = self.planner.composition.store.get_run(binding.get('run_id')) if binding.get('run_id') else None
        return run is not None and run.role == 'subagent' and run.profile_id == 'subagent.worker'

    def pure_text(self, error):
        from .policy_runtime import retry_policy_for
        return (type(error) is TaskModelInterrupted and error.owner is self.planner
            and retry_policy_for(self.planner.profile_request(error.request))(
                {'kind': 'pure_text_prefix', 'raw': error.raw_prefix}) is True)

    def paused(self, identity, project):
        row = self.records.read(COLLECTION, identity)
        if row is None or row.payload.get('state') != 'paused' or row.payload.get('project_id') != project:
            return None
        if set(row.payload) != {'project_id', 'state', 'capsule_ref', 'event_id'}:
            raise ValueError('invalid_task_continuation_descriptor')
        capsule = self.store.get(row.payload['capsule_ref'])
        fields = {'turn_id', 'project_id', 'request', 'step_id', 'model_request_id', 'lease',
            'attempts', 'model_event_id', 'model_receipt_ref', 'routing', 'messages',
            'completed_tools', 'partial', 'interruption', 'product_turn_id', 'thread_id', 'product_request'}
        request = self.store.get_request(identity)
        if (type(capsule) is not dict or set(capsule) != fields or capsule['turn_id'] != identity
                or capsule['project_id'] != project or capsule['request'] != request
                or request is None or request['desired_outcome'] != 'project.task'
                or request['scope']['project_id'] != project
                or type(capsule['model_request_id']) is not str
                or type(capsule['lease']) is not dict or set(capsule['lease']) != {'owner_id', 'generation'}
                or type(capsule['lease']['owner_id']) is not str or not capsule['lease']['owner_id']
                or type(capsule['lease']['generation']) is not int or capsule['lease']['generation'] < 1
                or type(capsule['partial']) is not str or capsule['interruption'] not in {'connection', 'sleep'}
                or type(capsule['messages']) is not list):
            raise ValueError('invalid_task_continuation_capsule')
        saved = self.store.get_immutable_payload(identity, PREFIX + capsule['model_request_id'])
        if saved is None or saved != (row.payload['capsule_ref'], capsule):
            raise ValueError('invalid_task_continuation_reference')
        root = self.planner.profile_request(request)
        product = self.records.read('v2_turns', capsule['product_turn_id'])
        execution = self.records.read('v2_task_executions', capsule['product_turn_id'])
        thread = self.records.read('v2_threads', capsule['thread_id'])
        if (root['session_id'] != 'session-' + capsule['product_turn_id'] or execution is None
                or execution.payload['request'] != capsule['product_request'] or product is None or thread is None
                or product.payload['intent'] != 'do' or product.payload['project_id'] != project
                or product.payload['thread_id'] != capsule['thread_id'] or thread.payload['project_id'] != project
                or product.payload['receipt']['do']['kernel_turn_id'] != root['turn_id']):
            raise ValueError('invalid_task_continuation_product_binding')
        self.validate_product_request(capsule['product_request'], root)
        events = tuple(self.store.events_after(identity))
        matches = [index for index, event in enumerate(events) if event['event_id'] == row.payload['event_id']]
        if len(matches) != 1 or matches[0] < 1:
            raise ValueError('invalid_task_continuation_event')
        event, failed = events[matches[0]], events[matches[0] - 1]
        if (event['type'] != 'model.result.discarded' or event['data']['status'] != 'waiting_approval'
                or event['correlation']['model_request_id'] != capsule['model_request_id']
                or event['correlation']['step_id'] != capsule['step_id']
                or event['data']['evidence_refs'] != [row.payload['capsule_ref']]
                or failed['event_id'] != capsule['model_event_id'] or failed['type'] != 'model.failed'
                or failed['correlation']['model_request_id'] != capsule['model_request_id']
                or failed['correlation']['step_id'] != capsule['step_id']
                or failed['data']['receipt_ref'] != capsule['model_receipt_ref']):
            raise ValueError('invalid_task_continuation_close_event')
        receipt = self.store.get(capsule['model_receipt_ref'])
        if receipt['status'] != 'failed' or receipt['model_request_id'] != capsule['model_request_id']:
            raise ValueError('invalid_task_continuation_model_receipt')
        attempts = closed_task_model_attempt_binding(self.root, identity, project,
            question=request['input']['text'], model_request_id=capsule['model_request_id'], lease=capsule['lease'])
        if not attempts or attempts != capsule['attempts'] or self.completed_tools(events, identity) != capsule['completed_tools']:
            raise ValueError('invalid_task_continuation_settled_binding')
        return row, capsule

    def validate_product_request(self, original, accepted):
        # The existing coordinator adds native Agent authority and intersects
        # capability/budget policy. Preserve both exact requests, never undo it.
        for name in ('turn_id', 'session_id', 'operation_id', 'idempotency_key', 'scope', 'input',
                     'desired_outcome', 'privacy', 'policy_versions', 'created_at'):
            if original.get(name) != accepted.get(name):
                raise ValueError('task_continuation_product_request_changed')
        self.planner.composition.coordinator.verify_agent_binding(accepted, accepted['agent_binding'])

    def validate_current(self, capsule):
        from ..model_config import _validate_governed_routing_snapshot
        from ..turn_routing import SNAPSHOT_KIND, RecognitionRoutingSnapshot, _revision
        request = capsule['request']
        self.planner.guard(request)
        if self.planner.profile_reader:
            self.planner.profile_reader(self.planner.profile_request(request))
        saved = self.store.get_immutable_payload(request['turn_id'], SNAPSHOT_KIND)
        if saved is None or RecognitionRoutingSnapshot(saved[0], _revision(saved[1]), saved[1]).generation_binding() != capsule['routing']:
            raise ValueError('task_continuation_routing_changed')
        _validate_governed_routing_snapshot(capsule['routing'], self.planner.models.public()['generation'])

    @contextmanager
    def executing(self, identity, action, binding):
        with self.lock:
            if identity in self.active:
                raise ValueError('task_continuation_in_progress')
            self.active[identity] = {'action': action, 'binding': binding, 'used': False}
        try:
            yield
        finally:
            with self.lock:
                self.active.pop(identity, None)

    def admit(self, runtime, action):
        identity = action['turn_id']
        execution = self.active.get(identity)
        binding = self.paused(identity, self.store.get_request(identity)['scope']['project_id'])
        lease = runtime._run_lease_context.get()
        if (execution is None or execution['action'] != action or binding != execution['binding']
                or lease is None or lease.turn_id != identity or self.store.assert_active_run_lease(lease) is None
                or runtime.receipt_for(identity).status != 'waiting_approval'):
            raise ValueError('task_continuation_requires_explicit_executor')
        row, capsule = binding
        events = tuple(self.store.events_after(identity))
        if action['expected_sequence'] != len(events) or events[-1]['event_id'] != row.payload['event_id']:
            raise ValueError('task_continuation_action_sequence_changed')
        self.validate_current(capsule)
        with self.records.begin() as tx:
            if tx.read(COLLECTION, identity) != row:
                raise ValueError('task_continuation_revision_changed')
            tx.put(COLLECTION, identity, {**row.payload, 'state': 'continuing'}, expected_revision=row.revision)
            tx.commit()
        with self.lock:
            if self.active.get(identity) is not execution:
                raise ValueError('task_continuation_executor_changed')
            # 留存原执行主人；后续只能读取当前实际租约，不从胶囊重建 token。
            execution['runtime'] = runtime

    def take(self, identity):
        execution = self.active.get(identity)
        if execution is None or execution['used']:
            return None
        self.validate_current(execution['binding'][1])
        execution['used'] = True
        return execution['binding'][1]

    def provider_resume_source(self, identity, *, capsule, execution_control):
        """原显式继续投影恢复数据；游标和当前设置均不能授予继续权限。"""
        execution = self.active.get(identity)
        if (execution is None or execution['used'] is not True
                or execution['binding'][1] is not capsule):
            raise ValueError('task_provider_resume_executor_unavailable')
        action, runtime = execution['action'], execution.get('runtime')
        if (runtime is None or action is None or action['type'] != 'resume' or action['actor'] != 'user'
                or action['turn_id'] != identity or runtime.task_continuations is not self):
            raise ValueError('task_provider_resume_action_unavailable')
        control = runtime._planner_controls.get(identity)
        if self.read_control_type is not None and type(execution_control) is self.read_control_type:
            matches = (execution_control.inner is control and execution_control.records is self.records
                and execution_control.turns is self.store and execution_control.agents is self.planner.composition.store
                and execution_control.request == capsule['request'])
        else:
            matches = execution_control is control
        if (control is None or not matches or control.turn_id != identity or control.model_terminal
                or control.model_request_id == capsule['model_request_id'] or control.purpose != 'primary'):
            raise ValueError('task_provider_resume_control_unavailable')
        execution_control.checkpoint()
        lease = runtime._run_lease_context.get()
        if lease is None or lease.turn_id != identity or self.store.assert_active_run_lease(lease) is None:
            raise RunLeaseRevoked()
        row, original = execution['binding']
        current = self.records.read(COLLECTION, identity)
        if (current is None or current.revision != row.revision + 1
                or current.payload != {**row.payload, 'state': 'continuing'}
                or original is not capsule or self.store.get_request(identity) != capsule['request']
                or self.store.get_immutable_payload(identity, PREFIX + capsule['model_request_id'])
                    != (row.payload['capsule_ref'], capsule)):
            raise ValueError('task_provider_resume_capsule_changed')
        events = tuple(self.store.events_after(identity))
        sequence = action['expected_sequence']
        if (sequence >= len(events) or events[sequence]['type'] != 'turn.resumed'
                or self.store.get(events[sequence]['data']['payload_ref']) != action):
            raise ValueError('task_provider_resume_action_binding_changed')
        if not self.planner.routing._main_owner(capsule['request'], ()):
            raise ValueError('task_provider_resume_owner_changed')
        self.validate_current(capsule)
        closed = []
        for event in events:
            if event['type'] != 'model.result.discarded':
                continue
            model_request_id = event['correlation'].get('model_request_id')
            if not isinstance(model_request_id, str):
                continue
            saved = self.store.get_immutable_payload(identity, PREFIX + model_request_id)
            if saved is not None:
                if (event['data']['evidence_refs'] != [saved[0]]
                        or saved[1]['model_request_id'] != model_request_id
                        or saved[1]['step_id'] != event['correlation'].get('step_id')):
                    raise ValueError('task_provider_resume_interruption_changed')
                closed.append(event)
        # 现有胶囊只存累计正文；仅首次单响应能由原事实证明为该响应局部正文。
        if len(closed) != 1 or len(capsule['attempts']) != 1:
            return None
        if (closed[0]['event_id'] != row.payload['event_id']
                or closed[0]['correlation']['model_request_id'] != capsule['model_request_id']):
            raise ValueError('task_provider_resume_first_interruption_changed')
        source = capsule['attempts'][0]
        if source['model_request_id'] != capsule['model_request_id'] or source['lease'] != capsule['lease']:
            raise ValueError('task_provider_resume_attempt_changed')
        return self.store.resolve_model_provider_resume_source(identity, attempt_id=source['attempt_id'],
            dispatch_ref=source['dispatch_ref'], terminal_ref=source['terminal_ref'])

    def automatic_text(self, identity, capsule):
        with self.lock:
            execution = self.active.get(identity)
            return (execution is not None and execution['action'] is None and execution['used']
                and execution['binding'][1] is capsule and self.worker(identity))

    def completed_tools(self, events, identity):
        intents = [event for event in events if event['type'] == 'tool.intent.recorded']
        completed = {event['correlation']['tool_call_id'] for event in events if event['type'] == 'tool.completed'}
        if {event['correlation']['tool_call_id'] for event in intents} != completed:
            return None
        for event in intents:
            invocation = event['correlation']['tool_call_id']
            outcomes = [row for row in events if row['type'] == 'tool.outcome.recorded'
                        and row['correlation']['tool_call_id'] == invocation]
            if len(outcomes) != 1:
                return None
            try:
                intent = self.store.get(event['data']['payload_ref'])
                outcome = self.store.get(outcomes[0]['data']['payload_ref'])
                effect = self.store.effect_runner.log.get(invocation)
                if (intent['turn_id'] != identity or intent['invocation_id'] != invocation
                        or outcome['invocation_id'] != invocation or outcome['turn_id'] != identity
                        or outcome['capability_id'] != intent['capability_id'] or outcome['status'] != 'completed'
                        or outcome['effect_certainty'] not in {'confirmed_none', 'confirmed_applied'}
                        or effect.state != EffectState.SETTLED_OK or effect.turn_id != identity
                        or effect.intent_ref != event['data']['payload_ref']):
                    return None
            except (AttributeError, KeyError, TypeError, ValueError):
                return None
        return sorted(completed)

    def suspended(self, identity):
        request = self.store.get_request(identity)
        if request is None or request.get('desired_outcome') != 'project.task':
            return False
        return (self.records.read(COLLECTION, identity) is not None or any(
            event['type'] == 'model.result.discarded' and event['data']['status'] == 'waiting_approval'
            and any(ref.startswith('crp://session/' + identity + '/' + PREFIX)
                for ref in event['data']['evidence_refs']) for event in self.store.events_after(identity)))

    def pause(self, runtime, identity, control, error):
        request = self.store.get_request(identity)
        lease = runtime._run_lease_context.get()
        supplied = getattr(error, 'control', None)
        if self.read_control_type is not None and type(supplied) is self.read_control_type:
            matches = (supplied.inner is control and supplied.records is self.records
                and supplied.turns is self.store and supplied.request == request
                and supplied.agents is self.planner.composition.store)
        else:
            matches = supplied is control
        if (type(error) is not TaskModelInterrupted or error.owner is not self.planner
                or control is None or not matches or error.request != request
                or request is None or request.get('desired_outcome') != 'project.task'
                or control.turn_id != identity or control.model_terminal or control.cancel_requested
                or runtime._planner_controls.get(identity) is not control
                or not is_provider_closed_witness(error.close_witness)
                or lease is None or lease.turn_id != identity
                or self.store.assert_active_run_lease(lease) is None):
            return False
        error.validate()
        root = self.planner.profile_request(request)
        if not root['session_id'].startswith('session-'):
            return False
        product_id = root['session_id'][len('session-'):]
        product = self.records.read('v2_turns', product_id)
        execution = self.records.read('v2_task_executions', product_id)
        if (product is None or execution is None
                or product.payload['intent'] != 'do' or product.payload['project_id'] != request['scope']['project_id']
                or product.payload['receipt']['do']['kernel_turn_id'] != root['turn_id']):
            return False
        self.validate_product_request(execution.payload['request'], root)
        owner = {'owner_id': lease.owner_id, 'generation': lease.generation}
        attempts = closed_task_model_attempt_binding(self.root, identity,
            request['scope']['project_id'], question=request['input']['text'],
            model_request_id=control.model_request_id, lease=owner)
        if not attempts:
            return False
        events = tuple(self.store.events_after(identity))
        completed = self.completed_tools(events, identity)
        if completed is None:
            return False
        if runtime._record_planner_model_terminal(identity, control,
                fallback_status='failed', fallback_error_code='ai.model_call_failed'):
            return False
        rows = tuple(self.store.events_after(identity))
        failed = rows[-1]
        if (failed['type'] != 'model.failed' or failed['correlation']['step_id'] != control.step_id
                or failed['correlation']['model_request_id'] != control.model_request_id
                or not failed['data']['receipt_ref']):
            return False
        receipt = self.store.get(failed['data']['receipt_ref'])
        if receipt['status'] != 'failed' or receipt['model_request_id'] != control.model_request_id:
            return False
        capsule = {'turn_id': identity, 'project_id': request['scope']['project_id'], 'request': request,
            'step_id': control.step_id, 'model_request_id': control.model_request_id, 'lease': owner,
            'attempts': attempts, 'model_event_id': failed['event_id'], 'model_receipt_ref': failed['data']['receipt_ref'],
            'routing': error.route, 'messages': error.messages, 'completed_tools': sorted(completed),
            'partial': error.partial, 'interruption': error.interruption,
            'product_turn_id': product_id, 'thread_id': product.payload['thread_id'],
            'product_request': execution.payload['request']}
        event = runtime._new_event(identity, 'model.result.discarded', 'waiting_approval',
            'task interrupted; explicit continuation required', step_id=control.step_id,
            model_request_id=control.model_request_id)
        committed = self.store.append_event_with_immutable_payload(event,
            expected_sequence=len(rows), immutable_kind=PREFIX + control.model_request_id,
            immutable_payload=capsule, run_lease=lease)
        with self.records.begin() as tx:
            previous = tx.read(COLLECTION, identity)
            tx.put(COLLECTION, identity, {'project_id': capsule['project_id'], 'state': 'paused',
                'capsule_ref': committed.immutable_payload_ref, 'event_id': committed.event['event_id']},
                expected_revision=previous.revision if previous else 0)
            tx.commit()
        runtime._finish_planner_control(identity, control)
        self.latest_interruption[identity] = error
        return True
