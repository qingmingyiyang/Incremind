from __future__ import annotations

import pytest

from core.ai_kernel.agent_contracts import AgentBudget
from core.ai_kernel.agent_dispatch_contracts import (
    AgentDispatchContractError, AgentDispatchPlan, CapacitySnapshot,
    DispatchPermit, ExpertAssignment, ExpertCluster, IntakeRoutingReceipt,
    WorkloadSnapshot, agent_dispatch_plan_from_payload,
    agent_dispatch_plan_to_payload, canonical_dispatch_ref,
    expert_assignment_from_payload, expert_assignment_to_payload,
)


def _ref(name: str) -> str: return f"crp://dispatch/{name}/snapshot"
def _budget() -> AgentBudget: return AgentBudget(2, 3, 40, 20, 500)


def _plan(*, effect_state: str = "none") -> AgentDispatchPlan:
    return AgentDispatchPlan("plan-a", "project-a", "main-run", "steward-run", 1, "draft", "cluster", canonical_dispatch_ref("intake", "intake-a"), 1, canonical_dispatch_ref("workload", "workload-a"), 1, canonical_dispatch_ref("capacity", "capacity-a"), 1, canonical_dispatch_ref("cluster", "cluster-a"), 1, ("assignment-a",), _budget(), 1, effect_state)


def test_contracts_are_immutable_provider_free_and_round_trip() -> None:
    receipt = IntakeRoutingReceipt("intake-a", "project-a", "turn-a", 1, "steward_required", _ref("input"), _ref("routing"), _ref("context-policy"), 1)
    workload = WorkloadSnapshot("workload-a", "project-a", 1, 1, 0, _budget())
    capacity = CapacitySnapshot("capacity-a", "project-a", 1, 1, 2, _budget())
    cluster = ExpertCluster("cluster-a", "project-a", 1, (_ref("expert"),), (_ref("skill"),))
    assignment = ExpertAssignment("assignment-a", "project-a", "cluster-a", 1, _ref("profile"), 1, _ref("task"), 1, ("memory.recall",), "expert-a", ("skill-a",), _ref("expert"), 1, _ref("skill"), 1, _ref("context-policy"), 1, _budget())
    assert receipt.project_id == workload.project_id == capacity.project_id == cluster.project_id == assignment.project_id
    assert agent_dispatch_plan_from_payload(agent_dispatch_plan_to_payload(_plan())) == _plan()
    assert expert_assignment_from_payload(expert_assignment_to_payload(assignment)) == assignment
    with pytest.raises(AttributeError): _plan().status = "ready"  # type: ignore[misc]
    payload = agent_dispatch_plan_to_payload(_plan()); payload["provider_id"] = "forbidden"
    with pytest.raises(AgentDispatchContractError): agent_dispatch_plan_from_payload(payload)


def test_budget_concurrency_and_unknown_effect_reschedule_boundaries() -> None:
    with pytest.raises(AgentDispatchContractError): CapacitySnapshot("capacity-a", "project-a", 1, 3, 2, _budget())
    with pytest.raises(AgentDispatchContractError): AgentDispatchPlan("plan-a", "project-a", "main-run", "steward-run", 1, "draft", "cluster", canonical_dispatch_ref("intake", "intake-a"), 1, canonical_dispatch_ref("workload", "workload-a"), 1, canonical_dispatch_ref("capacity", "capacity-a"), 1, canonical_dispatch_ref("cluster", "cluster-a"), 1, ("assignment-a",), _budget(), 2)
    assert _plan().may_reschedule
    assert not _plan(effect_state="unknown").may_reschedule
    assert DispatchPermit("permit-a", "project-a", "plan-a", 1, "assignment-a", "permit-op").status == "issued"


def test_main_only_and_optional_expert_skill_are_explicit() -> None:
    main_only = AgentDispatchPlan("plan-main", "project-a", "main-run", "steward-run", 1, "draft", "main_only", canonical_dispatch_ref("intake", "intake-a"), 1, canonical_dispatch_ref("workload", "workload-a"), 1, canonical_dispatch_ref("capacity", "capacity-a"), 1, None, None, (), _budget(), 0)
    assert main_only.mode == "main_only"
    assignment = ExpertAssignment("assignment-none", "project-a", "cluster-a", 1, _ref("profile"), 1, _ref("task"), 1, ("memory.recall",), None, (), None, None, None, None, _ref("context-policy"), 1, _budget())
    assert assignment.expert_snapshot_ref is None and assignment.skill_snapshot_ref is None
    with pytest.raises(AgentDispatchContractError): ExpertAssignment("bad", "project-a", "cluster-a", 1, _ref("profile"), 1, _ref("task"), 1, ("memory.recall",), "expert-a", (), _ref("expert"), None, None, None, _ref("context-policy"), 1, _budget())
    with pytest.raises(AgentDispatchContractError): IntakeRoutingReceipt("intake-b", "project-a", "turn-a", 1, "untrusted", _ref("input"), _ref("routing"), _ref("context-policy"), 1)
