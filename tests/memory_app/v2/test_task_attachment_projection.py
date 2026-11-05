from copy import deepcopy
import json

import pytest

from backend.recognition import RecognitionConflict
from tests.memory_app.v2.test_task_attachment_input import originals, native_request, Decision
from backend.memory_app.v2.task_attachment_input import (
    fit_task_attachment_messages, input_tokens, project_attachment_messages,
)


def organization(root, records, context):
    from backend.api.capability_admission import ReviewedCoreCapabilityRegistry, RuntimeCapabilityAdmission
    from backend.memory_app.kernel.agent_runtime_composition import build_agent_runtime_composition
    from backend.memory_app.kernel.agent_organization_runtime import _steward_turn_request
    from core.ai_kernel import SQLiteAITurnStore, ScopedCapabilityRegistry, SynchronousAIRuntime

    turns = SQLiteAITurnStore(root / '.rebuild-data' / 'ai-turns.sqlite3')
    registry = ScopedCapabilityRegistry()
    composition = build_agent_runtime_composition(runtime_root=root, session_store=turns,
        registry=ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(registry)))

    class LocalCompletion:
        def plan(self, request, events, capabilities, payloads, execution_control=None):
            return {'type': 'complete', 'summary': '本机领域检查完成'}

    runtime = SynchronousAIRuntime(planner=LocalCompletion(), registry=registry,
        events=turns, payloads=turns, state=turns)
    composition.bind_runtime(runtime)
    request = native_request(records, context)
    request['capability_policy']['allowed'] = ['agent.spawn', 'agent.plan', 'agent.list']
    main = composition.coordinator.accept_and_register_main(request)
    children = []
    for profile in ('steward.scheduler', 'subagent.explorer'):
        children.append(composition.coordinator.prepare_child(parent_turn_id=request['turn_id'],
            project_id='alpha', scope=request['scope'], privacy=request['privacy'],
            operation_id='prepare-' + profile, tool_call_id='call-' + profile,
            arguments={'profile_id': profile, 'task': '分别核对预算', 'capability_ids': [],
                'budget': {'model_calls': 1, 'tool_calls': 1, 'input_tokens': 2000,
                           'output_tokens': 500, 'wall_time_ms': 5000}},
            child_request_factory=_steward_turn_request if profile == 'steward.scheduler' else None))
    return turns, composition, main, children


@pytest.mark.parametrize('role', ['main', 'expert'])
def test_real_model_planner_inputs_are_projected_before_any_gateway_result(originals, tmp_path, role):
    from core.ai_kernel.model_planner import ModelGatewayAgentPlanner
    records, context = originals
    turns, _, main, children = organization(tmp_path, records, context)
    request = main.request if role == 'main' else children[1].request
    prepared = []

    class ProjectionBoundary:
        def invoke(self, model_request):
            messages = [{'role': 'user', 'content': model_request.input}]
            if role == 'expert':
                messages.insert(0, {'role': 'user', 'content': context['text']})
            selected, projected = fit_task_attachment_messages(records, None, context, '预算分别多少',
                messages, {'window': 3000, 'reserve': 200}, response_model=Decision,
                root_input=main.request['input']['text'])
            prepared.append((selected, projected))
            raise RecognitionConflict('local projection boundary has no model invocation')

    with pytest.raises(RecognitionConflict, match='local projection boundary'):
        ModelGatewayAgentPlanner(ProjectionBoundary()).plan(request, turns.events_after(request['turn_id']), [], turns)
    selected, projected = prepared[0]
    assert input_tokens(projected, Decision) <= 2800
    assert all(row['content']['windows'] for row in selected['originals'])
    assert context['text'] not in str(projected)


def test_real_steward_brief_keeps_its_observed_limit_and_projects_clipped_root(originals, tmp_path):
    from backend.api.agent_steward_proposal import AgentStewardProposalBuilder
    from backend.memory_app.v2.policies import get
    records, context = originals
    turns, composition, main, children = organization(tmp_path, records, context)
    builder = AgentStewardProposalBuilder(profiles=composition.profiles, agent_store=composition.store,
        request_loader=composition.request_loader, capability_profiles=None)
    brief = builder.brief(children[0].request)
    assert brief['task'].endswith('（已截断）')
    before = deepcopy(brief)
    messages = [{'role': 'user', 'content': json.dumps(brief, ensure_ascii=False)},
                {'role': 'user', 'content': context['text']}]
    selected, projected = fit_task_attachment_messages(records, None, context, '预算分别多少', messages,
        {'window': 3000, 'reserve': 200}, response_model=Decision, root_input=main.request['input']['text'])
    current = json.loads(projected[0]['content'])
    assert len(current['task']) <= len(brief['task'])
    assert brief == before
    assert current['task'] != brief['task']
    continuation = get('retry')({'kind': 'continue_task_messages', 'messages': projected, 'partial': '已完成的预算分析。'})
    revised = project_attachment_messages(continuation, [context['text'], selected['text']], selected['text'],
        root_input=main.request['input']['text'])
    assert revised[-2:] == continuation[-2:]
    changed = deepcopy(messages)
    bad = deepcopy(brief)
    bad['task'] = brief['task'].replace('预算分别多少', '其他用户问题')
    changed[0]['content'] = json.dumps(bad, ensure_ascii=False)
    with pytest.raises(RecognitionConflict, match='root binding'):
        project_attachment_messages(changed, [context['text']], selected['text'], root_input=main.request['input']['text'])


def test_an_extra_prefixed_full_body_is_rejected_even_when_another_copy_matches(originals):
    _, context = originals
    messages = [{'role': 'user', 'content': context['text']},
                {'role': 'system', 'content': '其他前缀\n' + context['text']}]
    with pytest.raises(RecognitionConflict, match='unprojected binding'):
        project_attachment_messages(messages, [context['text']], '选窗正文')
