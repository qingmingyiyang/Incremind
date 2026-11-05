from copy import deepcopy
import json

from backend.recognition import RecognitionConflict
from core.ai_kernel.contracts import _ERROR_CODE, validate_event_transition, validate_model_wire_attempt_receipt
from core.ai_kernel.product_admission import ProductAdmissionItem, admission_identity
from core.ai_kernel.product_snapshot import read_product_turn_snapshot
from core.ai_kernel.turn_receipt_projection import receipt_from_events

from ..answer_effective import NativeAnswerSnapshot
from ..kernel.route_source_snapshot import native_source_connection
from .outcome_source_reads import SourceReadRecords, task_dependency_scope
from .shared_auxiliary import _qualified_commit
from .shared_history import historical_origin
from .shared_rendered_preparation import _query_current
from .policies import get


KIND = 'shared-task-redo-v1'
FIELDS = {'schema_version', 'turn_id', 'execution', 'origin_ref', 'source'}
SOURCE_FIELDS = {'public_id', 'source_kind', 'revision', 'items', 'task_text', 'scene', 'guard',
    'native_request', 'native_identity', 'origin_ref', 'material_proof_ref', 'materials', 'proof_refs', 'source_proofs'}
TERMINAL_STATES = {'turn.completed': 'completed', 'turn.failed': 'failed', 'turn.cancelled': 'cancelled'}


def _reader_service(query):
    from .task_do import TaskDo
    application = query.answer_turns.application
    service = getattr(application.state, 'workbench_research_service', None)
    if (type(service) is not TaskDo or service.records is not query.receipt_records
            or service.models is not query.models or service.query is not application.state.workspace_domains.query):
        raise RecognitionConflict('shared_task_redo_actual_service_unavailable')
    return service


def _strict_sources(freshness, sources, reader):
    qualified = freshness._qualify_source_proofs(sources, reader=reader, local_only=True)
    if not qualified['complete']:
        raise RecognitionConflict('shared_task_redo_source_unverifiable')
    for source in sources:
        if not source.get('verified'):
            raise RecognitionConflict('shared_task_redo_source_unverifiable')
        frozen, origin = source['frozen'], source['origin']
        owner = freshness._source_owner(origin['project_id'], origin=origin, binding=source, reader=reader)
        current = freshness._source(owner, freshness._source_reader(owner, reader), frozen['source_type'], frozen['id'])
        if (current['incarnation'] != frozen['incarnation'] or current['revision'] != frozen['revision']
                or current['content'] != frozen['content'] or current['manual_forgotten'] or current['replaced_by']
                or get('stale', version='@1').changed(frozen, current, reference=freshness.now())['reasons']):
            raise RecognitionConflict('shared_task_redo_source_changed')
    return qualified


def _closed_run(connection, snapshot, run):
    request, events = snapshot['request'], snapshot['events']
    identity = request['turn_id']
    prior = None
    for event in events:
        prior = validate_event_transition(prior, event)
        if event['turn_id'] != identity or event['session_id'] != request['session_id']:
            raise RecognitionConflict('shared_task_redo_native_changed')
    if (prior is None or TERMINAL_STATES.get(prior['type']) != run.status or not run.is_terminal
            or prior['data']['status'] != run.status or not run.terminal_receipt_ref):
        raise RecognitionConflict('shared_task_redo_execution_not_closed')
    receipt = receipt_from_events(events, turn_id=identity, request=request)
    stored = snapshot['immutable'].get('agent-turn-terminal-receipt-v1')
    if stored is None or stored[0] != run.terminal_receipt_ref or not isinstance(stored[1], dict):
        raise RecognitionConflict('shared_task_redo_execution_not_closed')
    saved = stored[1]
    expected = {'schema_version': '1.0.0', 'kind': 'agent.turn-terminal-receipt.v1', 'turn_id': identity,
        'session_id': receipt.session_id, 'operation_id': receipt.operation_id, 'status': receipt.status,
        'sequence': receipt.current_sequence, 'replayed': saved.get('replayed')}
    if saved != expected or type(saved['replayed']) is not bool:
        raise RecognitionConflict('shared_task_redo_execution_not_closed')
    active = ('SELECT 1 FROM ai_turn_run_leases WHERE turn_id=?', 'SELECT 1 FROM ai_turn_pending WHERE turn_id=?',
        "SELECT 1 FROM ai_turn_recovery_queue WHERE turn_id=? AND status IN ('pending','running')",
        "SELECT 1 FROM ai_expert_job_waits WHERE turn_id=? AND status!='terminal_observed'")
    if any(connection.execute(statement, (identity,)).fetchone() for statement in active):
        raise RecognitionConflict('shared_task_redo_execution_not_closed')
    return prior


