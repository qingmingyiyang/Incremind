from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
import hashlib
import json
import re
from typing import Literal

from .contracts import (
    AIKernelContractError,
    TERMINAL_EVENT_TYPES,
    validate_governed_payload,
    validate_model_call_receipt,
    validate_model_dispatch_authority_receipt,
    validate_model_wire_attempt_dispatch,
    validate_model_wire_attempt_receipt,
    validate_prompt_cache_receipt,
)
from .tool_invocation import attempt_failure_from_payload, intent_from_payload


ExecutionProjectionView = Literal["simple", "developer"]
_TURN_STATUSES = {"accepted", "running", "waiting_approval", "completed", "failed", "cancelled"}
_CONTEXT_EVENTS = {"turn.accepted", "turn.resumed", "context.resolved"}
_MODEL_EVENTS = {"model.requested", "model.routed", "model.completed", "model.failed", "model.cancelled", "model.timed_out"}
_MODEL_TERMINAL_EVENTS = {"model.completed", "model.failed", "model.cancelled", "model.timed_out"}
_MODEL_ATTEMPT_EVENTS = {"model.attempt.dispatched", "model.attempt.terminal"}
_TOOL_EVENTS = {
    "tool.requested", "tool.intent.recorded", "tool.dispatch.claimed", "tool.started",
    "tool.attempt.failed", "tool.outcome.recorded", "tool.completed", "tool.failed",
    "tool.cancelled", "turn.cancel.requested",
}
_APPROVAL_EVENTS = {"approval.required", "approval.resolved"}
_STAGE_LABELS = {
    "context": "准备项目资料",
    "planning": "规划下一步",
    "tool": "处理资料",
    "approval": "确认执行权限",
    "result": "整理任务结果",
}
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_ROUTING_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_ROUTING_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


def build_execution_projection(
    events: Sequence[Mapping[str, object]],
    *,
    view: ExecutionProjectionView,
    payload_loader: Callable[[str], object] | None = None,
) -> dict[str, object]:
    if view not in {"simple", "developer"}:
        raise AIKernelContractError("execution projection view is unsupported")
    normalized = _validated_events(events)
    last = normalized[-1]
    last_data = _data(last)
    status = last_data.get("status")
    if status not in _TURN_STATUSES:
        raise AIKernelContractError("execution projection status is invalid")
    tool_steps = _tool_steps(normalized, payload_loader)
    unknown_effect = any(item["status"] == "unknown_effect" for item in tool_steps)
    current_stage, next_action = _current_stage(
        str(last["type"]), str(status), unknown_effect, tool_steps,
    )
    projection: dict[str, object] = {
        "schema_version": "1.0.0",
        "turn_id": str(last["turn_id"]),
        "view": view,
        "status": status,
        "current_sequence": int(last["sequence"]),
        "updated_at": str(last["occurred_at"]),
        "terminal": last["type"] in TERMINAL_EVENT_TYPES,
        "current_stage": current_stage,
        "stages": _stages(normalized, tool_steps, current_stage),
        "next_action": next_action,
        "expert": _expert_projection(normalized, payload_loader),
    }
    if view == "developer":
        projection.update({
            "model_steps": _model_steps(normalized, payload_loader),
            "tool_steps": tool_steps,
            "diagnostics": [_diagnostic(event) for event in normalized],
        })
    return validate_execution_projection(projection)


def validate_execution_projection(value: Mapping[str, object]) -> dict[str, object]:
    """Validate the public shape in production without a filesystem schema dependency."""

    projection = dict(value)
    projection.setdefault("expert", None)
    view = projection.get("view")
    common = {
        "schema_version", "turn_id", "view", "status", "current_sequence",
        "updated_at", "terminal", "current_stage", "stages", "next_action", "expert",
    }
    expected = common | ({"model_steps", "tool_steps", "diagnostics"} if view == "developer" else set())
    if view not in {"simple", "developer"} or set(projection) != expected:
        raise AIKernelContractError("execution projection fields are invalid")
    if projection.get("schema_version") != "1.0.0" or projection.get("status") not in _TURN_STATUSES:
        raise AIKernelContractError("execution projection identity is invalid")
    if not isinstance(projection.get("turn_id"), str) or not projection["turn_id"]:
        raise AIKernelContractError("execution projection Turn identity is invalid")
    if not isinstance(projection.get("current_sequence"), int) or isinstance(projection["current_sequence"], bool) or projection["current_sequence"] < 1:
        raise AIKernelContractError("execution projection sequence is invalid")
    if not isinstance(projection.get("terminal"), bool):
        raise AIKernelContractError("execution projection state is invalid")
    _validate_datetime(projection.get("updated_at"), "execution projection updated time")
    _validate_current_stage(projection.get("current_stage"))
    stages = _sequence(projection.get("stages"), "execution projection stages")
    if not stages or len(stages) > 5:
        raise AIKernelContractError("execution projection stages are invalid")
    for stage in stages:
        _validate_stage(stage)
    if projection.get("next_action") not in {"none", "approve", "review", "retry_manually"}:
        raise AIKernelContractError("execution projection next action is invalid")
    _validate_expert_projection(projection.get("expert"))
    if view == "developer":
        for model in _sequence(projection.get("model_steps"), "execution projection model steps"):
            _validate_model_step(model)
        for tool in _sequence(projection.get("tool_steps"), "execution projection tool steps"):
            _validate_tool_step(tool)
        for diagnostic in _sequence(projection.get("diagnostics"), "execution projection diagnostics"):
            _validate_diagnostic(diagnostic)
    validate_governed_payload(projection)
    return projection


def _validated_events(
    events: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)) or not events:
        raise AIKernelContractError("execution projection requires Turn events")
    normalized: list[dict[str, object]] = []
    turn_id: str | None = None
    terminal_seen = False
    for expected, raw in enumerate(events, start=1):
        if not isinstance(raw, Mapping):
            raise AIKernelContractError("execution projection event is invalid")
        event = dict(raw)
        if event.get("sequence") != expected:
            raise AIKernelContractError("execution projection event sequence is invalid")
        current_turn_id = event.get("turn_id")
        if not isinstance(current_turn_id, str) or not current_turn_id:
            raise AIKernelContractError("execution projection Turn identity is invalid")
        if turn_id is None:
            turn_id = current_turn_id
            if event.get("type") != "turn.accepted":
                raise AIKernelContractError("execution projection must start with turn.accepted")
        elif current_turn_id != turn_id:
            raise AIKernelContractError("execution projection Turn identity drifted")
        if terminal_seen:
            raise AIKernelContractError("execution projection has events after terminal state")
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise AIKernelContractError("execution projection event type is invalid")
        if not isinstance(event.get("occurred_at"), str) or not event["occurred_at"]:
            raise AIKernelContractError("execution projection event time is invalid")
        _data(event)
        normalized.append(event)
        terminal_seen = event_type in TERMINAL_EVENT_TYPES
    return tuple(normalized)


