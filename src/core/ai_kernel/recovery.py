from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from .contracts import validate_governed_payload, validate_model_wire_attempt_dispatch, validate_model_wire_attempt_receipt
from .ports import RecoveryDecision
from .tool_invocation import intent_from_payload, outcome_from_payload


_TERMINAL = {"turn.completed", "turn.failed", "turn.cancelled"}
_MODEL_TERMINAL = {"model.completed", "model.failed", "model.cancelled", "model.timed_out"}
_MODEL_ROUTED = "model.routed"
_MODEL_ATTEMPT_DISPATCHED = "model.attempt.dispatched"
_MODEL_ATTEMPT_TERMINAL = "model.attempt.terminal"
_KNOWN = {
    "turn.accepted", "context.resolved", "model.requested", "model.result.discarded", _MODEL_ROUTED, *_MODEL_TERMINAL,
    _MODEL_ATTEMPT_DISPATCHED, _MODEL_ATTEMPT_TERMINAL,
    "hook.invoked",
    "tool.requested", "tool.intent.recorded", "tool.dispatch.claimed",
    "tool.started", "tool.attempt.failed", "tool.outcome.recorded",
    "tool.completed", "tool.failed", "tool.cancelled", "approval.required",
    "mcp.continuation.required",
    "expert.selection.recorded", "expert.binding.frozen", "expert.job.waiting",
    "expert.job.terminal", "expert.job.observed", "expert.execution.receipted",
    "expert.memory.proposal.intent.recorded", "expert.memory.proposed",
    "approval.resolved", "turn.resumed", "turn.cancel.requested", *_TERMINAL,
}


def classify_recovery(
    turn_id: str,
    generation: int,
    events: Sequence[Mapping[str, object]],
    *,
    payload_loader: Callable[[str], object] | None = None,
) -> RecoveryDecision:
    if not events:
        return RecoveryDecision(turn_id, generation, "quarantine", "ai.recovery_event_missing", 0, "", "")
    last = events[-1]
    last_type = str(last.get("type", ""))
    last_sequence = last.get("sequence") if isinstance(last.get("sequence"), int) else 0
    last_id = str(last.get("event_id", ""))
    if not _stream_is_well_formed(turn_id, events):
        return RecoveryDecision(turn_id, generation, "quarantine", "ai.recovery_event_identity_unknown", last_sequence, last_id, last_type)
    if not _model_lifecycle_is_safe(events, payload_loader):
        return RecoveryDecision(turn_id, generation, "quarantine", "ai.recovery_model_incomplete", last_sequence, last_id, last_type)
    # A durable MCP continuation deliberately has tool.started without an
    # outcome. Its exact waiting event is the fence: startup must preserve it
    # and must never reinterpret it as an ordinary resumable intent.
    if last_type == "mcp.continuation.required" and _waiting_event_is_valid(last, events):
        return RecoveryDecision(
            turn_id, generation, "waiting_noop", "ai.recovery_waiting_mcp_continuation",
            last_sequence, last_id, last_type,
        )
    outcome_events = [event for event in events if event.get("type") == "tool.outcome.recorded"]
    if not _tool_lifecycles_are_safe(events, payload_loader):
        return RecoveryDecision(turn_id, generation, "quarantine", "ai.recovery_tool_effect_unknown", last_sequence, last_id, last_type)
    started_ids = {
        item for item in (_tool_id(event) for event in events if event.get("type") == "tool.started")
        if item is not None
    }
    if not _outcomes_are_safe(turn_id, outcome_events, payload_loader, started_ids):
        return RecoveryDecision(turn_id, generation, "quarantine", "ai.recovery_tool_outcome_unsafe", last_sequence, last_id, last_type)
    if last_type in _TERMINAL:
        return RecoveryDecision(turn_id, generation, "terminal_noop", "ai.recovery_terminal", last_sequence, last_id, last_type)
    if last_type == "approval.required" and _waiting_event_is_valid(last, events):
        return RecoveryDecision(turn_id, generation, "waiting_noop", "ai.recovery_waiting_approval", last_sequence, last_id, last_type)
    if last_type == "expert.job.waiting" and _expert_job_waiting_event_is_valid(last, events):
        return RecoveryDecision(
            turn_id, generation, "waiting_noop", "ai.recovery_waiting_expert_job",
            last_sequence, last_id, last_type,
        )
    return RecoveryDecision(turn_id, generation, "safe_resume", "ai.recovery_no_effect_started", last_sequence, last_id, last_type)