def _closed_model_inputs(connection, snapshot, native, agents):
    from .task_model_inputs import INPUT_PREFIX, WIRE_PREFIX, read_task_model_input
    identity = snapshot['request']['turn_id']
    events, attempts = snapshot['events'], snapshot['attempts']
    dispatched = [(event['data'].get('payload_ref'), event['correlation'].get('model_request_id'))
        for event in events if event['type'] == 'model.attempt.dispatched']
    terminal_events = [event for event in events if event['type'] == 'model.attempt.terminal']
    terminals = {(event['data'].get('receipt_ref'), event['correlation'].get('model_request_id')): event
        for event in terminal_events}
    if (set(dispatched) != {(item['dispatch_ref'], item['model_request_id']) for item in attempts}
            or set(terminals) != {(item['terminal_ref'], item['model_request_id']) for item in attempts}
            or len(dispatched) != len(attempts) or len(terminal_events) != len(attempts)):
        raise RecognitionConflict('shared_task_redo_dispatched_input_unavailable')
    payloads = set(connection.execute("SELECT kind,payload_ref FROM ai_turn_payloads WHERE turn_id=? "
        "AND kind IN ('model-wire-attempt-dispatch','model-wire-attempt-receipt')", (identity,)))
    expected_payloads = {('model-wire-attempt-dispatch', ref) for ref, _ in dispatched} | {
        ('model-wire-attempt-receipt', ref) for ref, _ in terminals}
    wires = {kind for kind in snapshot['immutable'] if kind.startswith(WIRE_PREFIX)}
    if payloads != expected_payloads or wires != {WIRE_PREFIX + item['attempt_id'] for item in attempts}:
        raise RecognitionConflict('shared_task_redo_dispatched_input_unavailable')
    effects = {operation: (state, error_ref, lease_owner, lease_expires_at)
        for operation, state, error_ref, lease_owner, lease_expires_at in connection.execute(
            "SELECT operation_id,state,error_ref,lease_owner,lease_expires_at FROM effect WHERE turn_id=? AND kind='model_call'",
            (identity,))}
    if set(effects) != {attempt['attempt_id'] for attempt in attempts}:
        raise RecognitionConflict('shared_task_redo_dispatched_input_unavailable')
    inputs, uncertain = set(), set()
    for attempt in attempts:
        identity_fields = ('turn_id', 'model_request_id', 'attempt_id', 'attempt_number',
            'routing_snapshot_revision', 'provider_id', 'model_id')
        if attempt['status'] != 'terminal' or attempt['dispatch'] is None or attempt['terminal'] is None:
            raise RecognitionConflict('shared_task_redo_execution_not_closed')
        closed = validate_model_wire_attempt_receipt(attempt['terminal'])
        event = terminals[attempt['terminal_ref'], attempt['model_request_id']]
        if (any(attempt['dispatch'][key] != closed[key] for key in identity_fields)
                or attempt['dispatch_ref'] not in event['data'].get('evidence_refs', ())):
            raise RecognitionConflict('shared_task_redo_dispatched_input_unavailable')
        effect = effects[attempt['attempt_id']]
        expected_effect = ('SETTLED_OK', None, None, None) if closed['status'] == 'succeeded' else (
            'UNKNOWN', closed['error_code'], None, None)
        if effect != expected_effect:
            raise RecognitionConflict('shared_task_redo_execution_not_closed')
        # 模型失败保留原用量和 UNKNOWN 副作用状态，终态凭证只用于核实已尝试发送的输入。
        if effect[0] == 'UNKNOWN':
            uncertain.add(attempt['attempt_id'])
        wire = snapshot['immutable'].get(WIRE_PREFIX + attempt['attempt_id'])
        if wire is None or not isinstance(wire[1].get('input_ref'), str):
            raise RecognitionConflict('shared_task_redo_dispatched_input_unavailable')
        reference = wire[1]['input_ref']
        read_task_model_input(native, agents, identity, reference, attempt_id=attempt['attempt_id'])
        inputs.add((identity, reference))
    unsettled = {operation for (operation,) in connection.execute("SELECT operation_id FROM effect WHERE turn_id=? "
        "AND state NOT IN ('SETTLED_OK','SETTLED_ERR','COMPENSATED','ABANDONED')", (identity,))}
    if unsettled != uncertain:
        raise RecognitionConflict('shared_task_redo_execution_not_closed')
    # 本机组织规则也会产生 model.completed，原生事件与发送、输入凭证分开核对。
    attempted = {item['model_request_id'] for item in attempts}
    routed = {event['correlation'].get('model_request_id') for event in events if event['type'] == 'model.routed'}
    receipts = {json.loads(raw).get('model_request_id') for (raw,) in connection.execute(
        "SELECT payload_json FROM ai_turn_payloads WHERE turn_id=? AND kind IN "
        "('model-call-receipt','model-dispatch-authority-receipt','prompt-cache-receipt')", (identity,))}
    for event in events:
        model_id = event['correlation'].get('model_request_id')
        if event['type'] != 'model.completed' or model_id in attempted:
            continue
        requested = [prior for prior in events if prior['type'] == 'model.requested'
            and prior['correlation'].get('model_request_id') == model_id]
        data = event['data']
        if (not isinstance(model_id, str) or not model_id or model_id in routed or model_id in receipts
                or len(requested) != 1 or requested[0]['sequence'] >= event['sequence']
                or requested[0]['correlation'] != event['correlation']
                or requested[0]['data'].get('model_call_purpose') != data.get('model_call_purpose')
                or data.get('status') != 'running' or data.get('error_code') is not None
                or data.get('payload_ref') is not None or data.get('receipt_ref') is not None or data.get('evidence_refs') != []
                or any(kind.startswith(INPUT_PREFIX + model_id + '-') for kind in snapshot['immutable'])):
            raise RecognitionConflict('shared_task_redo_dispatched_input_unavailable')
    return inputs


