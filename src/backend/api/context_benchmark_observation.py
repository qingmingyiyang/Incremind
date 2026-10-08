from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import re

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
from core.context_graph import TurnModelObservation


class ContextBenchmarkObservationError(ValueError):
    pass


_BENCHMARK_OPERATION_ID = re.compile(
    r"^op-lm-(?P<suite>[A-Za-z0-9._-]{1,40})-"
    r"(?P<case>project_skill|document|research_turn)-"
    r"r(?P<replicate>[0-9]{1,4})-(?P<variant>linear|linemap)$"
)


@dataclass(frozen=True, slots=True)
class BenchmarkTurnIdentity:
    suite_run_id: str
    case_id: str
    variant: str
    replicate_index: int
    turn_id: str
    operation_id: str


def context_benchmark_turn_identity(
    request: Mapping[str, object],
) -> tuple[dict[str, object], BenchmarkTurnIdentity]:
    try:
        durable_request = validate_turn_request(request)
    except (AIKernelContractError, TypeError, ValueError) as error:
        raise ContextBenchmarkObservationError(
            "benchmark durable Turn request is invalid"
        ) from error
    turn_id = _required_text(durable_request.get("turn_id"), "turn id")
    operation_id = _required_text(
        durable_request.get("operation_id"),
        "operation id",
    )
    identity = _BENCHMARK_OPERATION_ID.fullmatch(operation_id)
    if identity is None or durable_request.get("desired_outcome") != "context.evaluate":
        raise ContextBenchmarkObservationError("benchmark Turn identity is invalid")
    variant = identity.group("variant")
    _validate_variant_input(durable_request, variant)
    return durable_request, BenchmarkTurnIdentity(
        suite_run_id=identity.group("suite"),
        case_id=identity.group("case"),
        variant=variant,
        replicate_index=int(identity.group("replicate")),
        turn_id=turn_id,
        operation_id=operation_id,
    )


def context_benchmark_observation_from_turn(
    *,
    request: Mapping[str, object],
    events: Sequence[Mapping[str, object]],
    payload_loader: Callable[[str], object],
    capability_revision: str,
    compiler_revision: str,
    decoding_revision: str,
) -> TurnModelObservation:
    durable_request, identity = context_benchmark_turn_identity(request)
    turn_id = identity.turn_id
    capability_revision = _required_text(capability_revision, "capability revision")
    compiler_revision = _required_text(compiler_revision, "compiler revision")
    decoding_revision = _required_text(decoding_revision, "decoding revision")
    model_events = [event for event in events if event.get("type") == "model.completed"]
    terminal_events = [event for event in events if event.get("type") == "turn.completed"]
    if len(model_events) != 1 or len(terminal_events) != 1:
        raise ContextBenchmarkObservationError(
            "benchmark Turn must contain one completed model call and one terminal event"
        )
    model_event, terminal = model_events[0], terminal_events[0]
    if model_event.get("turn_id") != turn_id or terminal.get("turn_id") != turn_id:
        raise ContextBenchmarkObservationError("benchmark Turn event identity drifted")
    model_data = _mapping(model_event.get("data"), "model event data")
    terminal_data = _mapping(terminal.get("data"), "terminal event data")
    model_correlation = _correlation(model_event, "model completed")
    terminal_correlation = _correlation(terminal, "turn completed")
    model_request_id = _correlation_text(
        model_correlation, "model_request_id", "model completed"
    )
    if (
        _correlation_text(model_correlation, "step_id", "model completed")
        != _correlation_text(terminal_correlation, "step_id", "turn completed")
        or model_request_id
        != _correlation_text(terminal_correlation, "model_request_id", "turn completed")
    ):
        raise ContextBenchmarkObservationError(
            "benchmark model and terminal correlation drifted"
        )
    receipt_ref = _session_ref(model_data.get("receipt_ref"), turn_id, "model receipt ref")
    try:
        receipt = validate_model_call_receipt(payload_loader(receipt_ref))
    except (AIKernelContractError, KeyError, TypeError, ValueError) as error:
        raise ContextBenchmarkObservationError("benchmark model Receipt is invalid") from error
    if (
        receipt.get("turn_id") != turn_id
        or receipt.get("status") != "completed"
        or receipt.get("usage_status") != "recorded"
    ):
        raise ContextBenchmarkObservationError("benchmark model Receipt is incomplete")
    routing_ref, routing = _routing_snapshot(
        model_data.get("evidence_refs"), turn_id=turn_id, payload_loader=payload_loader,
    )
    routing_snapshot_revision = turn_model_routing_snapshot_revision(routing)
    selected = _mapping(routing.get("selected"), "routing selection")
    boundary = _mapping(routing.get("boundary"), "routing Boundary")
    requirement = _mapping(routing.get("requirement"), "routing requirement")
    requested_refs = _routing_input_refs(durable_request)
    if (
        receipt.get("model_request_id") != model_request_id
        or receipt.get("provider_id") != selected.get("provider_id")
        or receipt.get("model_id") != selected.get("model_name")
        or requested_refs != requirement.get("input_refs")
    ):
        raise ContextBenchmarkObservationError("benchmark model routing evidence drifted")
    attempt = _completed_wire_attempt(
        model_data.get("evidence_refs"),
        turn_id=turn_id,
        model_request_id=model_request_id,
        routing_snapshot_revision=routing_snapshot_revision,
        provider_id=_required_text(selected.get("provider_id"), "selected provider"),
        model_name=_required_text(selected.get("model_name"), "selected model"),
        execution_location=_required_text(
            selected.get("execution_location"), "selected execution location",
        ),
        payload_loader=payload_loader,
    )
    usage = _mapping(receipt.get("usage"), "model usage")
    summary = terminal_data.get("summary")
    terminal_event_id = terminal.get("event_id")
    if not isinstance(summary, str) or not summary.strip() or not isinstance(terminal_event_id, str):
        raise ContextBenchmarkObservationError("benchmark terminal output is unavailable")
    return TurnModelObservation(
        suite_run_id=identity.suite_run_id,
        replicate_index=identity.replicate_index,
        operation_id=identity.operation_id,
        case_id=identity.case_id,
        variant=identity.variant,
        turn_id=turn_id,
        turn_terminal_event_id=terminal_event_id,
        model_receipt_ref=receipt_ref,
        routing_snapshot_ref=routing_ref,
        model_request_id=model_request_id,
        model_attempt_id=str(attempt["attempt_id"]),
        routing_snapshot_revision=routing_snapshot_revision,
        status="completed",
        output_text=summary.strip(),
        input_tokens=int(usage["input_tokens"]),
        output_tokens=int(usage["output_tokens"]),
        total_tokens=int(usage["total_tokens"]),
        route_key=str(selected["route_key"]),
        route_revision=str(selected["route_revision"]),
        provider_id=str(selected["provider_id"]),
        provider_revision=str(selected["provider_revision"]),
        model_name=str(selected["model_name"]),
        execution_location=str(attempt["execution_location"]),
        capability_revision=capability_revision,
        compiler_revision=compiler_revision,
        boundary_revision=str(boundary["profile_revision"]),
        decoding_revision=decoding_revision,
    )