def _stream_is_well_formed(turn_id: str, events: Sequence[Mapping[str, object]]) -> bool:
    event_ids: set[str] = set()
    first_session_id = events[0].get("session_id")
    if not isinstance(first_session_id, str) or not first_session_id:
        return False
    for expected_sequence, event in enumerate(events, start=1):
        event_id = event.get("event_id")
        if (
            event.get("type") not in _KNOWN
            or not isinstance(event.get("sequence"), int)
            or isinstance(event.get("sequence"), bool)
            or event.get("sequence") != expected_sequence
            or not isinstance(event_id, str)
            or not event_id
            or event_id in event_ids
            or event.get("turn_id") != turn_id
            or event.get("session_id") != first_session_id
        ):
            return False
        event_ids.add(event_id)
    first_data = events[0].get("data")
    if events[0].get("type") != "turn.accepted" or not isinstance(first_data, Mapping) or first_data.get("status") != "accepted":
        return False
    for event in events[:-1]:
        if event.get("type") in _TERMINAL:
            return False
    if events[-1].get("type") in _TERMINAL:
        terminal_status = {
            "turn.completed": "completed", "turn.failed": "failed", "turn.cancelled": "cancelled",
        }[str(events[-1]["type"])]
        data = events[-1].get("data")
        if not isinstance(data, Mapping) or data.get("status") != terminal_status:
            return False
    return _approval_lifecycle_is_well_formed(events)


def _approval_lifecycle_is_well_formed(events: Sequence[Mapping[str, object]]) -> bool:
    pending_capability: str | None = None
    pending_kind: str | None = None
    for event in events:
        event_type = event.get("type")
        if event_type not in {"approval.required", "mcp.continuation.required", "approval.resolved"}:
            continue
        data = event.get("data")
        if not isinstance(data, Mapping):
            return False
        capability_id = data.get("capability_id")
        if event_type in {"approval.required", "mcp.continuation.required"}:
            if (
                pending_capability is not None
                or data.get("status") != "waiting_approval"
                or not isinstance(capability_id, str)
                or not capability_id
                or not isinstance(data.get("payload_ref"), str)
                or _tool_id(event) is None
            ):
                return False
            pending_capability = capability_id
            pending_kind = str(event_type)
        elif (
            pending_capability is None
            or capability_id != pending_capability
            or data.get("status") != "running"
            or data.get("summary") not in (
                {"mcp_continue", "mcp_reject"}
                if pending_kind == "mcp.continuation.required"
                else {"approve", "reject"}
            )
            or not isinstance(data.get("payload_ref"), str)
        ):
            return False
        else:
            pending_capability = None
            pending_kind = None
    return True


