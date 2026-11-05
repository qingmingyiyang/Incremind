"""原文初检候选显式启用，旧策略和冻结调查合同保持。"""
from copy import deepcopy

import pytest

from backend.memory_app.v2.agentic_rag import _policy
from backend.memory_app.v2.policies import ACTIVE, get, override, version
from backend.memory_app.v2.policies.pipelines import versions_for_turn


def test_raw_probe_requires_explicit_candidate_and_keeps_previous_policies():
    assert ACTIVE['retrieve'] == '@9' and version('retrieve') == '@9'
    old, initial = get('retrieve', version='@9'), get('retrieve', version='@10')
    assert not getattr(old, 'initial_memory_probe', False)
    assert not getattr(old, 'raw_evidence_probe', False)
    assert initial.initial_memory_probe is True
    assert not getattr(initial, 'raw_evidence_probe', False)
    assert initial(None, operation='initial_layers') == ('L3', 'L2', 'L1')
    with override(retrieve='@11'):
        policy = get('retrieve')
        assert policy.raw_evidence_probe is True and policy.initial_memory_probe is True
        assert policy(None, operation='initial_layers') == ('L3', 'L2', 'L1', 'L0')
        for capability in ('drilldown', 'demand_based', 'agentic',
                           'empty_layer_drilldown', 'explicit_answer_evidence'):
            assert getattr(policy, capability) == getattr(old, capability)
    assert version('retrieve') == ACTIVE['retrieve'] == '@9'


def test_raw_candidate_reserves_only_one_additional_nested_model_handle():
    previous = get('retrieve', version='@9')(None, operation='agentic_limits')
    unchanged = deepcopy(previous)
    candidate = get('retrieve', version='@11')(None, operation='agentic_limits')
    assert previous['nested_model_handle_budget'] == 9
    assert candidate == {**previous, 'nested_model_handle_budget': 10}
    assert previous == unchanged
    assert get('retrieve', version='@10')(None, operation='agentic_limits') == previous
    assert get('retrieve', version='@9')(None, operation='agentic_limits') == previous


def test_raw_candidate_preserves_explicit_final_evidence_and_decision_contract():
    old, candidate = get('retrieve', version='@9'), get('retrieve', version='@11')
    decision = {'action': 'answer', 'source': 'memory', 'query': '',
                'depth': 'original', 'ids': ['observed-original'], 'questions': []}
    original = deepcopy(decision)
    assert candidate(None, decision, operation='agentic_decision') == old(
        None, decision, operation='agentic_decision') == original
    assert decision == original
    context = {'materials': [], 'final_evidence_ids': ['observed-original']}
    assert candidate(None, context, operation='agentic_messages') == old(
        None, context, operation='agentic_messages')
    with pytest.raises(ValueError, match='invalid_rag_decision'):
        candidate(None, {**decision, 'ids': ['observed-original', 'observed-original']},
                  operation='agentic_decision')


@pytest.mark.parametrize(('requested', 'completed', 'available'), [
    ('original', [], {'L0'}),
    ('original', ['summary'], {'L0'}),
])
def test_raw_candidate_keeps_existing_staged_investigation_depth(requested, completed, available):
    old, candidate = get('retrieve', version='@9'), get('retrieve', version='@11')
    assert candidate(None, requested, completed, operation='agentic_depth') == old(
        None, requested, completed, operation='agentic_depth')
    assert candidate(None, requested, completed, available, operation='agentic_search_depths') == old(
        None, requested, completed, available, operation='agentic_search_depths')


@pytest.mark.parametrize(('frozen', 'ambient', 'raw'), [
    ('@11', '@9', True), ('@9', '@11', False), ('@10', '@11', False),
])
def test_raw_capability_and_budget_follow_frozen_plan_not_ambient_override(frozen, ambient, raw):
    with override(retrieve=frozen):
        plan = {'policy_versions': versions_for_turn('project.answer')}
    original = deepcopy(plan)
    with override(retrieve=ambient):
        assert version('retrieve') == ambient
        policy = _policy(plan)
        assert policy is get('retrieve', version=frozen)
        assert bool(getattr(policy, 'raw_evidence_probe', False)) is raw
        assert policy(None, operation='agentic_limits')['nested_model_handle_budget'] == (10 if raw else 9)
        if raw:
            assert policy(None, operation='initial_layers') == ('L3', 'L2', 'L1', 'L0')
    assert plan == original
