"""Durable, body-free research read proofs and scoped run topology."""
from collections.abc import Mapping
from backend.recognition import RecognitionConflict, WorkScope
from .original_sources import source_store
from .source_evidence_refs import READS, evidence_refs, roots_for
COLLECTION = 'v2_research_source_reads'


def scope_for(request):
    raw = request.get('scope')
    if not isinstance(raw,Mapping) or not isinstance(raw.get('project_id'),str):
        raise RecognitionConflict('research project scope is unavailable')
    return WorkScope('local-user',raw['project_id'])


def turn_tree(turns, agents, turn_id, project):
    if agents is None:
        identities=[turn_id]
    else:
        found=agents.get_run_by_turn_id(turn_id,project_id=project)
        run=found[0] if found else None
        seen=set()
        while run and run.parent_run_id:
            if run.run_id in seen or len(seen)>=256:
                raise RecognitionConflict('research topology is invalid')
            seen.add(run.run_id)
            run=agents.get_run(run.parent_run_id)
            if run is None or run.project_id!=project:
                raise RecognitionConflict('research topology is unavailable')
        queue=[run] if run else []
        identities=[] if queue else [turn_id]
        seen=set()
        while queue:
            run=queue.pop(0)
            if run.run_id in seen or len(seen)>=256 or run.project_id!=project:
                raise RecognitionConflict('research topology is invalid')
            seen.add(run.run_id);identities.append(run.turn_id)
            queue.extend(agents.list_runs(project_id=project,parent_run_id=run.run_id))
    for identity in identities:
        request=turns.get_request(identity)
        if request is None or scope_for(request).project_id!=project:
            raise RecognitionConflict('research turn scope is unavailable')
    return identities


def read_dependencies(records, turns, agents, turn_id, project):
    proofs=[]
    for identity in turn_tree(turns,agents,turn_id,project):
        for event in turns.events_after(identity,after_sequence=0):
            if event.get('type')!='tool.completed': continue
            data=event.get('data',{})
            if data.get('capability_id') not in READS: continue
            key=event.get('correlation',{}).get('tool_call_id')
            row=records.read(COLLECTION,key) if isinstance(key,str) else None
            if (row is None or row.payload.get('turn_id')!=identity or row.payload.get('project_id')!=project
                    or row.payload.get('capability_id')!=data['capability_id']):
                raise RecognitionConflict('research read authority is unavailable')
            proofs.append({'id':key,'revision':row.revision,**row.payload})
    return sorted(proofs,key=lambda proof:proof['id'])


def validate_reads(records, scope, proofs, *, authority, remote=True):
    if remote and authority.private_project(scope):
        raise RecognitionConflict('private_project_remote_blocked')
    for proof in proofs:
        if proof.get('project_id')!=scope.project_id:
            raise RecognitionConflict('research proof scope is invalid')
        row=records.read(COLLECTION,proof.get('id'))
        if row is None or row.revision!=proof.get('revision') or row.payload!={k:v for k,v in proof.items() if k not in ('id','revision')}:
            raise RecognitionConflict('research proof changed')
        for config in proof.get('configurations',[]):
            store=source_store(records)
            if store.read(config['collection'],config['id']) is None or store.revision(config['collection'],config['id'])!=config['revision']:
                raise RecognitionConflict('research configuration changed')
        for document in proof.get('documents',[]):
            row=records.read('documents',document['id'])
            if row is None or row.revision!=document['revision'] or row.payload.get('project_id')!=scope.project_id:
                raise RecognitionConflict('research document evidence changed')
        snapshots=[proof['source_egress']] if proof.get('source_egress') is not None else []
        snapshots.extend(proof.get('style_sources',[]))
        for snapshot in snapshots:
            own=WorkScope(snapshot['scope']['user_id'],snapshot['scope']['project_id'])
            authority.validate_snapshot(own,snapshot)
            if remote: authority.require(snapshot,'generation')


def product_read_sources(records, turns, agents, request, project):
    from .kernel.agent_coordinator import AgentCoordinatorError
    try:
        return _product_read_sources(records, turns, agents, request, project)
    except (KeyError, TypeError, AttributeError, IndexError, ValueError, AgentCoordinatorError) as error:
        raise RecognitionConflict('product draft read owner evidence is invalid') from error