def _current_stage(
    last_type: str,
    status: str,
    unknown_effect: bool,
    tool_steps: list[dict[str, object]],
) -> tuple[dict[str, object], str]:
    completed_tools = sum(item["status"] == "completed" for item in tool_steps)
    retries = sum(
        attempt["status"] == "retry_scheduled"
        for item in tool_steps
        for attempt in item["attempts"]  # type: ignore[union-attr]
    )
    detail = f"已完成 {completed_tools} 个工具步骤"
    if retries:
        detail = f"已安全重试 {retries} 次，{detail}"
    if unknown_effect:
        return {
            "kind": "review_required", "status": "review_required",
            "label": "操作结果需要核对", "detail": "系统没有确认副作用结果",
        }, "review"
    if last_type == "turn.completed":
        return {"kind": "completed", "status": "completed", "label": "任务已完成", "detail": detail}, "none"
    if last_type == "turn.cancelled":
        return {"kind": "cancelled", "status": "cancelled", "label": "任务已取消", "detail": detail}, "none"
    if last_type == "turn.failed":
        return {"kind": "failed", "status": "failed", "label": "任务执行失败", "detail": "可以查看诊断后决定是否重试"}, "retry_manually"
    if status == "waiting_approval" or last_type == "approval.required":
        return {"kind": "waiting_approval", "status": "waiting", "label": "等待你的确认", "detail": "确认后继续执行当前步骤"}, "approve"
    if last_type in _MODEL_EVENTS | _MODEL_ATTEMPT_EVENTS:
        return {"kind": "planning", "status": "running", "label": "正在规划下一步", "detail": detail}, "none"
    if last_type in _TOOL_EVENTS or last_type == "approval.resolved":
        return {"kind": "using_tool", "status": "running", "label": "正在处理资料", "detail": detail}, "none"
    return {"kind": "preparing", "status": "running", "label": "正在准备项目资料", "detail": detail}, "none"


def _stages(
    events: tuple[dict[str, object], ...],
    tool_steps: list[dict[str, object]],
    current_stage: Mapping[str, object],
) -> list[dict[str, object]]:
    event_types = {str(event["type"]) for event in events}
    result: list[dict[str, object]] = []
    if event_types & _CONTEXT_EVENTS:
        result.append(_stage("context", "completed" if "context.resolved" in event_types else "running", int("context.resolved" in event_types)))
    if event_types & (_MODEL_EVENTS | _MODEL_ATTEMPT_EVENTS):
        requested = sum(event["type"] == "model.requested" for event in events)
        completed = sum(event["type"] == "model.completed" for event in events)
        model_failed = bool(event_types & {"model.failed", "model.cancelled", "model.timed_out"})
        result.append(_stage("planning", "failed" if model_failed else ("completed" if requested == completed else "running"), completed))
    if event_types & _TOOL_EVENTS:
        status = "running"
        if current_stage["kind"] == "review_required":
            status = "review_required"
        elif tool_steps and all(item["status"] in {"completed", "failed", "cancelled"} for item in tool_steps):
            status = "completed"
        result.append(_stage("tool", status, sum(item["status"] == "completed" for item in tool_steps)))
    if event_types & _APPROVAL_EVENTS:
        waiting = events[-1]["type"] == "approval.required"
        result.append(_stage("approval", "waiting" if waiting else "completed", sum(event["type"] == "approval.resolved" for event in events)))
    if event_types & TERMINAL_EVENT_TYPES:
        result_status = current_stage["status"]
        result.append(_stage("result", str(result_status), 1 if events[-1]["type"] == "turn.completed" else 0))
    return result


def _stage(kind: str, status: str, completed_count: int) -> dict[str, object]:
    return {"kind": kind, "status": status, "label": _STAGE_LABELS[kind], "completed_count": completed_count}


def _expert_projection(
    events: tuple[dict[str, object], ...],
    payload_loader: Callable[[str], object] | None,
) -> dict[str, object] | None:
    selection = None
    receipt = None
    event_types = {str(event["type"]) for event in events}
    for event in events:
        if event["type"] == "expert.selection.recorded":
            selection = _load_expert_event_payload(event, payload_loader)
        elif event["type"] == "expert.execution.receipted":
            receipt = _load_expert_event_payload(event, payload_loader)
    if not isinstance(selection, Mapping):
        return None
    selected = selection.get("selected")
    if not isinstance(selected, Mapping):
        return None
    expert_id = selected.get("expert_id")
    reason = selected.get("reason")
    reason_labels = {
        "explicit_request": "由你明确指定",
        "project_default_binding": "这是当前项目的默认专家",
        "project_default": "项目默认专家配置",
    }
    if not isinstance(expert_id, str) or not expert_id or len(expert_id) > 128:
        return None
    phase = "selected"
    if "expert.binding.frozen" in event_types:
        phase = "bound"
    if event_types & {"tool.requested", "tool.started", "tool.completed"}:
        phase = "executing"
    if "expert.job.waiting" in event_types:
        phase = "awaiting_job"
    if "expert.job.observed" in event_types:
        phase = "grounded"
    if "expert.execution.receipted" in event_types:
        phase = "completed"
    evidence_count = 0
    receipt_status = "pending"
    if isinstance(receipt, Mapping):
        refs = receipt.get("input_evidence_refs")
        evidence_count = min(
            999,
            len(refs) if isinstance(refs, list) else 0,
        )
        receipt_status = "completed" if receipt.get("status") == "completed" else "pending"
    return {
        "selected_expert": expert_id,
        "selection_reason": reason_labels.get(reason, "由项目规则选择"),
        "current_phase": phase,
        "evidence_source_count": evidence_count,
        "receipt_status": receipt_status,
    }


def _validate_expert_projection(value: object) -> None:
    if value is None:
        return
    expert = _strict_mapping(
        value,
        {
            "selected_expert", "selection_reason", "current_phase",
            "evidence_source_count", "receipt_status",
        },
        "execution projection expert",
    )
    _validate_text(expert["selected_expert"], "execution projection expert id", maximum=128)
    _validate_text(expert["selection_reason"], "execution projection expert reason", maximum=120)
    if expert["current_phase"] not in {
        "selected", "bound", "executing", "awaiting_job", "grounded", "completed",
    }:
        raise AIKernelContractError("execution projection expert phase is invalid")
    _validate_int(
        expert["evidence_source_count"],
        "execution projection expert evidence count",
        minimum=0,
    )
    if expert["evidence_source_count"] > 999:
        raise AIKernelContractError("execution projection expert evidence count is invalid")
    if expert["receipt_status"] not in {"pending", "completed"}:
        raise AIKernelContractError("execution projection expert receipt status is invalid")


def _load_expert_event_payload(
    event: Mapping[str, object],
    payload_loader: Callable[[str], object] | None,
) -> object:
    data = _data(event)
    direct = _load_payload(data.get("payload_ref"), payload_loader)
    if direct is not None:
        return direct
    for reference in _refs(data.get("evidence_refs")):
        payload = _load_payload(reference, payload_loader)
        if payload is not None:
            return payload
    return None


