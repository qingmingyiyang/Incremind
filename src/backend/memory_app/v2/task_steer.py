from copy import deepcopy
from dataclasses import dataclass, replace
from uuid import NAMESPACE_URL, uuid5

from backend.recognition import RecognitionConflict
from backend.shared.task_division_graph import validate_divisions
from core.ai_kernel.agent_contracts import agent_budget_from_payload, agent_budget_to_payload
from core.ai_kernel.agent_dispatch_contracts import (
    DispatchPermit, dispatch_permit_from_payload, dispatch_permit_to_payload,
    expert_assignment_from_payload, expert_assignment_to_payload, parse_canonical_dispatch_ref,
)
from core.ai_kernel.tool_invocation import intent_from_payload
from core.ai_kernel.model_planner import _validate_decision
from ..kernel.agent_coordinator import _profile_id_from_ref
from .policies import get
from .task_model_inputs import read_task_model_input


INPUT = 'task-steer-input-v1-'
OUTPUT = 'task-steer-output-v1-'
DECISION = 'task-steer-decision-v1-'
COMMAND = 'task-steer-amend-v1-'


@dataclass(frozen=True)
class TaskSteerBoundary:
    messages: list
    steer_ids: tuple
    topology: dict | None
    adopted_refs: tuple
    directive_refs: tuple