def _aborted_spawn(connection, run):
    from core.ai_kernel.agent_contracts import AgentBudget, agent_budget_reservation_from_payload, agent_child_link_from_payload
    # 只有原保留已明确中止且没有任何原生执行事实的子轮占位可以跳过。
    if run.role != 'subagent' or run.status not in {'created', 'queued'} or run.parent_run_id is None:
        return False
    child = connection.execute('SELECT link_id,parent_run_id,project_id,status,payload_json '
        'FROM ai_agent_child_links WHERE child_run_id=?', (run.run_id,)).fetchone()
    reserved = connection.execute('SELECT reservation_id,parent_run_id,project_id,status,payload_json '
        'FROM ai_agent_budget_reservations WHERE child_run_id=?', (run.run_id,)).fetchone()
    if child is None or reserved is None:
        return False
    child_value, reserved_value = json.loads(child[4]), json.loads(reserved[4])
    link, reservation = agent_child_link_from_payload(child_value), agent_budget_reservation_from_payload(reserved_value)
    if (child[:4] != (link.link_id, run.parent_run_id, run.project_id, 'cancelled')
            or reserved[:4] != (reservation.reservation_id, run.parent_run_id, run.project_id, 'released')
            or link.parent_run_id != run.parent_run_id or reservation.parent_run_id != run.parent_run_id
            or link.child_run_id != run.run_id or reservation.child_run_id != run.run_id
            or link.parent_project_id != run.project_id or link.child_project_id != run.project_id
            or reservation.project_id != run.project_id or link.status != 'cancelled' or reservation.status != 'released'
            or reservation.operation_id != link.spawn_operation_id or reservation.parent_cancel_epoch != link.parent_cancel_epoch
            or run.cancel_epoch != link.parent_cancel_epoch or link.child_depth != run.depth
            or link.delegated_budget != run.budget_limit or reservation.reserved_budget != run.budget_limit
            or link.delegated_capability_ids != run.capability_ids or reservation.settled_budget != AgentBudget(0, 0, 0, 0, 0)):
        return False
    aborts = list(connection.execute('SELECT kind,request_json,result_json FROM ai_agent_operations '
        'WHERE operation_id IN (?,?)', (link.spawn_operation_id + '.abort', 'recovery-abort-' + run.turn_id)))
    if len(aborts) != 1:
        return False
    kind, abort_request, result = aborts[0]
    if (kind != 'abort_reserved_spawn' or json.loads(abort_request) != {'link_id': link.link_id,
            'reservation_id': reservation.reservation_id, 'epoch': link.parent_cancel_epoch}
            or json.loads(result) != {'link': child_value, 'reservation': reserved_value}):
        return False
    if connection.execute('SELECT 1 FROM ai_agent_runs WHERE parent_run_id=? LIMIT 1', (run.run_id,)).fetchone():
        return False
    tables = ('ai_turns', 'ai_turn_events', 'ai_turn_immutable_payloads', 'ai_turn_payloads', 'ai_turn_run_leases',
        'ai_turn_pending', 'ai_turn_recovery_queue', 'ai_expert_job_waits', 'ai_model_attempt_reservations', 'effect')
    return not any(connection.execute('SELECT 1 FROM ' + table + ' WHERE turn_id=? LIMIT 1', (run.turn_id,)).fetchone()
        for table in tables)