def _model_steps(
    events: tuple[dict[str, object], ...],
    payload_loader: Callable[[str], object] | None,
) -> list[dict[str, object]]:
    steps: dict[str, dict[str, object]] = {}
    routing_bindings: dict[str, dict[str, str]] = {}
    routing_sequences: dict[str, int] = {}
    terminal_sequences: dict[str, int] = {}
    parent_bindings: dict[str, tuple[str | None, bool]] = {}
    attempt_events: dict[str, list[Mapping[str, object]]] = {}
    for event in events:
        if event["type"] not in _MODEL_EVENTS | _MODEL_ATTEMPT_EVENTS:
            continue
        correlation = _correlation(event)
        request_id = correlation.get("model_request_id")
        if not isinstance(request_id, str) or not request_id:
            continue
        if event["type"] in _MODEL_ATTEMPT_EVENTS:
            attempt_events.setdefault(request_id, []).append(event)
            continue
        item = steps.setdefault(request_id, {
            "step_id": correlation.get("step_id") if isinstance(correlation.get("step_id"), str) else None,
            "parent_tool_call_id": None,
            "model_request_id": request_id,
            "status": "incomplete",
            "requested_at": None,
            "completed_at": None,
            "metadata_status": "not_recorded",
            "receipt_status": "not_recorded",
            "provider_id": None,
            "model_id": None,
            "usage_status": "not_recorded",
            "usage": None,
            "routing_status": "not_recorded",
            "routing": None,
            "prompt_cache_status": "not_recorded",
            "prompt_cache": None,
            "dispatch_authority_status": "not_recorded",
            "dispatch_authority": None,
            "wire_attempts_status": "not_recorded",
            "wire_attempts": [],
            "recorded": {"provider_id": False, "model_id": False, "usage": False, "input": False, "output": False},
        })
        parent_tool_call_id, parent_is_valid = _model_parent_tool_call_id(correlation)
        prior_parent = parent_bindings.get(request_id)
        if prior_parent is None:
            parent_bindings[request_id] = (parent_tool_call_id, parent_is_valid)
        elif not parent_is_valid or parent_tool_call_id != prior_parent[0]:
            parent_bindings[request_id] = (prior_parent[0], False)
        if event["type"] == "model.requested":
            item["status"] = "requested"
            item["requested_at"] = event["occurred_at"]
        elif event["type"] == "model.routed":
            binding = _apply_model_routing_snapshot(item, event, payload_loader)
            if binding is not None:
                routing_bindings[request_id] = binding
                routing_sequences[request_id] = int(event["sequence"])
        elif event["type"] in _MODEL_TERMINAL_EVENTS:
            terminal_sequences[request_id] = int(event["sequence"])
            item["status"] = str(event["type"]).removeprefix("model.")
            item["completed_at"] = event["occurred_at"]
            _apply_model_receipt(item, event, payload_loader)
            _apply_prompt_cache_receipt(item, event, payload_loader, routing_bindings.get(request_id))
            _apply_model_dispatch_authority_receipt(item, event, payload_loader)
    for request_id, (parent_tool_call_id, consistent) in parent_bindings.items():
        if consistent:
            steps[request_id]["parent_tool_call_id"] = parent_tool_call_id
    for request_id, item in steps.items():
        records = attempt_events.get(request_id)
        if records:
            _apply_model_wire_attempts(
                item,
                records,
                payload_loader,
                routing_bindings.get(request_id),
                route_sequence=routing_sequences.get(request_id),
                logical_terminal_sequence=terminal_sequences.get(request_id),
            )
    return list(steps.values())


def _apply_model_wire_attempts(
    item: dict[str, object],
    events: Sequence[Mapping[str, object]],
    payload_loader: Callable[[str], object] | None,
    routing_binding: Mapping[str, str] | None,
    *,
    route_sequence: int | None,
    logical_terminal_sequence: int | None,
) -> None:
    """Expose completed wire attempts only when their durable chain is intact.

    An empty historical list means *not recorded*, never that the provider was
    not contacted.  New attempts additionally bind to the frozen route, so a
    receipt from a different request or route cannot become developer-visible.
    """
    if routing_binding is None or route_sequence is None:
        return
    dispatched: dict[str, tuple[int, dict[str, object]]] = {}
    terminal_ids: set[str] = set()
    projected: list[dict[str, object]] = []
    invalid = False
    request_id = item["model_request_id"]
    for event in events:
        sequence = int(event["sequence"])
        if sequence <= route_sequence or (
            logical_terminal_sequence is not None
            and sequence >= logical_terminal_sequence
        ):
            invalid = True
            break
        correlation = _correlation(event)
        if correlation.get("model_request_id") != request_id:
            invalid = True
            break
        if event["type"] == "model.attempt.dispatched":
            try:
                dispatch = validate_model_wire_attempt_dispatch(
                    _load_payload(_data(event).get("payload_ref"), payload_loader)
                )
            except AIKernelContractError:
                invalid = True
                break
            if not _attempt_matches_event(dispatch, event, request_id) or dispatch["attempt_id"] in dispatched:
                invalid = True
                break
            dispatched[dispatch["attempt_id"]] = (sequence, dispatch)
            continue
        try:
            receipt = validate_model_wire_attempt_receipt(_load_payload(_data(event).get("receipt_ref"), payload_loader))
        except AIKernelContractError:
            invalid = True
            break
        attempt_id = receipt["attempt_id"]
        dispatch_record = dispatched.get(attempt_id)
        if (
            dispatch_record is None or attempt_id in terminal_ids
            or dispatch_record[0] >= sequence
            or not _attempt_matches_event(receipt, event, request_id)
            or not _attempt_binding_matches(dispatch_record[1], receipt, str(event["turn_id"]), routing_binding)
        ):
            invalid = True
            break
        terminal_ids.add(attempt_id)
        projected.append({
            "attempt_number": receipt["attempt_number"],
            "status": receipt["status"],
            "provider_id": receipt["provider_id"],
            "model_id": receipt["model_id"],
            "execution_location": receipt.get("execution_location"),
            "started_at": receipt["started_at"],
            "completed_at": receipt["completed_at"],
            "duration_ms": receipt["duration_ms"],
            "usage_status": receipt["usage_status"],
            "usage": receipt["usage"],
            "cache_status": receipt["cache_status"],
            "cache_metadata": receipt["cache_metadata"],
            "error_code": receipt["error_code"],
        })
    if invalid or not dispatched or set(dispatched) != terminal_ids:
        return
    numbers = [attempt["attempt_number"] for attempt in projected]
    if len(numbers) != len(set(numbers)) or numbers != sorted(numbers):
        return
    item["wire_attempts_status"] = "recorded"
    item["wire_attempts"] = projected


def _attempt_matches_event(attempt: Mapping[str, object], event: Mapping[str, object], request_id: object) -> bool:
    return attempt.get("turn_id") == event.get("turn_id") and attempt.get("model_request_id") == request_id


