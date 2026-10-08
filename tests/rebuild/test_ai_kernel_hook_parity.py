from __future__ import annotations

import json

import pytest

from core.ai_kernel.codex_hook_parity import (
    CODEX_HOOK_PARITY_REVISION,
    HookEvent,
    HookRun,
    HookRunStatus,
    PreToolUseBlocked,
    dispatch_pre_tool_use_hot_path,
    evaluate_hook_event,
)


def _run(
    config_order: int,
    *,
    output: object | None = None,
    completion_order: int | None = None,
    synchronous: bool = True,
    exit_code: int = 0,
    stderr: str = "",
    raw_stdout: str | None = None,
) -> HookRun:
    return HookRun(
        config_order=config_order,
        completion_order=config_order if completion_order is None else completion_order,
        synchronous=synchronous,
        exit_code=exit_code,
        stdout=raw_stdout if raw_stdout is not None else ("" if output is None else json.dumps(output)),
        stderr=stderr,
    )


def test_pins_the_audited_codex_revision_and_declares_the_complete_event_surface() -> None:
    assert CODEX_HOOK_PARITY_REVISION == "0fe877b4dedc86a29c8bebb3edbd3efcc3580c7d"
    assert {event.value for event in HookEvent} == {
        "PreToolUse", "PermissionRequest", "PostToolUse", "PreCompact", "PostCompact",
        "SessionStart", "SessionEnd", "UserPromptSubmit", "SubagentStart", "SubagentStop", "Stop",
    }


def test_pre_tool_deny_wins_and_never_applies_a_concurrent_rewrite() -> None:
    rewrite = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {"path": "safe"}}}
    deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "restricted", "additionalContext": "record this even when blocked"}}
    outcome = evaluate_hook_event(HookEvent.PRE_TOOL_USE, [_run(1, output=rewrite, completion_order=9), _run(2, output=deny)])
    assert outcome.dispatch_blocked is True
    assert outcome.stop_reason == "restricted"
    assert outcome.updated_input is None
    assert outcome.additional_context == ("record this even when blocked",)
    assert outcome.runs[1].status is HookRunStatus.BLOCKED


def test_pre_tool_uses_last_completion_for_valid_rewrites_and_rejects_invalid_allow() -> None:
    first = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {"path": "first"}}}
    last = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {"path": "last"}, "additionalContext": "keep the route rationale"}}
    invalid = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
    outcome = evaluate_hook_event(HookEvent.PRE_TOOL_USE, [_run(1, output=first, completion_order=3), _run(2, output=last, completion_order=7), _run(3, output=invalid)])
    assert outcome.updated_input == {"path": "last"}
    assert outcome.additional_context == ("keep the route rationale",)
    assert outcome.runs[-1].status is HookRunStatus.FAILED


@pytest.mark.parametrize("run", [
    _run(1, output={"decision": "block", "reason": "legacy"}),
    _run(1, exit_code=2, stderr="shell block"),
])
def test_pre_tool_supports_legacy_block_and_exit_two_with_stderr(run: HookRun) -> None:
    outcome = evaluate_hook_event(HookEvent.PRE_TOOL_USE, [run])
    assert outcome.dispatch_blocked is True


def test_pre_tool_exit_two_with_stderr_ignores_stdout_control_and_context() -> None:
    stdout = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {"path": "must-not-apply"},
            "additionalContext": "must-not-inject",
        }
    }
    outcome = evaluate_hook_event(
        HookEvent.PRE_TOOL_USE,
        [_run(1, output=stdout, exit_code=2, stderr="shell reason")],
    )
    assert outcome.dispatch_blocked is True
    assert outcome.stop_reason == "shell reason"
    assert outcome.updated_input is None
    assert outcome.additional_context == ()


