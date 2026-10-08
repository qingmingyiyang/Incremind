from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.ai_kernel import (
    CapabilityDefinition,
    CodexHookHost,
    HookHandlerManifest,
    HookPolicyCatalog,
    HookPolicySnapshot,
    RevisionPinnedHookRunner,
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    InMemoryTurnStateStore,
    ScopedCapabilityRegistry,
    ScopedTurnPayloadError,
    SQLiteAITurnStore,
    SynchronousAIRuntime,
    ToolExecutionBoundaryDecision,
)
from core.ai_kernel.codex_hook_parity import HookEvent, HookRun


ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOT_KIND = "codex-hook-policy-snapshot"


class _ToolPlanner:
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        if any(event["type"] == "tool.completed" for event in events):
            return {"type": "complete", "summary": "done"}
        return {"type": "tool", "capability_id": "memory.recall", "arguments": {"query": "raw-private-query"}}


class _Provider:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def invoke(self, request):
        self.calls.append(request)
        return {"summary": "called", "result": {"ok": True}}


class _Boundary:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, request, capability, decision):
        self.calls += 1
        return ToolExecutionBoundaryDecision(
            outcome="allow",
            reason_codes=("test_allow",),
            matched_grant_ids=(),
            policy_revision=1,
            requires_receipt=False,
            redaction_required=False,
            arguments=dict(decision["arguments"]),
        )


class _ReceiptPersistenceFailureStore(InMemoryTurnPayloadStore):
    def put(self, turn_id: str, kind: str, payload: object) -> str:
        if kind == "codex-hook-invocation-receipt":
            raise OSError("receipt authority unavailable")
        return super().put(turn_id, kind, payload)


class _HookEventPersistenceFailureStore(InMemoryTurnEventStore):
    def append(self, event, *, expected_sequence, run_lease=None):
        if event.get("type") == "hook.invoked":
            raise OSError("hook event authority unavailable")
        return super().append(
            event, expected_sequence=expected_sequence, run_lease=run_lease
        )


class _PlannerProbingPrivateHookAuthorities:
    def __init__(self, snapshot_ref: str) -> None:
        self.snapshot_ref = snapshot_ref
        self.denied_refs: list[str] = []

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        receipt_ref = next(
            (
                event["data"]["receipt_ref"]
                for event in events
                if event["type"] == "hook.invoked"
            ),
            None,
        )
        if receipt_ref is None:
            return {"type": "tool", "capability_id": "memory.recall", "arguments": {"query": "first"}}
        for ref in (self.snapshot_ref, receipt_ref):
            with pytest.raises(ScopedTurnPayloadError):
                payloads.get(ref)
            self.denied_refs.append(ref)
        return {"type": "complete", "summary": "private hook authority stayed private"}


def test_accept_persists_immutable_snapshot_and_hook_event_only_links_safe_receipt() -> None:
    provider = _Provider()
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    state = InMemoryTurnStateStore()
    runtime = _runtime(
        provider=provider,
        boundary=_Boundary(),
        host=_host("hook-policy-r1", "hook-r1", deny_reason="C:/private/secret.txt"),
        events=events,
        payloads=payloads,
        state=state,
    )
    request = _request()

    accepted = runtime.accept_turn(request)

    frozen = payloads.get_immutable_payload(accepted.turn_id, _SNAPSHOT_KIND)
    assert frozen is not None
    snapshot_ref, snapshot = frozen
    assert snapshot["revision"] == "hook-policy-r1"
    assert snapshot["handlers"][0]["handler_id"] == "hook-r1"
    assert tuple(events.events_after(accepted.turn_id))[0]["type"] == "turn.accepted"

    failed = runtime.run_accepted_turn(accepted.turn_id)

    assert failed.status == "failed"
    invoked = next(event for event in events.events_after(accepted.turn_id) if event["type"] == "hook.invoked")
    data = invoked["data"]
    assert data["payload_ref"] is None
    assert data["receipt_ref"] and data["receipt_ref"] != snapshot_ref
    encoded = json.dumps(payloads.get(data["receipt_ref"]))
    assert "raw-private-query" not in encoded
    assert "C:/private/secret.txt" not in encoded
    assert "raw_stdout" not in encoded
    assert "secret" not in encoded.casefold()


def test_hook_receipt_persistence_failure_before_deny_keeps_all_execution_boundaries_idle() -> None:
    provider = _Provider()
    boundary = _Boundary()
    checked: list[object] = []
    runtime = _runtime(
        provider=provider,
        boundary=boundary,
        host=_host("hook-policy-r1", "hook-r1", deny_reason="blocked"),
        events=InMemoryTurnEventStore(),
        payloads=_ReceiptPersistenceFailureStore(),
        state=InMemoryTurnStateStore(),
        frozen_check=lambda *args: checked.append(args) or True,
    )

    result = runtime.submit_turn(_request())

    assert result.status == "failed"
    assert checked == []
    assert boundary.calls == 0
    assert provider.calls == []


