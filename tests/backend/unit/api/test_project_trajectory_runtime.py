from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.project_provenance_runtime import ProjectProvenanceRuntime
from backend.api.project_task_graph_runtime import ProjectTaskGraphRuntime
from backend.api.project_trajectory_runtime import ProjectTrajectoryError, ProjectTrajectoryRuntime
from core.ai_kernel.agent_profiles import AgentProfileRegistry
from core.ai_kernel.agent_contracts import AgentBudget, AgentRun
from core.ai_kernel.agent_dispatch_contracts import CapacitySnapshot, WorkloadSnapshot
from core.long_horizon_runtime import NodeBudget, TaskBudget, TaskGraphNode, TaskGraphSpec, TraceSubject, VersionBinding, trajectory_checkpoint_world_identity
from core.personal_world_model import PersonalWorldModelError, WorldEventDraft, WorldEventKind


PROJECT = "project-trajectory"
NOW = "2026-09-03T10:00:00Z"


def test_checkpoint_binds_current_authorities_and_recovery_never_dispatches(tmp_path: Path) -> None:
    world, graph = _graph(tmp_path)
    trajectory = _trajectory(world)
    first = trajectory.create_checkpoint(
        project_id=PROJECT, graph_id=graph.spec.graph_id, segment_id="segment-one",
        profile_id="main.orchestrator", route_revision=1,
        route_ref=f"crp://routes/{PROJECT}/one", reason="first governed segment",
        evidence_ref=f"crp://receipts/{PROJECT}/one", recorded_at=NOW,
        capability_ids=("agent.list",),
    )
    assert first.world_cursor == len(world.events(PROJECT))
    assert first.verified_node_ids == ()
    assert trajectory.create_checkpoint(
        project_id=PROJECT, graph_id=graph.spec.graph_id, segment_id="segment-one",
        profile_id="main.orchestrator", route_revision=1,
        route_ref=f"crp://routes/{PROJECT}/one", reason="first governed segment",
        evidence_ref=f"crp://receipts/{PROJECT}/one", recorded_at=NOW,
        capability_ids=("agent.list",),
    ) == first
    assert len(world.events(PROJECT)) == first.world_cursor
    unbound_world, _ = _graph(tmp_path / "unbound")
    event_id, source_ref, source_revision = trajectory_checkpoint_world_identity(first)
    with pytest.raises(PersonalWorldModelError, match="authority is unavailable"):
        unbound_world.append_event(WorldEventDraft(
            event_id=event_id, project_id=PROJECT,
            kind=WorldEventKind.TRAJECTORY_CHECKPOINT_RECORDED, actor="system",
            source_ref=source_ref, source_revision=source_revision,
            occurred_at=NOW, recorded_at=NOW, payload=first.to_payload(),
        ))
    with pytest.raises(ProjectTrajectoryError, match="conflicts"):
        trajectory.create_checkpoint(
            project_id=PROJECT, graph_id=graph.spec.graph_id, segment_id="segment-one",
            profile_id="main.orchestrator", route_revision=2,
            route_ref=f"crp://routes/{PROJECT}/changed", reason="first governed segment",
            evidence_ref=f"crp://receipts/{PROJECT}/one", recorded_at=NOW,
            capability_ids=("agent.list",),
        )
    assert trajectory.recover(
        project_id=PROJECT, graph_id=graph.spec.graph_id,
        operation_id="restart-one",
    ).redispatch_ready == ("node-one",)

    second = trajectory.create_checkpoint(
        project_id=PROJECT, graph_id=graph.spec.graph_id, segment_id="segment-two",
        profile_id="subagent.worker", route_revision=2,
        route_ref=f"crp://routes/{PROJECT}/two", reason="new profile governed segment",
        evidence_ref=f"crp://receipts/{PROJECT}/two", recorded_at=NOW,
        capability_ids=("agent.list",),
    )
    assert second.segment.model_tier == "standard"
    assert second.segment.profile_revision == 1
    assert second.segment.node_budgets[0].allocated.units == 2