def test_exit_two_without_stderr_and_async_control_fail_open_for_pre_tool() -> None:
    deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "no", "additionalContext": "async context remains visible"}}
    outcome = evaluate_hook_event(HookEvent.PRE_TOOL_USE, [_run(1, exit_code=2), _run(2, output=deny, synchronous=False)])
    assert outcome.dispatch_blocked is False
    assert outcome.runs[0].status is HookRunStatus.FAILED
    assert outcome.runs[1].control_ignored is True
    assert outcome.additional_context == ("async context remains visible",)


def test_pre_tool_plain_stdout_is_a_completed_noop_but_json_shaped_invalid_output_fails() -> None:
    plain = evaluate_hook_event(HookEvent.PRE_TOOL_USE, [_run(1, raw_stdout="diagnostic only")])
    assert plain.runs[0].status is HookRunStatus.COMPLETED
    assert plain.dispatch_blocked is False
    assert plain.additional_context == ()

    malformed = evaluate_hook_event(HookEvent.PRE_TOOL_USE, [_run(1, raw_stdout="{not json")])
    assert malformed.runs[0].status is HookRunStatus.FAILED
    assert malformed.dispatch_blocked is False


def test_pre_tool_sync_invalid_control_drops_context_but_async_invalid_control_keeps_it() -> None:
    invalid = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "additionalContext": "must not leak from invalid sync control"}}
    sync = evaluate_hook_event(HookEvent.PRE_TOOL_USE, [_run(1, output=invalid)])
    assert sync.runs[0].status is HookRunStatus.FAILED
    assert sync.additional_context == ()

    asynchronous = evaluate_hook_event(HookEvent.PRE_TOOL_USE, [_run(1, output=invalid, synchronous=False)])
    assert asynchronous.runs[0].status is HookRunStatus.COMPLETED
    assert asynchronous.runs[0].control_ignored is True
    assert asynchronous.additional_context == ("must not leak from invalid sync control",)


def test_pre_tool_deny_hot_path_skips_authorization_and_dispatch() -> None:
    deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "scope"}}
    outcome = evaluate_hook_event(HookEvent.PRE_TOOL_USE, [_run(1, output=deny)])
    checks: list[object] = []
    dispatched: list[object] = []

    with pytest.raises(PreToolUseBlocked, match="scope"):
        dispatch_pre_tool_use_hot_path(
            outcome,
            {"path": "original"},
            lambda value: checks.append(value) or True,
            lambda value: dispatched.append(value),
        )

    assert checks == []
    assert dispatched == []


def test_pre_tool_pass_rewrites_input_but_does_not_grant_authorization() -> None:
    rewrite = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {"path": "rewritten"}}}
    outcome = evaluate_hook_event(HookEvent.PRE_TOOL_USE, [_run(1, output=rewrite)])
    checks: list[object] = []
    dispatched: list[object] = []

    with pytest.raises(PreToolUseBlocked, match="authorization"):
        dispatch_pre_tool_use_hot_path(
            outcome,
            {"path": "original"},
            lambda value: checks.append(value) or False,
            lambda value: dispatched.append(value),
        )

    assert checks == [{"path": "rewritten"}]
    assert dispatched == []


def test_pre_tool_fail_open_outcome_still_requires_hard_guard_before_dispatch() -> None:
    outcome = evaluate_hook_event(HookEvent.PRE_TOOL_USE, [_run(1, raw_stdout="{not json")])
    checks: list[object] = []
    dispatched: list[object] = []

    with pytest.raises(PreToolUseBlocked, match="authorization"):
        dispatch_pre_tool_use_hot_path(
            outcome,
            {"path": "original"},
            lambda value: checks.append(value) or False,
            lambda value: dispatched.append(value),
        )

    assert outcome.runs[0].status is HookRunStatus.FAILED
    assert checks == [{"path": "original"}]
    assert dispatched == []


