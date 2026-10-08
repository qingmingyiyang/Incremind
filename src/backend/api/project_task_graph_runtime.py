"""World-event-backed adapter for the pure long-horizon task graph."""

from __future__ import annotations

from collections.abc import Callable
from uuid import NAMESPACE_URL, uuid5

from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.project_provenance_runtime import ProjectProvenanceRuntime
from core.long_horizon_runtime import (
    ProvenanceContractError,
    CancellationAcknowledgement,
    TaskGraphEvent,
    TaskGraphNode,
    TaskGraphProjection,
    TaskGraphSpec,
    project_task_graph,
    task_graph_event_identity,
    task_graph_world_identity,
)
from core.personal_world_model import WorldEventDraft, WorldEventKind, validate_world_identifier


class ProjectTaskGraphError(ValueError):
    pass


class ProjectTaskGraphRuntime:
    """Persist graph facts only; execution remains an injected idempotent callback."""

    def __init__(
        self,
        *,
        world: PersonalWorldModelRuntime,
        verification_gate: Callable[[TaskGraphNode, str, str, str, bool], bool] | None = None,
    ) -> None:
        self._world = world
        self._provenance = ProjectProvenanceRuntime(world=world)
        self._verification_gate = verification_gate

    def create(self, *, spec: TaskGraphSpec, recorded_at: str) -> TaskGraphProjection:
        provenance = self._provenance.project(spec.project_id)
        try:
            for node in spec.nodes:
                provenance.current_validity.for_subject(node.subject, node.subject_version)
        except ProvenanceContractError as error:
            raise ProjectTaskGraphError(
                "task graph node provenance was not declared"
            ) from error
        self._append(spec.project_id, spec.graph_id, "graph.created", spec.command_id, {"spec": spec.to_payload()}, recorded_at)
        return self.project(project_id=spec.project_id, graph_id=spec.graph_id)

    def list(self, *, project_id: str) -> tuple[TaskGraphProjection, ...]:
        project = validate_world_identifier(project_id, "project id")
        graph_ids = sorted({item.graph_id for item in self._events(project)})
        return tuple(self.project(project_id=project, graph_id=graph_id) for graph_id in graph_ids)

    def project(self, *, project_id: str, graph_id: str) -> TaskGraphProjection:
        project = validate_world_identifier(project_id, "project id")
        graph = validate_world_identifier(graph_id, "graph id")
        events = tuple(item for item in self._events(project) if item.graph_id == graph)
        if not events:
            raise ProjectTaskGraphError("task graph is unavailable")
        return project_task_graph(events)

    def dispatch_ready(
        self, *, project_id: str, graph_id: str,
        dispatch: Callable[[object, str], str], recorded_at: str,
    ) -> TaskGraphProjection:
        projection = self.project(project_id=project_id, graph_id=graph_id)
        for state in projection.nodes:
            if state.status != "dispatching" or state.dispatch_operation_id is None:
                continue
            node = next(
                item for item in projection.spec.nodes if item.node_id == state.node_id
            )
            execution_ref = dispatch(node, state.dispatch_operation_id)
            if not isinstance(execution_ref, str) or not execution_ref.startswith("crp://"):
                raise ProjectTaskGraphError(
                    "task graph dispatch did not return an execution reference"
                )
            self._append(
                project_id,
                graph_id,
                "node.dispatched",
                state.dispatch_operation_id,
                {
                    "node_id": state.node_id,
                    "attempt": state.attempt,
                    "execution_ref": execution_ref,
                },
                recorded_at,
            )
        projection = self.project(project_id=project_id, graph_id=graph_id)
        for node_id in projection.next_dispatchable_node_ids:
            projection = self.project(project_id=project_id, graph_id=graph_id)
            if node_id not in projection.next_dispatchable_node_ids:
                continue
            node = next(item for item in projection.spec.nodes if item.node_id == node_id)
            state = projection.node(node_id)
            operation_id = _operation("dispatch", graph_id, node_id, state.attempt + 1)
            self._append(
                project_id,
                graph_id,
                "node.dispatch_requested",
                operation_id,
                {"node_id": node_id, "attempt": state.attempt + 1},
                recorded_at,
            )
            execution_ref = dispatch(node, operation_id)
            if not isinstance(execution_ref, str) or not execution_ref.startswith("crp://"):
                raise ProjectTaskGraphError("task graph dispatch did not return an execution reference")
            self._append(project_id, graph_id, "node.dispatched", operation_id, {
                "node_id": node_id, "attempt": state.attempt + 1, "execution_ref": execution_ref,
            }, recorded_at)
        return self.project(project_id=project_id, graph_id=graph_id)

    def observe_result(
        self, *, project_id: str, graph_id: str, node_id: str, result_ref: str,
        satisfied: bool, validation_status: str, evidence_ref: str, recorded_at: str,
    ) -> TaskGraphProjection:
        projection = self.project(project_id=project_id, graph_id=graph_id)
        state = projection.node(node_id)
        node = next(item for item in projection.spec.nodes if item.node_id == node_id)
        execution_ref = state.execution_ref
        if execution_ref is None:
            raise ProjectTaskGraphError("task graph result requires an external execution")
        if validation_status == "verified":
            gate = self._verification_gate
            if gate is None:
                raise ProjectTaskGraphError("verified task graph result requires an external gate")
            try:
                accepted = gate(node, execution_ref, result_ref, evidence_ref, satisfied)
            except Exception as error:
                raise ProjectTaskGraphError("task graph result verification failed") from error
            if accepted is not True:
                raise ProjectTaskGraphError("task graph result verification failed")
        self._append(project_id, graph_id, "node.result_observed", _operation("result", graph_id, node_id, state.attempt), {
            "node_id": node_id, "attempt": state.attempt, "result_ref": result_ref, "satisfied": satisfied,
        }, recorded_at)
        self._append(project_id, graph_id, "node.validation", _operation("validation", graph_id, node_id, state.attempt, validation_status), {
            "node_id": node_id, "status": validation_status, "evidence_ref": evidence_ref,
        }, recorded_at)
        return self.project(project_id=project_id, graph_id=graph_id)

    def cancel(
        self,
        *,
        project_id: str,
        graph_id: str,
        node_id: str,
        reason_ref: str,
        recorded_at: str,
        callback: Callable[[str], CancellationAcknowledgement] | None = None,
    ) -> TaskGraphProjection:
        operation = _operation("cancel", graph_id, node_id)
        self._append(
            project_id,
            graph_id,
            "node.cancel_requested",
            operation,
            {"node_id": node_id, "reason_ref": reason_ref},
            recorded_at,
        )
        projection = self.project(project_id=project_id, graph_id=graph_id)
        if projection.node(node_id).status == "cancelled":
            return projection
        if callback is not None:
            acknowledgement = callback(operation)
            if not isinstance(acknowledgement, CancellationAcknowledgement) or (
                acknowledgement.project_id,
                acknowledgement.graph_id,
                acknowledgement.node_id,
                acknowledgement.operation_id,
            ) != (project_id, graph_id, node_id, operation):
                raise ProjectTaskGraphError(
                    "task graph cancellation acknowledgement does not match the intent"
                )
            self._append(
                project_id,
                graph_id,
                "node.cancelled",
                operation,
                {
                    "node_id": node_id,
                    "cancellation_receipt_ref": acknowledgement.cancellation_receipt_ref,
                },
                recorded_at,
            )
        return self.project(project_id=project_id, graph_id=graph_id)

    def prune(self, *, project_id: str, graph_id: str, node_id: str, reason_ref: str, recorded_at: str) -> TaskGraphProjection:
        self._append(project_id, graph_id, "node.pruned", _operation("prune", graph_id, node_id), {"node_id": node_id, "reason_ref": reason_ref}, recorded_at)
        return self.project(project_id=project_id, graph_id=graph_id)

    def reschedule(self, *, project_id: str, graph_id: str, node_id: str, reason_ref: str, recorded_at: str) -> TaskGraphProjection:
        state = self.project(project_id=project_id, graph_id=graph_id).node(node_id)
        self._append(project_id, graph_id, "node.rescheduled", _operation("reschedule", graph_id, node_id, state.attempt + 1), {"node_id": node_id, "attempt": state.attempt + 1, "reason_ref": reason_ref}, recorded_at)
        return self.project(project_id=project_id, graph_id=graph_id)

    def sync_provenance(self, *, project_id: str, graph_id: str, recorded_at: str) -> TaskGraphProjection:
        projection = self.project(project_id=project_id, graph_id=graph_id)
        validity = self._provenance.project(project_id).current_validity
        for node in projection.spec.nodes:
            state = self.project(project_id=project_id, graph_id=graph_id).node(node.node_id)
            current = validity.for_subject(node.subject, node.subject_version)
            status = "invalidated" if current.status == "invalidated" else "stale" if current.status == "stale" else "verified" if state.result_ref and current.status == "verified" else None
            if status is not None and state.validation_status != status:
                self._append(project_id, graph_id, "node.validation", _operation("provenance", graph_id, node.node_id, status), {
                    "node_id": node.node_id, "status": status, "evidence_ref": node.subject_version.authority_ref,
                }, recorded_at)
        return self.project(project_id=project_id, graph_id=graph_id)

    def _events(self, project_id: str) -> tuple[TaskGraphEvent, ...]:
        return tuple(
            TaskGraphEvent.from_payload(event.payload)
            for event in self._world.events(project_id)
            if event.kind is WorldEventKind.TASK_GRAPH_EVENT_RECORDED
        )

    def _append(self, project_id: str, graph_id: str, kind: str, operation_id: str, payload: dict[str, object], recorded_at: str) -> None:
        event_identity = task_graph_event_identity(
            project_id,
            graph_id,
            kind,
            operation_id,
        )
        project_events = self._events(project_id)
        existing = next(
            (item for item in project_events if item.event_id == event_identity),
            None,
        )
        if existing is not None:
            candidate = TaskGraphEvent(
                event_id=event_identity,
                project_id=project_id,
                graph_id=graph_id,
                sequence=existing.sequence,
                kind=kind,
                operation_id=operation_id,
                payload=payload,
            )
            if existing != candidate:
                raise ProjectTaskGraphError("task graph event identity conflicts")
            return
        sequence = len([item for item in project_events if item.graph_id == graph_id]) + 1
        graph_event = TaskGraphEvent(
            event_id=event_identity, project_id=project_id,
            graph_id=graph_id, sequence=sequence, kind=kind, operation_id=operation_id, payload=payload,
        )
        event_id, source_ref, source_revision = task_graph_world_identity(graph_event)
        self._world.append_event(WorldEventDraft(
            event_id=event_id, project_id=project_id, kind=WorldEventKind.TASK_GRAPH_EVENT_RECORDED,
            actor="system", source_ref=source_ref,
            source_revision=source_revision, occurred_at=recorded_at, recorded_at=recorded_at,
            payload=graph_event.to_payload(),
        ))


def _operation(kind: str, graph_id: str, node_id: str, *parts: object) -> str:
    return f"task-graph-{kind}-{uuid5(NAMESPACE_URL, repr((graph_id, node_id, parts))).hex}"