def _attempt_binding_matches(
    dispatch: Mapping[str, object], receipt: Mapping[str, object], turn_id: str, routing_binding: Mapping[str, str],
) -> bool:
    fields = (
        "attempt_id", "turn_id", "model_request_id", "attempt_number",
        "routing_snapshot_revision", "provider_id", "model_id", "execution_location",
    )
    return (
        all(dispatch.get(field) == receipt.get(field) for field in fields)
        and dispatch.get("turn_id") == turn_id
        and dispatch.get("provider_id") == routing_binding["provider_id"]
        and dispatch.get("model_id") == routing_binding["model_id"]
        and dispatch.get("routing_snapshot_revision") == routing_binding["routing_snapshot_revision"]
        and (
            dispatch.get("execution_location") is None
            or dispatch.get("execution_location") == routing_binding["execution_location"]
        )
    )


def _model_parent_tool_call_id(correlation: Mapping[str, object]) -> tuple[str | None, bool]:
    """Return a safe parent association only when the event value is valid.

    A model lifecycle may be planner-owned (no parent) or nested under one
    tool call. The caller compares this value across every model event before
    it becomes developer-visible, so a partial or drifted correlation cannot
    falsely associate a model call with a tool execution.
    """
    value = correlation.get("tool_call_id")
    if value is None:
        return None, True
    if isinstance(value, str) and 0 < len(value) <= 128:
        return value, True
    return None, False


def _apply_model_routing_snapshot(
    item: dict[str, object],
    event: Mapping[str, object],
    payload_loader: Callable[[str], object] | None,
) -> dict[str, str] | None:
    """Project only the selected frozen route from a model.routed event."""
    resolved = _safe_route_from_snapshot(
        _load_payload(_data(event).get("payload_ref"), payload_loader),
        turn_id=str(event["turn_id"]),
        event_step_id=_correlation(event).get("step_id"),
        item_step_id=item["step_id"],
    )
    if resolved is not None:
        route, binding = resolved
        item["routing_status"] = "recorded"
        item["routing"] = route
        return binding
    return None


def _safe_route_from_snapshot(
    value: object,
    *,
    turn_id: str,
    event_step_id: object,
    item_step_id: object,
) -> tuple[dict[str, object], dict[str, str]] | None:
    """Validate a layer-safe slice of the frozen snapshot before display.

    Projections must remain readable during recovery without importing backend
    configuration services.  This verifies the Turn binding and the complete
    selected-route shape, then exposes only route metadata safe for developers.
    """
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "turn", "project", "profile", "boundary", "requirement",
        "routing", "registry", "runtime", "activation", "tiers", "selected",
        "catalog_revision", "prompt_cache_scope",
    }:
        return None
    if value.get("schema_version") != "1.0.0" or event_step_id != item_step_id:
        return None
    snapshot_turn = value.get("turn")
    selected = value.get("selected")
    if not isinstance(snapshot_turn, Mapping) or set(snapshot_turn) != {"turn_id"} or snapshot_turn.get("turn_id") != turn_id:
        return None
    if not isinstance(selected, Mapping) or set(selected) != {
        "tier", "route_key", "route_revision", "provider_id", "provider_revision",
        "model_name", "adapter_kind", "execution_location", "reason",
    }:
        return None
    tier = selected.get("tier")
    provider_id = selected.get("provider_id")
    model_name = selected.get("model_name")
    adapter_kind = selected.get("adapter_kind")
    execution_location = selected.get("execution_location")
    route_revision = selected.get("route_revision")
    if (
        tier not in {"fast", "standard", "deep", "vision"}
        or not isinstance(provider_id, str) or not _ROUTING_IDENTIFIER.fullmatch(provider_id)
        or not isinstance(model_name, str) or not _ROUTING_MODEL.fullmatch(model_name)
        or adapter_kind not in {"openai-compatible", "openai-compatible-vision"}
        or execution_location not in {"local_loopback", "remote"}
        or not isinstance(route_revision, int) or isinstance(route_revision, bool) or route_revision < 1
    ):
        return None
    route = {
        "tier": tier,
        "provider_id": provider_id,
        "model_id": model_name,
        "adapter_kind": adapter_kind,
        "route_revision": route_revision,
    }
    scope = value.get("prompt_cache_scope")
    if not isinstance(scope, Mapping) or not isinstance(scope.get("identity"), str) or not re.fullmatch(r"[a-f0-9]{64}", scope["identity"]):
        return None
    revision = hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return route, {
        "provider_id": provider_id,
        "model_id": model_name,
        "execution_location": execution_location,
        "routing_snapshot_revision": revision,
        "prompt_cache_scope_identity": scope["identity"],
    }


def _apply_model_receipt(
    item: dict[str, object],
    event: Mapping[str, object],
    payload_loader: Callable[[str], object] | None,
) -> None:
    data = _data(event)
    receipt = _load_payload(data.get("receipt_ref"), payload_loader)
    try:
        validated = validate_model_call_receipt(receipt)
    except AIKernelContractError:
        return
    correlation = _correlation(event)
    if (
        validated["turn_id"] != event.get("turn_id")
        or validated["model_request_id"] != correlation.get("model_request_id")
        or validated["status"] != item["status"]
    ):
        return
    usage_status = validated["usage_status"]
    item.update({
        "metadata_status": "recorded",
        "receipt_status": "recorded",
        "provider_id": validated["provider_id"],
        "model_id": validated["model_id"],
        "usage_status": usage_status,
        "usage": validated["usage"],
        "recorded": {
            "provider_id": validated["provider_id"] is not None,
            "model_id": validated["model_id"] is not None,
            "usage": usage_status == "recorded",
            "input": False,
            "output": False,
        },
    })


def _apply_prompt_cache_receipt(
    item: dict[str, object],
    event: Mapping[str, object],
    payload_loader: Callable[[str], object] | None,
    routing_binding: Mapping[str, str] | None,
) -> None:
    """Expose provider-reported cache counters only after all bindings match."""
    if routing_binding is None:
        return
    data = _data(event)
    correlation = _correlation(event)
    model_receipt = _load_payload(data.get("receipt_ref"), payload_loader)
    try:
        call = validate_model_call_receipt(model_receipt)
    except AIKernelContractError:
        return
    for reference in _refs(data.get("evidence_refs")):
        try:
            receipt = validate_prompt_cache_receipt(_load_payload(reference, payload_loader))
        except AIKernelContractError:
            continue
        if (
            receipt["turn_id"] != event.get("turn_id")
            or receipt["model_request_id"] != correlation.get("model_request_id")
            or receipt["provider_id"] != call["provider_id"]
            or receipt["model_id"] != call["model_id"]
            or receipt["provider_id"] != routing_binding["provider_id"]
            or receipt["model_id"] != routing_binding["model_id"]
            or receipt["routing_snapshot_revision"] != routing_binding["routing_snapshot_revision"]
            or receipt["prompt_cache_scope_identity"] != routing_binding["prompt_cache_scope_identity"]
        ):
            continue
        item["prompt_cache_status"] = "recorded"
        item["prompt_cache"] = {
            "status": receipt["cache_status"],
            "source": receipt["source_format"],
            "cache_read_input_tokens": receipt["cache_read_input_tokens"],
            "cache_write_input_tokens": receipt["cache_write_input_tokens"],
            "uncached_input_tokens": receipt["uncached_input_tokens"],
        }
        return