def _tool_lifecycles_are_safe(
    events: Sequence[Mapping[str, object]],
    payload_loader: Callable[[str], object] | None = None,
) -> bool:
    tool_types = {
        "tool.requested", "tool.intent.recorded", "tool.dispatch.claimed",
        "tool.started", "tool.attempt.failed", "tool.outcome.recorded",
        "tool.completed", "tool.failed", "tool.cancelled",
    }
    by_call: dict[str, list[tuple[int, str]]] = {}
    for position, event in enumerate(events):
        event_type = str(event.get("type", ""))
        if event_type not in tool_types:
            continue
        tool_call_id = _tool_id(event)
        if tool_call_id is None:
            return False
        by_call.setdefault(tool_call_id, []).append((position, event_type))
    final_types = {"tool.completed", "tool.failed", "tool.cancelled"}
    for tool_call_id, lifecycle in by_call.items():
        positions: dict[str, list[int]] = {}
        for position, event_type in lifecycle:
            positions.setdefault(event_type, []).append(position)
        if len(positions.get("tool.requested", ())) != 1:
            return False
        for event_type in {"tool.intent.recorded", "tool.outcome.recorded"}:
            if len(positions.get(event_type, ())) > 1:
                return False
        dispatches = positions.get("tool.dispatch.claimed", [])
        starts = positions.get("tool.started", [])
        if (
            len(dispatches) > 2
            or len(starts) > len(dispatches)
            or len(dispatches) - len(starts) > 1
        ):
            return False
        if len(dispatches) == 2:
            # A continuation dispatch without its durable start marker is not
            # automatically resumable; only the complete two-attempt shape is
            # recognized here.
            if len(starts) != 2:
                return False
            first_started, second_dispatch = starts[0], dispatches[1]
            attempt_failures = positions.get("tool.attempt.failed", [])
            waiting_positions = [
                index for index, event in enumerate(events)
                if event.get("type") == "mcp.continuation.required"
                and _tool_id(event) == tool_call_id
                and first_started < index < second_dispatch
            ]
            continued_positions = [
                index for index, event in enumerate(events)
                if event.get("type") == "approval.resolved"
                and _tool_id(event) == tool_call_id
                and isinstance(event.get("data"), Mapping)
                and event["data"].get("summary") == "mcp_continue"
                and first_started < index < second_dispatch
            ]
            if (
                len(attempt_failures) > 1
                or len(waiting_positions) != 1
                or len(continued_positions) != 1
                or not first_started < waiting_positions[0] < continued_positions[0] < second_dispatch
                or (
                    bool(attempt_failures)
                    and not first_started < attempt_failures[0] < waiting_positions[0]
                )
            ):
                return False
        elif "tool.attempt.failed" in positions:
            return False
        finals = sum((positions.get(event_type, []) for event_type in final_types), [])
        if len(finals) > 1:
            return False
        requested = positions["tool.requested"][0]
        intent = _single_position(positions, "tool.intent.recorded")
        dispatch = dispatches[0] if dispatches else None
        started = starts[-1] if starts else None
        outcome = _single_position(positions, "tool.outcome.recorded")
        if intent is not None and requested >= intent:
            return False
        if dispatch is not None and (intent is None or intent >= dispatch):
            return False
        if any(dispatch_position >= start_position for dispatch_position, start_position in zip(dispatches, starts)):
            return False
        if outcome is not None and (intent is None or intent >= outcome):
            return False
        if started is None and outcome is None and payload_loader is not None:
            # A result payload without its durable start marker is a damaged
            # chain, never permission to repeat an apparently unstarted call.
            for kind in ('tool-batch-buffer-v1', 'tool-batch-mcp-wait-v1'):
                try:
                    if payload_loader(f"crp://session/{events[0]['turn_id']}/{kind}-{tool_call_id}") is not None:
                        return False
                except (KeyError, ValueError):
                    pass
                except Exception:
                    return False
        if started is not None and outcome is None:
            if (intent is None or len(starts) != 1 or payload_loader is None
                or not _batch_dispatch_fact_is_safe(events[intent], events[started], events, payload_loader)):
                return False
        if started is not None and outcome is not None and started >= outcome:
            return False
        if finals and (outcome is None or outcome >= finals[0]):
            return False
        if intent is None and any(item is not None for item in (dispatch, started, outcome)):
            return False
    return True


