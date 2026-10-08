"""World-backed, read-only trajectory checkpoint adapter.

Checkpoints are facts about already governed graph and provenance state.  This
adapter deliberately never starts, cancels, or mutates an Agent, Job, or Effect.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.project_provenance_runtime import ProjectProvenanceRuntime
from backend.api.project_task_graph_runtime import ProjectTaskGraphRuntime
from core.ai_kernel.agent_profiles import AgentProfileRegistry
from core.ai_kernel.agent_contracts import AgentRun
from core.ai_kernel.agent_dispatch_contracts import CapacitySnapshot, WorkloadSnapshot
from core.long_horizon_runtime import (
    NodeBudget,
    PendingExecution,
    RecoveryProjection,
    TaskBudget,
    TrajectoryContractError,
    TrajectorySegment,
    TrustCheckpoint,
    project_checkpoint_recovery,
    trajectory_checkpoint_world_identity,
)
from core.personal_world_model import WorldEventDraft, WorldEventKind, validate_world_identifier


class ProjectTrajectoryError(ValueError):
    pass


class AgentDispatchTopologyReader(Protocol):
    """Read-only facade for the durable Agent reservation authority."""

    def main_run(self, *, project_id: str) -> AgentRun: ...
    def snapshot_load(self, main_run: AgentRun, *, operation_id: str) -> object: ...


class ProjectTrajectoryRuntime:
    """Build immutable trajectory checkpoints from current project authorities."""

    def __init__(
        self,
        *,
        world: PersonalWorldModelRuntime,
        profiles: AgentProfileRegistry,
        topology: AgentDispatchTopologyReader,
        authorization_gate: Callable[[str, TrajectorySegment, TrajectorySegment], bool] | None = None,
    ) -> None:
        self._world = world
        self._profiles = profiles
        self._topology = topology
        self._authorization_gate = authorization_gate
        self._graphs = ProjectTaskGraphRuntime(world=world)
        self._provenance = ProjectProvenanceRuntime(world=world)
        self._world.bind_trajectory_authority(self)

    def checkpoints(self, *, project_id: str, graph_id: str) -> tuple[TrustCheckpoint, ...]:
        project = validate_world_identifier(project_id, "project id")
        graph = validate_world_identifier(graph_id, "graph id")
        return tuple(
            TrustCheckpoint.from_payload(event.payload)
            for event in self._world.events(project)
            if event.kind is WorldEventKind.TRAJECTORY_CHECKPOINT_RECORDED
            and event.payload.get("graph_id") == graph
        )

    def latest(self, *, project_id: str, graph_id: str) -> TrustCheckpoint:
        records = self.checkpoints(project_id=project_id, graph_id=graph_id)
        if not records:
            raise ProjectTrajectoryError("trajectory checkpoint is unavailable")
        return records[-1]

    def create_checkpoint(
        self,
        *,
        project_id: str,
        graph_id: str,
        segment_id: str,
        profile_id: str,
        route_revision: int,
        route_ref: str,
        reason: str,
        evidence_ref: str,
        recorded_at: str,
        capability_ids: tuple[str, ...] | None = None,
        authorization_ref: str | None = None,
    ) -> TrustCheckpoint:
        project = validate_world_identifier(project_id, "project id")
        graph_id = validate_world_identifier(graph_id, "graph id")
        graph = self._graphs.project(project_id=project, graph_id=graph_id)
        provenance = self._provenance.project(project)
        previous = self.checkpoints(project_id=project, graph_id=graph_id)
        checkpoint_id = f"checkpoint-{segment_id}"
        try:
            profile = self._profiles.resolve_tier(profile_id)
        except ValueError as error:
            raise ProjectTrajectoryError("trajectory profile is unavailable") from error
        capabilities = tuple(profile.capability_ids) if capability_ids is None else tuple(capability_ids)
        if len(capabilities) != len(set(capabilities)) or not set(capabilities).issubset(profile.capability_ids):
            raise ProjectTrajectoryError("trajectory capabilities exceed profile ceiling")
        existing = next(
            (item for item in previous if item.checkpoint_id == checkpoint_id), None,
        )
        if existing is not None:
            # A restart repeats the same durable segment operation, not a new
            # budget decision.  Its immutable checkpoint is the replay result.
            self._assert_replay_request(
                existing, profile_id=profile_id, profile_revision=profile.profile_revision,
                route_revision=route_revision, route_ref=route_ref, reason=reason,
                evidence_ref=evidence_ref, capability_ids=capabilities,
                authorization_ref=authorization_ref,
            )
            return existing
        prior = previous[-1] if previous else None
        try:
            main = self._topology.main_run(project_id=project)
            workload, capacity = _load_snapshots(self._topology.snapshot_load(
                main, operation_id=f"trajectory.{graph_id}.{segment_id}",
            ))
            self._validate_dispatch_authority(main, profile, capabilities, project, workload, capacity)
            budgets = _graph_budgets(graph)
            pending = self._pending_executions(graph, project)
            verified_nodes = tuple(
                state.node_id for state in graph.nodes if state.status == "settled"
            )
            artifacts = tuple(sorted({
                item.version.authority_ref
                for item in provenance.current_validity.stable_subjects
                if item.subject.kind == "artifact"
            }))
            next_world_sequence = len(self._world.events(project)) + 1
            segment = TrajectorySegment(
                project_id=project, graph_id=graph_id, segment_id=segment_id,
                revision=1 if prior is None else prior.segment.revision + 1,
                previous_segment_id=None if prior is None else prior.segment.segment_id,
                basis_world_sequence=next_world_sequence,
                basis_graph_sequence=graph.through_sequence,
                provenance_sequence=provenance.through_sequence,
                route_revision=route_revision, route_ref=route_ref,
                profile_id=profile.profile_id, profile_revision=profile.profile_revision,
                model_tier=profile.model_tier, capability_ids=capabilities,
                node_budgets=budgets, verified_artifact_refs=artifacts,
                pending_executions=pending, reason=reason, evidence_ref=evidence_ref,
                authorization_ref=authorization_ref,
            )
            if prior is not None and not set(capabilities).issubset(prior.segment.capability_ids):
                gate = self._authorization_gate
                if authorization_ref is None or gate is None or gate(authorization_ref, prior.segment, segment) is not True:
                    raise ProjectTrajectoryError("trajectory capability expansion was not authorized")
            checkpoint = TrustCheckpoint(
                checkpoint_id=checkpoint_id, project_id=project,
                graph_id=graph_id,
                checkpoint_revision=1 if prior is None else prior.checkpoint_revision + 1,
                segment=segment, dag_cursor=graph.through_sequence,
                world_cursor=next_world_sequence, provenance_cursor=provenance.through_sequence,
                verified_node_ids=verified_nodes, verified_artifact_refs=artifacts,
                frozen_capability_ids=capabilities,
                frozen_remaining_budget=TaskBudget(sum(item.remaining.units for item in budgets)),
                pending_executions=pending,
                workload_snapshot_ref=f"crp://dispatch/workload/{workload.snapshot_id}",
                workload_snapshot_revision=workload.revision,
                capacity_snapshot_ref=f"crp://dispatch/capacity/{capacity.snapshot_id}",
                capacity_snapshot_revision=capacity.revision,
                main_run_id=main.run_id, main_cancel_epoch=main.cancel_epoch,
                frozen_agent_budget=capacity.remaining_budget,
                frozen_workload_queued=workload.queued_assignments,
                frozen_workload_active=workload.active_assignments,
                frozen_workload_reserved_budget=workload.reserved_budget,
                frozen_capacity_available=capacity.available_slots,
                frozen_capacity_maximum=capacity.maximum_slots,
            )
        except (TrajectoryContractError, ValueError) as error:
            if isinstance(error, ProjectTrajectoryError):
                raise
            raise ProjectTrajectoryError("trajectory checkpoint is invalid") from error
        event_id, source_ref, source_revision = trajectory_checkpoint_world_identity(checkpoint)
        self._world.append_event(WorldEventDraft(
            event_id=event_id, project_id=project,
            kind=WorldEventKind.TRAJECTORY_CHECKPOINT_RECORDED, actor="system",
            source_ref=source_ref, source_revision=source_revision,
            occurred_at=recorded_at, recorded_at=recorded_at,
            payload=checkpoint.to_payload(),
        ))
        return checkpoint

    # Friendly aliases keep callers from treating a segment as a mutable object.
    record_checkpoint = create_checkpoint
    create_segment = create_checkpoint

    def recover(
        self, *, project_id: str, graph_id: str, operation_id: str,
    ) -> RecoveryProjection:
        project = validate_world_identifier(project_id, "project id")
        graph_id = validate_world_identifier(graph_id, "graph id")
        recovery_operation = validate_world_identifier(operation_id, "recovery operation id")
        records = self.checkpoints(project_id=project, graph_id=graph_id)
        if not records:
            raise ProjectTrajectoryError("trajectory checkpoint is unavailable")
        checkpoint = records[-1]
        graph = self._graphs.project(project_id=project, graph_id=graph_id)
        provenance = self._provenance.project(project)
        try:
            profile = self._profiles.resolve_tier(checkpoint.segment.profile_id)
            main = self._topology.main_run(project_id=project)
            workload, capacity = _load_snapshots(self._topology.snapshot_load(
                main, operation_id=f"trajectory.recover.{recovery_operation}",
            ))
            self._validate_dispatch_authority(
                main, profile, checkpoint.frozen_capability_ids,
                project, workload, capacity,
            )
        except (TrajectoryContractError, ValueError) as error:
            if isinstance(error, ProjectTrajectoryError):
                raise
            raise ProjectTrajectoryError("trajectory checkpoint authority drifted") from error
        if (
            main.run_id != checkpoint.main_run_id
            or main.cancel_epoch != checkpoint.main_cancel_epoch
            or profile.profile_revision != checkpoint.segment.profile_revision
            or profile.model_tier != checkpoint.segment.model_tier
            or not set(checkpoint.frozen_capability_ids).issubset(profile.capability_ids)
            or capacity.remaining_budget != checkpoint.frozen_agent_budget
            or workload.queued_assignments != checkpoint.frozen_workload_queued
            or workload.active_assignments != checkpoint.frozen_workload_active
            or workload.reserved_budget != checkpoint.frozen_workload_reserved_budget
            or capacity.available_slots != checkpoint.frozen_capacity_available
            or capacity.maximum_slots != checkpoint.frozen_capacity_maximum
        ):
            raise ProjectTrajectoryError("trajectory checkpoint authority drifted")
        prior = records[-2] if len(records) > 1 else None
        if (
            prior is not None
            and not set(checkpoint.frozen_capability_ids).issubset(
                prior.frozen_capability_ids
            )
        ):
            gate = self._authorization_gate
            authorization_ref = checkpoint.segment.authorization_ref
            if (
                authorization_ref is None
                or gate is None
                or gate(authorization_ref, prior.segment, checkpoint.segment) is not True
            ):
                raise ProjectTrajectoryError(
                    "trajectory checkpoint authorization drifted"
                )
        valid_redispatch = set(graph.next_dispatchable_node_ids)
        validity: dict[str, str] = {}
        for node in graph.spec.nodes:
            state = graph.node(node.node_id)
            if state.status in {"stale", "invalidated"}:
                validity[node.node_id] = state.status
            elif state.status == "settled":
                validity[node.node_id] = "verified"
            else:
                validity[node.node_id] = (
                    "pending" if node.node_id in valid_redispatch else "unavailable"
                )
        try:
            recovery = project_checkpoint_recovery(
                checkpoint, world_cursor=len(self._world.events(project)),
                dag_cursor=graph.through_sequence,
                provenance_cursor=provenance.through_sequence,
                node_validity={
                    node_id: status if status != "unavailable" else "pending"
                    for node_id, status in validity.items()
                },
            )
            return RecoveryProjection(
                recovery.skip_verified,
                recovery.resume_pending,
                tuple(node_id for node_id in recovery.redispatch_ready if node_id in valid_redispatch),
            )
        except TrajectoryContractError as error:
            raise ProjectTrajectoryError("trajectory checkpoint recovery drifted") from error

    def validate_checkpoint(
        self,
        checkpoint: TrustCheckpoint,
        previous: TrustCheckpoint | None,
    ) -> None:
        """Revalidate every protected checkpoint inside the World append transaction."""

        try:
            profile = self._profiles.resolve_tier(checkpoint.segment.profile_id)
            main = self._topology.main_run(project_id=checkpoint.project_id)
            workload, capacity = _load_snapshots(self._topology.snapshot_load(
                main,
                operation_id=(
                    f"trajectory.{checkpoint.graph_id}."
                    f"{checkpoint.segment.segment_id}"
                ),
            ))
            self._validate_dispatch_authority(
                main, profile, checkpoint.frozen_capability_ids,
                checkpoint.project_id, workload, capacity,
            )
        except (TrajectoryContractError, ValueError) as error:
            if isinstance(error, ProjectTrajectoryError):
                raise
            raise ProjectTrajectoryError(
                "trajectory checkpoint append authority drifted"
            ) from error
        if (
            checkpoint.segment.profile_revision != profile.profile_revision
            or checkpoint.segment.model_tier != profile.model_tier
            or checkpoint.segment.capability_ids != checkpoint.frozen_capability_ids
            or main.run_id != checkpoint.main_run_id
            or main.cancel_epoch != checkpoint.main_cancel_epoch
            or checkpoint.workload_snapshot_ref
            != f"crp://dispatch/workload/{workload.snapshot_id}"
            or checkpoint.workload_snapshot_revision != workload.revision
            or checkpoint.capacity_snapshot_ref
            != f"crp://dispatch/capacity/{capacity.snapshot_id}"
            or checkpoint.capacity_snapshot_revision != capacity.revision
            or checkpoint.frozen_agent_budget != capacity.remaining_budget
            or checkpoint.frozen_workload_queued != workload.queued_assignments
            or checkpoint.frozen_workload_active != workload.active_assignments
            or checkpoint.frozen_workload_reserved_budget != workload.reserved_budget
            or checkpoint.frozen_capacity_available != capacity.available_slots
            or checkpoint.frozen_capacity_maximum != capacity.maximum_slots
        ):
            raise ProjectTrajectoryError(
                "trajectory checkpoint append authority drifted"
            )
        if previous is not None and not set(
            checkpoint.frozen_capability_ids
        ).issubset(previous.frozen_capability_ids):
            gate = self._authorization_gate
            authorization_ref = checkpoint.segment.authorization_ref
            if (
                authorization_ref is None
                or gate is None
                or gate(authorization_ref, previous.segment, checkpoint.segment) is not True
            ):
                raise ProjectTrajectoryError(
                    "trajectory checkpoint capability expansion was not authorized"
                )

    @staticmethod
    def _validate_dispatch_authority(
        main: AgentRun, profile: object, capabilities: tuple[str, ...], project_id: str,
        workload: WorkloadSnapshot, capacity: CapacitySnapshot,
    ) -> None:
        if (
            not isinstance(main, AgentRun)
            or main.project_id != project_id
            or main.role != "main"
            or main.parent_run_id is not None
            or main.status not in {
                "queued", "starting", "running", "waiting", "recovery_required",
            }
        ):
            raise ProjectTrajectoryError("trajectory main run authority drifted")
        if (
            not isinstance(getattr(profile, "profile_id", None), str)
            or not isinstance(getattr(profile, "profile_revision", None), int)
            or getattr(profile, "model_tier", None) not in {"fast", "standard", "deep"}
            or not set(capabilities).issubset(
                getattr(profile, "capability_ids", ())
            )
        ):
            raise ProjectTrajectoryError("trajectory selected profile authority drifted")
        if (
            not isinstance(workload, WorkloadSnapshot)
            or not isinstance(capacity, CapacitySnapshot)
            or workload.project_id != project_id
            or capacity.project_id != project_id
        ):
            raise ProjectTrajectoryError("trajectory dispatch snapshot authority drifted")

    @staticmethod
    def _assert_replay_request(
        checkpoint: TrustCheckpoint, *, profile_id: str, profile_revision: int,
        route_revision: int, route_ref: str, reason: str, evidence_ref: str,
        capability_ids: tuple[str, ...], authorization_ref: str | None,
    ) -> None:
        segment = checkpoint.segment
        if (
            segment.profile_id != profile_id
            or segment.profile_revision != profile_revision
            or segment.route_revision != route_revision
            or segment.route_ref != route_ref
            or segment.reason != reason
            or segment.evidence_ref != evidence_ref
            or segment.capability_ids != capability_ids
            or segment.authorization_ref != authorization_ref
        ):
            raise ProjectTrajectoryError("trajectory checkpoint replay conflicts")

    @staticmethod
    def _pending_executions(graph: object, project_id: str) -> tuple[PendingExecution, ...]:
        graph_id = getattr(getattr(graph, "spec", None), "graph_id", "")
        pending = []
        for state in getattr(graph, "nodes", ()):
            if state.status == "dispatching":
                operation = state.dispatch_operation_id
                if not isinstance(operation, str):
                    raise ProjectTrajectoryError("trajectory dispatch intent is unavailable")
                ref = f"crp://task-graphs/{project_id}/{graph_id}/dispatch/{operation}"
            elif state.status == "leased":
                ref = state.execution_ref
                if not _project_ref(ref, project_id):
                    raise ProjectTrajectoryError("trajectory leased execution is not project-scoped")
            else:
                continue
            pending.append(PendingExecution(state.node_id, "agent", ref))
        return tuple(pending)


def _project_ref(value: object, project_id: str) -> bool:
    if not isinstance(value, str) or not value.startswith("crp://"):
        return False
    parts = value.removeprefix("crp://").split("/")
    return len(parts) >= 3 and parts[1] == project_id


def _load_snapshots(value: object) -> tuple[WorkloadSnapshot, CapacitySnapshot]:
    workload = getattr(value, "workload", None)
    capacity = getattr(value, "capacity", None)
    if not isinstance(workload, WorkloadSnapshot) or not isinstance(capacity, CapacitySnapshot):
        raise ProjectTrajectoryError("trajectory dispatch snapshots are unavailable")
    return workload, capacity


def _graph_budgets(graph: object) -> tuple[NodeBudget, ...]:
    """Graph budget is scheduling metadata only, never an Agent budget authority."""

    return tuple(
        NodeBudget(
            node.node_id, node.budget, node.budget,
        )
        for node in graph.spec.nodes
    )