def _apply_model_dispatch_authority_receipt(
    item: dict[str, object],
    event: Mapping[str, object],
    payload_loader: Callable[[str], object] | None,
) -> None:
    """Expose a durable local-fence observation only when it binds to this call."""

    correlation = _correlation(event)
    for reference in _refs(_data(event).get("evidence_refs")):
        try:
            receipt = validate_model_dispatch_authority_receipt(
                _load_payload(reference, payload_loader)
            )
        except AIKernelContractError:
            continue
        if (
            receipt["turn_id"] != event.get("turn_id")
            or receipt["model_request_id"] != correlation.get("model_request_id")
        ):
            continue
        item["dispatch_authority_status"] = "recorded"
        item["dispatch_authority"] = {
            "wait_ms": receipt["wait_ms"],
            "hold_ms": receipt["hold_ms"],
            "outcome": receipt["outcome"],
        }
        return


def _tool_steps(
    events: tuple[dict[str, object], ...],
    payload_loader: Callable[[str], object] | None,
) -> list[dict[str, object]]:
    steps: dict[str, dict[str, object]] = {}
    attempt_maps: dict[str, dict[int, dict[str, object]]] = {}
    for event in events:
        correlation = _correlation(event)
        call_id = correlation.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        data = _data(event)
        item = steps.setdefault(call_id, _empty_tool(call_id, correlation, data))
        attempts = attempt_maps.setdefault(call_id, {})
        capability_id = data.get("capability_id")
        if isinstance(capability_id, str):
            item["capability_id"] = capability_id
        event_type = str(event["type"])
        payload = _load_payload(data.get("payload_ref"), payload_loader)
        if event_type == "tool.requested":
            item["status"] = "requested"
            _apply_boundary(item, payload)
        elif event_type == "approval.required":
            item["status"] = "waiting_approval"
        elif event_type == "approval.resolved":
            item["status"] = "running"
        elif event_type in {"tool.dispatch.claimed", "tool.started"}:
            item["status"] = "running"
            if event_type == "tool.started":
                attempt = len(attempts) + 1
                attempts.setdefault(attempt, _empty_attempt(attempt))
        elif event_type == "tool.intent.recorded":
            _apply_intent(item, payload, call_id, str(event["turn_id"]))
        elif event_type == "tool.attempt.failed":
            _apply_attempt_failure(item, attempts, payload, data, call_id, str(event["turn_id"]))
        elif event_type == "tool.outcome.recorded":
            _apply_outcome(item, attempts, payload, data, call_id, str(event["turn_id"]))
        elif event_type == "tool.completed":
            item["status"] = "completed"
            item["payload_available"] = isinstance(data.get("payload_ref"), str)
            item["receipt_available"] = isinstance(data.get("receipt_ref"), str)
            item["evidence_count"] = len(_refs(data.get("evidence_refs")))
        elif event_type == "tool.failed" and item["status"] != "unknown_effect":
            item["status"] = "failed"
        elif event_type == "tool.cancelled":
            item["status"] = "cancelled"
    for call_id, item in steps.items():
        item["attempts"] = [attempt_maps[call_id][key] for key in sorted(attempt_maps[call_id])]
    return list(steps.values())


def _empty_tool(call_id: str, correlation: Mapping[str, object], data: Mapping[str, object]) -> dict[str, object]:
    return {
        "step_id": correlation.get("step_id") if isinstance(correlation.get("step_id"), str) else None,
        "tool_call_id": call_id,
        "capability_id": data.get("capability_id") if isinstance(data.get("capability_id"), str) else None,
        "status": "requested",
        "execution_mode": "not_recorded",
        "idempotency": "not_recorded",
        "timeout_ms": None,
        "attempts": [],
        "boundary": {"status": "not_recorded", "requires_receipt": False, "redaction_required": False, "reason_codes": []},
        "receipt_available": False,
        "evidence_count": 0,
        "payload_available": False,
    }


def _empty_attempt(attempt: int) -> dict[str, object]:
    return {"attempt": attempt, "status": "running", "error_code": None, "retryable": False, "effect_certainty": "not_recorded", "backoff_ms": None}


def _apply_boundary(item: dict[str, object], payload: object) -> None:
    if not isinstance(payload, Mapping):
        return
    if set(payload) != {"schema_version", "outcome", "reason_codes", "matched_grant_ids", "policy_revision", "requires_receipt", "redaction_required"}:
        return
    if payload.get("schema_version") != "1.0.0":
        return
    status = payload.get("outcome")
    reasons = payload.get("reason_codes")
    if status not in {"allow", "allow_redacted", "ask", "deny"}:
        return
    item["boundary"] = {
        "status": status,
        "requires_receipt": payload.get("requires_receipt") is True,
        "redaction_required": payload.get("redaction_required") is True,
        "reason_codes": list(_reason_codes(reasons, limit=32)),
    }


def _apply_intent(item: dict[str, object], payload: object, call_id: str, turn_id: str) -> None:
    try:
        intent = intent_from_payload(payload)
    except (AIKernelContractError, KeyError, TypeError, ValueError):
        return
    if intent.invocation_id != call_id or intent.turn_id != turn_id or intent.capability_id != item["capability_id"]:
        return
    item["execution_mode"] = intent.execution_mode
    item["idempotency"] = intent.idempotency
    item["timeout_ms"] = intent.timeout_ms


def _apply_attempt_failure(
    item: dict[str, object],
    attempts: dict[int, dict[str, object]],
    payload: object,
    data: Mapping[str, object],
    call_id: str,
    turn_id: str,
) -> None:
    try:
        failure = attempt_failure_from_payload(payload)
    except (AIKernelContractError, KeyError, TypeError, ValueError):
        return
    if failure.invocation_id != call_id or failure.turn_id != turn_id or failure.capability_id != item["capability_id"]:
        return
    attempt = failure.attempt
    current = attempts.setdefault(attempt, _empty_attempt(attempt))
    current.update({
        "status": "retry_scheduled",
        "error_code": _safe_error_code(failure.error_code),
        "retryable": data.get("retryable") is True,
        "effect_certainty": "confirmed_none",
        "backoff_ms": failure.backoff_ms,
    })
    item["status"] = "retrying"