def _buffered_result_is_valid(result: object, operation_semantics: object) -> bool:
    """Use the same success shape as normal Tool settlement, before projection."""
    if not isinstance(result, Mapping):
        return False
    try:
        validate_governed_payload(result)
    except Exception:
        return False
    receipt = result.get('receipt_ref')
    # Ordinary settlement ignores a non-reference value; it cannot serve as
    # proof of a write. Only the same canonical crp reference can do so here.
    receipt = receipt if isinstance(receipt, str) and receipt.startswith('crp://') else None
    operation_receipt = result.get('operation_receipt')
    return (not (receipt is not None and operation_receipt is not None)
            and (operation_semantics != 'receipt_required'
                 or receipt is not None or operation_receipt is not None))


def _batch_dispatch_fact_is_safe(intent_event, started_event, events, payload_loader) -> bool:
    """Recognize a returned RPC only through its complete frozen batch chain.

    This permits projection of an already returned result. It does not permit
    another dispatch, nor reinterpret a missing/unknown response as success.
    """
    try:
        intent_ref = intent_event['data']['payload_ref']
        intent = intent_from_payload(payload_loader(intent_ref))
        turn_id, call_id, step_id = intent.turn_id, intent.invocation_id, intent.step_id
        if any(event.get('turn_id') != turn_id or _tool_id(event) != call_id
               or _step_id(event) != step_id or event['data'].get('capability_id') != intent.capability_id
               or event['data'].get('payload_ref') != intent_ref
               or event['correlation'].get('operation_id') != intent.operation_id
               for event in (intent_event, started_event)):
            return False
        prefix = f'crp://session/{turn_id}/'
        batch = payload_loader(f'{prefix}tool-batch-v1-{step_id}')
        preflight = payload_loader(f'{prefix}tool-batch-preflight-v1-{call_id}')
        validate_governed_payload(batch)
        validate_governed_payload(preflight)
        if (not isinstance(batch, Mapping) or batch.get('schema_version') != '1.0.0'
            or batch.get('step_id') != step_id or not isinstance(batch.get('calls'), list)
            or not 1 <= len(batch['calls']) <= 16
            or not isinstance(preflight, Mapping)
            or preflight.get('_execution') != {'step_id': step_id, 'tool_call_id': call_id}
            or preflight.get('capability_id') != intent.capability_id
            or preflight.get('arguments') != dict(intent.arguments)
            or preflight.get('_tool_contract') != dict(intent.tool_contract or {})):
            return False
        members = [call for call in batch['calls'] if isinstance(call, Mapping)
                   and call.get('_execution') == {'step_id': step_id, 'tool_call_id': call_id}
                   and call.get('capability_id') == intent.capability_id and call.get('type') == 'tool']
        model_events = [event for event in events if event.get('type') == 'model.completed'
                        and _step_id(event) == step_id and _model_id(event) == batch.get('model_request_id')]
        if len(members) != 1 or len(model_events) != 1 or model_events[0]['sequence'] >= intent_event['sequence']:
            return False
        try:
            buffer = payload_loader(f'{prefix}tool-batch-buffer-v1-{call_id}')
        except (KeyError, ValueError):
            buffer = None
        if buffer is not None:
            validate_governed_payload(buffer)
            if (not isinstance(buffer, Mapping)
                or set(buffer) != {'schema_version', 'turn_id', 'invocation_id', 'intent_ref',
                                   'step_id', 'operation_id', 'attempt', 'dispatch'}
                or buffer.get('schema_version') != '1.0.0' or buffer.get('turn_id') != turn_id
                or buffer.get('invocation_id') != call_id or buffer.get('intent_ref') != intent_ref
                or buffer.get('step_id') != step_id or buffer.get('operation_id') != intent.operation_id
                or type(buffer.get('attempt')) is not int or buffer['attempt'] != 1):
                return False
            dispatch = buffer.get('dispatch')
            if not isinstance(dispatch, Mapping):
                return False
            if dispatch.get('kind') == 'result':
                return (set(dispatch) == {'kind', 'result'}
                        and _buffered_result_is_valid(dispatch['result'], (intent.tool_contract or {}).get('operation_semantics')))
            if dispatch.get('kind') in {'cancelled', 'timeout'}:
                return (set(dispatch) == {'kind', 'provider_started'}
                        and type(dispatch.get('provider_started')) is bool
                        and (not dispatch['provider_started'] or (intent.tool_contract or {}).get('effect') == 'read'))
            if dispatch.get('kind') != 'failure' or set(dispatch) != {
                'kind', 'error_code', 'effect_certainty', 'provider_started', 'retry_after_ms', 'request_state',
            }:
                return False
            if (type(dispatch.get('provider_started')) is not bool
                or not isinstance(dispatch.get('error_code'), str) or not dispatch['error_code']
                or dispatch.get('effect_certainty') != 'confirmed_none'
                or (dispatch.get('retry_after_ms') is not None
                    and (type(dispatch['retry_after_ms']) is not int or dispatch['retry_after_ms'] < 0))):
                return False
            return dispatch.get('request_state') is None
        queued = payload_loader(f'{prefix}tool-batch-mcp-wait-v1-{call_id}')
        validate_governed_payload(queued)
        if not isinstance(queued, Mapping) or set(queued) != {'payload', 'waiting'}:
            return False
        waiting, frozen = queued['waiting'], queued['payload']
        contract = intent.tool_contract or {}
        return (isinstance(waiting, Mapping) and isinstance(frozen, Mapping)
                and waiting.get('_kind') == 'mcp_request_state_v1'
                and waiting.get('_execution') == {'step_id': step_id, 'tool_call_id': call_id}
                and waiting.get('_tool_batch_step_id') == step_id
                and waiting.get('capability_id') == intent.capability_id
                and waiting.get('_mcp_intent_ref') == intent_ref
                and type(waiting.get('_mcp_round')) is int and waiting['_mcp_round'] == 1
                and frozen.get('schema_version') == '1.0.0' and frozen.get('intent_ref') == intent_ref
                and frozen.get('capability_id') == intent.capability_id
                and frozen.get('operation_id') == intent.operation_id
                and type(frozen.get('round')) is int and frozen['round'] == 1
                and frozen.get('connection') == dict(contract)
                and contract.get('source') == 'mcp' and contract.get('effect') == 'read'
                and isinstance(frozen.get('request_state'), str) and bool(frozen['request_state'])
                and len(frozen['request_state'].encode('utf-8')) <= 4096)
    except Exception:
        return False


