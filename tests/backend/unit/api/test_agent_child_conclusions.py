from types import SimpleNamespace

import pytest

from backend.api import agent_coordinator as module
from backend.api.agent_message_authority import AgentMessagePayloadAuthority
from backend.api.agent_organization_planner import MainCoordinationPlanner
from backend.api.world_supervision_agent_observer import WorldSupervisionAgentObserver
from tests.backend.unit.api.test_agent_coordinator import _durable_coordinator, _request, _converge_child_for_fan_in
from core.ai_kernel import AgentBudget, AgentTerminalChildSummary
from core.ai_kernel.model_planner import _materialize_event


def test_conclusion_sanitizes_fragments_and_preserves_paragraphs():
    clean = getattr(module, '_child_conclusion', lambda _: None)
    assert clean('  已验证 docs/a.md C:\\x sk-example123456 crp://session/x/y\n\n\n  可继续  推进  ') == '已验证 [路径] [路径] [已移除] [路径]\n\n可继续 推进'
    assert clean('Bearer abc123 Authorization: hidden') == '[已移除] [已移除]'
    assert clean('Authorization: Bearer hidden 仍可推进') == '[已移除] 仍可推进'
    assert clean('访问 \\\\server\\share 后继续') == '访问 [路径] 后继续'
    assert clean(' \n ') is None
    assert clean('字' * 2001) == '字' * 1999 + '…'


@pytest.mark.parametrize('profile,text', [
    ('subagent.explorer', '专家已验证关键证据'),
    ('subagent.reviewer', 'VERDICT=supported;DISPOSITION=continue;FINDING=证据完整'),
])
def test_real_authority_delivers_parent_owned_conclusion(tmp_path, profile, text):
    turns, store, _, _, profiles, coordinator = _durable_coordinator(tmp_path)
    coordinator._message_payload_authority = AgentMessagePayloadAuthority(payload_reader=turns, immutable_payloads=turns)
    request = _request()
    request['capability_policy']['allowed'].extend(['agent.spawn', 'agent.fan_in', 'agent.list'])
    parent = coordinator.accept_main_and_submit(request)
    spawned = coordinator.spawn(parent_turn_id=parent.turn_id, project_id=parent.project_id, scope=request['scope'], privacy=request['privacy'], operation_id='op-conclusion-spawn-001', tool_call_id='tool-conclusion-spawn-001', arguments={'profile_id': profile, 'task': '核验事实'})
    child_id = spawned['run']['run_id']
    coordinator.fan_in(parent_turn_id=parent.turn_id, project_id=parent.project_id, scope=request['scope'], privacy=request['privacy'], operation_id='op-conclusion-join-001', tool_call_id='tool-conclusion-join-001', arguments={'child_run_ids': [child_id], 'policy': 'all'})
    _converge_child_for_fan_in(store, parent.project_id, child_id, 'completed')
    coordinator._events_loader = lambda _: ({'type': 'turn.completed', 'data': {'summary': text}},)
    coordinator._reconcile_parent_fan_ins(parent)
    listing = coordinator.list(parent_turn_id=parent.turn_id, project_id=parent.project_id, scope=request['scope'], privacy=request['privacy'])
    summary = listing['fan_ins'][0]['result']['children'][0]
    assert summary['conclusion'] == text
    assert listing['children'][0]['organization_role'] == profiles.get(profile).organization_role
    assert summary['summary_ref'].startswith(f'crp://session/{parent.turn_id}/')
    assert 'usage' not in turns.get(summary['summary_ref'])
    assert turns.get(listing['fan_ins'][0]['result']['result_ref'])['children'][0]['conclusion'] == text
    list_ref = turns.get_or_create_immutable_payload(parent.turn_id, 'agent-list-test', listing)
    visible_event = _materialize_event({'type': 'tool.completed', 'data': {'capability_id': 'agent.list', 'payload_ref': list_ref}}, turns)
    assert visible_event['resolved_payload']['fan_ins'][0]['result']['children'][0]['conclusion'] == text
    child = store.get_run_with_revision(child_id, project_id=parent.project_id)[0]
    child_ref = coordinator._write_immutable(child.turn_id, f'agent-child-terminal-summary-v1/{child_id}', {
        **turns.get(summary['summary_ref']), 'usage': summary['usage'],
    })
    assert 'usage' in turns.get(child_ref)
    if profile == 'subagent.reviewer':
        observer = WorldSupervisionAgentObserver(supervision=None, run_store=store, request_loader=turns.get_request, payload_loader=turns.get, now=lambda: '2026-10-01T00:00:00Z')
        assert observer._summary(SimpleNamespace(summary_ref=summary['summary_ref']), child) == ('supported', 'continue', '证据完整')


def _cluster():
    return {'children': [{'run_id': 'steward', 'profile_id': 'steward.scheduler', 'status': 'completed'}, {'run_id': 'expert', 'profile_id': 'subagent.worker', 'organization_role': '执行专家', 'status': 'completed'}], 'plans': [{'mode': 'cluster', 'status': 'dispatched', 'steward_run_id': 'steward'}], 'fan_ins': [{'status': 'completed', 'child_run_ids': ['expert'], 'result': {'status': 'completed', 'children': [{'child_run_id': 'expert', 'conclusion': '方案可行'}]}}]}