def _apply_outcome(
    item: dict[str, object],
    attempts: dict[int, dict[str, object]],
    payload: object,
    data: Mapping[str, object],
    call_id: str,
    turn_id: str,
) -> None:
    if not isinstance(payload, Mapping):
        return
    allowed_fields = {"schema_version", "invocation_id", "turn_id", "capability_id", "attempt", "status", "effect_certainty", "payload_ref", "receipt_ref", "evidence_refs", "error_code", "retryable"}
    historical_fields = allowed_fields - {"effect_certainty"}
    if frozenset(payload) not in {frozenset(allowed_fields), frozenset(historical_fields)}:
        return
    if payload.get("schema_version") != "1.0.0" or payload.get("invocation_id") != call_id or payload.get("turn_id") != turn_id or payload.get("capability_id") != item["capability_id"]:
        return
    attempt = payload.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or not 1 <= attempt <= 10:
        return
    status = payload.get("status")
    if status not in {"completed", "failed", "cancelled", "timed_out", "unknown_effect"}:
        return
    certainty = payload.get("effect_certainty")
    if certainty not in {"confirmed_none", "confirmed_applied", "unknown"}:
        certainty = "not_recorded"
    current = attempts.setdefault(attempt, _empty_attempt(attempt))
    current.update({
        "status": status,
        "error_code": _error_code(payload, data),
        "retryable": payload.get("retryable") is True,
        "effect_certainty": certainty,
        "backoff_ms": None,
    })
    item["status"] = "unknown_effect" if status == "unknown_effect" or certainty == "unknown" else ("failed" if status == "timed_out" else status)
    item["receipt_available"] = isinstance(payload.get("receipt_ref"), str)
    item["payload_available"] = isinstance(payload.get("payload_ref"), str)
    item["evidence_count"] = len(_refs(payload.get("evidence_refs")))


def _diagnostic(event: Mapping[str, object]) -> dict[str, object]:
    data = _data(event)
    return {
        "sequence": int(event["sequence"]),
        "event_type": str(event["type"]),
        "actor": str(event.get("actor") or "system")[:32],
        "occurred_at": str(event["occurred_at"]),
        "error_code": data.get("error_code") if isinstance(data.get("error_code"), str) else None,
    }


def _data(event: Mapping[str, object]) -> dict[str, object]:
    data = event.get("data")
    if not isinstance(data, Mapping):
        raise AIKernelContractError("execution projection event data is invalid")
    return dict(data)


def _correlation(event: Mapping[str, object]) -> dict[str, object]:
    value = event.get("correlation")
    return dict(value) if isinstance(value, Mapping) else {}


def _load_payload(value: object, loader: Callable[[str], object] | None) -> object:
    if not isinstance(value, str) or loader is None:
        return None
    try:
        return loader(value)
    except (KeyError, OSError, TypeError, ValueError):
        return None


def _refs(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.startswith("crp://"))[:256]


def _reason_codes(value: object, *, limit: int) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and _REASON_CODE.fullmatch(item) and item not in result:
            result.append(item)
        if len(result) == limit:
            break
    return tuple(result)


def _error_code(payload: object, data: Mapping[str, object]) -> str | None:
    value = payload.get("error_code") if isinstance(payload, Mapping) else None
    if not isinstance(value, str):
        value = data.get("error_code")
    return _safe_error_code(value)


def _safe_error_code(value: object) -> str | None:
    return value if isinstance(value, str) and _REASON_CODE.fullmatch(value) else None


