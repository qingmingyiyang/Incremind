"""Publish only closed product planner interruptions under their original lease."""
from copy import deepcopy
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone, timedelta
from threading import RLock
from uuid import uuid4

from backend.shared.llm.model_transport import ModelInterrupted, is_provider_closed_witness
from core.effect_log import EffectState
from core.ai_kernel import RunLeaseRevoked, validate_turn_action
from .receipt_projection import closed_task_model_attempt_binding
from ..workspace_contracts import _now

COLLECTION = 'v2_task_continuations'
PREFIX = 'product-task-closed-'
STOPS = 'v2_turn_stops'


class ProductStops:
    def __init__(self, records, store, planner):
        self.records, self.store, self.planner = records, store, planner
        self.runtime = None
        self.lock = RLock()

    def product(self, identity):
        request = self.store.get_request(identity)
        if request is None:
            return None
        root = self.planner.profile_request(request) if self.planner is not None else request
        if root.get('desired_outcome') == 'project.answer':
            product_id = root['turn_id']
            intent = 'ask'
        elif root.get('desired_outcome') == 'project.task' and root['session_id'].startswith('session-'):
            product_id = root['session_id'][len('session-'):]
            intent = 'do'
        else:
            return None
        product = self.records.read('v2_turns', product_id)
        if product is None and intent == 'ask':
            stream = self.records.read('v2_turn_streams', product_id)
            if stream is None:
                return None
            value = stream.payload
            thread = self.records.read('v2_threads', value['thread_id'])
            if (value.get('binding') != {'kernel_turn_id': root['turn_id'], 'request': root}
                    or value.get('project_id') != root['scope']['project_id']
                    or value.get('turn', {}).get('id') != product_id
                    or value['turn'].get('intent') != 'ask' or thread is None
                    or thread.payload['project_id'] != root['scope']['project_id']):
                return None
            return product_id, root
        if (product is None or product.payload.get('intent') != intent
                or product.payload.get('project_id') != root['scope']['project_id']
                or intent == 'do' and product.payload['receipt']['do'].get('kernel_turn_id') != root['turn_id']):
            return None
        return product_id, root

    def current(self, identity):
        product = self.product(identity)
        if product is None:
            return None
        row = self.records.read(STOPS, product[0])
        if (row is None or row.payload['project_id'] != product[1]['scope']['project_id']
                or row.payload['kernel_turn_id'] != product[1]['turn_id']):
            return None
        return row

    def requested(self, identity):
        row = self.current(identity)
        return row is not None and row.payload['state'] in {'requested', 'paused'}

    def cancellation_requested(self, identity):
        row = self.current(identity)
        if row is None or row.payload['state'] not in {'requested', 'paused'}:
            return False
        tasks, provider = self.runtime.task_continuations, self.runtime._answer_provider(identity)
        execution = (tasks.active.get(identity) if tasks is not None else None)
        if execution is None and provider is not None:
            execution = provider.service.active.get(identity)
        action = execution.get('action') if execution is not None else None
        return not (action is not None and action['turn_id'] == identity and action['type'] == 'resume'
            and action['actor'] == 'user' and execution.get('stop_generation') == row.payload['generation'])

    def checkpoint(self, identity):
        if not self.requested(identity):
            return False
        self.ensure_cancel_event(identity)
        self.runtime.request_background_cancel(identity, self.runtime._run_lease_context.get())
        return True

    def ensure_cancel_event(self, identity):
        with self.lock:
            return self._ensure_cancel_event(identity)

    def _ensure_cancel_event(self, identity):
        row = self.current(identity)
        lease = self.runtime._run_lease_context.get()
        if row is None or lease is None or lease.turn_id != identity or self.store.assert_active_run_lease(lease) is None:
            raise RunLeaseRevoked()
        events = tuple(self.store.events_after(identity))
        start = row.payload['members'].get(identity)
        if start is None:
            raise ValueError('product_stop_member_changed')
        if not any(event['type'] == 'turn.cancel.requested' for event in events[start:]):
            self.runtime._append(identity, 'turn.cancel.requested', 'running', 'user stopped product turn', actor='user')

    def settled_attempts(self, identity):
        events = tuple(self.store.events_after(identity))
        dispatched = [event['data']['payload_ref'] for event in events if event['type'] == 'model.attempt.dispatched']
        terminals = [event['data']['receipt_ref'] for event in events if event['type'] == 'model.attempt.terminal']
        if len(dispatched) != len(terminals):
            return None
        row = self.current(identity)
        known = set()
        proofs = [pair for member, pair in row.payload['pauses'].items() if member == identity]
        proofs.extend((value['proof_ref'], self.store.get(value['proof_ref']))
            for _, value in row.payload['resumes'] if value['action']['turn_id'] == identity)
        for reference, proof in proofs:
            if (self.store.get_immutable_payload(identity, 'product-user-stop-' + str(proof['generation']))
                    != (reference, proof) or proof['request'] != self.store.get_request(identity)):
                raise ValueError('product_stop_proof_changed')
            capsule = self.store.get(proof['proof_ref'])
            known.update(value['terminal_ref'] for value in capsule.get('attempts', []))
            known.update(capsule.get('settled_attempts', {}).get('terminals', []))
            auxiliary_refs = capsule.get('auxiliary_close_refs', [])
            if type(auxiliary_refs) is not list:
                raise ValueError('product_stop_proof_changed')
            for auxiliary_ref in auxiliary_refs:
                provider = self.runtime._answer_provider(identity)
                auxiliary = provider.service.closed_auxiliary(identity, auxiliary_ref) if provider is not None else None
                if (auxiliary is None or auxiliary['lease'] != capsule['lease']
                        or auxiliary['invocation_id'] != capsule['invocation_id']):
                    raise ValueError('product_stop_proof_changed')
                known.update(value['terminal_ref'] for value in auxiliary['attempts'])
        if any(self.store.get(reference)['status'] != 'succeeded' and reference not in known for reference in terminals):
            return None
        return {'dispatched': dispatched, 'terminals': terminals}

    def release_closed_action_lease(self, identity, run_lease, receipt):
        if run_lease is None or run_lease.turn_id != identity or receipt.status != 'waiting_approval':
            return
        with self.lock:
            row = self.current(identity)
            saved = row.payload['pauses'].get(identity) if row is not None else None
            if saved is None:
                return
            reference, proof = saved
            events = tuple(self.store.events_after(identity))
            if (self.store.get_immutable_payload(identity, 'product-user-stop-' + str(proof['generation'])) != (reference, proof)
                    or proof['request'] != self.store.get_request(identity) or not events
                    or events[-1]['event_id'] != proof['paused_event_id']
                    or events[-1]['data']['status'] != 'waiting_approval'):
                return
            provider, tasks = self.runtime._answer_provider(identity), self.runtime.task_continuations
            binding = (provider.service.paused(identity) if provider is not None else
                tasks.paused(identity, proof['request']['scope']['project_id']) if tasks is not None else None)
            capsule = self.store.get(proof['proof_ref'])
            if (binding is None or capsule.get('lease') != {'owner_id': run_lease.owner_id, 'generation': run_lease.generation}
                    or self.store.assert_active_run_lease(run_lease) is None):
                return
            # 本次原生动作已正常返回，关闭证明只释放它实际持有的租约。
            self.runtime.release_strict_run_lease(run_lease)

    def members(self, root):
        result = [root['turn_id']]
        if self.planner is None or not root.get('agent_binding'):
            return result
        pending = [root['agent_binding']['run_id']]
        while pending:
            parent = pending.pop()
            for link in self.planner.composition.store.list_child_links(
                    project_id=root['scope']['project_id'], parent_run_id=parent):
                child = self.planner.composition.store.get_run(link.child_run_id)
                if child is not None and child.turn_id not in result:
                    result.append(child.turn_id)
                    pending.append(child.run_id)
        return result

    @contextmanager
    def dispatching(self, identity):
        with self.lock:
            yield not self.requested(identity)

    def request(self, product_id, project, runner):
        with self.lock:
            prepared = self._request(product_id, project, runner)
        if len(prepared) == 2:
            return prepared
        identity, members, generation = prepared
        for member in members:
            current = self.current(identity)
            if current.payload['generation'] != generation or current.payload['state'] not in {'requested', 'paused'}:
                break
            if self.runtime.receipt_for(member).status not in {'completed', 'failed', 'cancelled'}:
                if not runner.request_turn_cancel(member, reason='user stopped product turn'):
                    self.park_idle(member)
        return self.state(identity, persist=True) or 'stopped', generation

    def _request(self, product_id, project, runner):
        product = self.records.read('v2_turns', product_id)
        if product is not None and (product.payload['project_id'] != project or product.payload['intent'] not in {'ask', 'do'}):
            raise ValueError('workbench_not_found')
        identity = (product.payload['receipt']['do']['kernel_turn_id']
            if product is not None and product.payload['intent'] == 'do' else product_id)
        bound = self.product(identity)
        if bound is None or bound[1]['scope']['project_id'] != project:
            raise ValueError('workbench_not_found')
        root = bound[1]
        with self.records.begin() as tx:
            old = tx.read(STOPS, product_id)
            if old is not None and old.payload['state'] in {'requested', 'paused'}:
                return ('stopped' if old.payload['state'] == 'paused' else 'stopping', old.payload['generation'])
            if self.runtime.receipt_for(identity).status in {'completed', 'failed', 'cancelled'}:
                raise ValueError('turn_finished')
            members = self.members(root)
            pending_pauses = ({member: proof for member, proof in old.payload['pauses'].items()
                if member in members and not any(value[1]['proof_ref'] == proof[0] for value in old.payload['resumes'])}
                if old else {})
            payload = {'project_id': project, 'kernel_turn_id': identity, 'state': 'requested',
                'generation': old.payload['generation'] + 1 if old else 1,
                'requested_at': _now(), 'members': {key: len(tuple(self.store.events_after(key))) for key in members},
                'pauses': pending_pauses, 'resumes': old.payload['resumes'] if old else []}
            tx.put(STOPS, product_id, payload, expected_revision=old.revision if old else 0)
            tx.commit()
        return identity, members, payload['generation']

    def park_idle(self, identity):
        tasks = self.runtime.task_continuations
        if tasks is None or tasks.paused(identity, self.store.get_request(identity)['scope']['project_id']) is not None:
            return
        now = datetime.now(timezone.utc)
        lease = self.runtime.try_acquire_run_lease(identity, 'product-stop-' + uuid4().hex,
            now=now, stale_after=now + timedelta(seconds=30))
        if lease is None:
            return
        scope = self.runtime._bind_run_lease(identity, lease)
        try:
            tasks.pause_boundary(self.runtime, identity)
        finally:
            self.runtime._run_lease_context.reset(scope)
            self.runtime.release_run_lease(identity, lease.owner_id, lease.generation)

    def pause(self, identity, event, proof_ref):
        row = self.current(identity)
        if row is None or not self.requested(identity):
            return False
        events = tuple(self.store.events_after(identity))
        start = row.payload['members'].get(identity)
        if start is None:
            return False
        cancels = [value['event_id'] for value in events[start:] if value['type'] == 'turn.cancel.requested']
        if not cancels or event['event_id'] not in {value['event_id'] for value in events}:
            return False
        proof = {'turn_id': identity, 'request': self.store.get_request(identity),
            'product_turn_id': row.object_id, 'generation': row.payload['generation'],
            'cancel_event_ids': cancels, 'paused_event_id': event['event_id'], 'proof_ref': proof_ref}
        reference = (self.store.get_or_create_immutable_payload(identity,
            'product-user-stop-' + str(row.payload['generation']), proof), proof)
        with self.records.begin() as tx:
            current = tx.read(STOPS, row.object_id)
            if current is None or current.payload['generation'] != row.payload['generation']:
                raise ValueError('product_stop_changed')
            tx.put(STOPS, row.object_id, {**current.payload,
                'pauses': {**current.payload['pauses'], identity: reference}}, expected_revision=current.revision)
            tx.commit()
        self.state(identity, persist=True)
        return True

    def state(self, identity, *, persist=False):
        row = self.current(identity)
        if row is None:
            return None
        if row.payload['state'] not in {'requested', 'paused'}:
            return None
        def ready_member(key):
            if self.runtime.receipt_for(key).status == 'completed':
                return True
            saved = row.payload['pauses'].get(key)
            if saved is None:
                return False
            proof, events = saved[1], tuple(self.store.events_after(key))
            tasks, provider = self.runtime.task_continuations, self.runtime._answer_provider(key)
            active = (tasks is not None and key in tasks.active
                or provider is not None and key in provider.service.active
                and provider.service.active[key].get('continuation') is not None)
            return (bool(events) and events[-1]['event_id'] == proof['paused_event_id']
                and events[-1]['data']['status'] == 'waiting_approval'
                and not (proof['generation'] < row.payload['generation'] and active))
        ready = all(ready_member(key) for key in row.payload['members'])
        if persist and ready and row.payload['state'] != 'paused':
            with self.records.begin() as tx:
                current = tx.read(STOPS, row.object_id)
                if current == row:
                    tx.put(STOPS, row.object_id, {**row.payload, 'state': 'paused'}, expected_revision=row.revision)
                    tx.commit()
        return 'stopped' if ready else 'stopping'

    def continuation_generation(self, identity):
        row = self.current(identity)
        if row is None or row.payload['state'] not in {'paused', 'continuing'}:
            raise ValueError('product_stop_changed')
        return row.payload['generation']

    def require_generation(self, identity, generation):
        if generation is not None and self.continuation_generation(identity) != generation:
            raise ValueError('product_stop_changed')

    def acknowledge(self, identity, action, *, generation=None):
        row = self.current(identity)
        if row is None or identity not in row.payload['pauses']:
            return
        if generation is not None and row.payload['generation'] != generation and row.payload['state'] in {'requested', 'paused'}:
            return False
        self.require_generation(identity, generation)
        reference, proof = row.payload['pauses'][identity]
        if any(value[1]['proof_ref'] == reference for value in row.payload['resumes']):
            return
        if generation is None:
            raise ValueError('product_stop_resume_generation_missing')
        saved = self.store.get_immutable_payload(identity, 'product-user-stop-' + str(proof['generation']))
        if saved != (reference, proof) or proof['request'] != self.store.get_request(identity):
            raise ValueError('product_stop_proof_changed')
        events = tuple(self.store.events_after(identity))
        sequence = action['expected_sequence']
        if (sequence < 1 or sequence >= len(events) or action['turn_id'] != identity or action['type'] != 'resume'
                or events[sequence]['type'] != 'turn.resumed'
                or self.store.get(events[sequence]['data']['payload_ref']) != action
                or events[sequence - 1]['event_id'] != proof['paused_event_id']):
            raise ValueError('product_stop_resume_changed')
        lease = self.runtime._run_lease_context.get()
        if lease is None or lease.turn_id != identity or self.store.assert_active_run_lease(lease) is None:
            raise RunLeaseRevoked()
        value = {'proof_ref': reference, 'action': action, 'event_id': events[sequence]['event_id'],
            'stop_generation': generation,
            'lease': {'owner_id': lease.owner_id, 'generation': lease.generation}}
        resumed = (self.store.get_or_create_immutable_payload(identity,
            'product-user-resume-' + action['action_id'], value), value)
        with self.records.begin() as tx:
            current = tx.read(STOPS, row.object_id)
            if current is not None and current.payload['generation'] != generation and current.payload['state'] in {'requested', 'paused'}:
                return False
            if (current is None or current.payload['generation'] != generation
                    or current.payload['state'] not in {'paused', 'continuing'}
                    or current.payload['pauses'].get(identity, [None])[0] != reference):
                raise ValueError('product_stop_changed')
            if resumed not in current.payload['resumes']:
                tx.put(STOPS, row.object_id, {**current.payload, 'state': 'continuing',
                    'resumes': [*current.payload['resumes'], resumed]}, expected_revision=current.revision)
                tx.commit()
        return True

    def acknowledged_cancels(self, identity):
        row = self.current(identity)
        if row is None:
            return set()
        result = set()
        events = tuple(self.store.events_after(identity))
        for reference, value in row.payload['resumes']:
            action = value['action']
            if action['turn_id'] != identity:
                continue
            saved = self.store.get_immutable_payload(identity, 'product-user-resume-' + action['action_id'])
            proof = self.store.get(value['proof_ref'])
            sequence = action['expected_sequence']
            if (saved != (reference, value) or proof['request'] != self.store.get_request(identity)
                    or sequence >= len(events) or events[sequence]['event_id'] != value['event_id']
                    or events[sequence]['type'] != 'turn.resumed'
                    or self.store.get(events[sequence]['data']['payload_ref']) != action
                    or events[sequence - 1]['event_id'] != proof['paused_event_id']):
                raise ValueError('product_stop_resume_changed')
            result.update(proof['cancel_event_ids'])
        return result

    def awaiting_resume(self, identity):
        row = self.current(identity)
        if row is None or identity not in row.payload['pauses']:
            return False
        reference = row.payload['pauses'][identity][0]
        return not any(value[1]['proof_ref'] == reference for value in row.payload['resumes'])

    def continuing(self, identity):
        row = self.current(identity)
        if row is None or row.payload['state'] not in {'continuing', 'requested'}:
            return False
        members = row.payload['members']
        accepted = any(self.acknowledged_cancels(member) for member in members)
        tasks = self.runtime.task_continuations
        provider = self.runtime._answer_provider(identity)
        return accepted and (any(member in tasks.active for member in members)
            if tasks is not None and provider is None else provider is not None and identity in provider.service.active)

    def resume_children(self, identity, project, key, runner, *, generation):
        row = self.current(identity)
        if row is None:
            return
        if row.payload['project_id'] != project or row.payload['state'] not in {'paused', 'continuing'}:
            raise ValueError('turn_interrupted')
        self.require_generation(identity, generation)
        service = self.runtime.task_continuations
        for child in reversed(tuple(row.payload['members'])):
            self.require_generation(identity, generation)
            if child == identity or self.runtime.receipt_for(child).status in {'completed', 'failed', 'cancelled'}:
                continue
            child_key = key + ':child:' + child
            prior = self.store.get_action(child_key)
            if prior is not None:
                if prior[0]['turn_id'] != child or prior[0]['type'] != 'resume':
                    raise ValueError('product_stop_resume_changed')
                continue
            binding = service.paused(child, project)
            if binding is None or self.runtime.receipt_for(child).status != 'waiting_approval':
                raise ValueError('turn_interrupted')
            service.validate_current(binding[1])
            action = validate_turn_action({'schema_version': '1.0.0', 'action_id': 'action-' + uuid4().hex,
                'turn_id': child, 'type': 'resume', 'target_event_id': None, 'reason': 'continue user stopped task',
                'actor': 'user', 'expected_sequence': len(tuple(self.store.events_after(child))),
                'idempotency_key': child_key, 'created_at': _now()})
            with service.executing(child, action, binding, stop_generation=generation):
                self.require_generation(identity, generation)
                runner.apply_action_and_wait(action)


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
        self.stops = None

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

    def settled_tools(self, events, identity):
        completed, cancelled = [], []
        for event in events:
            if event['type'] != 'tool.intent.recorded':
                continue
            invocation = event['correlation']['tool_call_id']
            outcomes = [value for value in events if value['type'] == 'tool.outcome.recorded'
                and value['correlation']['tool_call_id'] == invocation]
            if len(outcomes) != 1:
                return None
            outcome = self.store.get(outcomes[0]['data']['payload_ref'])
            intent = self.store.get(event['data']['payload_ref'])
            effect = self.store.effect_runner.log.get(invocation)
            if (outcome['turn_id'] != identity or outcome['invocation_id'] != invocation or effect is None
                    or intent['turn_id'] != identity or intent['invocation_id'] != invocation
                    or outcome['capability_id'] != intent['capability_id'] or effect.turn_id != identity
                    or effect.intent_ref != event['data']['payload_ref']):
                return None
            if (outcome['status'] == 'completed' and outcome['effect_certainty'] in {'confirmed_none', 'confirmed_applied'}
                    and effect.state == EffectState.SETTLED_OK):
                completed.append(invocation)
            elif (outcome['status'] == 'cancelled' and outcome['effect_certainty'] == 'confirmed_none'
                    and effect.state == EffectState.SETTLED_ERR):
                cancelled.append(outcomes[0]['data']['payload_ref'])
            else:
                return None
        return {'completed': sorted(completed), 'cancelled': sorted(cancelled)}

    def pause_boundary(self, runtime, identity):
        if self.stops is None or not self.stops.requested(identity):
            return None
        request = self.store.get_request(identity)
        if request is None or self.planner.profile_request(request).get('desired_outcome') != 'project.task':
            return None
        lease = runtime._run_lease_context.get()
        if lease is None or lease.turn_id != identity or self.store.assert_active_run_lease(lease) is None:
            return None
        events = tuple(self.store.events_after(identity))
        attempts = self.stops.settled_attempts(identity)
        if attempts is None:
            return None
        tools = self.settled_tools(events, identity)
        if tools is None:
            return None
        from ..turn_routing import RecognitionRoutingSnapshot, SNAPSHOT_KIND, _revision
        saved = self.store.get_immutable_payload(identity, SNAPSHOT_KIND)
        capsule = {'kind': 'stopped_task_boundary', 'turn_id': identity, 'project_id': request['scope']['project_id'],
            'request': request, 'routing': (RecognitionRoutingSnapshot(saved[0], _revision(saved[1]), saved[1]).generation_binding()
                if saved is not None else None),
            'tools': tools, 'settled_attempts': attempts, 'partial': '', 'interruption': 'stopped',
            'lease': {'owner_id': lease.owner_id, 'generation': lease.generation}}
        row = self.stops.current(identity)
        event = runtime._new_event(identity, 'turn.cancel.requested', 'waiting_approval',
            'user stopped task at settled boundary', actor='user')
        committed = self.store.append_event_with_immutable_payload(event, expected_sequence=len(events),
            immutable_kind='product-task-stop-boundary-' + str(row.payload['generation']),
            immutable_payload=capsule, run_lease=lease)
        if not self.stops.pause(identity, committed.event, committed.immutable_payload_ref):
            raise ValueError('task_stop_boundary_changed')
        return committed.event

    def boundary(self, identity, project):
        stop = self.stops.current(identity) if self.stops is not None else None
        if stop is None or not self.stops.awaiting_resume(identity):
            return None
        _, proof = stop.payload['pauses'][identity]
        capsule = self.store.get(proof['proof_ref'])
        if type(capsule) is not dict or capsule.get('kind') != 'stopped_task_boundary':
            return None
        events = tuple(self.store.events_after(identity))
        pause = next((event for event in events if event['event_id'] == proof['paused_event_id']), None)
        if (capsule['request'] != self.store.get_request(identity) or capsule['turn_id'] != identity
                or capsule['project_id'] != project or pause is None or pause['data']['status'] != 'waiting_approval'
                or proof['proof_ref'] not in pause['data']['evidence_refs']
                or self.stops.settled_attempts(identity) != capsule['settled_attempts']
                or self.settled_tools(events, identity) != capsule['tools']):
            raise ValueError('task_stop_boundary_changed')
        return stop, capsule

    def paused(self, identity, project):
        row = self.records.read(COLLECTION, identity)
        if row is None or row.payload.get('state') != 'paused' or row.payload.get('project_id') != project:
            return self.boundary(identity, project)
        if set(row.payload) != {'project_id', 'state', 'capsule_ref', 'event_id'}:
            raise ValueError('invalid_task_continuation_descriptor')
        capsule = self.store.get(row.payload['capsule_ref'])
        fields = {'turn_id', 'project_id', 'request', 'step_id', 'model_request_id', 'lease',
            'attempts', 'model_event_id', 'model_receipt_ref', 'routing', 'messages',
            'completed_tools', 'partial', 'interruption', 'product_turn_id', 'thread_id', 'product_request'}
        request = self.store.get_request(identity)
        if (type(capsule) is not dict or set(capsule) != fields or capsule['turn_id'] != identity
                or capsule['project_id'] != project or capsule['request'] != request
                or request is None or (request['desired_outcome'] != 'project.task'
                    and not (capsule['interruption'] == 'stopped' and self.planner.profile_request(request)['desired_outcome'] == 'project.task'))
                or request['scope']['project_id'] != project
                or type(capsule['model_request_id']) is not str
                or type(capsule['lease']) is not dict or set(capsule['lease']) != {'owner_id', 'generation'}
                or type(capsule['lease']['owner_id']) is not str or not capsule['lease']['owner_id']
                or type(capsule['lease']['generation']) is not int or capsule['lease']['generation'] < 1
                or type(capsule['partial']) is not str or capsule['interruption'] not in {'connection', 'sleep', 'stopped'}
                or type(capsule['messages']) is not list):
            raise ValueError('invalid_task_continuation_capsule')
        if capsule['interruption'] == 'stopped' and (self.stops is None or self.stops.current(identity) is None):
            raise ValueError('invalid_task_stop_binding')
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
            question=request['input']['text'], model_request_id=capsule['model_request_id'], lease=capsule['lease'],
            kind=request['desired_outcome'], stopped=capsule['interruption'] == 'stopped')
        tools = (self.settled_tools(events, identity) if capsule['interruption'] == 'stopped'
            else self.completed_tools(events, identity))
        if not attempts or attempts != capsule['attempts'] or tools != capsule['completed_tools']:
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
        if capsule.get('kind') == 'stopped_task_boundary' and capsule['routing'] is None and saved is None:
            return
        if saved is None or RecognitionRoutingSnapshot(saved[0], _revision(saved[1]), saved[1]).generation_binding() != capsule['routing']:
            raise ValueError('task_continuation_routing_changed')
        _validate_governed_routing_snapshot(capsule['routing'], self.planner.models.public()['generation'])

    @contextmanager
    def executing(self, identity, action, binding, *, stop_generation=None):
        with self.lock:
            if identity in self.active:
                raise ValueError('task_continuation_in_progress')
            self.active[identity] = {'action': action, 'binding': binding, 'used': False,
                'stop_generation': stop_generation}
        try:
            yield
        finally:
            with self.lock:
                self.active.pop(identity, None)

    def admit(self, runtime, action):
        with self.stops.lock if self.stops is not None else nullcontext():
            return self._admit(runtime, action)

    def _admit(self, runtime, action):
        identity = action['turn_id']
        execution = self.active.get(identity)
        if execution is not None and execution['action'] == action and execution.get('stop_generation') is not None:
            self.stops.require_generation(identity, execution['stop_generation'])
        binding = self.paused(identity, self.store.get_request(identity)['scope']['project_id'])
        lease = runtime._run_lease_context.get()
        if (execution is None or execution['action'] != action or binding != execution['binding']
                or lease is None or lease.turn_id != identity or self.store.assert_active_run_lease(lease) is None
                or runtime.receipt_for(identity).status != 'waiting_approval'):
            raise ValueError('task_continuation_requires_explicit_executor')
        row, capsule = binding
        events = tuple(self.store.events_after(identity))
        paused_event = (row.payload['pauses'][identity][1]['paused_event_id']
            if capsule.get('kind') == 'stopped_task_boundary' else row.payload['event_id'])
        if action['expected_sequence'] != len(events) or events[-1]['event_id'] != paused_event:
            raise ValueError('task_continuation_action_sequence_changed')
        self.validate_current(capsule)
        if capsule['interruption'] == 'stopped':
            if execution['stop_generation'] is None:
                raise ValueError('product_stop_resume_generation_missing')
            self.stops.require_generation(identity, execution['stop_generation'])
        if capsule.get('kind') == 'stopped_task_boundary':
            if self.stops.current(identity) != row:
                raise ValueError('task_continuation_revision_changed')
        else:
            with self.records.begin() as tx:
                if tx.read(COLLECTION, identity) != row:
                    raise ValueError('task_continuation_revision_changed')
                if capsule['interruption'] == 'stopped':
                    stop = tx.read(STOPS, self.stops.product(identity)[0])
                    if stop is None or stop.payload['generation'] != execution['stop_generation']:
                        raise ValueError('product_stop_changed')
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
        capsule = execution['binding'][1]
        return None if capsule.get('kind') == 'stopped_task_boundary' else capsule

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
        if capsule['interruption'] == 'stopped':
            return None
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
        if request is None:
            return False
        if request.get('desired_outcome') != 'project.task':
            return (request.get('desired_outcome') == 'agent.steward.plan' and self.stops is not None
                and self.stops.awaiting_resume(identity)
                and self.planner.profile_request(request)['desired_outcome'] == 'project.task')
        return (self.stops is not None and self.stops.awaiting_resume(identity)
            or self.records.read(COLLECTION, identity) is not None or any(
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
        stopped = (type(error) is TaskModelInterrupted and error.interruption == 'stopped'
            and self.stops is not None and self.stops.requested(identity))
        if (type(error) is not TaskModelInterrupted or error.owner is not self.planner
                or control is None or not matches or error.request != request
                or request is None or (request.get('desired_outcome') != 'project.task'
                    and not (stopped and self.planner.profile_request(request)['desired_outcome'] == 'project.task'))
                or control.turn_id != identity or control.model_terminal or control.cancel_requested and not stopped
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
            model_request_id=control.model_request_id, lease=owner,
            kind=request['desired_outcome'], stopped=stopped)
        if not attempts:
            return False
        events = tuple(self.store.events_after(identity))
        completed = self.settled_tools(events, identity) if stopped else self.completed_tools(events, identity)
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
            'routing': error.route, 'messages': error.messages, 'completed_tools': completed,
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
        if stopped:
            self.stops.pause(identity, committed.event, committed.immutable_payload_ref)
        runtime._finish_planner_control(identity, control)
        self.latest_interruption[identity] = error
        return True