def _single_position(positions: Mapping[str, Sequence[int]], event_type: str) -> int | None:
    values = positions.get(event_type, ())
    return values[0] if values else None


def _waiting_event_is_valid(
    event: Mapping[str, object],
    events: Sequence[Mapping[str, object]],
) -> bool:
    data = event.get("data")
    tool_call_id = _tool_id(event)
    if (
        not isinstance(data, Mapping)
        or data.get("status") != "waiting_approval"
        or not isinstance(data.get("capability_id"), str)
        or not isinstance(data.get("payload_ref"), str)
        or tool_call_id is None
    ):
        return False
    return any(
        prior.get("type") == "tool.requested" and _tool_id(prior) == tool_call_id
        for prior in events[:-1]
    )


def _expert_job_waiting_event_is_valid(
    event: Mapping[str, object],
    events: Sequence[Mapping[str, object]],
) -> bool:
    data = event.get("data")
    correlation = event.get("correlation")
    return (
        isinstance(data, Mapping)
        and data.get("status") == "waiting_job"
        and isinstance(data.get("payload_ref"), str)
        and bool(data.get("payload_ref"))
        and isinstance(correlation, Mapping)
        and correlation.get("tool_call_id") is None
        and any(prior.get("type") == "expert.binding.frozen" for prior in events[:-1])
    )


