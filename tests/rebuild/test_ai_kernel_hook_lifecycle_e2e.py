from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from core.ai_kernel import (
    CapabilityDefinition,
    CodexHookHost,
    HookHandlerManifest,
    HookPolicyCatalog,
    HookPolicySnapshot,
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    InMemoryTurnStateStore,
    RevisionPinnedHookRunner,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
)
from core.ai_kernel.codex_hook_parity import HookEvent, HookRun


ROOT = Path(__file__).resolve().parents[2]


class _Provider:
    def __init__(self) -> None:
        self.calls: list[Mapping[str, object]] = []

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        self.calls.append(request)
        return {"summary": "tool completed", "result": {"ok": True}}


class _DecisionPlanner:
    def __init__(self, decisions: list[Mapping[str, object]]) -> None:
        self._decisions = list(decisions)
        self.calls: list[tuple[tuple[Mapping[str, object], ...], object]] = []

    def plan(self, _request, events, _capabilities, payloads, _execution_control=None):
        self.calls.append((tuple(events), payloads))
        return dict(self._decisions.pop(0))


def test_permission_request_deny_prevents_approval_and_tool_dispatch() -> None:
    provider = _Provider()
    runtime, events, _payloads = _runtime(
        planner=_DecisionPlanner([_tool_decision()]),
        provider=provider,
        hook_outputs={
            HookEvent.PERMISSION_REQUEST: [
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PermissionRequest",
                        "decision": {"behavior": "deny", "reason": "approval scope rejected"},
                    }
                }
            ]
        },
        requires_approval=True,
    )

    receipt = runtime.submit_turn(_request())

    assert receipt.status == "failed"
    assert provider.calls == []
    types = _event_types(events, receipt.turn_id)
    assert "hook.invoked" in types
    assert "approval.required" not in types
    assert "tool.requested" in types
    assert types[-1] == "turn.failed"


def test_post_tool_block_delivers_feedback_to_next_planner_step_without_rollback() -> None:
    provider = _Provider()
    planner = _DecisionPlanner([_tool_decision(), {"type": "complete", "summary": "reviewed"}])
    runtime, events, payloads = _runtime(
        planner=planner,
        provider=provider,
        hook_outputs={HookEvent.POST_TOOL_USE: [{"decision": "block", "reason": "verify evidence"}]},
    )

    receipt = runtime.submit_turn(_request())

    assert receipt.status == "completed"
    assert len(provider.calls) == 1
    assert len(planner.calls) == 2
    feedback_event = next(event for event in events.events_after(receipt.turn_id) if event["type"] == "hook.feedback")
    feedback_ref = feedback_event["data"]["payload_ref"]
    assert isinstance(feedback_ref, str)
    assert payloads.get(feedback_ref) == {
        "schema_version": "1.0.0",
        "event": "PostToolUse",
        "items": ["verify evidence"],
    }
    assert planner.calls[1][1].get(feedback_ref) == payloads.get(feedback_ref)
    assert _event_types(events, receipt.turn_id).index("tool.completed") < _event_types(events, receipt.turn_id).index("hook.feedback")


def test_post_tool_continue_false_stops_after_effect_is_recorded() -> None:
    provider = _Provider()
    planner = _DecisionPlanner([_tool_decision(), {"type": "complete", "summary": "must not run"}])
    runtime, events, _payloads = _runtime(
        planner=planner,
        provider=provider,
        hook_outputs={HookEvent.POST_TOOL_USE: [{"continue": False, "stopReason": "stop after effect"}]},
    )

    receipt = runtime.submit_turn(_request())

    assert receipt.status == "cancelled"
    assert len(provider.calls) == 1
    assert len(planner.calls) == 1
    types = _event_types(events, receipt.turn_id)
    assert types.index("tool.completed") < types.index("turn.cancelled")
    assert "turn.completed" not in types


def test_stop_block_writes_continuation_then_allows_the_next_completion() -> None:
    planner = _DecisionPlanner([
        {"type": "complete", "summary": "first attempt"},
        {"type": "complete", "summary": "continued completion"},
    ])
    runtime, events, payloads = _runtime(
        planner=planner,
        provider=_Provider(),
        hook_outputs={HookEvent.STOP: [
            {"decision": "block", "reason": "finish the evidence review"},
            None,
        ]},
    )

    receipt = runtime.submit_turn(_request())

    assert receipt.status == "completed"
    assert len(planner.calls) == 2
    continuation = next(event for event in events.events_after(receipt.turn_id) if event["type"] == "hook.continuation")
    continuation_ref = continuation["data"]["payload_ref"]
    assert isinstance(continuation_ref, str)
    assert payloads.get(continuation_ref) == {
        "schema_version": "1.0.0",
        "event": "Stop",
        "prompt": "finish the evidence review",
    }
    assert planner.calls[1][1].get(continuation_ref) == payloads.get(continuation_ref)