def test_capability_expansion_requires_authorization_and_budget_snapshot_is_exact(tmp_path: Path) -> None:
    world, graph = _graph(tmp_path)
    gate_state = {"allowed": False}
    runtime = _trajectory(
        world,
        gate=lambda ref, _previous, _current: (
            gate_state["allowed"] and ref.endswith("/expand")
        ),
    )
    runtime.create_checkpoint(
        project_id=PROJECT, graph_id=graph.spec.graph_id, segment_id="narrow",
        profile_id="main.orchestrator", route_revision=1,
        route_ref=f"crp://routes/{PROJECT}/narrow", reason="narrow scope",
        evidence_ref=f"crp://receipts/{PROJECT}/narrow", recorded_at=NOW,
        capability_ids=("agent.list",),
    )
    with pytest.raises(ProjectTrajectoryError, match="authorized"):
        runtime.create_checkpoint(
            project_id=PROJECT, graph_id=graph.spec.graph_id, segment_id="expanded",
            profile_id="main.orchestrator", route_revision=2,
            route_ref=f"crp://routes/{PROJECT}/expanded", reason="expanded scope",
            evidence_ref=f"crp://receipts/{PROJECT}/expanded", recorded_at=NOW,
            capability_ids=("agent.list", "agent.message"),
            authorization_ref=f"crp://authorizations/{PROJECT}/expand",
        )
    gate_state["allowed"] = True
    authorized = runtime.create_checkpoint(
        project_id=PROJECT, graph_id=graph.spec.graph_id, segment_id="expanded",
        profile_id="main.orchestrator", route_revision=2,
        route_ref=f"crp://routes/{PROJECT}/expanded", reason="expanded scope",
        evidence_ref=f"crp://receipts/{PROJECT}/expanded", recorded_at=NOW,
        capability_ids=("agent.list", "agent.message"),
        authorization_ref=f"crp://authorizations/{PROJECT}/expand",
    )
    assert authorized.checkpoint_revision == 2
    drift_world, drift_graph = _graph(tmp_path / "snapshot-drift")
    drift = ProjectTrajectoryRuntime(
        world=drift_world, profiles=AgentProfileRegistry(),
        topology=_Topology(PROJECT, drift=True),
    )
    with pytest.raises(ProjectTrajectoryError, match="snapshot"):
        drift.create_checkpoint(
            project_id=PROJECT, graph_id=drift_graph.spec.graph_id,
            segment_id="bad-budget",
            profile_id="main.orchestrator", route_revision=3,
            route_ref=f"crp://routes/{PROJECT}/three", reason="budget snapshot drift",
            evidence_ref=f"crp://receipts/{PROJECT}/three", recorded_at=NOW,
        )


def test_checkpoint_recovery_rejects_world_graph_and_provenance_drift(tmp_path: Path) -> None:
    world, graph = _graph(tmp_path)
    runtime = _trajectory(world)
    runtime.create_checkpoint(
        project_id=PROJECT, graph_id=graph.spec.graph_id, segment_id="checkpoint",
        profile_id="main.orchestrator", route_revision=1,
        route_ref=f"crp://routes/{PROJECT}/one", reason="recoverable scope",
        evidence_ref=f"crp://receipts/{PROJECT}/one", recorded_at=NOW,
    )
    world.append_event(_observation("after-checkpoint"))
    with pytest.raises(ProjectTrajectoryError, match="drifted"):
        runtime.recover(
            project_id=PROJECT, graph_id=graph.spec.graph_id,
            operation_id="restart-world-drift",
        )

    world, graph = _graph(tmp_path / "graph-drift")
    runtime = _trajectory(world)
    runtime.create_checkpoint(
        project_id=PROJECT, graph_id=graph.spec.graph_id, segment_id="graph-checkpoint",
        profile_id="main.orchestrator", route_revision=1,
        route_ref=f"crp://routes/{PROJECT}/graph", reason="graph recovery scope",
        evidence_ref=f"crp://receipts/{PROJECT}/graph", recorded_at=NOW,
    )
    ProjectTaskGraphRuntime(world=world).dispatch_ready(
        project_id=PROJECT, graph_id=graph.spec.graph_id,
        dispatch=lambda _node, _operation: f"crp://executions/{PROJECT}/node-one",
        recorded_at=NOW,
    )
    with pytest.raises(ProjectTrajectoryError, match="drifted"):
        runtime.recover(
            project_id=PROJECT, graph_id=graph.spec.graph_id,
            operation_id="restart-graph-drift",
        )

    world, graph = _graph(tmp_path / "provenance-drift")
    runtime = _trajectory(world)
    runtime.create_checkpoint(
        project_id=PROJECT, graph_id=graph.spec.graph_id, segment_id="provenance-checkpoint",
        profile_id="main.orchestrator", route_revision=1,
        route_ref=f"crp://routes/{PROJECT}/provenance", reason="provenance recovery scope",
        evidence_ref=f"crp://receipts/{PROJECT}/provenance", recorded_at=NOW,
    )
    ProjectProvenanceRuntime(world=world).record_subject(
        subject=TraceSubject(PROJECT, "artifact", "later"),
        version=VersionBinding(f"crp://artifacts/{PROJECT}/later", "r1", None), recorded_at=NOW,
    )
    with pytest.raises(ProjectTrajectoryError, match="drifted"):
        runtime.recover(
            project_id=PROJECT, graph_id=graph.spec.graph_id,
            operation_id="restart-provenance-drift",
        )