def test_pre_tool_hot_path_dispatches_once_only_after_frozen_authorization() -> None:
    outcome = evaluate_hook_event(HookEvent.PRE_TOOL_USE, [_run(1)])
    calls: list[tuple[str, object]] = []

    result = dispatch_pre_tool_use_hot_path(
        outcome,
        {"path": "original"},
        lambda value: calls.append(("check", value)) or True,
        lambda value: calls.append(("dispatch", value)) or "done",
    )

    assert result == "done"
    assert calls == [("check", {"path": "original"}), ("dispatch", {"path": "original"})]


def test_permission_request_only_accepts_allow_or_reasoned_deny_and_deny_wins() -> None:
    allow = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
    deny = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "deny", "reason": "scope"}}}
    outcome = evaluate_hook_event(HookEvent.PERMISSION_REQUEST, [_run(1, output=allow), _run(2, output=deny)])
    assert outcome.permission_denied is True
    assert outcome.permission_allowed is False
    assert outcome.stop_reason == "scope"

    unsupported = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "ask"}}}
    invalid = evaluate_hook_event(HookEvent.PERMISSION_REQUEST, [_run(1, output=unsupported)])
    assert invalid.permission_denied is False
    assert invalid.permission_allowed is None
    assert invalid.runs[0].status is HookRunStatus.FAILED

    unsupported_field = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}, "updatedInput": {}}}
    failed = evaluate_hook_event(HookEvent.PERMISSION_REQUEST, [_run(1, output=unsupported_field)])
    assert failed.permission_denied is False
    assert failed.runs[0].status is HookRunStatus.FAILED

    permitted = evaluate_hook_event(HookEvent.PERMISSION_REQUEST, [_run(1, output=allow)])
    assert permitted.permission_denied is False
    assert permitted.permission_allowed is True


@pytest.mark.parametrize("event", [HookEvent.PERMISSION_REQUEST, HookEvent.POST_TOOL_USE, HookEvent.USER_PROMPT_SUBMIT, HookEvent.STOP, HookEvent.SUBAGENT_STOP])
def test_exit_two_with_stderr_has_each_event_specific_control_effect(event: HookEvent) -> None:
    outcome = evaluate_hook_event(event, [_run(1, exit_code=2, stderr="controlled by shell")])
    if event is HookEvent.PERMISSION_REQUEST:
        assert outcome.permission_denied is True
    elif event in {HookEvent.STOP, HookEvent.SUBAGENT_STOP}:
        assert outcome.allow_stop is False
        assert outcome.continuation_prompt == "controlled by shell"
    elif event is HookEvent.POST_TOOL_USE:
        assert outcome.block_follow_up is True
        assert outcome.feedback == ("controlled by shell",)
    else:
        assert outcome.should_stop is True
        assert outcome.stop_reason == "controlled by shell"


@pytest.mark.parametrize("event", list(HookEvent))
def test_exit_two_without_stderr_is_failed_and_never_controls(event: HookEvent) -> None:
    outcome = evaluate_hook_event(event, [_run(1, exit_code=2)])
    assert outcome.runs[0].status is HookRunStatus.FAILED
    assert outcome.dispatch_blocked is False
    assert outcome.permission_denied is False
    assert outcome.should_stop is False


def test_post_tool_blocks_follow_up_or_stops_without_claiming_a_rollback() -> None:
    block = {"decision": "block", "reason": "verify result"}
    stop = {"continue": False, "stopReason": "end turn"}
    outcome = evaluate_hook_event(HookEvent.POST_TOOL_USE, [_run(1, output=block), _run(2, output=stop)])
    assert outcome.block_follow_up is True
    assert outcome.feedback == ("verify result",)
    assert outcome.should_stop is True
    assert outcome.stop_reason == "end turn"
    assert outcome.runs[0].status is HookRunStatus.BLOCKED
    assert outcome.runs[1].status is HookRunStatus.STOPPED
    assert not hasattr(outcome, "rolled_back")