def _outcomes_are_safe(
    turn_id: str,
    events: Sequence[Mapping[str, object]],
    payload_loader: Callable[[str], object] | None,
    started_ids: set[str],
) -> bool:
    if not events:
        return True
    if payload_loader is None:
        return False
    for event in events:
        data = event.get("data")
        correlation = event.get("correlation")
        ref = data.get("payload_ref") if isinstance(data, Mapping) else None
        capability_id = data.get("capability_id") if isinstance(data, Mapping) else None
        tool_call_id = correlation.get("tool_call_id") if isinstance(correlation, Mapping) else None
        if not all(isinstance(value, str) and value for value in (ref, capability_id, tool_call_id)):
            return False
        try:
            outcome = outcome_from_payload(payload_loader(ref))
        except Exception:
            return False
        if (
            outcome.turn_id != turn_id
            or outcome.invocation_id != tool_call_id
            or outcome.capability_id != capability_id
            or outcome.effect_certainty == "unknown"
        ):
            return False
        if outcome.effect_certainty == "confirmed_applied" and (
            not outcome.receipt_ref or tool_call_id not in started_ids
        ):
            return False
    return True


def _model_id(event: Mapping[str, object]) -> str | None:
    value = event.get("correlation")
    return value.get("model_request_id") if isinstance(value, Mapping) and isinstance(value.get("model_request_id"), str) else None


def _model_lifecycle_is_safe(
    events: Sequence[Mapping[str, object]],
    payload_loader: Callable[[str], object] | None,
) -> bool:
    """Validate model intent without treating an uncertain egress as replayable.

    ``model.routed`` is a durable pre-egress marker.  Once a request exists,
    either a routed marker or a missing terminal means the process may already
    have crossed the provider boundary, so recovery must quarantine it.  A
    standalone routed marker is allowed to resume only as an incomplete
    pre-request record; it has no matching request or terminal and therefore
    cannot be mistaken for a completed provider call.
    """
    lifecycles: dict[str, dict[str, list[int]]] = {}
    lifecycle_events: dict[str, list[tuple[int, Mapping[str, object]]]] = {}
    model_types = {
        "model.requested", _MODEL_ROUTED, *_MODEL_TERMINAL,
        _MODEL_ATTEMPT_DISPATCHED, _MODEL_ATTEMPT_TERMINAL,
    }
    for position, event in enumerate(events):
        event_type = event.get("type")
        if event_type not in model_types:
            continue
        model_request_id = _model_id(event)
        if model_request_id is None:
            return False
        lifecycle = lifecycles.setdefault(model_request_id, {})
        lifecycle.setdefault(str(event_type), []).append(position)
        lifecycle_events.setdefault(model_request_id, []).append((position, event))

    for model_request_id, lifecycle in lifecycles.items():
        requested = lifecycle.get("model.requested", [])
        routed = lifecycle.get(_MODEL_ROUTED, [])
        terminals = [
            position
            for terminal_type in _MODEL_TERMINAL
            for position in lifecycle.get(terminal_type, [])
        ]
        if len(requested) > 1 or len(routed) > 1 or len(terminals) > 1:
            return False
        if terminals:
            if len(requested) != 1 or requested[0] >= terminals[0]:
                return False
        if routed and requested:
            if requested[0] >= routed[0]:
                return False
            if terminals and routed[0] >= terminals[0]:
                return False
        elif routed and terminals:
            # A terminal may never exist without a request, even if an
            # otherwise valid pre-egress routing record was written.
            return False
        # A request with no terminal is intentionally not safe to resume.
        # This preserves the no-auto-replay boundary for unknown egress.
        if requested and not terminals:
            return False
        if not _model_wire_attempts_are_safe(
            model_request_id,
            lifecycle_events[model_request_id],
            events,
            payload_loader,
            logical_terminal_position=terminals[0] if terminals else None,
        ):
            return False
        if not _nested_model_lifecycle_is_safe(
            lifecycle_events[model_request_id],
            events,
            payload_loader=payload_loader,
            terminal_position=terminals[0] if terminals else None,
        ):
            return False
    return True