def test_hook_event_append_failure_keeps_all_execution_boundaries_idle() -> None:
    provider = _Provider()
    boundary = _Boundary()
    checked: list[object] = []
    events = _HookEventPersistenceFailureStore()
    payloads = InMemoryTurnPayloadStore()
    runtime = _runtime(
        provider=provider,
        boundary=boundary,
        host=_host("hook-policy-r1", "hook-r1"),
        events=events,
        payloads=payloads,
        state=InMemoryTurnStateStore(),
        frozen_check=lambda *args: checked.append(args) or True,
    )

    result = runtime.submit_turn(_request())

    assert result.status == "failed"
    assert checked == []
    assert boundary.calls == 0
    assert provider.calls == []
    assert all(event["type"] != "tool.requested" for event in events.events_after(result.turn_id))


def test_restart_uses_turn_frozen_r1_not_runtime_b_catalog_r2() -> None:
    provider = _Provider()
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    state = InMemoryTurnStateStore()
    runtime_a = _runtime(
        provider=provider,
        boundary=_Boundary(),
        host=_host("hook-policy-r1", "hook-r1"),
        events=events,
        payloads=payloads,
        state=state,
    )
    accepted = runtime_a.accept_turn(_request())
    invoked_handler_ids: list[str] = []
    runtime_b = _runtime(
        provider=provider,
        boundary=_Boundary(),
        host=_host("hook-policy-r2", "hook-r2", invoked_handler_ids=invoked_handler_ids),
        events=events,
        payloads=payloads,
        state=state,
    )
    result = runtime_b.run_accepted_turn(accepted.turn_id)

    assert result.status == "completed"
    assert provider.calls
    assert invoked_handler_ids == ["hook-r1"]
    receipt_event = next(event for event in events.events_after(accepted.turn_id) if event["type"] == "hook.invoked")
    receipt = payloads.get(receipt_event["data"]["receipt_ref"])
    assert receipt["policy_snapshot_revision"] == "hook-policy-r1"
    assert receipt["handler_runs"][0]["handler_id"] == "hook-r1"


def test_sqlite_restart_uses_durable_turn_snapshot_and_receipt(tmp_path: Path) -> None:
    database = tmp_path / "hook-session.sqlite3"
    provider = _Provider()
    store_a = SQLiteAITurnStore(database)
    runtime_a = _runtime(
        provider=provider,
        boundary=_Boundary(),
        host=_host("hook-policy-r1", "hook-r1"),
        events=store_a,
        payloads=store_a,
        state=store_a,
    )
    accepted = runtime_a.accept_turn(_request())

    invoked_handler_ids: list[str] = []
    store_b = SQLiteAITurnStore(database)
    runtime_b = _runtime(
        provider=provider,
        boundary=_Boundary(),
        host=_host("hook-policy-r2", "hook-r2", invoked_handler_ids=invoked_handler_ids),
        events=store_b,
        payloads=store_b,
        state=store_b,
    )
    result = runtime_b.run_accepted_turn(accepted.turn_id)

    assert result.status == "completed"
    assert invoked_handler_ids == ["hook-r1"]
    frozen = store_b.get_immutable_payload(accepted.turn_id, _SNAPSHOT_KIND)
    assert frozen is not None and frozen[1]["revision"] == "hook-policy-r1"
    hook_event = next(
        event for event in store_b.events_after(accepted.turn_id)
        if event["type"] == "hook.invoked"
    )
    receipt = store_b.get(hook_event["data"]["receipt_ref"])
    assert receipt["policy_snapshot_ref"] == frozen[0]
    assert receipt["policy_snapshot_revision"] == "hook-policy-r1"