def _product_read_sources(records, turns, agents, request, project):
    """Bind successful product reads to their immutable result and CAS-1 proof.

    This reads existing authorities only. Privacy is evaluated by SourceEgress
    from the proof's original ceiling and a fresh snapshot, allowing regrant
    only where the original ceiling permitted it.
    """
    identity = request['turn_id']
    accepted = turns.get_request(identity)
    if accepted != request:
        if accepted != _product_agent_request(turns, agents, request, project):
            raise RecognitionConflict('product draft kernel request changed')
    proofs = read_dependencies(records, turns, agents, identity, project)
    entries, snapshots = [], []
    for proof in proofs:
        if (type(proof['revision']) is not int or proof['revision'] != 1
                or proof.get('tool_call_id') != proof['id']):
            raise RecognitionConflict('product draft read proof changed')
        events = tuple(turns.events_after(proof['turn_id'], after_sequence=0))
        completed = [event for event in events if event.get('type') == 'tool.completed'
            and event.get('correlation', {}).get('tool_call_id') == proof['id']]
        if len(completed) != 1:
            raise RecognitionConflict('product draft read completion is unavailable')
        completed = completed[0]
        result_ref = completed.get('data', {}).get('payload_ref')
        if completed.get('data', {}).get('capability_id') != proof['capability_id']:
            raise RecognitionConflict('product draft read capability changed')
        matching = []
        for event in events:
            if (event.get('type') != 'tool.outcome.recorded'
                    or event.get('correlation', {}).get('tool_call_id') != proof['id']
                    or event['sequence'] >= completed['sequence']):
                continue
            outcome_ref = event.get('data', {}).get('payload_ref')
            outcome = turns.get(outcome_ref) if isinstance(outcome_ref, str) else None
            if (isinstance(outcome, Mapping) and outcome.get('status') == 'completed'
                    and outcome.get('turn_id') == proof['turn_id']
                    and outcome.get('invocation_id') == proof['id']
                    and outcome.get('capability_id') == proof['capability_id']
                    and outcome.get('payload_ref') == result_ref):
                matching.append(outcome_ref)
        if len(matching) != 1 or not isinstance(result_ref, str):
            raise RecognitionConflict('product draft read result binding is unavailable')
        result = turns.get(result_ref)
        scope = WorkScope('local-user', project)
        refs = evidence_refs(proof['capability_id'], {'result': result})
        with records.begin() as reader:
            roots = roots_for(reader, scope, refs)
        frozen = proof.get('source_egress')
        if (frozen is None and roots or frozen is not None and (
                not isinstance(frozen, Mapping) or frozen.get('roots') != roots
                or frozen.get('scope') != {'user_id': scope.user_id, 'project_id': project})):
            raise RecognitionConflict('product draft read source identity changed')
        for document in proof.get('documents', []):
            row = records.read('documents', document.get('id'))
            if (row is None or type(document.get('revision')) is not int
                    or row.revision != document['revision'] or row.payload.get('project_id') != project):
                raise RecognitionConflict('product draft read document changed')
        store = source_store(records)
        for config in proof.get('configurations', []):
            if (store.read(config['collection'], config['id']) is None
                    or type(config.get('revision')) is not int
                    or store.revision(config['collection'], config['id']) != config['revision']):
                raise RecognitionConflict('product draft read configuration changed')
        if proof['capability_id'] == 'source.evidence.read':
            from core.product_core.source_template_document import prepare_source_document_ai_evidence
            source = store.read('sources', result['source_id'])
            actual_revision = store.revision('sources', result['source_id'])
            if (source is None or type(result.get('source_revision')) is not int
                    or result['source_revision'] != actual_revision):
                raise RecognitionConflict('product draft read original changed')
            try:
                canonical = prepare_source_document_ai_evidence(source=source, source_revision=actual_revision,
                    template_type=result['template_type'], prompt_context=result.get('prompt_context', []),
                    style_prefix=result.get('style_prefix', ''), outline_override=result.get('outline'),
                    document_baseline=result.get('document_baseline'))
            except (KeyError, TypeError, ValueError) as error:
                raise RecognitionConflict('product draft source body is unavailable') from error
            for field in ('source_id', 'source_revision', 'project_id', 'source_title', 'series_name',
                    'summary', 'key_points', 'structured_body', 'structure_ref', 'source_refs'):
                if canonical[field] != result.get(field):
                    raise RecognitionConflict('product draft read body changed')
        entry = {'kind': 'read_proof', 'scope': {'user_id': scope.user_id, 'project_id': project},
            'product_turn_id': identity, 'turn_id': proof['turn_id'], 'tool_call_id': proof['id'],
            'proof_revision': proof['revision'], 'capability_id': proof['capability_id'],
            'completed_sequence': completed['sequence'], 'outcome_ref': matching[0], 'result_ref': result_ref}
        entries.append(entry)
        if frozen is not None:
            snapshots.append(frozen)
        snapshots.extend(proof.get('style_sources', []))
    return tuple(entries), tuple(snapshots)


