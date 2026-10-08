"""Real frozen permits and operation receipts bound deterministic completion."""
from copy import deepcopy

import pytest

from backend.memory_app.kernel.task_division_authority import (
    frozen_division_binding, frozen_division_capabilities,
)
from backend.memory_app.kernel.task_draft_capability import (
    TaskDraftCapability, final_draft_decision, task_draft_definition,
)
from backend.memory_app.v2.task_drafts import COLLECTION, TaskDrafts
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.privacy import freeze_turn_materials
from core.ai_kernel import CapabilityDefinition, SQLiteAITurnStore
from core.ai_kernel.tool_invocation import build_intent, intent_to_payload
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from tests.backend.unit.api.test_agent_organization_e2e import (
    _organization, _request, _cluster_proposal, _converge_child,
)
from tests.memory_app.v2.test_turn_requests import Models


@pytest.fixture
def authority(tmp_path):
    composition, _, organization = _organization(tmp_path)
    records = SQLiteStructuredRecordStore(tmp_path / 'documents.sqlite3')
    request = _request(suffix='final-draft')
    request['desired_outcome'] = 'project.task'
    _, request['privacy'] = freeze_turn_materials(records, Models(), 'project-alpha', [],
        authority=SourceEgressService(records))
    request['capability_policy']['allowed'].append('document.draft.propose')
    started = organization.start(request, agent_turn_mode=True)
    steward = composition.request_loader(started['steward']['turn_id'])
    proposal = _cluster_proposal()
    proposal['assignments'][0].update(profile_id='subagent.worker',
        capability_ids=['document.draft.propose'],
        division={'goal': '核对材料', 'deliverable': '任意交付名称', 'depends_on': []})
    composition.coordinator.plan(parent_turn_id=steward['turn_id'],
        operation_id='op-final-draft-plan', project_id='project-alpha',
        scope=steward['scope'], privacy=steward['privacy'], arguments=proposal)
    _converge_child(composition, started['steward']['run_id'])
    organization.on_terminal(steward['turn_id'])
    worker = next(run for run in composition.store.list_runs(project_id='project-alpha')
                  if run.profile_id == 'subagent.worker')
    frozen = composition.request_loader(worker.turn_id)
    sessions = SQLiteAITurnStore(tmp_path / '.rebuild-data' / 'ai-turns.sqlite3')
    drafts = TaskDrafts(records, SQLiteDocumentRepository(records))
    provider = TaskDraftCapability(legacy=None, drafts=drafts,
        request_loader=composition.request_loader,
        division_capabilities=lambda value: frozen_division_capabilities(composition, value),
        division_binding=lambda value: frozen_division_binding(composition, value, sessions))
    return composition, sessions, frozen, drafts, provider


def completed_draft(authority, *, markdown='核对原件再引用'):
    composition, sessions, request, drafts, provider = authority
    args = {'title': '另一个有效标题', 'markdown': markdown, 'final_for': '任意交付名称'}
    invocation = {'turn_id': request['turn_id'], 'operation_id': request['operation_id'], 'arguments': args}
    result = provider.invoke(invocation)
    definition = task_draft_definition(CapabilityDefinition('document.draft.propose', 1,
        'write', True, 'receipt_required', 'crp://input', 'crp://output'))
    intent = build_intent(invocation_id='tool-final-draft', turn_id=request['turn_id'],
        step_id='step-draft', operation_id=request['operation_id'],
        tool=definition.tool_definition, arguments=args)
    intent_ref = sessions.put(request['turn_id'], 'tool-invocation-intent', intent_to_payload(intent))
    result_ref = sessions.put(request['turn_id'], 'tool-result', result['result'])
    events = [
        {'type': 'tool.intent.recorded', 'turn_id': request['turn_id'],
         'correlation': {'tool_call_id': intent.invocation_id},
         'data': {'capability_id': 'document.draft.propose', 'payload_ref': intent_ref}},
        {'type': 'tool.completed', 'turn_id': request['turn_id'],
         'correlation': {'tool_call_id': intent.invocation_id},
         'data': {'capability_id': 'document.draft.propose', 'payload_ref': result_ref,
                  'receipt_ref': result['receipt_ref'], 'evidence_refs': result['evidence_refs']}},
    ]
    binding = frozen_division_binding(composition, request, sessions)
    return invocation, result, events, binding


def decide(authority, events, binding):
    _, sessions, request, drafts, _ = authority
    return final_draft_decision(request, events, sessions, binding=binding, drafts=drafts)


def test_final_proof_replays_without_creating_another_draft_and_caps_summary(authority):
    _, _, _, drafts, provider = authority
    invocation, result, events, binding = completed_draft(authority, markdown='一' * 3000)
    assert provider.invoke(invocation) == result
    assert len(drafts.documents.list()) == 1
    decision = decide(authority, events, binding)
    assert decision['type'] == 'complete'
    assert decision['summary'].startswith('另一个有效标题\n')
    assert len(decision['summary']) == 2000