def test_restart_executes_revision_pinned_r1_behavior_not_current_r2_behavior() -> None:
    provider = _Provider()
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    state = InMemoryTurnStateStore()
    runtime_a = _runtime(
        provider=provider,
        boundary=_Boundary(),
        host=_host("hook-policy-r1", "hook-r1"),
        events=events,
        payloads=payloads,
        state=state,
    )
    accepted = runtime_a.accept_turn(_request())
    r1_calls: list[str] = []

    def pass_r1(manifest, payload):
        r1_calls.append(manifest.revision)
        return HookRun(0, 0, True, stdout="", hook_id=manifest.hook_id)

    def deny_r2(manifest, payload):
        return HookRun(
            0, 0, True,
            stdout=json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": "r2 must not control old Turn",
            }}),
            hook_id=manifest.hook_id,
        )

    current = HookPolicySnapshot(
        revision="hook-policy-r2",
        handlers=(HookHandlerManifest("hook-r2", "hook-r2-revision", HookEvent.PRE_TOOL_USE, 0),),
    )
    host_b = CodexHookHost(
        catalog=HookPolicyCatalog(current),
        runner=RevisionPinnedHookRunner({
            ("hook-r1", "hook-r1-revision"): pass_r1,
            ("hook-r2", "hook-r2-revision"): deny_r2,
        }),
    )
    runtime_b = _runtime(
        provider=provider,
        boundary=_Boundary(),
        host=host_b,
        events=events,
        payloads=payloads,
        state=state,
    )

    result = runtime_b.run_accepted_turn(accepted.turn_id)

    assert result.status == "completed"
    assert r1_calls == ["hook-r1-revision"]
    assert provider.calls


def test_restart_without_frozen_handler_revision_fails_closed() -> None:
    provider = _Provider()
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    state = InMemoryTurnStateStore()
    runtime_a = _runtime(
        provider=provider,
        boundary=_Boundary(),
        host=_host("hook-policy-r1", "hook-r1"),
        events=events,
        payloads=payloads,
        state=state,
    )
    accepted = runtime_a.accept_turn(_request())
    current = HookPolicySnapshot(
        revision="hook-policy-r2",
        handlers=(HookHandlerManifest("hook-r2", "hook-r2-revision", HookEvent.PRE_TOOL_USE, 0),),
    )
    host_b = CodexHookHost(
        catalog=HookPolicyCatalog(current),
        runner=RevisionPinnedHookRunner({
            ("hook-r2", "hook-r2-revision"): lambda manifest, payload: HookRun(
                0, 0, True, stdout="", hook_id=manifest.hook_id
            ),
        }),
    )
    runtime_b = _runtime(
        provider=provider,
        boundary=_Boundary(),
        host=host_b,
        events=events,
        payloads=payloads,
        state=state,
    )

    result = runtime_b.run_accepted_turn(accepted.turn_id)

    assert result.status == "failed"
    assert provider.calls == []


@pytest.mark.parametrize("mode", ["missing", "tampered"])
def test_missing_or_tampered_frozen_snapshot_fails_closed_before_provider(mode: str) -> None:
    provider = _Provider()
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    state = InMemoryTurnStateStore()
    runtime_a = _runtime(
        provider=provider,
        boundary=_Boundary(),
        host=_host("hook-policy-r1", "hook-r1"),
        events=events,
        payloads=payloads,
        state=state,
    )
    accepted = runtime_a.accept_turn(_request())
    identity = (accepted.turn_id, _SNAPSHOT_KIND)
    with payloads._lock:  # type: ignore[attr-defined]  # fault injection against reference in-memory store
        ref, _payload, encoded = payloads._immutable_payloads[identity]  # type: ignore[attr-defined]
        if mode == "missing":
            payloads._immutable_payloads.pop(identity)  # type: ignore[attr-defined]
            payloads._payloads.pop(ref)  # type: ignore[attr-defined]
        else:
            invalid = {"schema_version": "1.0.0", "kind": "tampered"}
            payloads._immutable_payloads[identity] = (ref, invalid, encoded)  # type: ignore[attr-defined]
            payloads._payloads[ref] = invalid  # type: ignore[attr-defined]
    runtime_b = _runtime(
        provider=provider,
        boundary=_Boundary(),
        host=_host("hook-policy-r2", "hook-r2"),
        events=events,
        payloads=payloads,
        state=state,
    )

    result = runtime_b.run_accepted_turn(accepted.turn_id)

    assert result.status == "failed"
    assert provider.calls == []


def test_planner_scoped_payload_cannot_read_hook_receipt_or_snapshot() -> None:
    provider = _Provider()
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    state = InMemoryTurnStateStore()
    bootstrap = _runtime(
        provider=provider,
        boundary=_Boundary(),
        host=_host("hook-policy-r1", "hook-r1"),
        events=events,
        payloads=payloads,
        state=state,
    )
    accepted = bootstrap.accept_turn(_request())
    snapshot = payloads.get_immutable_payload(accepted.turn_id, _SNAPSHOT_KIND)
    assert snapshot is not None
    planner = _PlannerProbingPrivateHookAuthorities(snapshot[0])
    runtime = _runtime(
        provider=provider,
        boundary=_Boundary(),
        host=_host("hook-policy-r2", "hook-r2"),
        events=events,
        payloads=payloads,
        state=state,
        planner=planner,
    )

    result = runtime.run_accepted_turn(accepted.turn_id)

    assert result.status == "completed", tuple(events.events_after(accepted.turn_id))
    assert len(planner.denied_refs) == 2
    assert planner.denied_refs[0] == snapshot[0]
    assert planner.denied_refs[1].startswith(f"crp://session/{accepted.turn_id}/codex-hook-invocation-receipt/")


