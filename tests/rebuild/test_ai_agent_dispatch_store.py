from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from core.ai_kernel.agent_contracts import AgentBudget
from core.ai_kernel.agent_dispatch_contracts import AgentDispatchPlan, CapacitySnapshot, DispatchPermit, ExpertAssignment, ExpertCluster, IntakeRoutingReceipt, WorkloadSnapshot, canonical_dispatch_ref
from core.ai_kernel.agent_dispatch_store import AgentDispatchStoreConflict, AgentDispatchStoreInvalid, AgentDispatchStoreNotFound, SQLiteAgentDispatchStore
from core.ai_kernel.sqlite_store import SQLiteAITurnStore


def _ref(name: str) -> str: return f"crp://dispatch/{name}/snapshot"
def _budget(calls: int = 2) -> AgentBudget: return AgentBudget(calls, calls, calls * 10, calls * 10, calls * 100)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteAgentDispatchStore:
    path = tmp_path / "ai-turns.sqlite"
    SQLiteAITurnStore(path)
    return SQLiteAgentDispatchStore(path)


def _seed(store: SQLiteAgentDispatchStore) -> AgentDispatchPlan:
    store.put_intake_receipt(IntakeRoutingReceipt("intake-a", "project-a", "turn-a", 1, "steward_required", _ref("input"), _ref("route"), _ref("context-policy"), 1), operation_id="intake-op")
    store.put_workload_snapshot(WorkloadSnapshot("workload-a", "project-a", 1, 0, 0, _budget()), operation_id="workload-op")
    store.put_capacity_snapshot(CapacitySnapshot("capacity-a", "project-a", 1, 2, 2, _budget()), operation_id="capacity-op")
    store.put_expert_cluster(ExpertCluster("cluster-a", "project-a", 1, (_ref("expert"),), (_ref("skill"),)), operation_id="cluster-op")
    store.put_assignment(ExpertAssignment("assignment-a", "project-a", "cluster-a", 1, _ref("profile"), 1, _ref("task"), 1, ("memory.recall",), "expert-a", ("skill-a",), _ref("expert"), 1, _ref("skill"), 1, _ref("context-policy"), 1, _budget()), operation_id="assignment-op")
    return AgentDispatchPlan("plan-a", "project-a", "main-run", "steward-run", 1, "draft", "cluster", canonical_dispatch_ref("intake", "intake-a"), 1, canonical_dispatch_ref("workload", "workload-a"), 1, canonical_dispatch_ref("capacity", "capacity-a"), 1, canonical_dispatch_ref("cluster", "cluster-a"), 1, ("assignment-a",), _budget(), 1)


def test_requires_existing_turn_authority(tmp_path: Path) -> None:
    with pytest.raises(AgentDispatchStoreNotFound): SQLiteAgentDispatchStore(tmp_path / "absent.sqlite")


def test_immutable_idempotency_and_cross_project_scope(store: SQLiteAgentDispatchStore) -> None:
    plan = _seed(store)
    assert store.create_plan(plan, operation_id="plan-create") == (plan, True)
    assert store.create_plan(plan, operation_id="plan-create") == (plan, False)
    assert store.get_plan("plan-a", project_id="project-b") is None
    assert store.get_plan("plan-a", project_id="project-a") == plan
    with pytest.raises(AgentDispatchStoreConflict): store.create_plan(replace(plan, max_concurrent_assignments=1, effect_state="known"), operation_id="other-op")


def test_plan_cas_supersede_and_unknown_effect_blocks_reschedule(store: SQLiteAgentDispatchStore) -> None:
    plan = _seed(store); store.create_plan(plan, operation_id="plan-create")
    ready = replace(plan, revision=2, status="ready")
    assert store.transition_plan(ready, expected_revision=1, operation_id="plan-ready") == ready
    with pytest.raises(AgentDispatchStoreConflict): store.transition_plan(replace(ready, revision=2), expected_revision=1, operation_id="stale")
    replacement = replace(plan, plan_id="plan-b")
    prior, next_plan = store.supersede_plan(project_id="project-a", prior_plan_id="plan-a", expected_revision=2, replacement=replacement, operation_id="plan-supersede")
    assert prior.status == "superseded" and next_plan == replacement
    blocked = replace(replacement, plan_id="plan-c", effect_state="unknown")
    store.create_plan(blocked, operation_id="plan-c-create")
    with pytest.raises(AgentDispatchStoreInvalid): store.supersede_plan(project_id="project-a", prior_plan_id="plan-c", expected_revision=1, replacement=replace(plan, plan_id="plan-d"), operation_id="bad-reschedule")