def test_stop_continue_false_permits_terminal_completion_without_continuation() -> None:
    planner = _DecisionPlanner([{"type": "complete", "summary": "done"}])
    runtime, events, _payloads = _runtime(
        planner=planner,
        provider=_Provider(),
        hook_outputs={HookEvent.STOP: [{"continue": False}]},
    )

    receipt = runtime.submit_turn(_request())

    assert receipt.status == "completed"
    assert len(planner.calls) == 1
    assert "hook.continuation" not in _event_types(events, receipt.turn_id)


def test_session_start_prompt_submit_and_session_end_are_automatic_and_prompt_context_is_visible() -> None:
    planner = _DecisionPlanner([{"type": "complete", "summary": "done"}])
    runtime, events, payloads = _runtime(
        planner=planner,
        provider=_Provider(),
        hook_outputs={
            HookEvent.SESSION_START: [{"additionalContext": "session baseline"}],
            # Codex treats plain stdout as UserPromptSubmit additional context.
            HookEvent.USER_PROMPT_SUBMIT: ["planner instruction"],
            HookEvent.SESSION_END: [None],
        },
    )

    receipt = runtime.submit_turn(_request())

    assert receipt.status == "completed"
    invoked = _invoked_events(events, payloads, receipt.turn_id)
    assert invoked == ["SessionStart", "UserPromptSubmit", "SessionEnd"]
    context_event = next(event for event in events.events_after(receipt.turn_id) if event["type"] == "hook.context")
    context_ref = context_event["data"]["payload_ref"]
    assert isinstance(context_ref, str)
    assert planner.calls[0][1].get(context_ref) == {
        "schema_version": "1.0.0",
        "event": "UserPromptSubmit",
        "items": ["planner instruction"],
    }


def test_subagent_hooks_are_durable_idempotent_and_stop_cannot_block_terminal() -> None:
    hook_payloads: list[tuple[HookEvent, Mapping[str, object]]] = []
    runtime, events, payloads = _runtime(
        planner=_DecisionPlanner([{"type": "complete", "summary": "child done"}]),
        provider=_Provider(),
        hook_outputs={
            HookEvent.SUBAGENT_START: [{"additionalContext": "observe child"}],
            HookEvent.SUBAGENT_STOP: [{"decision": "block", "reason": "audit only"}],
        },
        hook_payloads=hook_payloads,
    )
    request = _subagent_request()

    accepted = runtime.accept_turn(request)
    replay = runtime.accept_turn(request)
    assert accepted.status == "running"
    assert replay.replayed is True
    assert _invoked_events(events, payloads, accepted.turn_id) == ["SubagentStart"]

    terminal = runtime.run_accepted_turn(accepted.turn_id)
    assert terminal.status == "completed"
    assert _invoked_events(events, payloads, accepted.turn_id) == [
        "SubagentStart", "SubagentStop",
    ]
    event_stream = tuple(events.events_after(accepted.turn_id))
    stop_index = next(
        index
        for index, event in enumerate(event_stream)
        if event["type"] == "hook.invoked"
        and payloads.get(event["data"]["receipt_ref"])["event"] == "SubagentStop"
    )
    terminal_index = next(
        index for index, event in enumerate(event_stream)
        if event["type"] == "turn.completed"
    )
    assert stop_index < terminal_index
    assert len(hook_payloads) == 2
    start_event, start_payload = hook_payloads[0]
    stop_event, stop_payload = hook_payloads[1]
    assert start_event is HookEvent.SUBAGENT_START
    assert stop_event is HookEvent.SUBAGENT_STOP
    assert set(start_payload) == {
        "turn_id", "session_id", "agent_run_id", "parent_run_id", "profile_id",
        "profile_revision", "depth", "spawn_operation_id",
    }
    assert start_payload["agent_run_id"] == "agent-run-child-001"
    assert stop_payload == {
        **start_payload,
        "terminal_type": "turn.completed",
        "terminal_status": "completed",
    }
    assert runtime.accept_turn(request).replayed is True
    assert _invoked_events(events, payloads, accepted.turn_id) == [
        "SubagentStart", "SubagentStop",
    ]


