from __future__ import annotations

import copy
import json
from pathlib import Path

from core.ai_kernel import (
    CapabilityDefinition,
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    InMemoryTurnStateStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
    ToolExecutionBoundaryDecision,
    CodexHookHost,
    HookHandlerManifest,
    HookPolicyCatalog,
    HookPolicySnapshot,
    RevisionPinnedHookRunner,
)
from core.ai_kernel.codex_hook_parity import HookEvent, HookRun


ROOT = Path(__file__).resolve().parents[2]


class _Planner:
    def __init__(self, arguments=None) -> None:
        self.arguments = arguments or {"query": "raw"}

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        if any(event["type"] == "tool.completed" for event in events):
            return {"type": "complete", "summary": "done"}
        return {"type": "tool", "capability_id": "memory.recall", "arguments": self.arguments}


class _Provider:
    def __init__(self) -> None:
        self.calls = []

    def invoke(self, request):
        self.calls.append(request)
        return {"summary": "called", "result": {"ok": True}}


class _Boundary:
    def __init__(self, outcome: str, arguments=None) -> None:
        self.outcome = outcome
        self.arguments = arguments or {"query": "sanitized"}
        self.calls = 0

    def evaluate(self, request, capability, decision):
        self.calls += 1
        return ToolExecutionBoundaryDecision(
            outcome=self.outcome,
            reason_codes=(f"test_{self.outcome}",),
            matched_grant_ids=(),
            policy_revision=7,
            requires_receipt=False,
            redaction_required=self.outcome == "allow_redacted",
            arguments=self.arguments,
        )


def test_boundary_deny_is_persisted_and_provider_is_not_called() -> None:
    provider = _Provider()
    boundary = _Boundary("deny", arguments={})
    runtime, events, payloads = _runtime(provider, boundary)
    receipt = runtime.submit_turn(_request())
    assert receipt.status == "failed" and provider.calls == []
    requested = next(event for event in events.events_after(receipt.turn_id) if event["type"] == "tool.requested")
    decision = payloads.get(requested["data"]["payload_ref"])
    assert decision["outcome"] == "deny"
    assert decision["policy_revision"] == 7


def test_boundary_ask_uses_same_sanitized_decision_after_approval() -> None:
    provider = _Provider()
    boundary = _Boundary("ask", arguments={"query": "[[CRP:EMAIL:safe-placeholder-value]]"})
    runtime, events, _payloads = _runtime(provider, boundary)
    request = _request()
    waiting = runtime.submit_turn(request)
    assert waiting.status == "waiting_approval" and boundary.calls == 1
    approval_event = tuple(events.events_after(waiting.turn_id))[-1]
    action = {
        "schema_version": "1.0.0",
        "action_id": "action-0123456789abcdef0123456789abcdef",
        "turn_id": waiting.turn_id,
        "type": "approve",
        "target_event_id": approval_event["event_id"],
        "reason": "approved",
        "actor": "user",
        "expected_sequence": waiting.current_sequence,
        "idempotency_key": "boundary-approval-key-0001",
        "created_at": "2026-08-23T08:00:00Z",
    }
    completed = runtime.apply_action(action)
    assert completed.status == "completed"
    assert boundary.calls == 1
    assert provider.calls[0]["arguments"] == boundary.arguments


def test_boundary_redacted_allow_invokes_with_transformed_arguments() -> None:
    provider = _Provider()
    boundary = _Boundary("allow_redacted", arguments={"query": "tokenized"})
    runtime, _events, _payloads = _runtime(provider, boundary)
    assert runtime.submit_turn(_request()).status == "completed"
    assert provider.calls[0]["arguments"] == {"query": "tokenized"}


def test_pre_tool_hook_deny_short_circuits_before_boundary_and_provider() -> None:
    provider = _Provider()
    boundary = _Boundary("allow")
    host = _hook_host(
        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "local policy"}}
    )
    authorization_checks = []
    runtime, _events, _payloads = _runtime(
        provider,
        boundary,
        hook_host=host,
        frozen_hook_authorization_check=lambda *args: authorization_checks.append(args) or True,
    )

    receipt = runtime.submit_turn(_request())

    assert receipt.status == "failed"
    assert boundary.calls == 0
    assert authorization_checks == []
    assert provider.calls == []
    assert host.audit_outbox.metrics().audit_queue_depth == 1


def test_pre_tool_hook_rewrite_is_not_authorization_and_frozen_facts_still_govern() -> None:
    provider = _Provider()
    boundary = _Boundary("deny", arguments={})
    host = _hook_host(
        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {"query": "rewritten"}}}
    )
    checked = []
    runtime, _events, _payloads = _runtime(
        provider,
        boundary,
        hook_host=host,
        frozen_hook_authorization_check=lambda request, definition, decision: checked.append(
            (request, definition, decision)
        ) or False,
    )

    receipt = runtime.submit_turn(_request())

    assert receipt.status == "failed"
    assert boundary.calls == 0
    assert checked[0][2]["arguments"] == {"query": "rewritten"}
    assert provider.calls == []


def _hook_host(output):
    snapshot = HookPolicySnapshot(
        revision="hook-policy-v1",
        handlers=(HookHandlerManifest("pre-tool", "handler-v1", HookEvent.PRE_TOOL_USE, 0),),
    )

    def runner(manifest, payload):
        return HookRun(
            config_order=manifest.config_order,
            completion_order=0,
            synchronous=manifest.synchronous,
            stdout=json.dumps(output),
            hook_id=manifest.hook_id,
        )

    return CodexHookHost(
        catalog=HookPolicyCatalog(snapshot),
        runner=RevisionPinnedHookRunner({("pre-tool", "handler-v1"): runner}),
    )


def _runtime(provider, boundary, *, hook_host=None, frozen_hook_authorization_check=None):
    registry = ScopedCapabilityRegistry()
    registry.register(
        CapabilityDefinition(
            "memory.recall", 1, "read", False, "read_only",
            "crp://default/contracts/in.schema.json",
            "crp://default/contracts/out.schema.json",
        ),
        provider,
    )
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    runtime = SynchronousAIRuntime(
        planner=_Planner(), registry=registry, events=events, payloads=payloads,
        state=InMemoryTurnStateStore(), execution_boundary=boundary, hook_host=hook_host,
        frozen_hook_authorization_check=frozen_hook_authorization_check,
    )
    return runtime, events, payloads


def _request() -> dict[str, object]:
    return json.loads(
        (ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(
            encoding="utf-8"
        )
    )