def _validate_variant_input(request: Mapping[str, object], variant: str) -> None:
    turn_input = _mapping(request.get("input"), "benchmark Turn input")
    refs = turn_input.get("refs")
    if not isinstance(refs, list):
        raise ContextBenchmarkObservationError("benchmark Turn refs are invalid")
    if variant == "linear" and refs:
        raise ContextBenchmarkObservationError("linear benchmark Turn must not use refs")
    if variant == "linemap":
        if len(refs) != 1 or not isinstance(refs[0], Mapping):
            raise ContextBenchmarkObservationError(
                "LineMap benchmark Turn requires one ContextBinding ref"
            )
        ref = refs[0]
        if (
            ref.get("kind") != "context_binding"
            or not isinstance(ref.get("uri"), str)
            or not str(ref["uri"]).startswith("crp://context-bindings/")
        ):
            raise ContextBenchmarkObservationError(
                "LineMap benchmark ContextBinding ref is invalid"
            )


def _routing_snapshot(
    value: object, *, turn_id: str, payload_loader: Callable[[str], object],
) -> tuple[str, Mapping[str, object]]:
    if not isinstance(value, list):
        raise ContextBenchmarkObservationError("benchmark model evidence refs are invalid")
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
        raise ContextBenchmarkObservationError(
            "benchmark Routing Snapshot is unavailable or ambiguous"
        )
    return matches[0]


def _completed_wire_attempt(
    value: object,
    *,
    turn_id: str,
    model_request_id: str,
    routing_snapshot_revision: str,
    provider_id: str,
    model_name: str,
    execution_location: str,
    payload_loader: Callable[[str], object],
) -> Mapping[str, object]:
    if not isinstance(value, list):
        raise ContextBenchmarkObservationError("benchmark model evidence refs are invalid")
    prefix = f"crp://session/{turn_id}/model-wire-attempt-receipt/"
    refs = [item for item in value if isinstance(item, str) and item.startswith(prefix)]
    if len(refs) != 1:
        raise ContextBenchmarkObservationError(
            "benchmark completed wire attempt Receipt is unavailable or ambiguous"
        )
    try:
        receipt = validate_model_wire_attempt_receipt(payload_loader(refs[0]))
    except (AIKernelContractError, KeyError, TypeError, ValueError) as error:
        raise ContextBenchmarkObservationError(
            "benchmark completed wire attempt Receipt is invalid"
        ) from error
    if (
        receipt.get("status") != "succeeded"
        or receipt.get("turn_id") != turn_id
        or receipt.get("model_request_id") != model_request_id
        or receipt.get("routing_snapshot_revision") != routing_snapshot_revision
        or receipt.get("provider_id") != provider_id
        or receipt.get("model_id") != model_name
        or receipt.get("execution_location") != execution_location
    ):
        raise ContextBenchmarkObservationError(
            "benchmark completed wire attempt Receipt drifted"
        )
    return receipt


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContextBenchmarkObservationError(f"{label} must be an object")
    return value


def _correlation(event: Mapping[str, object], label: str) -> Mapping[str, object]:
    return _mapping(event.get("correlation"), f"{label} correlation")


def _correlation_text(
    correlation: Mapping[str, object], field: str, label: str,
) -> str:
    value = correlation.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ContextBenchmarkObservationError(
            f"benchmark {label} correlation {field} is invalid"
        )
    return value


def _session_ref(value: object, turn_id: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith(f"crp://session/{turn_id}/"):
        raise ContextBenchmarkObservationError(f"{label} is invalid")
    return value


def _routing_input_refs(request: Mapping[str, object]) -> list[dict[str, str]]:
    turn_input = _mapping(request.get("input"), "benchmark Turn input")
    values = turn_input.get("refs")
    if not isinstance(values, list):
        raise ContextBenchmarkObservationError("benchmark Turn refs are invalid")
    projected: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise ContextBenchmarkObservationError("benchmark Turn ref is invalid")
        if value.get("object_id") is not None:
            projected.append({
                key: str(value[key])
                for key in sorted(value)
                if key != "uri"
            })
        else:
            projected.append({"ref": str(value.get("uri") or "")})
    return sorted(projected, key=lambda item: json.dumps(item, sort_keys=True))


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextBenchmarkObservationError(f"benchmark {label} is invalid")
    return value
