from __future__ import annotations

from dataclasses import replace
import pytest

from core.ai_kernel.agent_contracts import AgentBudget
from core.long_horizon_runtime import (
    NodeBudget, PendingExecution, TaskBudget, TrajectoryContractError, TrajectorySegment,
    TrustCheckpoint, project_checkpoint_recovery, validate_segment_transition,
)


def _segment(*, revision: int = 1, previous: str | None = None, caps=("read", "write"), allocated=(6, 4), remaining=(6, 4), pending=(), authorization_ref=None) -> TrajectorySegment:
    return TrajectorySegment(
        project_id="project-alpha",
        graph_id="graph-alpha",
        segment_id=f"segment-{revision}",
        revision=revision,
        previous_segment_id=previous,
        basis_world_sequence=revision,
        basis_graph_sequence=revision,
        provenance_sequence=revision,
        route_revision=revision,
        route_ref=f"crp://routes/project-alpha/route-{revision}",
        profile_id="main.orchestrator",
        profile_revision=revision,
        model_tier="standard",
        capability_ids=tuple(caps),
        node_budgets=tuple(
            NodeBudget(
                f"node-{index}",
                TaskBudget(total),
                TaskBudget(left),
            )
            for index, (total, left) in enumerate(
                zip(allocated, remaining), start=1,
            )
        ),
        verified_artifact_refs=("crp://artifacts/project-alpha/a",),
        pending_executions=tuple(pending),
        reason="verified trajectory",
        evidence_ref="crp://receipts/project-alpha/evidence",
        authorization_ref=authorization_ref,
    )


def _checkpoint(segment: TrajectorySegment) -> TrustCheckpoint:
    return TrustCheckpoint(
        checkpoint_id="checkpoint-alpha",
        project_id="project-alpha",
        graph_id="graph-alpha",
        checkpoint_revision=1,
        segment=segment,
        dag_cursor=segment.basis_graph_sequence,
        world_cursor=segment.basis_world_sequence,
        provenance_cursor=segment.provenance_sequence,
        verified_node_ids=("node-1",),
        verified_artifact_refs=("crp://artifacts/project-alpha/a",),
        frozen_capability_ids=segment.capability_ids,
        frozen_remaining_budget=TaskBudget(segment.remaining_budget),
        pending_executions=segment.pending_executions,
        workload_snapshot_ref="crp://dispatch/workload/workload-alpha",
        workload_snapshot_revision=1,
        capacity_snapshot_ref="crp://dispatch/capacity/capacity-alpha",
        capacity_snapshot_revision=1,
        main_run_id="main-alpha",
        main_cancel_epoch=0,
        frozen_agent_budget=AgentBudget(2, 4, 1000, 500, 60_000),
        frozen_workload_queued=0,
        frozen_workload_active=0,
        frozen_workload_reserved_budget=AgentBudget(0, 0, 0, 0, 0),
        frozen_capacity_available=2,
        frozen_capacity_maximum=2,
    )


def test_transition_narrows_capabilities_and_conserves_reallocated_budget() -> None:
    first = _segment()
    second = _segment(revision=2, previous="segment-1", caps=("read",), allocated=(5, 5), remaining=(5, 5))
    assert validate_segment_transition(first, second) == second
    expanded = _segment(revision=2, previous="segment-1", caps=("read", "write", "admin"))
    with pytest.raises(TrajectoryContractError, match="expansion"):
        validate_segment_transition(first, expanded)
    authorized = _segment(revision=2, previous="segment-1", caps=("read", "write", "admin"), authorization_ref="crp://authorizations/project-alpha/grant")
    assert validate_segment_transition(first, authorized) == authorized
    consumed = _segment(revision=2, previous="segment-1", caps=("read",), allocated=(5, 5), remaining=(4, 5))
    assert validate_segment_transition(first, consumed) == consumed
    later_first = _segment(revision=2)
    later_second = _segment(revision=3, previous="segment-2")
    with pytest.raises(TrajectoryContractError, match="cursor"):
        validate_segment_transition(later_first, replace(later_second, basis_world_sequence=1))


def test_checkpoint_round_trip_and_recovery_never_redispatches_verified_or_pending() -> None:
    checkpoint = _checkpoint(_segment())
    assert TrustCheckpoint.from_payload(checkpoint.to_payload()) == checkpoint
    recovery = project_checkpoint_recovery(checkpoint, world_cursor=1, dag_cursor=1, provenance_cursor=1, node_validity={"node-1": "verified", "node-2": "pending"})
    assert recovery.skip_verified == ("node-1",)
    assert recovery.resume_pending == ()
    assert recovery.redispatch_ready == ("node-2",)
    with pytest.raises(TrajectoryContractError, match="cursor"):
        project_checkpoint_recovery(checkpoint, world_cursor=2, dag_cursor=1, provenance_cursor=1, node_validity={})
    with pytest.raises(TrajectoryContractError, match="stale"):
        project_checkpoint_recovery(checkpoint, world_cursor=1, dag_cursor=1, provenance_cursor=1, node_validity={"node-2": "stale"})


def test_pending_execution_is_node_bound_and_not_redispatched() -> None:
    pending = PendingExecution("node-2", "job", "crp://jobs/project-alpha/job-2")
    checkpoint = _checkpoint(_segment(pending=(pending,)))
    recovery = project_checkpoint_recovery(checkpoint, world_cursor=1, dag_cursor=1, provenance_cursor=1, node_validity={"node-1": "verified", "node-2": "pending"})
    assert recovery.resume_pending == ("node-2",)
    assert recovery.redispatch_ready == ()
    payload = _segment().to_payload()
    payload["evidence_ref"] = "crp://receipts/other/contains/project-alpha"
    with pytest.raises(TrajectoryContractError, match="scope"):
        TrajectorySegment.from_payload(payload)