def test_normal_turn_never_invokes_subagent_lifecycle_hooks() -> None:
    runtime, events, payloads = _runtime(
        planner=_DecisionPlanner([{"type": "complete", "summary": "main done"}]),
        provider=_Provider(),
        hook_outputs={
            HookEvent.SUBAGENT_START: [None],
            HookEvent.SUBAGENT_STOP: [None],
        },
    )

    receipt = runtime.submit_turn(_request())

    assert receipt.status == "completed"
    assert _invoked_events(events, payloads, receipt.turn_id) == []


def _runtime(*, planner: _DecisionPlanner, provider: _Provider, hook_outputs: Mapping[HookEvent, list[object | None]], requires_approval: bool = False, hook_payloads: list[tuple[HookEvent, Mapping[str, object]]] | None = None):
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    state = InMemoryTurnStateStore()
    registry = ScopedCapabilityRegistry()
    registry.register(
        CapabilityDefinition(
            "memory.recall", 1, "read", requires_approval, "read_only",
            "crp://default/contracts/memory-recall-input.schema.json",
            "crp://default/contracts/memory-recall-output.schema.json",
        ),
        provider,
    )
    return (
        SynchronousAIRuntime(
            planner=planner,
            registry=registry,
            events=events,
            payloads=payloads,
            state=state,
            hook_host=_host(hook_outputs, hook_payloads=hook_payloads),
            frozen_hook_authorization_check=lambda *_args: True,
        ),
        events,
        payloads,
    )


def _host(
    outputs: Mapping[HookEvent, list[object | None]],
    *,
    hook_payloads: list[tuple[HookEvent, Mapping[str, object]]] | None = None,
) -> CodexHookHost:
    manifests = tuple(
        HookHandlerManifest(
            hook_id=f"{event.value}-handler",
            revision=f"{event.value}-r1",
            event=event,
            config_order=index,
        )
        for index, event in enumerate(outputs)
    )
    calls = {event: 0 for event in outputs}

    def runner(manifest, payload):
        event = manifest.event
        if hook_payloads is not None:
            hook_payloads.append((event, dict(payload)))
        index = calls[event]
        calls[event] += 1
        values = outputs[event]
        output = values[min(index, len(values) - 1)]
        return HookRun(
            config_order=manifest.config_order,
            completion_order=index,
            synchronous=manifest.synchronous,
            stdout=(
                "" if output is None
                else output if isinstance(output, str)
                else json.dumps(output)
            ),
            hook_id=manifest.hook_id,
        )

    return CodexHookHost(
        catalog=HookPolicyCatalog(HookPolicySnapshot(revision="lifecycle-r1", handlers=manifests)),
        runner=RevisionPinnedHookRunner({
            (manifest.hook_id, manifest.revision): runner for manifest in manifests
        }),
    )


def _request() -> dict[str, object]:
    return json.loads((
        ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json"
    ).read_text(encoding="utf-8"))


def _subagent_request() -> dict[str, object]:
    request = _request()
    request["agent_binding"] = {
        "schema_version": "1.0.0",
        "kind": "internal_agent_run_v1",
        "run_id": "agent-run-child-001",
        "role": "subagent",
        "profile_id": "subagent.researcher",
        "profile_revision": 3,
        "model_tier": "standard",
        "parent_run_id": "agent-run-parent-001",
        "link_id": "agent-link-child-001",
        "reservation_id": "agent-reservation-001",
        "spawn_operation_id": "op-agent-spawn-child-001",
        "depth": 1,
        "cancel_epoch": 0,
        "budget_snapshot_ref": "crp://agent-runs/agent-run-child-001/budget-snapshot",
    }
    return request


def _tool_decision() -> dict[str, object]:
    return {"type": "tool", "capability_id": "memory.recall", "arguments": {"query": "hook lifecycle"}}


def _event_types(events: InMemoryTurnEventStore, turn_id: str) -> list[str]:
    return [str(event["type"]) for event in events.events_after(turn_id)]


def _invoked_events(events: InMemoryTurnEventStore, payloads: InMemoryTurnPayloadStore, turn_id: str) -> list[str]:
    return [
        str(payloads.get(event["data"]["receipt_ref"])["event"])
        for event in events.events_after(turn_id)
        if event["type"] == "hook.invoked"
    ]