def test_user_prompt_plain_text_is_context_and_first_configured_stop_reason_wins() -> None:
    block = {"decision": "block", "reason": "policy"}
    stop = {"continue": False, "stopReason": "later"}
    outcome = evaluate_hook_event(HookEvent.USER_PROMPT_SUBMIT, [_run(1, raw_stdout="ground this answer"), _run(2, output=block), _run(3, output=stop)])
    assert outcome.additional_context == ("ground this answer",)
    assert outcome.should_stop is True
    assert outcome.stop_reason == "policy"
    assert outcome.runs[1].status is HookRunStatus.BLOCKED
    assert outcome.runs[2].status is HookRunStatus.STOPPED


def test_user_prompt_invalid_structured_output_fails_open() -> None:
    outcome = evaluate_hook_event(HookEvent.USER_PROMPT_SUBMIT, [_run(1, raw_stdout="{not json")])
    assert outcome.should_stop is False
    assert outcome.runs[0].status is HookRunStatus.FAILED


@pytest.mark.parametrize("event", [HookEvent.STOP, HookEvent.SUBAGENT_STOP])
def test_stop_events_aggregate_blocks_in_config_order_but_continue_false_has_priority(event: HookEvent) -> None:
    first = {"decision": "block", "reason": "finish checks"}
    second = {"decision": "block", "reason": "save state"}
    blocked = evaluate_hook_event(event, [_run(2, output=second), _run(1, output=first)])
    assert blocked.allow_stop is False
    assert blocked.continuation_prompt == "finish checks\nsave state"

    forced = evaluate_hook_event(event, [_run(1, output=first), _run(2, output={"continue": False})])
    assert forced.allow_stop is True
    assert forced.continuation_prompt is None
    assert forced.runs[1].status is HookRunStatus.STOPPED


@pytest.mark.parametrize("event", [HookEvent.PRE_COMPACT, HookEvent.POST_COMPACT])
def test_compact_events_only_stop_for_sync_continue_false(event: HookEvent) -> None:
    output = {"continue": False, "stopReason": "keep context"}
    outcome = evaluate_hook_event(event, [_run(1, output=output), _run(2, output=output, synchronous=False)])
    assert outcome.should_stop is True
    assert outcome.stop_reason == "keep context"
    assert outcome.runs[0].status is HookRunStatus.STOPPED
    assert outcome.runs[1].control_ignored is True


def test_session_start_can_add_context_and_stop_but_subagent_start_is_context_only() -> None:
    output = {"additionalContext": "Use project facts", "continue": False, "stopReason": "wait"}
    session = evaluate_hook_event(HookEvent.SESSION_START, [_run(1, output=output)])
    assert session.additional_context == ("Use project facts",)
    assert session.should_stop is True
    assert session.runs[0].status is HookRunStatus.STOPPED

    subagent = evaluate_hook_event(HookEvent.SUBAGENT_START, [_run(1, output=output)])
    assert subagent.additional_context == ("Use project facts",)
    assert subagent.should_stop is False
    assert subagent.runs[0].status is HookRunStatus.COMPLETED


def test_session_end_is_observational_and_does_not_apply_stop_control() -> None:
    outcome = evaluate_hook_event(HookEvent.SESSION_END, [_run(1, output={"continue": False})])
    assert outcome.should_stop is False
    assert outcome.runs[0].control_ignored is True


@pytest.mark.parametrize("event", [HookEvent.SESSION_END])
def test_other_events_are_observational_and_never_fabricate_control(event: HookEvent) -> None:
    outcome = evaluate_hook_event(event, [_run(1, output={"decision": "block", "reason": "ignored"}), _run(2, raw_stdout="not-json")])
    assert outcome.dispatch_blocked is False
    assert outcome.permission_denied is False
    assert outcome.should_stop is False
    assert outcome.allow_stop is None
    assert outcome.runs[0].control_ignored is True
    assert outcome.runs[1].status is HookRunStatus.FAILED
