"""Codex-hook compatible control aggregation, pinned to revision 0fe877b4.

This module is deliberately a pure contract layer.  It accepts already-run hook
process results and describes their event-specific effect; it neither starts a
process nor dispatches a tool, writes Session state, or decides Boundary
authority.  The production Hook Host consumes these outcomes directly, queues
Session/audit facts asynchronously, and never sends a deny through Boundary for
a second runtime decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
import json
from typing import Callable, TypeVar


CODEX_HOOK_PARITY_REVISION = "0fe877b4dedc86a29c8bebb3edbd3efcc3580c7d"

_DispatchResult = TypeVar("_DispatchResult")


class HookEvent(str, Enum):
    PRE_TOOL_USE = "PreToolUse"
    PERMISSION_REQUEST = "PermissionRequest"
    POST_TOOL_USE = "PostToolUse"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    STOP = "Stop"


class HookRunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    STOPPED = "stopped"


class PreToolUseBlocked(RuntimeError):
    """The hook outcome or frozen hard guard prohibited a tool dispatch."""


@dataclass(frozen=True)
class HookRun:
    """One completed hook process result.

    ``config_order`` is the stable configured order. ``completion_order`` is
    only meaningful for concurrent PreToolUse input rewrites, where Codex
    accepts the last completed valid rewrite when no deny wins.
    """

    config_order: int
    completion_order: int
    synchronous: bool
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    hook_id: str = ""


@dataclass(frozen=True)
class HookRunResult:
    run: HookRun
    status: HookRunStatus
    diagnostic: str | None = None
    control_ignored: bool = False


@dataclass(frozen=True)
class HookEventOutcome:
    """Event-specific result of Codex compatible aggregation.

    Fields unused by a given event remain their neutral value.  Keeping them
    explicit avoids a generic verdict vocabulary that would subtly change the
    different Codex event contracts.
    """

    event: HookEvent
    runs: tuple[HookRunResult, ...]
    dispatch_blocked: bool = False
    permission_denied: bool = False
    permission_allowed: bool | None = None
    updated_input: object | None = None
    should_stop: bool = False
    stop_reason: str | None = None
    block_follow_up: bool = False
    feedback: tuple[str, ...] = ()
    additional_context: tuple[str, ...] = ()
    allow_stop: bool | None = None
    continuation_prompt: str | None = None


def evaluate_hook_event(event: HookEvent | str, runs: Iterable[HookRun]) -> HookEventOutcome:
    """Aggregate hook results using the Codex revision's event-specific rules.

    Only synchronous hooks have a control effect.  Async hooks are retained in
    the returned receipt-shaped results, but their otherwise valid control
    output is explicitly ignored.
    """

    resolved_event = HookEvent(event)
    ordered = tuple(sorted(runs, key=lambda item: item.config_order))
    if resolved_event is HookEvent.PRE_TOOL_USE:
        return _pre_tool_use(resolved_event, ordered)
    if resolved_event is HookEvent.PERMISSION_REQUEST:
        return _permission_request(resolved_event, ordered)
    if resolved_event is HookEvent.POST_TOOL_USE:
        return _post_tool_use(resolved_event, ordered)
    if resolved_event is HookEvent.USER_PROMPT_SUBMIT:
        return _user_prompt_submit(resolved_event, ordered)
    if resolved_event in {HookEvent.PRE_COMPACT, HookEvent.POST_COMPACT}:
        return _compact_event(resolved_event, ordered)
    if resolved_event is HookEvent.SESSION_START:
        return _session_start(resolved_event, ordered)
    if resolved_event is HookEvent.SUBAGENT_START:
        return _subagent_start(resolved_event, ordered)
    if resolved_event in {HookEvent.STOP, HookEvent.SUBAGENT_STOP}:
        return _stop_event(resolved_event, ordered)
    return _observational_event(resolved_event, ordered)


def dispatch_pre_tool_use_hot_path(
    outcome: HookEventOutcome,
    original_input: object,
    frozen_authorization_check: Callable[[object], bool],
    dispatch: Callable[[object], _DispatchResult],
) -> _DispatchResult:
    """Apply the tiny, fail-closed PreToolUse data-plane sequence.

    This adapter intentionally does not interpret a hook allow as permission:
    every non-blocked outcome still takes the one frozen authorization check.
    It is suitable only after the caller has created the normal immutable
    receipt/event context; this helper neither creates nor persists one.
    """

    if outcome.event is not HookEvent.PRE_TOOL_USE:
        raise ValueError("PreToolUse hot path requires a PreToolUse outcome")
    if outcome.dispatch_blocked:
        raise PreToolUseBlocked(outcome.stop_reason or "PreToolUse hook blocked dispatch")
    candidate_input = outcome.updated_input if outcome.updated_input is not None else original_input
    if not frozen_authorization_check(candidate_input):
        raise PreToolUseBlocked("frozen authorization check denied dispatch")
    return dispatch(candidate_input)


def _pre_tool_use(event: HookEvent, runs: tuple[HookRun, ...]) -> HookEventOutcome:
    results: list[HookRunResult] = []
    denies: list[str] = []
    rewrites: list[tuple[int, object]] = []
    additional_context: list[str] = []
    for run in runs:
        if run.synchronous and _exit_two_blocks(run):
            denies.append(run.stderr.strip())
            results.append(HookRunResult(run, HookRunStatus.BLOCKED))
            continue
        parsed, parse_error = _parse_pre_tool_output(run.stdout)
        context, context_error = _pre_tool_additional_context(parsed)
        if not run.synchronous:
            if context:
                additional_context.append(context)
            results.append(_async_result(run, parse_error or context_error))
            continue
        if _non_controlling_process_failure(run):
            results.append(HookRunResult(run, HookRunStatus.FAILED, "hook process failed"))
            continue
        if parse_error or context_error:
            results.append(HookRunResult(run, HookRunStatus.FAILED, parse_error or context_error))
            continue
        verdict = _pre_tool_verdict(parsed)
        if verdict is None:
            if context:
                additional_context.append(context)
            results.append(HookRunResult(run, HookRunStatus.COMPLETED))
            continue
        kind, value = verdict
        if kind == "failed":
            results.append(HookRunResult(run, HookRunStatus.FAILED, str(value)))
        elif kind == "deny":
            if context:
                additional_context.append(context)
            denies.append(str(value))
            results.append(HookRunResult(run, HookRunStatus.BLOCKED))
        else:
            if context:
                additional_context.append(context)
            rewrites.append((run.completion_order, value))
            results.append(HookRunResult(run, HookRunStatus.COMPLETED))
    if denies:
        return HookEventOutcome(
            event, tuple(results), dispatch_blocked=True, stop_reason=denies[0],
            additional_context=tuple(additional_context),
        )
    updated_input = max(rewrites, key=lambda item: item[0])[1] if rewrites else None
    return HookEventOutcome(
        event, tuple(results), updated_input=updated_input,
        additional_context=tuple(additional_context),
    )


def _permission_request(event: HookEvent, runs: tuple[HookRun, ...]) -> HookEventOutcome:
    results: list[HookRunResult] = []
    denies: list[str] = []
    allows = False
    for run in runs:
        parsed, parse_error = _parse_control_json(run.stdout)
        if not run.synchronous:
            results.append(_async_result(run, parse_error))
            continue
        if _exit_two_blocks(run):
            denies.append(run.stderr.strip())
            results.append(HookRunResult(run, HookRunStatus.BLOCKED))
            continue
        if _non_controlling_process_failure(run):
            results.append(HookRunResult(run, HookRunStatus.FAILED, "hook process failed"))
            continue
        if parse_error:
            results.append(HookRunResult(run, HookRunStatus.FAILED, parse_error))
            continue
        verdict = _permission_verdict(parsed)
        if verdict is None:
            results.append(HookRunResult(run, HookRunStatus.COMPLETED))
        elif verdict[0] == "failed":
            results.append(HookRunResult(run, HookRunStatus.FAILED, str(verdict[1])))
        elif verdict[0] == "deny":
            denies.append(str(verdict[1]))
            results.append(HookRunResult(run, HookRunStatus.BLOCKED))
        else:
            allows = True
            results.append(HookRunResult(run, HookRunStatus.COMPLETED))
    return HookEventOutcome(
        event,
        tuple(results),
        permission_denied=bool(denies),
        permission_allowed=False if denies else (True if allows else None),
        stop_reason=denies[0] if denies else None,
    )


def _post_tool_use(event: HookEvent, runs: tuple[HookRun, ...]) -> HookEventOutcome:
    results: list[HookRunResult] = []
    feedback: list[str] = []
    should_stop = False
    stop_reason: str | None = None
    for run in runs:
        parsed, parse_error = _parse_control_json(run.stdout)
        if not run.synchronous:
            results.append(_async_result(run, parse_error))
            continue
        if _exit_two_blocks(run):
            feedback.append(run.stderr.strip())
            results.append(HookRunResult(run, HookRunStatus.BLOCKED))
            continue
        if _non_controlling_process_failure(run):
            results.append(HookRunResult(run, HookRunStatus.FAILED, "hook process failed"))
            continue
        if parse_error:
            results.append(HookRunResult(run, HookRunStatus.FAILED, parse_error))
            continue
        block, reason, stop, error = _standard_block_or_stop(parsed)
        if error:
            results.append(HookRunResult(run, HookRunStatus.FAILED, error))
            continue
        if block:
            feedback.append(reason)
        if stop and not should_stop:
            should_stop, stop_reason = True, reason
        results.append(HookRunResult(run, HookRunStatus.STOPPED if stop else (HookRunStatus.BLOCKED if block else HookRunStatus.COMPLETED)))
    return HookEventOutcome(
        event, tuple(results), should_stop=should_stop, stop_reason=stop_reason,
        block_follow_up=bool(feedback), feedback=tuple(feedback),
    )


def _user_prompt_submit(event: HookEvent, runs: tuple[HookRun, ...]) -> HookEventOutcome:
    results: list[HookRunResult] = []
    context: list[str] = []
    should_stop = False
    stop_reason: str | None = None
    for run in runs:
        parsed, parse_error = _parse_prompt_output(run.stdout)
        if not run.synchronous:
            results.append(_async_result(run, parse_error))
            continue
        if _exit_two_blocks(run):
            if not should_stop:
                should_stop, stop_reason = True, run.stderr.strip()
            results.append(HookRunResult(run, HookRunStatus.BLOCKED))
            continue
        if _non_controlling_process_failure(run):
            results.append(HookRunResult(run, HookRunStatus.FAILED, "hook process failed"))
            continue
        if parse_error:
            results.append(HookRunResult(run, HookRunStatus.FAILED, parse_error))
            continue
        if isinstance(parsed, str):
            if parsed:
                context.append(parsed)
            results.append(HookRunResult(run, HookRunStatus.COMPLETED))
            continue
        block, reason, stop, error = _standard_block_or_stop(parsed)
        if error:
            results.append(HookRunResult(run, HookRunStatus.FAILED, error))
            continue
        if (block or stop) and not should_stop:
            should_stop, stop_reason = True, reason
        results.append(HookRunResult(run, HookRunStatus.STOPPED if stop else (HookRunStatus.BLOCKED if block else HookRunStatus.COMPLETED)))
    return HookEventOutcome(
        event, tuple(results), should_stop=should_stop, stop_reason=stop_reason,
        additional_context=tuple(context),
    )


def _stop_event(event: HookEvent, runs: tuple[HookRun, ...]) -> HookEventOutcome:
    results: list[HookRunResult] = []
    block_reasons: list[str] = []
    force_stop = False
    for run in runs:
        parsed, parse_error = _parse_control_json(run.stdout)
        if not run.synchronous:
            results.append(_async_result(run, parse_error))
            continue
        if _exit_two_blocks(run):
            block_reasons.append(run.stderr.strip())
            results.append(HookRunResult(run, HookRunStatus.BLOCKED))
            continue
        if _non_controlling_process_failure(run):
            results.append(HookRunResult(run, HookRunStatus.FAILED, "hook process failed"))
            continue
        if parse_error:
            results.append(HookRunResult(run, HookRunStatus.FAILED, parse_error))
            continue
        block, reason, stop, error = _standard_block_or_stop(parsed)
        if error:
            results.append(HookRunResult(run, HookRunStatus.FAILED, error))
            continue
        if stop:
            force_stop = True
        if block:
            block_reasons.append(reason)
        results.append(HookRunResult(run, HookRunStatus.STOPPED if stop else (HookRunStatus.BLOCKED if block else HookRunStatus.COMPLETED)))
    if force_stop:
        return HookEventOutcome(event, tuple(results), allow_stop=True)
    if block_reasons:
        reason = "\n".join(block_reasons)
        return HookEventOutcome(
            event, tuple(results), allow_stop=False, stop_reason=reason,
            continuation_prompt=reason,
        )
    return HookEventOutcome(event, tuple(results), allow_stop=True)


def _compact_event(event: HookEvent, runs: tuple[HookRun, ...]) -> HookEventOutcome:
    results: list[HookRunResult] = []
    should_stop = False
    stop_reason: str | None = None
    for run in runs:
        parsed, parse_error = _parse_control_json(run.stdout)
        if not run.synchronous:
            results.append(_async_result(run, parse_error))
            continue
        if _non_controlling_process_failure(run):
            results.append(HookRunResult(run, HookRunStatus.FAILED, "hook process failed"))
            continue
        if parse_error:
            results.append(HookRunResult(run, HookRunStatus.FAILED, parse_error))
            continue
        stop, reason, error = _continue_false(parsed)
        if error:
            results.append(HookRunResult(run, HookRunStatus.FAILED, error))
        elif stop:
            if not should_stop:
                should_stop, stop_reason = True, reason
            results.append(HookRunResult(run, HookRunStatus.STOPPED))
        else:
            results.append(HookRunResult(run, HookRunStatus.COMPLETED))
    return HookEventOutcome(event, tuple(results), should_stop=should_stop, stop_reason=stop_reason)


def _session_start(event: HookEvent, runs: tuple[HookRun, ...]) -> HookEventOutcome:
    results: list[HookRunResult] = []
    context: list[str] = []
    should_stop = False
    stop_reason: str | None = None
    for run in runs:
        parsed, parse_error = _parse_context_output(run.stdout)
        if not run.synchronous:
            results.append(_async_result(run, parse_error))
            continue
        if _non_controlling_process_failure(run):
            results.append(HookRunResult(run, HookRunStatus.FAILED, "hook process failed"))
            continue
        if parse_error:
            results.append(HookRunResult(run, HookRunStatus.FAILED, parse_error))
            continue
        item_context, stop, reason, error = _context_and_continue(parsed)
        if error:
            results.append(HookRunResult(run, HookRunStatus.FAILED, error))
            continue
        if item_context:
            context.append(item_context)
        if stop and not should_stop:
            should_stop, stop_reason = True, reason
        results.append(HookRunResult(run, HookRunStatus.STOPPED if stop else HookRunStatus.COMPLETED))
    return HookEventOutcome(
        event, tuple(results), should_stop=should_stop, stop_reason=stop_reason,
        additional_context=tuple(context),
    )


def _subagent_start(event: HookEvent, runs: tuple[HookRun, ...]) -> HookEventOutcome:
    results: list[HookRunResult] = []
    context: list[str] = []
    for run in runs:
        parsed, parse_error = _parse_context_output(run.stdout)
        if _non_controlling_process_failure(run):
            results.append(HookRunResult(run, HookRunStatus.FAILED, "hook process failed", not run.synchronous))
            continue
        if parse_error:
            results.append(HookRunResult(run, HookRunStatus.FAILED, parse_error, not run.synchronous))
            continue
        item_context, _, _, error = _context_and_continue(parsed)
        if error:
            results.append(HookRunResult(run, HookRunStatus.FAILED, error, not run.synchronous))
            continue
        if item_context:
            context.append(item_context)
        results.append(HookRunResult(run, HookRunStatus.COMPLETED, control_ignored=not run.synchronous))
    return HookEventOutcome(event, tuple(results), additional_context=tuple(context))


def _observational_event(event: HookEvent, runs: tuple[HookRun, ...]) -> HookEventOutcome:
    results: list[HookRunResult] = []
    for run in runs:
        _, parse_error = _parse_control_json(run.stdout)
        if _non_controlling_process_failure(run):
            results.append(HookRunResult(run, HookRunStatus.FAILED, "hook process failed"))
        elif parse_error and run.stdout.strip():
            results.append(HookRunResult(run, HookRunStatus.FAILED, parse_error))
        else:
            results.append(HookRunResult(run, HookRunStatus.COMPLETED, control_ignored=bool(run.stdout.strip())))
    return HookEventOutcome(event, tuple(results))


def _pre_tool_verdict(payload: object | None) -> tuple[str, object] | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        return "failed", "PreToolUse control output must be an object"
    if payload.get("decision") == "block":
        reason = _nonempty_text(payload.get("reason"))
        return ("deny", reason) if reason else ("failed", "legacy block requires reason")
    output = payload.get("hookSpecificOutput")
    if output is None:
        return None
    if not isinstance(output, Mapping) or output.get("hookEventName") != HookEvent.PRE_TOOL_USE.value:
        return "failed", "PreToolUse hook-specific output is invalid"
    decision = output.get("permissionDecision")
    reason = _nonempty_text(output.get("permissionDecisionReason"))
    if decision == "deny":
        return ("deny", reason) if reason else ("failed", "PreToolUse deny requires reason")
    if decision == "allow":
        return ("allow", output["updatedInput"]) if "updatedInput" in output else ("failed", "PreToolUse allow requires updatedInput")
    return "failed", "PreToolUse permission decision is unsupported"


def _permission_verdict(payload: object | None) -> tuple[str, object] | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        return "failed", "PermissionRequest control output must be an object"
    output = payload.get("hookSpecificOutput")
    if not isinstance(output, Mapping) or output.get("hookEventName") != HookEvent.PERMISSION_REQUEST.value:
        return "failed", "PermissionRequest hook-specific output is invalid"
    if (
        "updatedInput" in output
        or "updatedPermissions" in output
        or output.get("interrupt") is True
        or payload.get("continue") is False
    ):
        return "failed", "PermissionRequest output includes an unsupported control field"
    decision = output.get("decision")
    if not isinstance(decision, Mapping):
        return "failed", "PermissionRequest decision is invalid"
    behavior = decision.get("behavior")
    reason = _nonempty_text(decision.get("reason"))
    if behavior == "allow":
        return "allow", None
    if behavior == "deny":
        return ("deny", reason) if reason else ("failed", "PermissionRequest deny requires reason")
    return "failed", "PermissionRequest behavior is unsupported"


def _standard_block_or_stop(payload: object | None) -> tuple[bool, str, bool, str | None]:
    if payload is None:
        return False, "", False, None
    if not isinstance(payload, Mapping):
        return False, "", False, "hook control output must be an object"
    decision = payload.get("decision")
    reason = _nonempty_text(payload.get("reason")) or _nonempty_text(payload.get("stopReason")) or "Hook requested stop"
    if decision is not None and decision != "block":
        return False, "", False, "hook decision is unsupported"
    if decision == "block" and not _nonempty_text(payload.get("reason")):
        return False, "", False, "hook block requires reason"
    return decision == "block", reason, payload.get("continue") is False, None


def _parse_control_json(stdout: str) -> tuple[object | None, str | None]:
    text = stdout.strip()
    if not text:
        return None, None
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, "hook control output is invalid JSON"


def _parse_pre_tool_output(stdout: str) -> tuple[object | None, str | None]:
    """Codex treats ordinary PreToolUse stdout as a successful no-op.

    Only text which purports to be structured JSON is parsed as a control
    output.  This preserves shell-hook diagnostics and plain text output while
    still surfacing malformed JSON-shaped output as a failed hook run.
    """

    text = stdout.strip()
    if not text or not text.startswith(("{", "[")):
        return None, None
    return _parse_control_json(text)


def _pre_tool_additional_context(payload: object | None) -> tuple[str | None, str | None]:
    if payload is None or not isinstance(payload, Mapping):
        return None, None
    output = payload.get("hookSpecificOutput")
    if not isinstance(output, Mapping):
        return None, None
    context = output.get("additionalContext")
    if context is None:
        return None, None
    if not isinstance(context, str):
        return None, "PreToolUse additionalContext is invalid"
    return context, None


def _parse_prompt_output(stdout: str) -> tuple[object | str | None, str | None]:
    text = stdout.strip()
    if not text:
        return None, None
    if text.startswith(("{", "[")):
        return _parse_control_json(text)
    return text, None


def _parse_context_output(stdout: str) -> tuple[object | str | None, str | None]:
    return _parse_prompt_output(stdout)


def _context_and_continue(payload: object | str | None) -> tuple[str | None, bool, str, str | None]:
    if payload is None:
        return None, False, "", None
    if isinstance(payload, str):
        return payload, False, "", None
    if not isinstance(payload, Mapping):
        return None, False, "", "hook context output must be an object"
    context = payload.get("additionalContext")
    if context is not None and not isinstance(context, str):
        return None, False, "", "hook additional context is invalid"
    stop, reason, error = _continue_false(payload)
    return context, stop, reason, error


def _continue_false(payload: object | None) -> tuple[bool, str, str | None]:
    if payload is None:
        return False, "", None
    if not isinstance(payload, Mapping):
        return False, "", "hook control output must be an object"
    if payload.get("continue") is False:
        return True, _nonempty_text(payload.get("stopReason")) or "Hook requested stop", None
    return False, "", None


def _async_result(run: HookRun, parse_error: str | None) -> HookRunResult:
    if _non_controlling_process_failure(run):
        return HookRunResult(run, HookRunStatus.FAILED, "hook process failed", control_ignored=True)
    if parse_error:
        return HookRunResult(run, HookRunStatus.FAILED, parse_error, control_ignored=True)
    return HookRunResult(run, HookRunStatus.COMPLETED, control_ignored=bool(run.stdout.strip() or run.exit_code or run.stderr.strip()))


def _exit_two_blocks(run: HookRun) -> bool:
    return run.exit_code == 2 and bool(run.stderr.strip())


def _non_controlling_process_failure(run: HookRun) -> bool:
    """All nonzero exits fail where this event has no exit-2 control meaning."""
    return run.exit_code != 0


def _nonempty_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