class TaskSteerRuntime:
    def __init__(self, service, runtime, planner):
        self.service, self.runtime, self.planner = service, runtime, planner
        self.turns, self.composition = runtime._state, planner.composition
        if planner.store is not self.turns or self.composition.store._path != self.turns._path:
            raise RecognitionConflict('task_steer_native_owner_changed')

    def _main(self, request):
        if not self.service.registered(request['turn_id']):
            return None
        lease = self.runtime._run_lease_context.get()
        found = self.composition.store.get_run_by_turn_id(request['turn_id'], project_id=request['scope']['project_id'])
        if (self.turns.get_request(request['turn_id']) != request or lease is None or lease.turn_id != request['turn_id']
                or self.turns.assert_active_run_lease(lease) is None or found is None or found[0].role != 'main'
                or found[0].parent_run_id is not None or found[0].is_terminal
                or found[0].status in {'cancelling', 'quarantined', 'recovery_required', 'waiting_approval'}
                or self.runtime.receipt_for(request['turn_id']).status != 'running' or request['desired_outcome'] != 'project.task'):
            raise RecognitionConflict('task_steer_running_main_changed')
        return found

    def pending(self, request):
        return bool(self.service.pending(request['turn_id'])) and self._main(request) is not None

    def _adopted(self, request):
        binding = self.turns.get_immutable_payload(request['turn_id'], 'workbench-steer-binding-v1')
        if binding is None:
            return ()
        self.turns.restore_admission(request)
        rows = []
        for row in self.turns.workbench_steers(binding[1]['public_turn_id']):
            if row['payload']['turn_id'] != request['turn_id'] or row['state'] not in {'injected', 'released', 'amended'}:
                continue
            decision = self.turns.get(row['decision_ref']) if row['decision_ref'] is not None else None
            if isinstance(decision, dict) and decision.get('reason') == 'run_owner_recovered':
                continue
            source = self.turns.get(row['input_ref'])
            self._verify_directive(request, binding[1], row, source)
            rows.append(row)
        return tuple(rows)

    def _verify_directive(self, request, binding, row, source):
        fact = row['payload']
        if (fact['policy_version'] != '@2' or fact['project_id'] != request['scope']['project_id']
                or source.get('turn_id') != request['turn_id'] or row['payload_ref'] not in source.get('steer_refs', [])
                or fact['lease'] != {'owner_id': source.get('run_owner_id'), 'generation': source.get('run_generation')}
                or any(fact.get(key) != binding.get(key) for key in
                    ('owner_user_id', 'namespace_id', 'origin_kind', 'public_turn_id', 'target_turn_id'))):
            raise RecognitionConflict('task_steer_assignment_directives_changed')
        saved = self.turns.get_immutable_payload(request['turn_id'], INPUT + source['model_request_id'])
        if saved != (row['input_ref'], source):
            raise RecognitionConflict('task_steer_assignment_input_changed')
        read_task_model_input(self.turns, self.composition.store, request['turn_id'], source['model_input_ref'])
        applied = [event for event in self.turns.events_after(request['turn_id']) if event['type'] == 'turn.steer.applied'
            and row['input_ref'] in event['data'].get('evidence_refs', [])]
        if len(applied) != 1:
            raise RecognitionConflict('task_steer_assignment_input_unapplied')

    def inherited_messages(self, request, source_request, messages):
        if request['turn_id'] == source_request['turn_id']:
            return messages
        rows = self._adopted(source_request)
        if not rows:
            return messages
        return get('steer', version='@2').task_messages(messages, [row['payload'] for row in rows], topology=None, amend_allowed=False)

    def _topology(self, main):
        store = self.composition.dispatch_store
        plans = [plan for plan in store.list_plans_for_main(project_id=main.project_id, main_run_id=main.run_id)
            if plan.status in {'ready', 'dispatching', 'dispatched', 'completed'}]
        if not plans:
            return None
        if len(plans) != 1:
            raise RecognitionConflict('task_steer_effective_plan_ambiguous')
        effective = store.effective_snapshot(plans[0].plan_id, project_id=main.project_id)
        rows = []
        for identity in effective.permit_ids:
            permit = store.get_permit(identity, project_id=main.project_id)
            assignment = store.get_assignment(permit.assignment_id, project_id=main.project_id) if permit is not None else None
            if permit is None or assignment is None:
                raise RecognitionConflict('task_steer_assignment_unavailable')
            body = self.turns.get_immutable_payload(main.turn_id, 'agent.dispatch.task.' + assignment.assignment_id + '.v1')
            if body is None or body[0] != assignment.task_payload_ref:
                raise RecognitionConflict('task_steer_assignment_input_changed')
            self.composition.coordinator._permit_task(assignment)
            spec = {'assignment_id': assignment.assignment_id, 'profile_id': _profile_id_from_ref(assignment.profile_ref),
                'profile_revision': assignment.profile_revision, 'task': body[1]['task'],
                'budget': agent_budget_to_payload(assignment.delegated_budget), 'capability_ids': list(assignment.capability_ids),
                'expert': deepcopy(body[1]['expert']), 'skill': {'skill_ids': list(assignment.skill_ids)} if assignment.skill_ids else None}
            if 'division' in body[1]:
                spec['division'] = deepcopy(body[1]['division'])
            rows.append({'permit_id': permit.permit_id,
                'bound': store.get_permit_child_run_binding(permit.permit_id, project_id=main.project_id) is not None, 'assignment': spec})
        return {'plan_id': effective.plan.plan_id,
            'cluster_id': parse_canonical_dispatch_ref(effective.plan.expert_cluster_ref, 'cluster') if effective.plan.expert_cluster_ref is not None else None,
            'plan_revision': effective.plan.revision, 'effective_revision': effective.revision,
            'main_revision': self.composition.store.get_run_with_revision(main.run_id, project_id=main.project_id)[1],
            'cancel_epoch': main.cancel_epoch, 'assignments': rows, 'budget_limit': agent_budget_to_payload(effective.plan.budget_limit),
            'max_concurrent_assignments': effective.plan.max_concurrent_assignments}

    def prepare_boundary(self, request, model_request_id, messages):
        queued, adopted = self.service.pending(request['turn_id']), self._adopted(request)
        if not queued and not adopted:
            return None
        found = self._main(request)
        if found is None:
            return None
        existing = self.turns.get_immutable_payload(request['turn_id'], INPUT + model_request_id)
        if existing is not None:
            by_ref = {row['payload_ref']: row for row in adopted}
            if any(reference not in by_ref for reference in existing[1]['directive_refs']):
                raise RecognitionConflict('task_steer_existing_directives_changed')
            rows = tuple(by_ref[reference] for reference in existing[1]['directive_refs'])
            projected = get('steer', version='@2').task_messages(messages, [row['payload'] for row in rows],
                topology=existing[1]['topology'], amend_allowed='agent.plan' in found[0].capability_ids)
            return TaskSteerBoundary(projected, (), deepcopy(existing[1]['topology']), (existing[0],), tuple(existing[1]['directive_refs']))
        rows = (*adopted, *queued)
        if len({row['payload']['id'] for row in rows}) != len(rows):
            raise RecognitionConflict('task_steer_input_selection_changed')
        topology = self._topology(found[0])
        projected = get('steer', version='@2').task_messages(messages, [row['payload'] for row in rows],
            topology=topology, amend_allowed=bool(queued) and 'agent.plan' in found[0].capability_ids)
        return TaskSteerBoundary(projected, tuple(row['payload']['id'] for row in queued), topology,
            tuple(dict.fromkeys(row['input_ref'] for row in adopted)), tuple(row['payload_ref'] for row in rows))

    def input_frozen(self, request, boundary, prepared):
        if boundary is None or not boundary.steer_ids:
            return None
        self._main(request)
        actual = read_task_model_input(self.turns, self.composition.store, request['turn_id'], prepared.input_ref)
        if actual != prepared.payload:
            raise RecognitionConflict('task_steer_final_input_changed')
        return self.service.freeze_input(request['turn_id'], kind=INPUT + actual['model_request_id'],
            payload={'model_request_id': actual['model_request_id'], 'model_input_ref': prepared.input_ref,
                'model_input_kind': prepared.kind, 'policy_version': '@2', 'topology': deepcopy(boundary.topology),
                'adopted_input_refs': list(boundary.adopted_refs), 'directive_refs': list(boundary.directive_refs)},
            steer_ids=list(boundary.steer_ids))

    def output_frozen(self, request, control, prepared, decision, *, capability_ids):
        saved = self.turns.get_immutable_payload(request['turn_id'], INPUT + control.model_request_id)
        if saved is None:
            return
        actual = read_task_model_input(self.turns, self.composition.store, request['turn_id'], prepared.input_ref)
        if actual['model_request_id'] != control.model_request_id or actual != prepared.payload:
            raise RecognitionConflict('task_steer_model_input_changed')
        decision = _validate_decision(deepcopy(decision), set(capability_ids))
        self.turns.get_or_create_immutable_payload(request['turn_id'], OUTPUT + control.model_request_id,
            {'turn_id': request['turn_id'], 'model_request_id': control.model_request_id,
             'steer_input_ref': saved[0], 'model_input_ref': prepared.input_ref, 'decision': deepcopy(decision)})

    def decision_committed(self, request, control, terminal):
        saved = self.turns.get_immutable_payload(request['turn_id'], INPUT + control.model_request_id)
        if saved is None:
            return
        lease = self.runtime._run_lease_context.get()
        if (self.turns.get_request(request['turn_id']) != request or lease is None or self.turns.assert_active_run_lease(lease) is None
                or saved[1]['run_owner_id'] != lease.owner_id or saved[1]['run_generation'] != lease.generation):
            raise RecognitionConflict('task_steer_terminal_owner_changed')
        events = [event for event in self.turns.events_after(request['turn_id']) if event['correlation']['model_request_id'] == control.model_request_id
            and event['type'] in {'model.completed', 'model.failed', 'model.cancelled', 'model.timed_out'}]
        if len(events) != 1 or terminal != events[0]:
            raise RecognitionConflict('task_steer_model_terminal_changed')
        output = self.turns.get_immutable_payload(request['turn_id'], OUTPUT + control.model_request_id)
        if terminal['type'] == 'model.completed' and output is None:
            raise RecognitionConflict('task_steer_model_output_unavailable')
        decision = output[1]['decision'] if output is not None else None
        calls = [decision] if isinstance(decision, dict) and decision.get('type') == 'tool' else (
            decision.get('calls', []) if isinstance(decision, dict) and decision.get('type') == 'tools' else [])
        amend = terminal['type'] == 'model.completed' and any(call.get('capability_id') == 'agent.plan' for call in calls)
        proof = {'turn_id': request['turn_id'], 'model_request_id': control.model_request_id,
            'steer_input_ref': saved[0], 'model_input_ref': output[1]['model_input_ref'] if output is not None else saved[1]['model_input_ref'],
            'output_ref': output[0] if output is not None else None, 'terminal': deepcopy(terminal), 'amend': amend}
        reference = self.turns.get_or_create_immutable_payload(request['turn_id'], DECISION + control.model_request_id, proof)
        if not amend:
            self.turns.release_steer_input(request['turn_id'], run_lease=lease, input_ref=saved[0], decision_ref=reference)

    def _directives(self, main, frozen):
        request = self.turns.get_request(main.turn_id)
        binding = self.turns.get_immutable_payload(main.turn_id, 'workbench-steer-binding-v1')
        if binding is None:
            raise RecognitionConflict('task_steer_assignment_identity_unavailable')
        self.turns.restore_admission(request)
        references = frozen['directive_refs']
        rows = {row['payload_ref']: row for row in self.turns.workbench_steers(binding[1]['public_turn_id'])}
        if not references or len(references) != len(set(references)):
            raise RecognitionConflict('task_steer_assignment_directives_changed')
        directives = []
        for reference in references:
            row = rows.get(reference)
            if row is None or row['state'] not in {'injected', 'released', 'amended'} or row['payload']['turn_id'] != main.turn_id:
                raise RecognitionConflict('task_steer_assignment_directives_changed')
            self._verify_directive(request, binding[1], row, self.turns.get(row['input_ref']))
            directives.append(deepcopy(row['payload']))
        return directives

    def _amend_source(self, main, operation_id, tool_call_id, arguments):
        events = tuple(self.turns.events_after(main.turn_id))
        intents = [event for event in events if event['type'] == 'tool.intent.recorded' and event['correlation']['tool_call_id'] == tool_call_id]
        if len(intents) != 1:
            raise RecognitionConflict('task_steer_tool_intent_unavailable')
        intent = intent_from_payload(self.turns.get(intents[0]['data']['payload_ref']))
        if (intent.turn_id != main.turn_id or intent.operation_id != operation_id or intent.invocation_id != tool_call_id
                or intent.capability_id != 'agent.plan' or dict(intent.arguments) != arguments):
            raise RecognitionConflict('task_steer_tool_intent_changed')
        completed = [event for event in events if event['type'] == 'model.completed'
            and event['correlation']['step_id'] == intent.step_id and event['sequence'] < intents[0]['sequence']]
        if len(completed) != 1:
            raise RecognitionConflict('task_steer_model_completion_unavailable')
        model_id = completed[0]['correlation']['model_request_id']
        saved = self.turns.get_immutable_payload(main.turn_id, DECISION + model_id)
        output = self.turns.get_immutable_payload(main.turn_id, OUTPUT + model_id)
        if (saved is None or output is None or saved[1]['terminal'] != completed[0] or saved[1]['output_ref'] != output[0]
                or saved[1]['amend'] is not True):
            raise RecognitionConflict('task_steer_amend_decision_changed')
        frozen = self.turns.get_immutable_payload(main.turn_id, INPUT + model_id)
        if (frozen is None or frozen[0] != saved[1]['steer_input_ref'] or frozen[1].get('policy_version') != '@2'
                or saved[1]['model_input_ref'] != output[1]['model_input_ref'] or output[1]['steer_input_ref'] != frozen[0]):
            raise RecognitionConflict('task_steer_amend_input_changed')
        actual = read_task_model_input(self.turns, self.composition.store, main.turn_id, output[1]['model_input_ref'])
        if actual['model_request_id'] != model_id:
            raise RecognitionConflict('task_steer_amend_final_input_changed')
        self._directives(main, frozen[1])
        value = output[1]['decision']
        calls = [value] if value.get('type') == 'tool' else value.get('calls', [])
        if sum(call.get('capability_id') == 'agent.plan' and call.get('arguments') == arguments for call in calls) != 1:
            raise RecognitionConflict('task_steer_amend_arguments_changed')
        return saved

    def amend_main(self, main, operation_id, tool_call_id, arguments):
        arguments = deepcopy(dict(arguments))
        if arguments['mode'] != 'amend_unbound':
            raise RecognitionConflict('task_steer_amend_mode_required')
        source = self._amend_source(main, operation_id, tool_call_id, arguments)
        frozen = self.turns.get_immutable_payload(main.turn_id, COMMAND + operation_id)
        if frozen is None:
            current = self._main(self.turns.get_request(main.turn_id))
            if current is None or current[0] != main:
                raise RecognitionConflict('task_steer_main_owner_changed')
            topology = self._topology(main)
            if topology is None or topology['plan_id'] != arguments['plan_id']:
                raise RecognitionConflict('task_steer_original_plan_unavailable')
            command = self._command(main, operation_id, arguments, source, topology)
            reference = self.turns.get_or_create_immutable_payload(main.turn_id, COMMAND + operation_id, command)
            frozen = reference, command
        command = frozen[1]
        if (command['operation_id'] != operation_id or command['arguments'] != arguments or command['decision_ref'] != source[0]
                or command['main_run_id'] != main.run_id or command['project_id'] != main.project_id):
            raise RecognitionConflict('task_steer_amend_replay_changed')
        result = self.composition.dispatch_store.amend_unbound(project_id=main.project_id, plan_id=command['plan_id'],
            main_run_id=main.run_id, expected_plan_revision=command['plan_revision'], expected_effective_revision=command['effective_revision'],
            expected_main_revision=command['main_revision'], expected_cancel_epoch=command['cancel_epoch'],
            replace_permit_ids=command['replace_permit_ids'], new_assignments=tuple(expert_assignment_from_payload(item) for item in command['assignments']),
            new_permits=tuple(dispatch_permit_from_payload(item) for item in command['permits']),
            steer_input_ref=source[1]['steer_input_ref'], operation_id=operation_id, agent_store=self.composition.store)
        return {'summary': '已更新尚未派发的分工', 'plan_id': result.plan.plan_id, 'permit_ids': list(result.permit_ids),
            'effective_revision': result.revision, 'evidence_refs': (frozen[0], source[0])}

    def _command(self, main, operation_id, arguments, source, topology):
        store = self.composition.dispatch_store
        original = {row['assignment']['assignment_id']: row for row in topology['assignments']}
        proposed, bound = arguments['assignments'], {identity: row for identity, row in original.items() if row['bound']}
        if arguments['cluster_id'] != topology['cluster_id'] or set(arguments['preserve_assignment_ids']) != set(bound):
            raise RecognitionConflict('task_steer_dispatched_assignment_changed')
        if any(spec['assignment_id'] in bound for spec in proposed):
            raise RecognitionConflict('task_steer_assignment_identity_collision')
        get('steer', version='@2').validate_assignment_texts(proposed, {key: row['assignment'] for key, row in original.items()}, bound_ids=tuple(bound))
        if any('division' in spec for spec in proposed):
            validate_divisions(proposed, {spec['assignment_id']: spec['assignment_id'] for spec in proposed}, external_ids=tuple(bound))
        replacements = [row['permit_id'] for row in original.values() if not row['bound']]
        mapping = {**{identity: identity for identity in bound}, **{spec['assignment_id']:
            'assignment-' + uuid5(NAMESPACE_URL, operation_id + ':' + spec['assignment_id']).hex for spec in proposed}}
        assignments, permits = [], []
        for spec in proposed:
            templates = [row for row in original.values() if all(spec[key] == row['assignment'][key]
                for key in ('profile_id', 'profile_revision', 'expert', 'skill'))]
            if not templates:
                raise RecognitionConflict('task_steer_frozen_assignment_authority_changed')
            template = templates[0]
            old = store.get_assignment(template['assignment']['assignment_id'], project_id=main.project_id)
            budget = agent_budget_from_payload(spec['budget'])
            if not set(spec['capability_ids']).issubset(old.capability_ids):
                raise RecognitionConflict('task_steer_assignment_budget_changed')
            identity = mapping[spec['assignment_id']]
            self._directives(main, self.turns.get(source[1]['steer_input_ref']))
            # 目标保留原冻结正文，用户补充由子轮实际模型输入独立继承并留证。
            body = {'schema_version': '1.0.0', 'assignment_id': identity, 'task': spec['task'], 'expert': deepcopy(spec['expert'])}
            if 'division' in spec:
                body['division'] = {**deepcopy(spec['division']), 'depends_on': [mapping[dependency] for dependency in spec['division']['depends_on']]}
            reference = self.turns.get_or_create_immutable_payload(main.turn_id, 'agent.dispatch.task.' + identity + '.v1', body)
            assignment = replace(old, assignment_id=identity, task_payload_ref=reference, task_payload_revision=1,
                delegated_budget=budget, capability_ids=tuple(spec['capability_ids']))
            permit = DispatchPermit('permit-' + uuid5(NAMESPACE_URL, operation_id + ':permit:' + identity).hex,
                main.project_id, topology['plan_id'], topology['plan_revision'], identity, operation_id)
            assignments.append(expert_assignment_to_payload(assignment))
            permits.append(dispatch_permit_to_payload(permit))
        return {'operation_id': operation_id, 'arguments': deepcopy(arguments), 'decision_ref': source[0], 'main_run_id': main.run_id,
            'project_id': main.project_id, 'plan_id': topology['plan_id'], 'plan_revision': topology['plan_revision'],
            'effective_revision': topology['effective_revision'], 'main_revision': topology['main_revision'], 'cancel_epoch': topology['cancel_epoch'],
            'replace_permit_ids': replacements, 'assignments': assignments, 'permits': permits}
