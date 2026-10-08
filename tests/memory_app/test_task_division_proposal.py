import pytest
from tests.backend.unit.api.test_agent_steward_proposal import _builder, _request, _main_request, _main
from core.ai_kernel import CapabilityDefinition
from backend.api.agent_steward_proposal import AgentStewardProposalError
from backend.memory_app.kernel.task_draft_capability import task_draft_definition


def _definitions(capability):
    mode = 'write' if capability == 'document.draft.propose' else 'read'
    return CapabilityDefinition(capability, 1, mode, mode == 'write',
        'receipt_required' if mode == 'write' else 'read_only', 'crp://input', 'crp://output')


def test_task_model_can_propose_three_bounded_dependent_items():
    request = _main_request()
    request['desired_outcome'] = 'project.task'
    builder = _builder(request=request, capability_definition=_definitions)
    proposal = builder.complete(_request(), {'mode':'cluster','assignments':[
        {'profile_id':'subagent.explorer','task':f'完成第{i}部分',
         'goal':f'目标{i}', 'deliverable':f'草稿{i}', 'capabilities':['memory.recall'],
         'depends_on':[1,2] if i == 3 else []} for i in range(1,4)]})
    assert len(proposal['assignments']) == 3
    assert proposal['assignments'][2]['division']['depends_on'] == ['steward-assignment-1','steward-assignment-2']
    assert all(item['capability_ids'] == ['memory.recall'] for item in proposal['assignments'])


def test_task_division_filters_exclusive_write_even_when_frozen_allowed():
    request, main = _main_request(), _main()
    request['desired_outcome'] = 'project.task'
    request['capability_policy']['allowed'].append('document.draft.propose')
    main.capability_ids += ('document.draft.propose',)
    builder = _builder(main=main, request=request, capability_definition=_definitions)
    assert all('document.draft.propose' not in row['capabilities']
               for row in builder.brief(_request())['profiles'])
    selection = {'mode':'cluster','assignments':[{'profile_id':'subagent.worker',
        'task':'写草稿','goal':'草稿','deliverable':'正文',
        'capabilities':['document.draft.propose'],'depends_on':[]}]}
    with pytest.raises(AgentStewardProposalError):
        builder.complete(_request(), selection)
    def safe_definition(capability):
        value = _definitions(capability)
        return task_draft_definition(value) if value.mode == 'write' else value
    builder = _builder(main=main, request=request, capability_definition=safe_definition)
    assert builder.complete(_request(), selection)['assignments'][0]['capability_ids'] == ['document.draft.propose']