def test_permit_is_idempotent_once_only_and_concurrent_consumption_is_atomic(store: SQLiteAgentDispatchStore) -> None:
    plan = _seed(store); store.create_plan(plan, operation_id="plan-create")
    ready = store.transition_plan(replace(plan, revision=2, status="ready"), expected_revision=1, operation_id="plan-ready")
    permit = DispatchPermit("permit-a", "project-a", ready.plan_id, ready.revision, "assignment-a", "permit-issue")
    assert store.issue_permit(permit) == (permit, True)
    assert store.issue_permit(permit) == (permit, False)
    with pytest.raises(AgentDispatchStoreConflict):
        store.issue_permit(replace(permit, permit_id="permit-copy", operation_id="permit-copy"))
    bound = store.get_assignment_for_permit("permit-a", project_id="project-a")
    assert bound is not None and bound[0] == permit and bound[1].task_payload_ref == _ref("task")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda operation: _claim(store, operation), ("claim-a", "claim-b")))
    assert outcomes.count("claimed") == 1 and outcomes.count("conflict") == 1
    assert store.claim_permit_assignment("permit-a", project_id="project-a", operation_id=("claim-a" if outcomes[0] == "claimed" else "claim-b"))[0].status == "consumed"
    assert store.get_permit("permit-a", project_id="project-a").status == "consumed"  # type: ignore[union-attr]
    assert store.get_assignment("assignment-a", project_id="project-a") == bound[1]


def test_plan_requires_canonical_persisted_scope_revision_and_budget(store: SQLiteAgentDispatchStore) -> None:
    plan = _seed(store)
    with pytest.raises(AgentDispatchStoreInvalid): store.create_plan(replace(plan, intake_receipt_ref=canonical_dispatch_ref("intake", "missing")), operation_id="dangling")
    with pytest.raises(AgentDispatchStoreInvalid): store.create_plan(replace(plan, capacity_revision=2), operation_id="wrong-revision")
    with pytest.raises(AgentDispatchStoreInvalid): store.create_plan(replace(plan, project_id="project-b"), operation_id="cross-project")
    oversized = ExpertAssignment("assignment-big", "project-a", "cluster-a", 1, _ref("profile"), 1, _ref("task-big"), 1, ("memory.recall",), "expert-a", (), _ref("expert"), 1, _ref("skill"), 1, _ref("context-policy"), 1, _budget(3))
    store.put_assignment(oversized, operation_id="assignment-big")
    with pytest.raises(AgentDispatchStoreInvalid): store.create_plan(replace(plan, plan_id="plan-big", assignment_ids=("assignment-big",)), operation_id="budget-over")


def test_main_only_plan_needs_no_cluster_or_assignment(store: SQLiteAgentDispatchStore) -> None:
    plan = _seed(store)
    main_only = AgentDispatchPlan("plan-main", "project-a", "main-run", "steward-run", 1, "draft", "main_only", plan.intake_receipt_ref, plan.intake_revision, plan.workload_snapshot_ref, plan.workload_revision, plan.capacity_snapshot_ref, plan.capacity_revision, None, None, (), plan.budget_limit, 0)
    assert store.create_plan(main_only, operation_id="main-only") == (main_only, True)


def test_publish_ready_plan_is_atomic_and_replays_exact_result(store: SQLiteAgentDispatchStore) -> None:
    seed = _seed(store)
    base = store.get_assignment("assignment-a", project_id="project-a")
    assert base is not None
    cluster = ExpertCluster("cluster-publish", "project-a", 1, (_ref("expert"),), (_ref("skill"),))
    assignment = replace(base, assignment_id="assignment-publish", cluster_id=cluster.cluster_id)
    draft = replace(
        seed, plan_id="plan-publish", expert_cluster_ref=canonical_dispatch_ref("cluster", cluster.cluster_id),
        assignment_ids=(assignment.assignment_id,),
    )
    wrong_permit = DispatchPermit("permit-publish", "project-a", draft.plan_id, 1, assignment.assignment_id, "publish-permit")
    with pytest.raises(AgentDispatchStoreInvalid):
        store.publish_ready_plan(replace(draft, effect_state="unknown"), cluster=cluster, assignments=(assignment,), permits=(wrong_permit,), operation_id="publish-unknown")
    with pytest.raises(AgentDispatchStoreInvalid):
        store.publish_ready_plan(draft, cluster=cluster, assignments=(assignment,), permits=(wrong_permit,), operation_id="publish-rollback")
    assert store.get_plan(draft.plan_id, project_id="project-a") is None
    assert store.get_assignment(assignment.assignment_id, project_id="project-a") is None
    permit = replace(wrong_permit, plan_revision=2)
    ready, permits = store.publish_ready_plan(draft, cluster=cluster, assignments=(assignment,), permits=(permit,), operation_id="publish-ready")
    assert ready.status == "ready" and ready.revision == 2 and permits == (permit,)
    assert store.publish_ready_plan(draft, cluster=cluster, assignments=(assignment,), permits=(permit,), operation_id="publish-ready") == (ready, permits)
    with pytest.raises(AgentDispatchStoreConflict):
        store.publish_ready_plan(draft, cluster=cluster, assignments=(assignment,), permits=(replace(permit, operation_id="different"),), operation_id="publish-ready")