def _model_wire_attempts_are_safe(
    model_request_id: str,
    lifecycle: Sequence[tuple[int, Mapping[str, object]]],
    events: Sequence[Mapping[str, object]],
    payload_loader: Callable[[str], object] | None,
    *,
    logical_terminal_position: int | None,
) -> bool:
    """Fail closed around every durable provider-boundary marker.

    Attempt identity and route binding intentionally live in immutable payloads,
    rather than in the public event envelope.  This scanner therefore loads
    both sides of a dispatched/terminal pair before deciding that a stale turn
    can resume.  A missing terminal is treated as unknown provider effect and
    is never replayed.
    """
    attempt_events = [
        (position, event)
        for position, event in lifecycle
        if event.get("type") in {_MODEL_ATTEMPT_DISPATCHED, _MODEL_ATTEMPT_TERMINAL}
    ]
    if not attempt_events:
        return True
    if payload_loader is None or logical_terminal_position is None:
        return False

    routed_positions = [
        position for position, event in lifecycle if event.get("type") == _MODEL_ROUTED
    ]
    if len(routed_positions) != 1:
        return False
    routed_position = routed_positions[0]
    dispatched: dict[str, tuple[int, dict[str, object]]] = {}
    terminal_ids: set[str] = set()
    next_attempt_number = 1

    for position, event in attempt_events:
        if position <= routed_position or position >= logical_terminal_position:
            return False
        data = event.get("data")
        if not isinstance(data, Mapping):
            return False
        if event.get("type") == _MODEL_ATTEMPT_DISPATCHED:
            dispatch = _model_wire_dispatch(payload_loader, data.get("payload_ref"))
            if (
                dispatch is None
                or not _attempt_matches_event(dispatch, event, model_request_id)
                or dispatch["attempt_id"] in dispatched
                or dispatch["attempt_number"] != next_attempt_number
            ):
                return False
            dispatched[dispatch["attempt_id"]] = (position, dispatch)
            next_attempt_number += 1
            continue

        receipt = _model_wire_attempt_receipt(payload_loader, data.get("receipt_ref"))
        if receipt is None:
            return False
        attempt_id = receipt["attempt_id"]
        dispatch_record = dispatched.get(attempt_id)
        if (
            dispatch_record is None
            or attempt_id in terminal_ids
            or dispatch_record[0] >= position
            or not _attempt_matches_event(receipt, event, model_request_id)
            or not _attempt_binding_matches(dispatch_record[1], receipt, str(event.get("turn_id", "")))
        ):
            return False
        terminal_ids.add(attempt_id)

    return bool(dispatched) and set(dispatched) == terminal_ids


def _model_wire_dispatch(
    payload_loader: Callable[[str], object],
    payload_ref: object,
) -> dict[str, object] | None:
    if not isinstance(payload_ref, str) or not payload_ref:
        return None
    try:
        value = payload_loader(payload_ref)
    except Exception:
        return None
    try:
        return validate_model_wire_attempt_dispatch(value)
    except Exception:
        return None


def _model_wire_attempt_receipt(
    payload_loader: Callable[[str], object],
    receipt_ref: object,
) -> dict[str, object] | None:
    if not isinstance(receipt_ref, str) or not receipt_ref:
        return None
    try:
        return validate_model_wire_attempt_receipt(payload_loader(receipt_ref))
    except Exception:
        return None


def _attempt_matches_event(
    attempt: Mapping[str, object],
    event: Mapping[str, object],
    model_request_id: str,
) -> bool:
    return (
        attempt.get("turn_id") == event.get("turn_id")
        and attempt.get("model_request_id") == model_request_id
        and _model_id(event) == model_request_id
    )