@pytest.mark.parametrize('field', ['turn_id', 'payload_ref', 'receipt_ref', 'tool_call_id', 'document_id', 'title', 'binding'])
def test_forged_or_cross_turn_result_cannot_terminate(authority, field):
    composition, sessions, request, _, _ = authority
    _, result, events, binding = completed_draft(authority)
    events = deepcopy(events)
    if field == 'turn_id':
        events[-1]['turn_id'] = 'turn-other'
    elif field == 'payload_ref':
        parent = composition.store.get_run(binding['parent_run_id'])
        events[-1]['data']['payload_ref'] = sessions.put(parent.turn_id, 'tool-result', result['result'])
    elif field == 'receipt_ref':
        events[-1]['data']['receipt_ref'] = 'crp://elsewhere/operation'
    elif field == 'tool_call_id':
        events[-1]['correlation']['tool_call_id'] = 'tool-other'
    else:
        payload = deepcopy(result['result'])
        if field == 'document_id':
            payload['document_id'] = 'document-other'
        elif field == 'title':
            payload['title'] = '伪造标题'
        else:
            payload['division_binding']['assignment_id'] = 'assignment-other'
        events[-1]['data']['payload_ref'] = sessions.put(request['turn_id'], 'tool-result', payload)
    assert decide(authority, events, binding) is None


def test_terminate_marker_requires_a_successful_operation(authority):
    _, _, request, drafts, _ = authority
    _, _, events, binding = completed_draft(authority)
    with drafts.records.begin() as tx:
        row = tx.read(COLLECTION, request['operation_id'])
        tx.delete(COLLECTION, request['operation_id'], expected_revision=row.revision)
        tx.commit()
    assert decide(authority, events, binding) is None


def test_consumed_permit_binding_is_rechecked_before_termination(authority):
    composition, sessions, request, _, _ = authority
    _, _, events, binding = completed_draft(authority)
    # Corrupt only the temporary fixture's durable join, with no mock authority.
    connection = composition.dispatch_store._connect()
    try:
        connection.execute('DELETE FROM ai_agent_dispatch_permit_bindings WHERE permit_id=?', (binding['permit_id'],))
        connection.commit()
    finally:
        connection.close()
    assert frozen_division_binding(composition, request, sessions) is None
    assert decide(authority, events, frozen_division_binding(composition, request, sessions)) is None


def test_frozen_task_text_cannot_be_replaced_to_claim_another_deliverable(authority):
    composition, sessions, request, _, _ = authority
    changed = deepcopy(request)
    changed['input']['text'] = '伪造另一个任务'
    assert frozen_division_binding(composition, changed, sessions) is None


@pytest.mark.parametrize('value', [None, True, 1, '', ' ', 'x' * 2001],
                         ids=['none', 'bool', 'int', 'empty', 'blank', 'overlong'])
def test_invalid_final_for_is_rejected_before_creating_a_draft(authority, value):
    _, _, request, drafts, provider = authority
    with pytest.raises(ValueError, match='final_for'):
        provider.invoke({'turn_id': request['turn_id'], 'operation_id': request['operation_id'],
            'arguments': {'title': '有效标题', 'markdown': '正文', 'final_for': value}})
    assert not drafts.documents.list()


def test_later_tool_intent_or_failure_does_not_reuse_an_old_final_marker(authority):
    _, _, request, _, _ = authority
    _, _, events, binding = completed_draft(authority)
    for kind in ('tool.intent.recorded', 'tool.failed', 'hook.continuation'):
        assert decide(authority, events + [{'type': kind, 'turn_id': request['turn_id']}], binding) is None


@pytest.mark.parametrize('changed', ['privacy', 'profile', 'cancel', 'deadline'])
def test_planner_checks_current_authority_and_execution_control_before_completion(authority, changed):
    from backend.memory_app.kernel.task_planner import ProductTaskPlanner
    from backend.memory_app.v2.privacy import set_private_project
    from backend.memory_app.v2.profile import freeze_task_profile, frozen_task_profile
    from backend.memory_app.v2.turn_requests import validate_frozen_inputs
    from backend.recognition import RecognitionConflict
    from core.ai_kernel.runtime import _PlannerExecutionContext, _PlannerCancelled, _PlannerDeadlineExceeded

    composition, sessions, request, drafts, _ = authority
    _, _, events, _ = completed_draft(authority)
    models = Models()  # No transport: a terminal decision must never call a gateway.
    planner = ProductTaskPlanner(models=models, store=sessions, composition=composition,
        guard=lambda value: validate_frozen_inputs(drafts.records, models, value),
        profile_reader=lambda value: frozen_task_profile(drafts.records, None, value),
        builder=None, fallback=None, drafts=drafts)
    planner.acquire(request=request, project_id='project-alpha', project_profile_id='profile-task',
        project_profile_revision=1, boundary_profile_id='boundary-task',
        boundary_profile_revision=1, capability_ids=['document.draft.propose'])
    control = _PlannerExecutionContext(request['turn_id'], 'step-final', 'model-final', 10000)
    assert planner.plan(request, events, [], sessions, execution_control=control)['type'] == 'complete'
    expected = RecognitionConflict
    if changed == 'privacy':
        set_private_project(drafts.records, 'project-alpha', True, 0)
    elif changed == 'profile':
        parent = planner.profile_request(request)
        freeze_task_profile(drafts.records, parent, {})
        with drafts.records.begin() as tx:
            row = tx.read('v2_task_profiles', parent['turn_id'])
            tx.put('v2_task_profiles', parent['turn_id'], {**row.payload, 'project_id': 'another-project'},
                   expected_revision=row.revision)
            tx.commit()
    elif changed == 'cancel':
        control.request_cancel()
        expected = _PlannerCancelled
    else:
        control = _PlannerExecutionContext(request['turn_id'], 'step-final', 'model-final', 0)
        expected = _PlannerDeadlineExceeded
    with pytest.raises(expected):
        planner.plan(request, events, [], sessions, execution_control=control)
