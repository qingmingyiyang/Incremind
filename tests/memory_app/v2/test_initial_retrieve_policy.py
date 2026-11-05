"""初始记忆探查只由显式候选及冻结策略启用；不调用模型。"""
from copy import deepcopy

import pytest

from backend.memory_app.v2.agentic_rag import _policy
from backend.memory_app.v2.policies import ACTIVE, get, override, version
from backend.memory_app.v2.policies.pipelines import versions_for_turn


def test_initial_probe_requires_explicit_candidate_and_leaves_default_unchanged():
    assert ACTIVE['retrieve'] == '@9'
    assert not getattr(get('retrieve', version='@9'), 'initial_memory_probe', False)
    with override(retrieve='@10'):
        policy = get('retrieve')
        assert version('retrieve') == '@10'
        assert policy.initial_memory_probe is True
        assert policy(None, operation='initial_layers') == ('L3', 'L2', 'L1')
        for capability in ('drilldown', 'demand_based', 'agentic',
                           'empty_layer_drilldown', 'explicit_answer_evidence'):
            assert getattr(policy, capability) == getattr(get('retrieve', version='@9'), capability)
    assert ACTIVE['retrieve'] == '@9'
    assert version('retrieve') == '@9'


def test_candidate_preserves_explicit_answer_decision_and_final_evidence_instructions():
    old, candidate = get('retrieve', version='@9'), get('retrieve', version='@10')
    decision = {'action': 'answer', 'source': 'memory', 'query': '',
                'depth': 'summary', 'ids': ['observed-one', 'observed-two'], 'questions': []}
    original = deepcopy(decision)
    assert candidate(None, decision, operation='agentic_decision') == old(
        None, decision, operation='agentic_decision') == original
    assert decision == original
    context = {'materials': [], 'final_evidence_ids': ['observed-one']}
    assert candidate(None, context, operation='agentic_messages') == old(
        None, context, operation='agentic_messages')
    assert candidate(None, operation='agentic_limits') == old(None, operation='agentic_limits')
    with pytest.raises(ValueError, match='invalid_rag_decision'):
        candidate(None, {**decision, 'ids': ['observed-one', 'observed-one']}, operation='agentic_decision')


@pytest.mark.parametrize(('requested', 'completed', 'available'), [
    ('original', [], {'L0'}),
    ('summary', [], {'L1'}),
    ('original', ['summary'], {'L1'}),
    ('original', ['summary', 'body'], {'L0'}),
])
def test_candidate_preserves_staged_depth_and_empty_layer_drilldown(requested, completed, available):
    old, candidate = get('retrieve', version='@9'), get('retrieve', version='@10')
    assert candidate(None, requested, completed, operation='agentic_depth') == old(
        None, requested, completed, operation='agentic_depth')
    assert candidate(None, requested, completed, available, operation='agentic_search_depths') == old(
        None, requested, completed, available, operation='agentic_search_depths')


@pytest.mark.parametrize(('frozen', 'ambient', 'enabled'), [('@10', '@9', True), ('@9', '@10', False)])
def test_initial_probe_reads_frozen_plan_version_under_different_override(frozen, ambient, enabled):
    with override(retrieve=frozen):
        plan = {'policy_versions': versions_for_turn('project.answer')}
    original = deepcopy(plan)
    with override(retrieve=ambient):
        assert version('retrieve') == ambient
        selected = _policy(plan)
        assert selected is get('retrieve', version=frozen)
        assert bool(getattr(selected, 'initial_memory_probe', False)) is enabled
        if enabled:
            assert selected(None, operation='initial_layers') == ('L3', 'L2', 'L1')
    assert plan == original