def test_progress_queries_and_child_run_binding_are_recoverable(store: SQLiteAgentDispatchStore) -> None:
    plan = _seed(store)
    store.create_plan(plan, operation_id="plan-create")
    ready = store.transition_plan(replace(plan, revision=2, status="ready"), expected_revision=1, operation_id="plan-ready")
    permit = DispatchPermit("permit-bound", "project-a", ready.plan_id, ready.revision, "assignment-a", "permit-bound-issue")
    store.issue_permit(permit)
    assert store.list_active_plans(project_id="project-a", main_run_id="main-run", steward_run_id="steward-run") == (ready,)
    assert store.list_permits(project_id="project-a", plan_id=ready.plan_id) == (permit,)
    bound = store.bind_permit_to_child_run("permit-bound", project_id="project-a", child_run_id="child-run-a", operation_id="bind-a")
    assert bound[0].status == "consumed" and bound[1] == "child-run-a"
    assert store.get_permit_child_run_binding("permit-bound", project_id="project-a") == "child-run-a"
    assert store.bind_permit_to_child_run("permit-bound", project_id="project-a", child_run_id="child-run-a", operation_id="bind-a") == bound
    assert store.bind_permit_to_child_run("permit-bound", project_id="project-a", child_run_id="child-run-a", operation_id="bind-retry") == bound
    with pytest.raises(AgentDispatchStoreConflict):
        store.bind_permit_to_child_run("permit-bound", project_id="project-a", child_run_id="child-run-b", operation_id="bind-other")
    dispatching = store.transition_plan(replace(ready, revision=3, status="dispatching"), expected_revision=2, operation_id="plan-dispatching")
    assert store.list_active_plans(project_id="project-a", main_run_id="main-run", steward_run_id="steward-run") == (dispatching,)
    dispatched = store.transition_plan(replace(dispatching, revision=4, status="dispatched"), expected_revision=3, operation_id="plan-dispatched")
    completed = store.transition_plan(replace(dispatched, revision=5, status="completed"), expected_revision=4, operation_id="plan-completed")
    assert store.list_active_plans(project_id="project-a", main_run_id="main-run", steward_run_id="steward-run") == ()
    assert store.list_plans_for_main(project_id="project-a", main_run_id="main-run") == (completed,)
    assert store.list_plans_for_main(project_id="project-a", main_run_id="missing-run") == ()


def test_dispatching_plan_retains_its_published_ready_permit_authority(
    store: SQLiteAgentDispatchStore,
) -> None:
    plan = _seed(store)
    store.create_plan(plan, operation_id="fenced-plan-create")
    ready = store.transition_plan(
        replace(plan, revision=2, status="ready"),
        expected_revision=1, operation_id="fenced-plan-ready",
    )
    permit = DispatchPermit(
        "permit-fenced", "project-a", ready.plan_id, ready.revision,
        "assignment-a", "permit-fenced-issue",
    )
    store.issue_permit(permit)
    store.transition_plan(
        replace(ready, revision=3, status="dispatching"),
        expected_revision=2, operation_id="fenced-plan-dispatching",
    )

    bound, child_run_id = store.bind_permit_to_child_run(
        permit.permit_id, project_id="project-a",
        child_run_id="child-run-fenced", operation_id="permit-fenced-bind",
    )

    assert bound.status == "consumed"
    assert child_run_id == "child-run-fenced"


def test_recovery_plan_scan_is_bounded_and_excludes_terminal_or_draft(store: SQLiteAgentDispatchStore) -> None:
    plan = _seed(store)
    store.create_plan(plan, operation_id="plan-create")
    assert store.list_recovery_plans(limit=1) == ()
    ready = store.transition_plan(replace(plan, revision=2, status="ready"), expected_revision=1, operation_id="plan-ready")
    assert store.list_recovery_plans(limit=1) == (ready,)
    dispatching = store.transition_plan(replace(ready, revision=3, status="dispatching"), expected_revision=2, operation_id="plan-dispatching")
    assert store.list_recovery_plans(limit=1) == (dispatching,)
    dispatched = store.transition_plan(replace(dispatching, revision=4, status="dispatched"), expected_revision=3, operation_id="plan-dispatched")
    assert store.list_recovery_plans(limit=1) == (dispatched,)
    completed = store.transition_plan(replace(dispatched, revision=5, status="completed"), expected_revision=4, operation_id="plan-completed")
    assert completed.status == "completed" and store.list_recovery_plans(limit=1) == ()
    with pytest.raises(ValueError):
        store.list_recovery_plans(limit=257)


def _claim(store: SQLiteAgentDispatchStore, operation: str) -> str:
    try:
        permit, assignment = store.claim_permit_assignment("permit-a", project_id="project-a", operation_id=operation)
        assert assignment.assignment_id == permit.assignment_id
        return "claimed"
    except AgentDispatchStoreConflict:
        return "conflict"