def _failed_execution(freshness, view, request, root_snapshot):
    from core.ai_kernel.agent_contracts import agent_run_from_payload
    from .task_model_inputs import task_profile_chain
    native = freshness._native()
    agents = freshness.application.state.agent_runtime_composition.store
    root, project = request['turn_id'], request['scope']['project_id']
    inputs, accepted, failure = set(), set(), None
    with native_source_connection(freshness.records, reader=view) as connection:
        rows = list(connection.execute('WITH RECURSIVE related(run_id,turn_id,project_id,parent_run_id,payload_json) AS ('
            'SELECT run_id,turn_id,project_id,parent_run_id,payload_json FROM ai_agent_runs WHERE turn_id=? UNION '
            'SELECT child.run_id,child.turn_id,child.project_id,child.parent_run_id,child.payload_json '
            'FROM ai_agent_runs AS child JOIN related ON child.parent_run_id=related.run_id) SELECT * FROM related LIMIT 257',
            (root,)))
        if not 1 <= len(rows) <= 256 or len({row[1] for row in rows}) != len(rows):
            raise RecognitionConflict('shared_task_redo_native_changed')
        for run_id, identity, run_project, parent, raw in rows:
            found = agents.get_run_by_turn_id(identity, project_id=project)
            run = agent_run_from_payload(json.loads(raw))
            if (run_project != project or (run_id, identity, project, parent) != (
                    run.run_id, run.turn_id, run.project_id, run.parent_run_id)
                    or found is None or found[0] != run):
                raise RecognitionConflict('shared_task_redo_native_changed')
            if identity != root and _aborted_spawn(connection, run):
                continue
            value = read_product_turn_snapshot(connection, identity)
            if task_profile_chain(native, agents, value['request'])[-1] != request:
                raise RecognitionConflict('shared_task_redo_native_changed')
            terminal = _closed_run(connection, value, run)
            if identity == root:
                code = terminal['data'].get('error_code')
                if (value != root_snapshot or run.role != 'main' or parent is not None or terminal['type'] != 'turn.failed'
                        or not isinstance(code, str) or not _ERROR_CODE.fullmatch(code)):
                    raise RecognitionConflict('shared_task_redo_execution_not_closed')
                failure = {'terminal_sequence': terminal['sequence'], 'error_code': code}
            inputs.update(_closed_model_inputs(connection, value, native, agents))
            accepted.add(identity)
    if failure is None:
        raise RecognitionConflict('shared_task_redo_execution_not_closed')
    return failure, inputs, accepted


def _initial_sources(query, request, original, proof, reference, reader):
    from .shared_task_materials import validate_historical_task_materials
    validate_historical_task_materials(query, request, original, proof, reader=reader)
    freshness = query.outcome_freshness
    result = []
    for material, snapshot in zip(proof['selection']['origin_materials'], proof['privacy']['source_snapshots'], strict=True):
        value = {**material, 'snapshot': snapshot, 'proof_ref': reference, 'role': 'initial_admission'}
        owner = freshness._material_owner(value, reader)
        result.extend(freshness._freeze_material(owner, value, reader=reader))
    result.extend(inherited_sources(query, request, reader=reader))
    return result


