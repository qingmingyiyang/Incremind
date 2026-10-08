"""Read-only proof bridge from a governed Turn to LineMap replay evidence.

This module deliberately has no Turn submission, model gateway, effect, or
recovery behaviour.  It only reads the durable AI Turn record and issues the
private Context Graph proof objects after every correlation fence is proven.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from backend.model_routing_snapshot import (
    TurnModelRoutingSnapshotError,
    turn_model_routing_snapshot_revision,
    validate_turn_model_routing_snapshot,
)
from core.ai_kernel.contracts import (
    AIKernelContractError,
    validate_model_call_receipt,
    validate_model_wire_attempt_receipt,
    validate_turn_request,
)
from core.context_graph.compiler import FrozenContextRevisions
from core.context_graph.binding_payload import context_binding_from_payload
from core.context_graph.replay_completion import (
    ReplayRequest,
    TrustedCompletion,
    _issue_trusted_model_wire_evidence,
    _issue_trusted_terminal_evidence,
)


class ContextGraphReplayEvidenceError(ValueError):
    """The ordinary Turn cannot prove this replay completion."""


class AITurnEvidenceReader(Protocol):
    """The read-only subset of ``SQLiteAITurnStore`` used by this bridge."""

    def get_request(self, turn_id: str) -> Mapping[str, object] | None: ...

    def events_after(
        self, turn_id: str, after_sequence: int = 0,
    ) -> Sequence[Mapping[str, object]]: ...

    def get(self, payload_ref: str) -> object: ...

    def get_immutable_payload(
        self, turn_id: str, kind: str,
    ) -> tuple[str, object] | None: ...


@dataclass(frozen=True, slots=True)
class VerifiedReplayTurnEvidence:
    """A non-executable terminal summary with Core-owned replay proof.

    ``output_text`` is copied only from the accepted ``turn.completed`` event.
    It is data, never an instruction, and the bridge does not render or execute
    it.  The caller may persist it through the normal immutable content path.
    """

    completion: TrustedCompletion
    output_text: str
    operation_id: str
    routing_snapshot_ref: str
    model_receipt_ref: str


def verified_replay_completion_from_turn(
    *,
    store: AITurnEvidenceReader,
    replay_request: ReplayRequest,
    expected_operation_id: str,
    expected_turn_request: Mapping[str, object],
) -> VerifiedReplayTurnEvidence:
    """Read and validate one completed governed ``context.evaluate`` Turn.

    A caller cannot nominate event or payload references: all references are
    loaded from the Turn's durable event stream and must exactly match the
    immutable replay request.  Any absent, malformed, duplicated, or drifted
    proof fails closed.
    """

    _required_text(expected_operation_id, "expected operation id")
    try:
        request = validate_turn_request(_required_request(store, replay_request.turn_id))
        expected = validate_turn_request(expected_turn_request)
    except (AIKernelContractError, TypeError, ValueError) as error:
        raise ContextGraphReplayEvidenceError("replay durable Turn request is invalid") from error
    if request != expected:
        raise ContextGraphReplayEvidenceError("replay durable Turn request drifted")
    if expected_operation_id != replay_request.turn_operation_id:
        raise ContextGraphReplayEvidenceError("replay requested operation identity drifted")
    _validate_turn_identity(request, replay_request, expected_operation_id)
    _validate_context_binding(store, request, replay_request)

    events = tuple(store.events_after(replay_request.turn_id))
    model_event, terminal = _unique_completed_events(events, replay_request.turn_id)
    model_request_id = _correlate(model_event, terminal)
    model_data = _mapping(model_event.get("data"), "model completed data")
    terminal_data = _mapping(terminal.get("data"), "Turn completed data")
    terminal_event_ref = _required_text(terminal.get("event_id"), "terminal event ref")

    receipt_ref = _session_ref(model_data.get("receipt_ref"), replay_request.turn_id, "model Receipt ref")
    try:
        receipt = validate_model_call_receipt(store.get(receipt_ref))
    except (AIKernelContractError, KeyError, TypeError, ValueError) as error:
        raise ContextGraphReplayEvidenceError("replay model Receipt is invalid") from error
    if (
        receipt.get("turn_id") != replay_request.turn_id
        or receipt.get("model_request_id") != model_request_id
        or receipt.get("status") != "completed"
        or receipt.get("usage_status") != "recorded"
    ):
        raise ContextGraphReplayEvidenceError("replay model Receipt is incomplete")

    evidence_refs = model_data.get("evidence_refs")
    routing_ref, routing = _routing_snapshot(
        evidence_refs, turn_id=replay_request.turn_id, payload_loader=store.get,
    )
    _validate_routing(routing, request, replay_request, receipt, model_request_id)
    attempt_ref = _completed_wire_attempt(
        evidence_refs,
        turn_id=replay_request.turn_id,
        model_request_id=model_request_id,
        routing_snapshot_revision=turn_model_routing_snapshot_revision(routing),
        provider_id=str(_mapping(routing.get("selected"), "routing selection")["provider_id"]),
        model_name=str(_mapping(routing.get("selected"), "routing selection")["model_name"]),
        payload_loader=store.get,
    )

    summary = terminal_data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ContextGraphReplayEvidenceError("replay terminal output is unavailable")
    completed_at = receipt.get("completed_at")
    if not isinstance(completed_at, str) or not completed_at.strip():
        raise ContextGraphReplayEvidenceError("replay completion time is unavailable")
    return VerifiedReplayTurnEvidence(
        completion=TrustedCompletion(
            _issue_trusted_terminal_evidence(
                replay_request.turn_id, replay_request.turn_operation_id,
                terminal_event_ref, "completed",
            ),
            _issue_trusted_model_wire_evidence(
                replay_request.turn_id, replay_request.turn_operation_id, attempt_ref,
            ),
            f"turn-terminal-summary:{terminal_event_ref}",
            completed_at,
        ),
        output_text=summary.strip(),
        operation_id=str(request["operation_id"]),
        routing_snapshot_ref=routing_ref,
        model_receipt_ref=receipt_ref,
    )


def _required_request(store: AITurnEvidenceReader, turn_id: str) -> Mapping[str, object]:
    value = store.get_request(turn_id)
    if not isinstance(value, Mapping):
        raise ContextGraphReplayEvidenceError("replay Turn is unavailable")
    return value


def _validate_turn_identity(
    request: Mapping[str, object], replay: ReplayRequest, expected_operation_id: str,
) -> None:
    if request.get("turn_id") != replay.turn_id or request.get("operation_id") != expected_operation_id:
        raise ContextGraphReplayEvidenceError("replay Turn identity drifted")
    scope = _mapping(request.get("scope"), "Turn scope")
    if scope.get("kind") not in {"project", "series"} or scope.get("project_id") != replay.project_id:
        raise ContextGraphReplayEvidenceError("replay Turn project scope drifted")
    if request.get("desired_outcome") != "context.evaluate":
        raise ContextGraphReplayEvidenceError("replay Turn outcome is invalid")


def _validate_context_binding(
    store: AITurnEvidenceReader, request: Mapping[str, object], replay: ReplayRequest,
) -> None:
    turn_input = _mapping(request.get("input"), "Turn input")
    refs = turn_input.get("refs")
    if not isinstance(refs, list):
        raise ContextGraphReplayEvidenceError("replay ContextBinding refs are invalid")
    bindings = [item for item in refs if isinstance(item, Mapping) and item.get("kind") == "context_binding"]
    if len(bindings) != 1 or set(bindings[0]) != {"kind", "object_id", "uri"}:
        raise ContextGraphReplayEvidenceError("replay ContextBinding is unavailable or ambiguous")
    binding_ref = bindings[0].get("uri")
    if binding_ref != replay.binding_ref:
        raise ContextGraphReplayEvidenceError("replay ContextBinding identity drifted")
    immutable = store.get_immutable_payload(replay.turn_id, "context-binding-v1")
    if immutable is None or not isinstance(immutable[1], Mapping):
        raise ContextGraphReplayEvidenceError("replay immutable ContextBinding is unavailable")
    value = immutable[1]
    if (
        value.get("schema_version") != "1.0.0"
        or value.get("project_id") != replay.project_id
        or value.get("binding_id") != bindings[0].get("object_id")
        or value.get("capability_revision") != replay.revisions.capability_revision
    ):
        raise ContextGraphReplayEvidenceError("replay immutable ContextBinding identity drifted")
    try:
        binding = context_binding_from_payload(value.get("binding"))
    except (TypeError, ValueError) as error:
        raise ContextGraphReplayEvidenceError("replay immutable ContextBinding is invalid") from error
    if (
        binding.graph_id != replay.graph_id
        or binding.graph_revision != replay.source_graph_revision
        or _binding_revisions(binding) != replay.revisions
    ):
        raise ContextGraphReplayEvidenceError("replay immutable ContextBinding revisions drifted")


def _binding_revisions(binding: object) -> FrozenContextRevisions:
    return FrozenContextRevisions(
        capability_revision=str(getattr(binding, "capability_revision")),
        boundary_revision=str(getattr(binding, "boundary_revision")),
        provider_revision=str(getattr(binding, "provider_revision")),
        model_route_revision=str(getattr(binding, "model_route_revision")),
        compiler_revision=str(getattr(binding, "compiler_revision")),
    )


def _unique_completed_events(
    events: Sequence[Mapping[str, object]], turn_id: str,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    model = [item for item in events if item.get("type") == "model.completed"]
    terminal = [item for item in events if item.get("type") == "turn.completed"]
    if len(model) != 1 or len(terminal) != 1:
        raise ContextGraphReplayEvidenceError("replay Turn completion is absent or ambiguous")
    if model[0].get("turn_id") != turn_id or terminal[0].get("turn_id") != turn_id:
        raise ContextGraphReplayEvidenceError("replay Turn event identity drifted")
    return model[0], terminal[0]


def _correlate(model: Mapping[str, object], terminal: Mapping[str, object]) -> str:
    model_correlation = _mapping(model.get("correlation"), "model completion correlation")
    terminal_correlation = _mapping(terminal.get("correlation"), "Turn completion correlation")
    model_request_id = _required_text(model_correlation.get("model_request_id"), "model request id")
    if (
        _required_text(model_correlation.get("step_id"), "model step id")
        != _required_text(terminal_correlation.get("step_id"), "terminal step id")
        or model_request_id
        != _required_text(terminal_correlation.get("model_request_id"), "terminal model request id")
    ):
        raise ContextGraphReplayEvidenceError("replay model and terminal correlation drifted")
    return model_request_id


def _routing_snapshot(
    value: object, *, turn_id: str, payload_loader: object,
) -> tuple[str, Mapping[str, object]]:
    if not isinstance(value, list) or not callable(payload_loader):
        raise ContextGraphReplayEvidenceError("replay model evidence refs are invalid")
    matches: list[tuple[str, Mapping[str, object]]] = []
    for item in value:
        if not isinstance(item, str) or not item.startswith(f"crp://session/{turn_id}/"):
            continue
        try:
            snapshot = validate_turn_model_routing_snapshot(payload_loader(item))
        except (KeyError, TurnModelRoutingSnapshotError, TypeError, ValueError):
            continue
        matches.append((item, snapshot))
    if len(matches) != 1 or matches[0][1]["turn"]["turn_id"] != turn_id:
        raise ContextGraphReplayEvidenceError("replay Routing Snapshot is unavailable or ambiguous")
    return matches[0]


def _validate_routing(
    routing: Mapping[str, object], request: Mapping[str, object], replay: ReplayRequest,
    receipt: Mapping[str, object], model_request_id: str,
) -> None:
    selected = _mapping(routing.get("selected"), "routing selection")
    boundary = _mapping(routing.get("boundary"), "routing Boundary")
    requirement = _mapping(routing.get("requirement"), "routing requirement")
    project = _mapping(routing.get("project"), "routing project")
    if (
        project.get("project_id") != replay.project_id
        or requirement.get("required_capability") != "structured"
        or requirement.get("output_contract") != "json_object"
        or _revision_text(boundary.get("profile_revision")) != replay.revisions.boundary_revision
        or str(selected.get("provider_revision")) != replay.revisions.provider_revision
        or _revision_text(selected.get("route_revision")) != replay.revisions.model_route_revision
        or receipt.get("model_request_id") != model_request_id
        or receipt.get("provider_id") != selected.get("provider_id")
        or receipt.get("model_id") != selected.get("model_name")
        or requirement.get("input_refs") != _routing_input_refs(request)
    ):
        raise ContextGraphReplayEvidenceError("replay model routing evidence drifted")


def _completed_wire_attempt(
    value: object, *, turn_id: str, model_request_id: str,
    routing_snapshot_revision: str, provider_id: str, model_name: str,
    payload_loader: object,
) -> str:
    if not isinstance(value, list) or not callable(payload_loader):
        raise ContextGraphReplayEvidenceError("replay model evidence refs are invalid")
    prefix = f"crp://session/{turn_id}/model-wire-attempt-receipt/"
    refs = [item for item in value if isinstance(item, str) and item.startswith(prefix)]
    if len(refs) != 1:
        raise ContextGraphReplayEvidenceError("replay completed wire attempt Receipt is unavailable or ambiguous")
    try:
        receipt = validate_model_wire_attempt_receipt(payload_loader(refs[0]))
    except (AIKernelContractError, KeyError, TypeError, ValueError) as error:
        raise ContextGraphReplayEvidenceError("replay completed wire attempt Receipt is invalid") from error
    if (
        receipt.get("status") != "succeeded"
        or receipt.get("turn_id") != turn_id
        or receipt.get("model_request_id") != model_request_id
        or receipt.get("routing_snapshot_revision") != routing_snapshot_revision
        or receipt.get("provider_id") != provider_id
        or receipt.get("model_id") != model_name
    ):
        raise ContextGraphReplayEvidenceError("replay completed wire attempt Receipt drifted")
    return refs[0]


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContextGraphReplayEvidenceError(f"{label} is invalid")
    return value


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextGraphReplayEvidenceError(f"{label} is invalid")
    return value


def _session_ref(value: object, turn_id: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith(f"crp://session/{turn_id}/"):
        raise ContextGraphReplayEvidenceError(f"{label} is invalid")
    return value


def _revision_text(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if type(value) is int and value > 0:
        return str(value)
    raise ContextGraphReplayEvidenceError("routing revision is invalid")


def _routing_input_refs(request: Mapping[str, object]) -> list[dict[str, str]]:
    values = _mapping(request.get("input"), "Turn input").get("refs")
    if not isinstance(values, list):
        raise ContextGraphReplayEvidenceError("Turn refs are invalid")
    projected: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise ContextGraphReplayEvidenceError("Turn ref is invalid")
        if value.get("object_id") is not None:
            projected.append({key: str(value[key]) for key in sorted(value) if key != "uri"})
        else:
            projected.append({"ref": str(value.get("uri") or "")})
    return sorted(projected, key=lambda item: repr(sorted(item.items())))