def _product_agent_request(turns, agents, request, project):
    """Rebuild only the registered main Run's existing admission transforms."""
    import json
    from core.ai_kernel import validate_turn_request
    from core.ai_kernel.agent_contracts import agent_run_from_payload
    from core.ai_kernel.agent_store import _same_run_snapshot
    from core.recursive_evolution.agent_policy import AgentEvolutionPolicy
    from .kernel.agent_organization_runtime import AgentOrganizationRuntime
    from .kernel.agent_coordinator import (
        _FrozenPolicyAdmission, _agent_binding_from_run, _apply_policy_to_request,
        _profile_budget_uri, _require_policy_role_and_profile,
    )
    identity = request['turn_id']
    found = agents.get_run_by_turn_id(identity, project_id=project) if agents else None
    if found is None:
        raise RecognitionConflict('product draft main Run is unavailable')
    run = found[0]
    operation = agents._read_one(
        'SELECT kind,request_json,result_json FROM ai_agent_operations WHERE operation_id=?',
        (request['operation_id'],))
    if operation is None or operation[0] != 'write_run' or operation[1] != operation[2]:
        raise RecognitionConflict('product draft main operation changed')
    initial = agent_run_from_payload(json.loads(operation[2]))
    if (initial.turn_id != identity or initial.project_id != project
            or initial.run_id != 'main-run-' + identity or initial.role != 'main'
            or initial.profile_id != 'main.orchestrator' or initial.depth != 0
            or initial.parent_run_id is not None or not _same_run_snapshot(initial, run)
            or initial.budget_snapshot_ref != _profile_budget_uri(
                identity, initial.profile_id, initial.profile_revision)):
        raise RecognitionConflict('product draft main Run binding changed')
    expected = AgentOrganizationRuntime._with_host_capabilities(request)
    policy = expected['capability_policy']
    original_allowed = set(policy['allowed'])
    allowed = set(initial.capability_ids) & original_allowed
    if set(initial.capability_ids) != allowed:
        raise RecognitionConflict('product draft main capability binding changed')
    expected['capability_policy'] = {
        'allowed': sorted(allowed),
        'denied': sorted(set(policy['denied']) | (original_allowed - allowed)),
        'require_approval': sorted(set(policy['require_approval']) & allowed),
    }
    expected['agent_binding'] = _agent_binding_from_run(initial)
    accepted = validate_turn_request(turns.get_request(identity))
    if 'agent_policy_binding' in accepted:
        binding = accepted['agent_policy_binding']
        if not isinstance(binding, Mapping) or set(binding) != {'policy_id', 'revision', 'snapshot_ref'}:
            raise RecognitionConflict('product draft agent policy binding is invalid')
        ref = turns.immutable_payload_reference(identity, 'agent-policy-snapshot-v1')
        if binding['snapshot_ref'] != ref:
            raise RecognitionConflict('product draft agent policy owner changed')
        frozen = turns.get(ref)
        if (not isinstance(frozen, Mapping) or set(frozen) != {
                'schema_version', 'kind', 'policy_id', 'revision', 'policy'}
                or frozen['schema_version'] != '1.0.0' or frozen['kind'] != 'agent.policy.snapshot.v1'
                or frozen['policy_id'] != binding['policy_id'] or frozen['revision'] != binding['revision']):
            raise RecognitionConflict('product draft frozen agent policy changed')
        policy = AgentEvolutionPolicy.from_payload(frozen['policy'])
        if policy.policy_id != binding['policy_id'] or policy.revision != binding['revision']:
            raise RecognitionConflict('product draft frozen agent policy identity changed')
        _require_policy_role_and_profile(policy, role=initial.role, profile_id=initial.profile_id)
        admission = _FrozenPolicyAdmission(identity, policy.policy_id, policy.revision, ref, policy, frozen)
        expected = _apply_policy_to_request(expected, admission)
    return expected