@pytest.mark.parametrize('remote_allowed,remote_result', [(False, {}), (True, None), (True, RuntimeError('offline'))])
def test_cluster_deterministic_summary_on_local_or_remote_failure(remote_allowed, remote_result):
    calls = []
    def remote(*args):
        calls.append(args)
        if isinstance(remote_result, Exception):
            raise remote_result
        return remote_result
    planner = MainCoordinationPlanner(remote=SimpleNamespace(plan=remote))
    ref = 'crp://session/main/list'
    event = {'type': 'tool.completed', 'data': {'capability_id': 'agent.list', 'payload_ref': ref}}
    result = planner.plan({'turn_id': 'main', 'privacy': {'allow_remote': remote_allowed}}, [event], [], SimpleNamespace(get=lambda _: _cluster()))
    assert result['summary'] == '专家结论：\n\n【执行专家】方案可行'
    assert len(calls) == int(remote_allowed)


def test_remote_synthesis_keeps_only_final_completed_topology():
    calls = []
    planner = MainCoordinationPlanner(remote=SimpleNamespace(plan=lambda *args: calls.append(args) or {'type': 'complete', 'summary': '综合结论'}))
    ref = 'crp://session/main/list'
    final = {'type': 'tool.completed', 'data': {'capability_id': 'agent.list', 'payload_ref': ref}}
    other = {'type': 'tool.completed', 'data': {'capability_id': 'memory.recall'}}
    events = [final, {'type': 'tool.started', 'data': {'capability_id': 'agent.wait'}}, {'type': 'tool.failed', 'data': {'capability_id': 'agent.list'}}, other, final]
    planner.plan({'turn_id': 'main', 'privacy': {'allow_remote': True}}, events, [], SimpleNamespace(get=lambda _: _cluster()))
    assert list(calls[0][1]) == [other, final]


@pytest.mark.parametrize('ref,payload', [
    ('crp://session/foreign/summary', {'kind': 'agent.child-terminal-summary.v1', 'child_run_id': 'child', 'status': 'completed', 'final_summary': '外部结论'}),
    ('crp://session/main/summary', {'kind': 'agent.child-status.v1', 'final_summary': '错误类型'}),
    ('crp://session/main/summary', {'kind': 'agent.child-terminal-summary.v1', 'child_run_id': 'other', 'status': 'completed', 'final_summary': '另一专家'}),
    ('crp://session/main/summary', None),
])
def test_conclusion_requires_parent_scope_and_matching_terminal_payload(ref, payload):
    summary = AgentTerminalChildSummary('child', 'project', 'completed', 'crp://receipt/child', ref, (), AgentBudget(0, 0, 0, 0, 0))
    assert module._safe_terminal_summary(summary, lambda _: payload, 'main')['conclusion'] is None


def test_missing_conclusions_and_long_cluster_summary_are_explicit_and_bounded():
    topology = _cluster()
    topology['fan_ins'][0]['result']['children'][0]['conclusion'] = None
    planner = MainCoordinationPlanner(remote=None)
    result = planner._synthesize({}, [], [], None, None, topology['children'][1:], topology)
    assert result['summary'] == '专家结论：\n\n【执行专家】未返回结论（completed）'
    experts = [{'run_id': str(index), 'organization_role': '专家', 'status': 'completed'} for index in range(4)]
    topology['fan_ins'][0]['result']['children'] = [{'child_run_id': str(index), 'conclusion': '字' * 2000} for index in range(4)]
    assert len(planner._synthesize({}, [], [], None, None, experts, topology)['summary']) == 6000


@pytest.mark.parametrize('plan_status', ['ready', 'dispatching'])
def test_main_waits_for_persisted_expert_while_cluster_dispatch_is_in_progress(plan_status):
    topology = _cluster()
    topology['plans'][0]['status'] = plan_status
    topology['children'][1]['status'] = 'queued'
    topology['fan_ins'] = []
    event = {'type': 'tool.completed', 'data': {'capability_id': 'agent.list', 'payload_ref': 'crp://session/main/list'}}
    result = MainCoordinationPlanner(remote=None).plan({'turn_id': 'main'}, [event], [], SimpleNamespace(get=lambda _: topology))
    assert result['capability_id'] == 'agent.wait'
    assert result['arguments']['child_run_ids'] == ['expert']


@pytest.mark.parametrize('with_expert', [False, True])
def test_incomplete_dispatch_never_synthesizes_partial_terminal_experts(with_expert):
    topology = _cluster()
    topology['plans'][0]['status'] = 'dispatching' if with_expert else 'ready'
    if not with_expert:
        topology['children'] = topology['children'][:1]
    event = {'type': 'tool.completed', 'data': {'capability_id': 'agent.list', 'payload_ref': 'crp://session/main/list'}}
    result = MainCoordinationPlanner(remote=None).plan({'turn_id': 'main'}, [event], [], SimpleNamespace(get=lambda _: topology))
    assert result['capability_id'] == 'agent.list'