def test_checkpoint_recovery_revalidates_agent_budget_and_cancel_epoch(tmp_path: Path) -> None:
    world, graph = _graph(tmp_path)
    topology = _Topology(PROJECT)
    runtime = ProjectTrajectoryRuntime(
        world=world, profiles=AgentProfileRegistry(), topology=topology,
    )
    runtime.create_checkpoint(
        project_id=PROJECT, graph_id=graph.spec.graph_id,
        segment_id="agent-authority", profile_id="main.orchestrator",
        route_revision=1, route_ref=f"crp://routes/{PROJECT}/agent",
        reason="freeze current Agent authority",
        evidence_ref=f"crp://receipts/{PROJECT}/agent", recorded_at=NOW,
    )
    topology.cancel_epoch = 1
    with pytest.raises(ProjectTrajectoryError, match="authority drifted"):
        runtime.recover(
            project_id=PROJECT, graph_id=graph.spec.graph_id,
            operation_id="restart-cancel-epoch",
        )

    topology.cancel_epoch = 0
    topology.remaining_budget = AgentBudget(7, 16, 16_000, 4_000, 120_000)
    with pytest.raises(ProjectTrajectoryError, match="authority drifted"):
        runtime.recover(
            project_id=PROJECT, graph_id=graph.spec.graph_id,
            operation_id="restart-budget-change",
        )

    topology.remaining_budget = None
    topology.available_slots = 2
    with pytest.raises(ProjectTrajectoryError, match="authority drifted"):
        runtime.recover(
            project_id=PROJECT, graph_id=graph.spec.graph_id,
            operation_id="restart-capacity-change",
        )

    topology.available_slots = 3
    topology.status = "quarantined"
    with pytest.raises(ProjectTrajectoryError, match="main run authority"):
        runtime.recover(
            project_id=PROJECT, graph_id=graph.spec.graph_id,
            operation_id="restart-quarantined",
        )


def _graph(root: Path):
    world = PersonalWorldModelRuntime.for_root(
        root, now=lambda: datetime(2026, 9, 3, 10, tzinfo=timezone.utc),
    )
    subject = TraceSubject(PROJECT, "artifact", "output-one")
    version = VersionBinding(f"crp://artifacts/{PROJECT}/output-one", "r1", None)
    ProjectProvenanceRuntime(world=world).record_subject(subject=subject, version=version, recorded_at=NOW)
    spec = TaskGraphSpec("graph-one", PROJECT, "command-one", TaskBudget(2), 1, (
        TaskGraphNode("node-one", (), 1, TaskBudget(2), "all", subject, version),
    ))
    graph_runtime = ProjectTaskGraphRuntime(world=world)
    return world, graph_runtime.create(spec=spec, recorded_at=NOW)


def _trajectory(world: PersonalWorldModelRuntime, gate=None) -> ProjectTrajectoryRuntime:
    return ProjectTrajectoryRuntime(
        world=world, profiles=AgentProfileRegistry(), authorization_gate=gate,
        topology=_Topology(PROJECT),
    )


class _Topology:
    def __init__(self, project_id: str, drift: bool = False) -> None:
        self.project_id = project_id
        self.drift = drift
        self.cancel_epoch = 0
        self.remaining_budget = None
        self.available_slots = 3
        self.status = "queued"
    def main_run(self, *, project_id: str) -> AgentRun:
        profile = AgentProfileRegistry().resolve_tier("main.orchestrator")
        return AgentRun("main-run", "turn-main", project_id, profile.profile_id, profile.profile_revision, "main", profile.model_tier, self.status, 0, self.cancel_epoch, profile.budget_limit, profile.capability_ids, profile.max_concurrent_children, profile.max_depth, profile.max_steps, profile.timeout_ms, profile.allow_child_spawn, "crp://agent/main/route", "crp://agent/main/capabilities", "crp://agent/main/context", "crp://agent/main/budget", None, None)
    def snapshot_load(self, main_run: AgentRun, *, operation_id: str):
        budget = main_run.budget_limit
        project = "other" if self.drift else main_run.project_id
        return SimpleNamespace(
            workload=WorkloadSnapshot("load-work", project, 1, 0, 0, AgentBudget(0, 0, 0, 0, 0)),
            capacity=CapacitySnapshot(
                "load-cap", project, 1, self.available_slots, 3,
                self.remaining_budget or budget,
            ),
        )


def _observation(event_id: str):
    from core.personal_world_model import WorldEventDraft, WorldEventKind
    return WorldEventDraft(
        event_id=event_id, project_id=PROJECT, kind=WorldEventKind.PROJECT_OBSERVATION,
        actor="system", source_ref=f"crp://observations/{PROJECT}/{event_id}",
        source_revision="1", occurred_at=NOW, recorded_at=NOW,
        payload={"observation_id": event_id, "category": "environment", "summary": "fresh state changed", "evidence_refs": [f"crp://observations/{PROJECT}/{event_id}"]},
    )