def _attempt_binding_matches(
    dispatch: Mapping[str, object],
    receipt: Mapping[str, object],
    turn_id: str,
) -> bool:
    return all(
        dispatch.get(field) == receipt.get(field)
        for field in (
            "attempt_id", "turn_id", "model_request_id", "attempt_number",
            "routing_snapshot_revision", "provider_id", "model_id", "execution_location",
        )
    ) and receipt.get("turn_id") == turn_id


def _nested_model_lifecycle_is_safe(
    lifecycle: Sequence[tuple[int, Mapping[str, object]]],
    events: Sequence[Mapping[str, object]],
    *,
    terminal_position: int | None,
    payload_loader: Callable[[str], object] | None = None,
) -> bool:
    """Require nested model calls to remain inside their parent tool window.

    A model event with a ``tool_call_id`` denotes a model call made while that
    tool is executing. It must retain one non-empty tool/step correlation for
    its whole lifecycle, start only after the matching parent ``tool.started``
    event, and finish before that parent's recorded outcome.
    """
    correlations = [_nested_model_correlation(event) for _position, event in lifecycle]
    nested = [correlation for correlation in correlations if correlation is not None]
    if not nested:
        return True
    if len(nested) != len(correlations) or any(correlation != nested[0] for correlation in nested[1:]):
        return False

    tool_call_id, step_id = nested[0]
    parent_started = _tool_event_position(events, "tool.started", tool_call_id, step_id)
    if parent_started is None or parent_started >= lifecycle[0][0]:
        return False
    if terminal_position is None:
        return True
    parent_outcome = _tool_event_position(events, "tool.outcome.recorded", tool_call_id, step_id)
    if parent_outcome is None and payload_loader is not None:
        parent_intent = _tool_event_position(events, 'tool.intent.recorded', tool_call_id, step_id)
        return (parent_intent is not None
                and _batch_dispatch_fact_is_safe(events[parent_intent], events[parent_started], events, payload_loader))
    return parent_outcome is not None and terminal_position < parent_outcome


def _nested_model_correlation(event: Mapping[str, object]) -> tuple[str, str] | None:
    correlation = event.get("correlation")
    if not isinstance(correlation, Mapping):
        return None
    tool_call_id = correlation.get("tool_call_id")
    if tool_call_id is None:
        return None
    step_id = correlation.get("step_id")
    if not isinstance(tool_call_id, str) or not tool_call_id or not isinstance(step_id, str) or not step_id:
        return ("", "")
    return tool_call_id, step_id


def _tool_event_position(
    events: Sequence[Mapping[str, object]],
    event_type: str,
    tool_call_id: str,
    step_id: str,
) -> int | None:
    positions = [
        position
        for position, event in enumerate(events)
        if event.get("type") == event_type
        and _tool_id(event) == tool_call_id
        and _step_id(event) == step_id
    ]
    return positions[0] if len(positions) == 1 else None


def _tool_id(event: Mapping[str, object]) -> str | None:
    value = event.get("correlation")
    return value.get("tool_call_id") if isinstance(value, Mapping) and isinstance(value.get("tool_call_id"), str) else None


def _step_id(event: Mapping[str, object]) -> str | None:
    value = event.get("correlation")
    return value.get("step_id") if isinstance(value, Mapping) and isinstance(value.get("step_id"), str) else None


def _lifecycle_is_closed(
    events: Sequence[Mapping[str, object]],
    start_type: str,
    terminal_types: set[str],
    identity: Callable[[Mapping[str, object]], str | None],
) -> bool:
    active: set[str] = set()
    seen: set[str] = set()
    for event in events:
        event_type = event.get("type")
        if event_type != start_type and event_type not in terminal_types:
            continue
        item_id = identity(event)
        if item_id is None:
            return False
        if event_type == start_type:
            if item_id in seen:
                return False
            seen.add(item_id)
            active.add(item_id)
        elif item_id not in active:
            return False
        else:
            active.remove(item_id)
    return not active