def _bounded_int(value: object, minimum: int, maximum: int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum:
        return value
    return None


def _validate_current_stage(value: object) -> None:
    stage = _strict_mapping(value, {"kind", "status", "label", "detail"}, "execution projection current stage")
    if stage["kind"] not in {"preparing", "planning", "using_tool", "waiting_approval", "completed", "failed", "cancelled", "review_required"}:
        raise AIKernelContractError("execution projection current stage kind is invalid")
    _validate_stage_status(stage["status"])
    _validate_text(stage["label"], "execution projection stage label", maximum=80)
    _validate_text(stage["detail"], "execution projection stage detail", maximum=160)


def _validate_stage(value: object) -> None:
    stage = _strict_mapping(value, {"kind", "status", "label", "completed_count"}, "execution projection stage")
    if stage["kind"] not in _STAGE_LABELS:
        raise AIKernelContractError("execution projection stage kind is invalid")
    _validate_stage_status(stage["status"])
    _validate_text(stage["label"], "execution projection stage label", maximum=80)
    _validate_int(stage["completed_count"], "execution projection completed count", minimum=0)


def _validate_model_step(value: object) -> None:
    base_fields = {"step_id", "parent_tool_call_id", "model_request_id", "status", "requested_at", "completed_at", "metadata_status", "receipt_status", "provider_id", "model_id", "usage_status", "usage", "routing_status", "routing", "prompt_cache_status", "prompt_cache", "wire_attempts_status", "wire_attempts", "recorded"}
    extended_fields = base_fields | {"dispatch_authority_status", "dispatch_authority"}
    if not isinstance(value, Mapping) or (set(value) != base_fields and set(value) != extended_fields):
        raise AIKernelContractError("execution projection model step fields are invalid")
    step = dict(value)
    _validate_optional_text(step["step_id"], "execution projection model step id", maximum=128)
    _validate_optional_text(step["parent_tool_call_id"], "execution projection model parent tool call id", maximum=128)
    _validate_text(step["model_request_id"], "execution projection model request id", maximum=128)
    if step["status"] not in {"requested", "completed", "failed", "cancelled", "timed_out", "incomplete"}:
        raise AIKernelContractError("execution projection model status is invalid")
    if step["metadata_status"] not in {"recorded", "not_recorded"} or step["receipt_status"] not in {"recorded", "not_recorded"}:
        raise AIKernelContractError("execution projection model receipt availability is invalid")
    _validate_optional_datetime(step["requested_at"], "execution projection model requested time")
    _validate_optional_datetime(step["completed_at"], "execution projection model completed time")
    _validate_optional_text(step["provider_id"], "execution projection provider id", maximum=128)
    _validate_optional_text(step["model_id"], "execution projection model id", maximum=128)
    if step["usage_status"] not in {"recorded", "not_recorded"}:
        raise AIKernelContractError("execution projection model usage status is invalid")
    _validate_model_usage(step["usage"], recorded=step["usage_status"] == "recorded")
    if step["routing_status"] not in {"recorded", "not_recorded"}:
        raise AIKernelContractError("execution projection routing availability is invalid")
    _validate_model_routing(step["routing"], recorded=step["routing_status"] == "recorded")
    if step["prompt_cache_status"] not in {"recorded", "not_recorded"}:
        raise AIKernelContractError("execution projection cache receipt availability is invalid")
    _validate_prompt_cache(step["prompt_cache"], recorded=step["prompt_cache_status"] == "recorded")
    if "dispatch_authority_status" in step:
        if step["dispatch_authority_status"] not in {"recorded", "not_recorded"}:
            raise AIKernelContractError("execution projection dispatch authority availability is invalid")
        _validate_dispatch_authority(
            step["dispatch_authority"],
            recorded=step["dispatch_authority_status"] == "recorded",
        )
    if step["wire_attempts_status"] not in {"recorded", "not_recorded"}:
        raise AIKernelContractError("execution projection wire attempt availability is invalid")
    attempts = _sequence(step["wire_attempts"], "execution projection model wire attempts")
    if len(attempts) > 100:
        raise AIKernelContractError("execution projection model wire attempts are invalid")
    for attempt in attempts:
        _validate_model_wire_attempt(attempt)
    if step["wire_attempts_status"] == "not_recorded" and attempts:
        raise AIKernelContractError("execution projection cannot infer absent model wire attempts")
    if step["wire_attempts_status"] == "recorded" and not attempts:
        raise AIKernelContractError("execution projection recorded model wire attempts are empty")
    recorded = _strict_mapping(step["recorded"], {"provider_id", "model_id", "usage", "input", "output"}, "execution projection model recorded flags")
    if any(not isinstance(item, bool) for item in recorded.values()):
        raise AIKernelContractError("execution projection model recorded flag is invalid")
    if recorded["input"] or recorded["output"]:
        raise AIKernelContractError("execution projection cannot expose model input or output")
    if step["receipt_status"] == "not_recorded" and (
        step["metadata_status"] != "not_recorded" or step["provider_id"] is not None
        or step["model_id"] is not None or step["usage_status"] != "not_recorded"
        or step["usage"] is not None or any(recorded.values())
    ):
        raise AIKernelContractError("execution projection cannot infer an absent model receipt")


def _validate_dispatch_authority(value: object, *, recorded: bool) -> None:
    if not recorded:
        if value is not None:
            raise AIKernelContractError("unrecorded dispatch authority observation must be null")
        return
    observation = _strict_mapping(
        value,
        {"wait_ms", "hold_ms", "outcome"},
        "execution projection model dispatch authority",
    )
    for field in ("wait_ms", "hold_ms"):
        _validate_int(
            observation[field],
            f"execution projection dispatch authority {field}",
            minimum=0,
            maximum=86_400_000,
        )
    if observation["outcome"] not in {"completed", "failed", "timed_out"}:
        raise AIKernelContractError("execution projection dispatch authority outcome is invalid")


def _validate_model_wire_attempt(value: object) -> None:
    required = {
        "attempt_number", "status", "provider_id", "model_id", "execution_location",
        "started_at", "completed_at",
        "duration_ms", "usage_status", "usage", "cache_status", "cache_metadata", "error_code",
    }
    if not isinstance(value, Mapping):
        raise AIKernelContractError("execution projection model wire attempt must be an object")
    actual = {str(key) for key in value}
    legacy_required = required - {"execution_location"}
    if actual != required and actual != legacy_required:
        raise AIKernelContractError("execution projection model wire attempt fields are invalid")
    attempt = dict(value)
    _validate_int(attempt["attempt_number"], "execution projection model wire attempt number", minimum=1, maximum=10_000)
    if attempt["status"] not in {"succeeded", "failed_transport", "consumer_cancelled"}:
        raise AIKernelContractError("execution projection model wire attempt status is invalid")
    _validate_text(attempt["provider_id"], "execution projection model wire provider", maximum=128)
    _validate_text(attempt["model_id"], "execution projection model wire model", maximum=128)
    if attempt.get("execution_location") not in {None, "remote", "local_loopback"}:
        raise AIKernelContractError("execution projection model wire location is invalid")
    _validate_datetime(attempt["started_at"], "execution projection model wire started time")
    _validate_datetime(attempt["completed_at"], "execution projection model wire completed time")
    _validate_int(attempt["duration_ms"], "execution projection model wire duration", minimum=0, maximum=86_400_000)
    if attempt["usage_status"] not in {"reported", "unavailable"}:
        raise AIKernelContractError("execution projection model wire usage status is invalid")
    _validate_model_usage(attempt["usage"], recorded=attempt["usage_status"] == "reported")
    if attempt["cache_status"] not in {"reported", "unavailable"}:
        raise AIKernelContractError("execution projection model wire cache status is invalid")
    _validate_attempt_cache_metadata(attempt["cache_metadata"], recorded=attempt["cache_status"] == "reported")
    error_code = _safe_error_code(attempt["error_code"])
    if attempt["status"] == "succeeded" and attempt["error_code"] is not None:
        raise AIKernelContractError("execution projection succeeded model wire attempt cannot have error")
    if attempt["status"] != "succeeded" and error_code is None:
        raise AIKernelContractError("execution projection failed model wire attempt requires error")
    if attempt["status"] == "consumer_cancelled" and error_code != "ai.consumer_cancelled":
        raise AIKernelContractError("execution projection cancelled model wire attempt error is invalid")


def _validate_attempt_cache_metadata(value: object, *, recorded: bool) -> None:
    if not recorded:
        if value is not None:
            raise AIKernelContractError("execution projection unrecorded model wire cache must be null")
        return
    cache = _strict_mapping(value, {
        "source_format", "cache_read_input_tokens", "cache_write_input_tokens", "uncached_input_tokens",
    }, "execution projection model wire cache")
    if cache["source_format"] != "provider_usage":
        raise AIKernelContractError("execution projection model wire cache source is invalid")
    counts = tuple(cache[key] for key in ("cache_read_input_tokens", "cache_write_input_tokens", "uncached_input_tokens"))
    for count in counts:
        if count is not None:
            _validate_int(count, "execution projection model wire cache tokens", minimum=0, maximum=2_147_483_647)
    if all(count is None for count in counts):
        raise AIKernelContractError("execution projection model wire cache is invalid")


def _validate_model_routing(value: object, *, recorded: bool) -> None:
    if not recorded:
        if value is not None:
            raise AIKernelContractError("execution projection unrecorded route must be null")
        return
    route = _strict_mapping(value, {"tier", "provider_id", "model_id", "adapter_kind", "route_revision"}, "execution projection model route")
    if route["tier"] not in {"fast", "standard", "deep", "vision"}:
        raise AIKernelContractError("execution projection model route tier is invalid")
    _validate_text(route["provider_id"], "execution projection model route provider", maximum=160)
    _validate_text(route["model_id"], "execution projection model route model", maximum=200)
    if route["adapter_kind"] not in {"openai-compatible", "openai-compatible-vision"}:
        raise AIKernelContractError("execution projection model route adapter is invalid")
    _validate_int(route["route_revision"], "execution projection model route revision", minimum=1)


def _validate_prompt_cache(value: object, *, recorded: bool) -> None:
    if not recorded:
        if value is not None:
            raise AIKernelContractError("execution projection unrecorded prompt cache must be null")
        return
    cache = _strict_mapping(value, {
        "status", "source", "cache_read_input_tokens", "cache_write_input_tokens", "uncached_input_tokens",
    }, "execution projection prompt cache")
    if cache["status"] not in {"reported", "unavailable"} or cache["source"] not in {"provider_usage", "unavailable"}:
        raise AIKernelContractError("execution projection prompt cache status is invalid")
    for count in (cache["cache_read_input_tokens"], cache["cache_write_input_tokens"], cache["uncached_input_tokens"]):
        if count is not None:
            _validate_int(count, "execution projection prompt cache tokens", minimum=0)
    if cache["status"] == "reported":
        if cache["source"] != "provider_usage" or all(count is None for count in (cache["cache_read_input_tokens"], cache["cache_write_input_tokens"], cache["uncached_input_tokens"])):
            raise AIKernelContractError("execution projection reported prompt cache is invalid")
    elif cache["source"] != "unavailable" or any(count is not None for count in (cache["cache_read_input_tokens"], cache["cache_write_input_tokens"], cache["uncached_input_tokens"])):
        raise AIKernelContractError("execution projection unavailable prompt cache is invalid")


def _validate_model_usage(value: object, *, recorded: bool) -> None:
    if not recorded:
        if value is not None:
            raise AIKernelContractError("execution projection unrecorded model usage must be null")
        return
    usage = _strict_mapping(value, {"input_tokens", "output_tokens", "total_tokens"}, "execution projection model usage")
    for count in usage.values():
        _validate_int(count, "execution projection model usage", minimum=0, maximum=2_147_483_647)


def _validate_tool_step(value: object) -> None:
    tool = _strict_mapping(value, {"step_id", "tool_call_id", "capability_id", "status", "execution_mode", "idempotency", "timeout_ms", "attempts", "boundary", "receipt_available", "evidence_count", "payload_available"}, "execution projection tool step")
    _validate_optional_text(tool["step_id"], "execution projection tool step id", maximum=128)
    _validate_text(tool["tool_call_id"], "execution projection tool call id", maximum=128)
    _validate_optional_text(tool["capability_id"], "execution projection capability id", maximum=128)
    if tool["status"] not in {"requested", "waiting_approval", "running", "retrying", "completed", "failed", "cancelled", "unknown_effect"}:
        raise AIKernelContractError("execution projection tool status is invalid")
    if tool["execution_mode"] not in {"parallel", "exclusive", "not_recorded"}:
        raise AIKernelContractError("execution projection execution mode is invalid")
    if tool["idempotency"] not in {"idempotent", "verify_before_retry", "never_retry", "not_recorded"}:
        raise AIKernelContractError("execution projection idempotency is invalid")
    if tool["timeout_ms"] is not None:
        _validate_int(tool["timeout_ms"], "execution projection timeout", minimum=1, maximum=3_600_000)
    attempts = _sequence(tool["attempts"], "execution projection attempts")
    if len(attempts) > 10:
        raise AIKernelContractError("execution projection attempts are invalid")
    for attempt in attempts:
        _validate_attempt(attempt)
    _validate_boundary(tool["boundary"])
    if not isinstance(tool["receipt_available"], bool) or not isinstance(tool["payload_available"], bool):
        raise AIKernelContractError("execution projection payload availability is invalid")
    _validate_int(tool["evidence_count"], "execution projection evidence count", minimum=0, maximum=256)


def _validate_attempt(value: object) -> None:
    attempt = _strict_mapping(value, {"attempt", "status", "error_code", "retryable", "effect_certainty", "backoff_ms"}, "execution projection attempt")
    _validate_int(attempt["attempt"], "execution projection attempt number", minimum=1, maximum=10)
    if attempt["status"] not in {"running", "retry_scheduled", "completed", "failed", "cancelled", "timed_out", "unknown_effect"}:
        raise AIKernelContractError("execution projection attempt status is invalid")
    if attempt["error_code"] is not None and _safe_error_code(attempt["error_code"]) is None:
        raise AIKernelContractError("execution projection error code is invalid")
    if not isinstance(attempt["retryable"], bool):
        raise AIKernelContractError("execution projection retry flag is invalid")
    if attempt["effect_certainty"] not in {"confirmed_none", "confirmed_applied", "unknown", "not_recorded"}:
        raise AIKernelContractError("execution projection effect certainty is invalid")
    if attempt["backoff_ms"] is not None:
        _validate_int(attempt["backoff_ms"], "execution projection retry backoff", minimum=0, maximum=300_000)


def _validate_boundary(value: object) -> None:
    boundary = _strict_mapping(value, {"status", "requires_receipt", "redaction_required", "reason_codes"}, "execution projection boundary")
    if boundary["status"] not in {"allow", "allow_redacted", "ask", "deny", "not_recorded"}:
        raise AIKernelContractError("execution projection boundary status is invalid")
    if not isinstance(boundary["requires_receipt"], bool) or not isinstance(boundary["redaction_required"], bool):
        raise AIKernelContractError("execution projection boundary flags are invalid")
    reasons = _sequence(boundary["reason_codes"], "execution projection boundary reasons")
    if len(reasons) > 32 or len(set(reasons)) != len(reasons) or any(not isinstance(item, str) or not _REASON_CODE.fullmatch(item) for item in reasons):
        raise AIKernelContractError("execution projection boundary reasons are invalid")


def _validate_diagnostic(value: object) -> None:
    diagnostic = _strict_mapping(value, {"sequence", "event_type", "actor", "occurred_at", "error_code"}, "execution projection diagnostic")
    _validate_int(diagnostic["sequence"], "execution projection diagnostic sequence", minimum=1)
    _validate_text(diagnostic["event_type"], "execution projection diagnostic event type", maximum=128)
    _validate_text(diagnostic["actor"], "execution projection diagnostic actor", maximum=32)
    _validate_datetime(diagnostic["occurred_at"], "execution projection diagnostic time")
    if diagnostic["error_code"] is not None and _safe_error_code(diagnostic["error_code"]) is None:
        raise AIKernelContractError("execution projection diagnostic error is invalid")


def _validate_stage_status(value: object) -> None:
    if value not in {"pending", "running", "waiting", "completed", "failed", "cancelled", "review_required"}:
        raise AIKernelContractError("execution projection stage status is invalid")


def _strict_mapping(value: object, fields: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise AIKernelContractError(f"{label} fields are invalid")
    return dict(value)


def _sequence(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AIKernelContractError(f"{label} must be an array")
    return tuple(value)


def _validate_text(value: object, label: str, *, maximum: int) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AIKernelContractError(f"{label} is invalid")


def _validate_optional_text(value: object, label: str, *, maximum: int) -> None:
    if value is not None:
        _validate_text(value, label, maximum=maximum)


def _validate_datetime(value: object, label: str) -> None:
    _validate_text(value, label, maximum=80)
    assert isinstance(value, str)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AIKernelContractError(f"{label} is invalid") from error
    if parsed.tzinfo is None:
        raise AIKernelContractError(f"{label} must include a timezone")


def _validate_optional_datetime(value: object, label: str) -> None:
    if value is not None:
        _validate_datetime(value, label)


def _validate_int(value: object, label: str, *, minimum: int, maximum: int | None = None) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or (maximum is not None and value > maximum):
        raise AIKernelContractError(f"{label} is invalid")