def capture_source(query, public_id, *, source_kind, reader=None):
    from backend.recognition.product_draft_dependencies import ProductDraftDependencyError, _shared_task_materials
    from .shared_task_materials import KIND as MATERIAL_KIND
    _query_current(query)
    freshness = query.outcome_freshness
    with task_dependency_scope(freshness, 'redo/' + public_id), _qualified_commit(query.origin_admission), freshness._read_scope(reader) as view:
        if not isinstance(view, SourceReadRecords):
            raise RecognitionConflict('shared_task_redo_source_scope_required')
        public = view.read('v2_turns', public_id)
        execution = view.read('v2_task_executions', public_id)
        if public is None or execution is None or public.payload.get('intent') != 'do':
            raise RecognitionConflict('shared_task_redo_original_unavailable')
        sample = _reader_service(query)._redo_source(public, source_kind=source_kind)
        guard = sample['guard']
        division = view.read('v2_task_divisions', public_id)
        if (guard['turn_revision'] != public.revision or guard['execution_revision'] != execution.revision
                or guard['sample_revision'] != (division.revision if division is not None else None)):
            raise RecognitionConflict('shared_task_redo_original_changed')
        native = freshness._native()
        identity = execution.payload['request']['turn_id']
        with native_source_connection(freshness.records, reader=view) as connection:
            value = read_product_turn_snapshot(connection, identity)
        snapshot = NativeAnswerSnapshot(value)
        request = snapshot.get_request(identity)
        if request is None or admission_identity(request) != admission_identity(execution.payload['request']):
            raise RecognitionConflict('shared_task_redo_native_changed')
        original, _ = historical_origin(query, snapshot)
        failure = None
        if value['events'] and value['events'][-1]['type'] == 'turn.failed':
            if (public.payload.get('receipt', {}).get('do', {}).get('state') != 'failed'
                    or execution.payload.get('started') is not True or execution.payload.get('owner') is not None):
                raise RecognitionConflict('shared_task_redo_execution_not_closed')
            failure, inputs, accepted = _failed_execution(freshness, view, request, value)
        else:
            freshness._completed(request)
        try:
            material_proof = _shared_task_materials(view, request)
        except ProductDraftDependencyError as error:
            raise RecognitionConflict('shared_task_redo_material_proof_unavailable') from error
        if material_proof is None:
            raise RecognitionConflict('shared_task_redo_material_proof_unavailable')
        reference = native.get_immutable_payload(identity, MATERIAL_KIND)
        if reference is None:
            raise RecognitionConflict('shared_task_redo_material_proof_unavailable')
        context = (freshness.completion_context(request, reader=view) if failure is None else
            freshness._completion_context(request, freshness._completion_basis_refs(view, accepted)))
        if failure is None and not context['proof_refs']:
            raise RecognitionConflict('shared_task_redo_dispatched_input_unavailable')
        sources = context['source_proofs']
        if failure is not None:
            if inputs != {(proof['turn_id'], proof['input_ref']) for proof in context['proof_refs']}:
                raise RecognitionConflict('shared_task_redo_dispatched_input_unavailable')
            sources += _initial_sources(query, request, original, material_proof, reference[0], view)
        materials = deepcopy(material_proof['selection']['origin_materials'])
        for material, frozen in zip(materials, material_proof['privacy']['source_snapshots'], strict=True):
            descriptor = {key: material[key] for key in ('type', 'id', 'revision', 'project_id')}
            if not any(source.get('verified') and source.get('role') == 'initial_admission'
                    and source['material'] == descriptor and source['origin']['namespace'] == material['namespace']
                    and source['snapshot'] == frozen for source in sources):
                raise RecognitionConflict('shared_task_redo_initial_ceiling_unavailable')
        _strict_sources(freshness, sources, view)
        for source in sources:
            material = {**deepcopy(source['material']), 'namespace': source['origin']['namespace']}
            if material not in materials:
                materials.append(material)
        view.validate(freshness)
        return {**deepcopy(sample), 'public_id': public_id, 'native_request': deepcopy(request),
            'native_identity': admission_identity(request), 'origin_ref': deepcopy(original.ref),
            'material_proof_ref': reference[0], 'materials': materials,
            'proof_refs': deepcopy(context['proof_refs']), 'source_proofs': deepcopy(sources),
            **({'failed_execution': failure} if failure is not None else {})}


