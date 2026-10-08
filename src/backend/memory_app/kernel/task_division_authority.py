"""Read the existing durable dispatch permit; never infer permission from UI input."""


def _division_authority(composition, request):
    if request.get('desired_outcome') != 'project.task':
        return None
    project = request.get('scope', {}).get('project_id')
    identity = request.get('turn_id')
    frozen = composition.request_loader(identity)
    if not frozen or any(frozen.get(key) != request.get(key) for key in (
        'scope', 'privacy', 'capability_policy', 'agent_binding', 'desired_outcome',
    )):
        return None
    found = composition.store.get_run_by_turn_id(identity, project_id=project)
    if found is None:
        return None
    run, _ = found
    if run.role != 'subagent' or run.is_terminal or run.parent_run_id is None:
        return None
    matches = [permit for permit in composition.dispatch_store.list_permits(project_id=project)
        if permit.status == 'consumed' and composition.dispatch_store.get_permit_child_run_binding(
            permit.permit_id, project_id=project) == run.run_id]
    if len(matches) != 1:
        return None
    permit = matches[0]
    plan = composition.dispatch_store.get_plan(permit.plan_id, project_id=project)
    assignment = composition.dispatch_store.get_assignment(permit.assignment_id, project_id=project)
    if (plan is None or assignment is None or plan.main_run_id != run.parent_run_id
            or plan.status not in {'dispatching', 'dispatched'}
            or assignment.assignment_id not in plan.assignment_ids
            or set(assignment.capability_ids) != set(run.capability_ids)):
        return None
    return frozen, run, permit, plan, assignment


def frozen_division_capabilities(composition, request):
    authority = _division_authority(composition, request)
    if authority is None:
        return ()
    frozen, _, _, _, assignment = authority
    allowed = set(frozen['capability_policy']['allowed']) - set(frozen['capability_policy']['denied'])
    return tuple(capability for capability in assignment.capability_ids if capability in allowed)


def frozen_division_binding(composition, request, payloads):
    """Read deliverable identity only from the consumed permit's parent snapshot.

    This host-only read does not add the parent's payload to a worker's model
    context or change the frozen Turn contract.
    """
    authority = _division_authority(composition, request)
    if authority is None:
        return None
    frozen, run, permit, plan, assignment = authority
    allowed = set(frozen['capability_policy']['allowed']) - set(frozen['capability_policy']['denied'])
    if (frozen.get('input') != request.get('input')
            or assignment.project_id != run.project_id
            or 'document.draft.propose' not in (set(assignment.capability_ids) & allowed)
            or assignment.task_payload_revision != 1):
        return None
    parent = composition.store.get_run(run.parent_run_id)
    if parent is None or parent.project_id != run.project_id or parent.role != 'main':
        return None
    stored = payloads.get_immutable_payload(parent.turn_id,
        f'agent.dispatch.task.{assignment.assignment_id}.v1')
    if stored is None or stored[0] != assignment.task_payload_ref:
        return None
    payload = stored[1]
    if (not isinstance(payload, dict) or set(payload) != {'schema_version', 'assignment_id', 'task', 'expert', 'division'}
            or payload['schema_version'] != '1.0.0'
            or payload['assignment_id'] != assignment.assignment_id):
        return None
    division = payload['division']
    if (not isinstance(division, dict) or set(division) != {'goal', 'deliverable', 'depends_on'}
            or any(not isinstance(division[key], str) or not division[key].strip()
                   or len(division[key]) > 2000 for key in ('goal', 'deliverable'))
            or not isinstance(division['depends_on'], list)
            or any(not isinstance(value, str) for value in division['depends_on'])
            or not set(division['depends_on']).issubset(plan.assignment_ids)):
        return None
    task = payload['task']
    text = frozen.get('input', {}).get('text')
    if not isinstance(task, str) or not isinstance(text, str) or not (
            text == task.strip() or (division['depends_on']
                and text.startswith(task.strip() + '\n\n主智能体转交的依赖结果：\n'))):
        return None
    return {'turn_id': run.turn_id, 'project_id': run.project_id,
        'run_id': run.run_id, 'parent_run_id': run.parent_run_id,
        'permit_id': permit.permit_id, 'plan_id': plan.plan_id,
        'assignment_id': assignment.assignment_id,
        'task_payload_ref': assignment.task_payload_ref,
        'task_payload_revision': assignment.task_payload_revision,
        'deliverable': division['deliverable']}