def test_all_codex_lifecycle_events_use_one_durable_turn_frozen_entry() -> None:
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()
    state = InMemoryTurnStateStore()
    observed: list[HookEvent] = []
    handlers = tuple(
        HookHandlerManifest(
            f"hook-{event.value.casefold()}",
            f"revision-{event.value.casefold()}",
            event,
            order,
        )
        for order, event in enumerate(HookEvent)
    )
    snapshot = HookPolicySnapshot(revision="hook-policy-all-events", handlers=handlers)

    def runner(manifest, payload):
        observed.append(manifest.event)
        return HookRun(
            config_order=manifest.config_order,
            completion_order=0,
            synchronous=manifest.synchronous,
            stdout="",
            hook_id=manifest.hook_id,
        )

    pinned = RevisionPinnedHookRunner({
        (manifest.hook_id, manifest.revision): runner for manifest in handlers
    })
    runtime = _runtime(
        provider=_Provider(),
        boundary=_Boundary(),
        host=CodexHookHost(catalog=HookPolicyCatalog(snapshot), runner=pinned),
        events=events,
        payloads=payloads,
        state=state,
    )

    accepted = runtime.accept_turn(_request())
    for event in HookEvent:
        if event not in {HookEvent.SESSION_START, HookEvent.USER_PROMPT_SUBMIT}:
            runtime.invoke_lifecycle_hook(
                accepted.turn_id,
                event,
                {"turn_id": accepted.turn_id, "event": event.value},
            )

    invoked = tuple(
        item for item in events.events_after(accepted.turn_id)
        if item["type"] == "hook.invoked"
    )
    receipt_events = {
        payloads.get(item["data"]["receipt_ref"])["event"] for item in invoked
    }
    assert receipt_events == {event.value for event in HookEvent}
    assert set(observed) == set(HookEvent)
    assert all(item["data"]["payload_ref"] is None for item in invoked)


def _runtime(
    *,
    provider: _Provider,
    boundary: _Boundary,
    host: CodexHookHost,
    events: InMemoryTurnEventStore,
    payloads: InMemoryTurnPayloadStore,
    state: InMemoryTurnStateStore,
    frozen_check=None,
    planner=None,
) -> SynchronousAIRuntime:
    registry = ScopedCapabilityRegistry()
    registry.register(
        CapabilityDefinition(
            "memory.recall", 1, "read", False, "read_only",
            "crp://default/contracts/in.schema.json",
            "crp://default/contracts/out.schema.json",
        ),
        provider,
    )
    return SynchronousAIRuntime(
        planner=planner or _ToolPlanner(),
        registry=registry,
        events=events,
        payloads=payloads,
        state=state,
        execution_boundary=boundary,
        hook_host=host,
        frozen_hook_authorization_check=frozen_check or (lambda *_args: True),
    )


def _host(
    policy_revision: str,
    handler_id: str,
    *,
    deny_reason: str | None = None,
    invoked_handler_ids: list[str] | None = None,
) -> CodexHookHost:
    snapshot = HookPolicySnapshot(
        revision=policy_revision,
        handlers=(HookHandlerManifest(handler_id, f"{handler_id}-revision", HookEvent.PRE_TOOL_USE, 0),),
    )

    def runner(manifest, payload):
        if invoked_handler_ids is not None:
            invoked_handler_ids.append(manifest.hook_id)
        output = (
            {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": deny_reason}}
            if deny_reason is not None
            else None
        )
        return HookRun(
            config_order=manifest.config_order,
            completion_order=0,
            synchronous=manifest.synchronous,
            stdout=json.dumps(output) if output is not None else "",
            hook_id=manifest.hook_id,
        )

    pinned = RevisionPinnedHookRunner({
        ("hook-r1", "hook-r1-revision"): runner,
        ("hook-r2", "hook-r2-revision"): runner,
        (handler_id, f"{handler_id}-revision"): runner,
    })
    return CodexHookHost(catalog=HookPolicyCatalog(snapshot), runner=pinned)


def _request() -> dict[str, object]:
    return json.loads(
        (ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(
            encoding="utf-8"
        )
    )