def validate_source(query, source, *, local_only, reader=None):
    if not isinstance(source, dict) or set(source) not in (SOURCE_FIELDS, SOURCE_FIELDS | {'failed_execution'}):
        raise RecognitionConflict('shared_task_redo_source_shape_changed')
    fresh = capture_source(query, source['public_id'], source_kind=source['source_kind'], reader=reader)
    if fresh != source:
        raise RecognitionConflict('shared_task_redo_source_changed')
    with _qualified_commit(query.origin_admission), query.outcome_freshness._read_scope(reader) as view:
        qualified = _strict_sources(query.outcome_freshness, source['source_proofs'], view)
        if not local_only and (not source['native_request']['privacy']['allow_remote']
                or 'generation' not in qualified['privacy_ceiling']['effective_purposes']):
            raise RecognitionConflict('private_source_remote_blocked')
    return fresh


def admission_item(query, request, source):
    validate_source(query, source, local_only=not request['privacy']['allow_remote'])
    body = json.loads(request['input']['text'])
    if (request['desired_outcome'] != 'project.task' or request['turn_id'] == source['native_request']['turn_id']
            or body.get('task') != source['task_text'] or body.get('division_override') != source['items']):
        raise RecognitionConflict('shared_task_redo_input_changed')
    admission = query.origin_admission
    reference = {'kind': 'session_event', 'object_id': request['turn_id'],
        'uri': admission._store().immutable_payload_reference(request['turn_id'], KIND)}
    return ProductAdmissionItem(KIND, reference, admission._scope(request), {'schema_version': 1,
        'turn_id': request['turn_id'], 'execution': deepcopy(admission.personal),
        'origin_ref': admission._reference(request), 'source': deepcopy(source)})


def read_item(query, request, *, bundle=None, reader=None):
    native = query.origin_admission._store()
    admitted = native.restore_admission(request) if bundle is None else bundle
    selected = [] if admitted is None else [item for item in admitted.items if item.kind == KIND]
    body = json.loads(request['input']['text'])
    if not selected:
        if body.get('division_override') is not None:
            raise RecognitionConflict('shared_task_redo_source_proof_unavailable')
        return None
    if len(selected) != 1:
        raise RecognitionConflict('shared_task_redo_source_proof_changed')
    item = selected[0]
    value = item.payload
    origins = [entry for entry in admitted.items if entry.kind == 'material-origin-v1']
    if (not isinstance(value, dict) or set(value) != FIELDS or type(value['schema_version']) is not int
            or value['schema_version'] != 1 or value['turn_id'] != request['turn_id'] or len(origins) != 1
            or value['execution'] != origins[0].payload['execution'] or value['origin_ref'] != origins[0].ref
            or item.scope != origins[0].scope or request['input']['refs'].count(item.ref) != 1
            or value['source']['native_request']['turn_id'] == request['turn_id']
            or body.get('task') != value['source']['task_text']
            or body.get('division_override') != value['source']['items']):
        raise RecognitionConflict('shared_task_redo_source_proof_changed')
    validate_source(query, value['source'], local_only=not request['privacy']['allow_remote'], reader=reader)
    materials = [entry for entry in admitted.items if entry.kind == 'shared-task-materials-v1']
    if (len(materials) != 1 or any(material not in materials[0].payload['proof']['selection']['origin_materials']
            for material in value['source']['materials'])):
        raise RecognitionConflict('shared_task_redo_material_admission_changed')
    return value


def inherited_sources(query, request, *, reader):
    value = read_item(query, request, reader=reader)
    if value is None:
        return []
    reference = query.origin_admission._store().get_immutable_payload(request['turn_id'], KIND)
    if reference is None:
        raise RecognitionConflict('shared_task_redo_source_proof_unavailable')
    return [{**deepcopy(source), 'role': 'redo_inheritance', 'proof_ref': reference[0]}
        for source in value['source']['source_proofs']]
