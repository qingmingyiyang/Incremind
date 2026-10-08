from __future__ import annotations

import json
from pathlib import Path
from threading import Barrier, Event
from time import monotonic, sleep

import pytest
from jsonschema import Draft202012Validator

import core.ai_kernel.codex_hook_runtime as hook_runtime
from core.ai_kernel.codex_hook_parity import (
    HookEvent,
    HookRun,
    HookRunStatus,
    PreToolUseBlocked,
    dispatch_pre_tool_use_hot_path,
)
from core.ai_kernel.codex_hook_runtime import (
    CODEX_HOOK_PARITY_REVISION,
    CodexHookHost,
    HookAuditOutbox,
    HookHandlerManifest,
    HookPolicyCatalog,
    HookPolicySnapshot,
    hook_invocation_receipt_to_payload,
    hook_policy_snapshot_from_payload,
    hook_policy_snapshot_to_payload,
)


ROOT = Path(__file__).resolve().parents[2]


def _manifest(
    hook_id: str,
    *,
    revision: str = "handler-v1",
    event: HookEvent = HookEvent.PRE_TOOL_USE,
    config_order: int = 0,
    synchronous: bool = True,
) -> HookHandlerManifest:
    return HookHandlerManifest(
        hook_id=hook_id,
        revision=revision,
        event=event,
        config_order=config_order,
        synchronous=synchronous,
    )


def _snapshot(revision: str, *handlers: HookHandlerManifest) -> HookPolicySnapshot:
    return HookPolicySnapshot(revision=revision, handlers=handlers)


def _run(*, stdout: object | None = None, exit_code: int = 0, stderr: str = "") -> HookRun:
    return HookRun(
        config_order=999,
        completion_order=999,
        synchronous=False,
        exit_code=exit_code,
        stdout="" if stdout is None else json.dumps(stdout),
        stderr=stderr,
        hook_id="runner-must-be-normalized",
    )


def _nearest_rank(samples: tuple[int, ...], percentile: int) -> int:
    ordered = sorted(samples)
    index = max(0, (len(ordered) * percentile + 99) // 100 - 1)
    return ordered[index]


def test_host_runs_real_local_runner_and_records_pre_tool_deny_receipt() -> None:
    manifest = _manifest("deny-private-write", revision="handler-r3")
    calls: list[tuple[HookHandlerManifest, dict[str, object]]] = []

    def runner(received_manifest: HookHandlerManifest, payload: dict[str, object]) -> HookRun:
        calls.append((received_manifest, payload))
        return _run(stdout={
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "private scope",
            }
        })

    host = CodexHookHost(
        catalog=HookPolicyCatalog(_snapshot("policy-r7", manifest)),
        runner=runner,
    )
    payload = {"tool_name": "write_file", "path": "private.txt"}

    receipt = host.invoke(HookEvent.PRE_TOOL_USE, payload)

    assert calls == [(manifest, payload)]
    assert receipt.policy_revision == "policy-r7"
    assert receipt.codex_revision == CODEX_HOOK_PARITY_REVISION
    assert receipt.handler_ids == ("deny-private-write",)
    assert receipt.outcome.dispatch_blocked is True
    assert receipt.outcome.stop_reason == "private scope"
    assert receipt.outcome.runs[0].run.config_order == 0
    assert receipt.outcome.runs[0].run.synchronous is True
    assert host.audit_outbox.metrics().audit_queue_depth == 1


def test_host_pass_is_not_authorization_and_caller_hard_guard_still_controls_dispatch() -> None:
    manifest = _manifest("pass-through")
    host = CodexHookHost(
        catalog=HookPolicyCatalog(_snapshot("policy-r1", manifest)),
        runner=lambda _manifest, _payload: _run(),
    )
    receipt = host.invoke(HookEvent.PRE_TOOL_USE, {"path": "restricted.txt"})
    checks: list[object] = []
    dispatched: list[object] = []

    with pytest.raises(PreToolUseBlocked, match="authorization"):
        dispatch_pre_tool_use_hot_path(
            receipt.outcome,
            {"path": "restricted.txt"},
            lambda value: checks.append(value) or False,
            lambda value: dispatched.append(value),
        )

    assert receipt.outcome.dispatch_blocked is False
    assert checks == [{"path": "restricted.txt"}]
    assert dispatched == []


def test_async_control_is_observed_but_cannot_block_the_local_data_plane() -> None:
    asynchronous_deny = _manifest("async-observer", synchronous=False)
    host = CodexHookHost(
        catalog=HookPolicyCatalog(_snapshot("policy-r1", asynchronous_deny)),
        runner=lambda _manifest, _payload: _run(stdout={
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "must not control",
            }
        }),
    )

    receipt = host.invoke(HookEvent.PRE_TOOL_USE, {"path": "safe.txt"})

    assert receipt.outcome.dispatch_blocked is False
    assert receipt.outcome.runs[0].status is HookRunStatus.COMPLETED
    assert receipt.outcome.runs[0].control_ignored is True


def test_runner_timeout_or_crash_is_captured_as_fail_open_receipt_not_raised() -> None:
    timeout = _manifest("timeout", config_order=0)
    crash = _manifest("crash", config_order=1)

    def runner(manifest: HookHandlerManifest, _payload: dict[str, object]) -> HookRun:
        if manifest.hook_id == "timeout":
            raise TimeoutError("deadline")
        raise RuntimeError("unexpected")

    host = CodexHookHost(
        catalog=HookPolicyCatalog(_snapshot("policy-r1", timeout, crash)),
        runner=runner,
    )

    receipt = host.invoke(HookEvent.PRE_TOOL_USE, {"path": "allowed.txt"})

    assert receipt.outcome.dispatch_blocked is False
    assert [item.status for item in receipt.outcome.runs] == [HookRunStatus.FAILED, HookRunStatus.FAILED]
    assert "TimeoutError" in receipt.outcome.runs[0].run.stderr
    assert "RuntimeError" in receipt.outcome.runs[1].run.stderr


def test_manifest_timeout_limits_hook_wait_and_late_deny_cannot_block_dispatch() -> None:
    timeout = HookHandlerManifest(
        "slow-deny", "handler-v1", HookEvent.PRE_TOOL_USE, 0, timeout_ms=20,
    )
    started = Event()

    def runner(_manifest: HookHandlerManifest, _payload: dict[str, object]) -> HookRun:
        started.set()
        sleep(0.15)
        return _run(stdout={
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "too late",
            }
        })

    host = CodexHookHost(
        catalog=HookPolicyCatalog(_snapshot("policy-r1", timeout)), runner=runner,
    )
    before = monotonic()
    receipt = host.invoke(HookEvent.PRE_TOOL_USE, {"path": "allowed.txt"})

    assert started.is_set()
    assert monotonic() - before < 0.12
    assert receipt.outcome.dispatch_blocked is False
    assert receipt.outcome.runs[0].status is HookRunStatus.FAILED
    assert receipt.outcome.runs[0].run.stderr == "local hook runner timed out"


def test_concurrent_handlers_use_actual_completion_order_for_pre_tool_rewrites() -> None:
    slow_first = HookHandlerManifest(
        "slow-first", "handler-v1", HookEvent.PRE_TOOL_USE, 0, timeout_ms=500,
    )
    fast_second = HookHandlerManifest(
        "fast-second", "handler-v1", HookEvent.PRE_TOOL_USE, 1, timeout_ms=500,
    )
    both_started = Barrier(2)

    def runner(manifest: HookHandlerManifest, _payload: dict[str, object]) -> HookRun:
        both_started.wait(timeout=0.2)
        if manifest.hook_id == "slow-first":
            sleep(0.04)
            replacement = {"path": "slow"}
        else:
            sleep(0.01)
            replacement = {"path": "fast"}
        return _run(stdout={
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": replacement,
            }
        })

    host = CodexHookHost(
        catalog=HookPolicyCatalog(_snapshot("policy-r1", slow_first, fast_second)), runner=runner,
    )
    receipt = host.invoke(HookEvent.PRE_TOOL_USE, {"path": "original"})

    by_id = {result.run.hook_id: result.run.completion_order for result in receipt.outcome.runs}
    assert by_id == {"slow-first": 1, "fast-second": 0}
    assert receipt.outcome.updated_input == {"path": "slow"}


def test_policy_revision_switch_is_atomic_and_explicit_old_snapshot_stays_stable() -> None:
    old_handler = _manifest("old-deny", revision="handler-r1")
    new_handler = _manifest("new-pass", revision="handler-r2")
    old_snapshot = _snapshot("policy-r1", old_handler)
    catalog = HookPolicyCatalog(old_snapshot)

    def runner(manifest: HookHandlerManifest, _payload: dict[str, object]) -> HookRun:
        if manifest.revision == "handler-r1":
            return _run(stdout={
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "old policy",
                }
            })
        return _run()

    host = CodexHookHost(catalog=catalog, runner=runner)
    catalog.install(_snapshot("policy-r2", new_handler))

    old_turn = host.invoke(HookEvent.PRE_TOOL_USE, {"path": "x"}, snapshot=old_snapshot)
    new_turn = host.invoke(HookEvent.PRE_TOOL_USE, {"path": "x"})

    assert old_turn.policy_revision == "policy-r1"
    assert old_turn.outcome.dispatch_blocked is True
    assert new_turn.policy_revision == "policy-r2"
    assert new_turn.outcome.dispatch_blocked is False


def test_bounded_outbox_and_failing_audit_sink_never_block_invocation() -> None:
    outbox = HookAuditOutbox(max_items=1, latency_sample_limit=8)
    host = CodexHookHost(
        catalog=HookPolicyCatalog(_snapshot("policy-r1", _manifest("observe"))),
        runner=lambda _manifest, _payload: _run(),
        audit_outbox=outbox,
    )

    first = host.invoke(HookEvent.PRE_TOOL_USE, {"turn": 1})
    second = host.invoke(HookEvent.PRE_TOOL_USE, {"turn": 2})

    assert first.policy_revision == second.policy_revision == "policy-r1"
    assert outbox.metrics().audit_queue_depth == 1
    assert outbox.metrics().audit_dropped == 1
    assert outbox.drain(lambda _receipt: (_ for _ in ()).throw(OSError("audit offline"))) == 0
    assert outbox.metrics().audit_failures == 1
    assert outbox.metrics().audit_queue_depth == 1
    delivered: list[object] = []
    assert outbox.drain(delivered.append) == 1
    assert len(delivered) == 1
    observation = delivered[0]
    assert observation.policy_revision == second.policy_revision
    assert observation.handler_ids == second.handler_ids
    assert observation.run_statuses == ("completed",)
    assert not hasattr(observation, "outcome")


def test_latency_samples_support_p50_p95_p99_and_cumulative_hook_time(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter((0, 10, 100, 130, 200, 260))
    monkeypatch.setattr(hook_runtime, "perf_counter_ns", lambda: next(ticks))
    outbox = HookAuditOutbox(max_items=8, latency_sample_limit=8)
    host = CodexHookHost(
        catalog=HookPolicyCatalog(_snapshot("policy-r1", _manifest("observe"))),
        runner=lambda _manifest, _payload: _run(),
        audit_outbox=outbox,
    )

    for turn in range(3):
        host.invoke(HookEvent.PRE_TOOL_USE, {"turn": turn})

    metrics = outbox.metrics()
    assert metrics.latency_samples_ns == (10, 30, 60)
    assert _nearest_rank(metrics.latency_samples_ns, 50) == 30
    assert _nearest_rank(metrics.latency_samples_ns, 95) == 60
    assert _nearest_rank(metrics.latency_samples_ns, 99) == 60
    assert sum(metrics.latency_samples_ns) == 100
    assert metrics.cumulative_hook_time_ns == 100
    assert metrics.p50_ms == 30 / 1_000_000
    assert metrics.p95_ms == 60 / 1_000_000
    assert metrics.p99_ms == 60 / 1_000_000


def test_long_running_local_hook_metrics_remain_bounded_under_audit_backpressure() -> None:
    manifest = HookHandlerManifest(
        "steady-pass", "handler-v1", HookEvent.PRE_TOOL_USE, 0, timeout_ms=100,
    )
    outbox = HookAuditOutbox(max_items=16, latency_sample_limit=32)
    host = CodexHookHost(
        catalog=HookPolicyCatalog(_snapshot("policy-r1", manifest)),
        runner=lambda item, payload: HookRun(
            config_order=item.config_order,
            completion_order=0,
            synchronous=item.synchronous,
            stdout="",
            hook_id=item.hook_id,
        ),
        audit_outbox=outbox,
    )

    for index in range(200):
        receipt = host.invoke(HookEvent.PRE_TOOL_USE, {"sequence": index})
        assert receipt.outcome.dispatch_blocked is False

    metrics = outbox.metrics()
    assert metrics.audit_queue_depth == 16
    assert metrics.audit_dropped == 184
    assert len(metrics.latency_samples_ns) == 32
    assert metrics.cumulative_hook_time_ns >= sum(metrics.latency_samples_ns)
    assert 0 <= metrics.p50_ms <= metrics.p95_ms <= metrics.p99_ms


def test_policy_snapshot_payload_roundtrip_is_schema_valid_and_keeps_declared_order() -> None:
    snapshot = HookPolicySnapshot(
        revision="policy-r9",
        snapshot_id="hook-snapshot-r9",
        manifest_ref="crp://default/hooks/manifests/default",
        manifest_revision="manifest-r4",
        local_hard_guard_revision="guard-r2",
        handlers=(
            _manifest("later", event=HookEvent.STOP, config_order=3),
            HookHandlerManifest(
                "first", "handler-v7", HookEvent.PRE_TOOL_USE, 0,
                synchronous=False, enabled=False,
                handler_ref="crp://local/hooks/first-v7", timeout_ms=17,
            ),
        ),
    )

    payload = hook_policy_snapshot_to_payload(snapshot)
    schema = _schema("codex-hook-policy-snapshot.schema.json")
    assert Draft202012Validator(schema).is_valid(payload)
    restored = hook_policy_snapshot_from_payload(json.loads(json.dumps(payload)))

    assert restored.revision == "policy-r9"
    assert restored.snapshot_id == "hook-snapshot-r9"
    assert [handler.hook_id for handler in restored.handlers] == ["later", "first"]
    assert [handler.config_order for handler in restored.handlers] == [3, 0]
    assert restored.manifest_revision == "manifest-r4"
    assert restored.handlers[1].enabled is False
    assert restored.handlers[1].handler_ref == "crp://local/hooks/first-v7"
    assert restored.handlers[1].timeout_ms == 17
    assert restored.handlers[1].synchronous is False


@pytest.mark.parametrize(
    ("event", "stdout", "expected"),
    [
        (HookEvent.PRE_TOOL_USE, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "restricted"}}, {"dispatch_blocked": True, "input_rewrite_applied": False}),
        (HookEvent.PERMISSION_REQUEST, {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "deny", "reason": "restricted"}}}, {"permission": "denied"}),
        (HookEvent.POST_TOOL_USE, {"decision": "block", "reason": "verify"}, {"follow_up_blocked": True, "turn_stopped": False}),
        (HookEvent.USER_PROMPT_SUBMIT, {"continue": False, "stopReason": "pause"}, {"turn_stopped": True, "additional_context_count": 0}),
        (HookEvent.STOP, {"decision": "block", "reason": "continue"}, {"stop_allowed": False}),
        (HookEvent.SUBAGENT_START, {"additionalContext": "context"}, {"additional_context_count": 1}),
        (HookEvent.SESSION_END, None, {"observed": True}),
    ],
)
def test_receipt_payload_is_safe_schema_valid_and_event_specific(
    event: HookEvent, stdout: object | None, expected: dict[str, object],
) -> None:
    manifest = _manifest("safe-handler", event=event)
    snapshot = _snapshot("policy-r1", manifest)
    host = CodexHookHost(
        catalog=HookPolicyCatalog(snapshot),
        runner=lambda _manifest, _payload: _run(stdout=stdout),
    )
    receipt = host.invoke(event, {"path": "C:/private.txt", "secret": "must-not-serialize"})

    payload = hook_invocation_receipt_to_payload(
        receipt,
        turn_id="turn-1",
        project_id="project-1",
        policy_snapshot_ref="crp://default/hooks/snapshots/hook-snapshot-r1",
    )

    schema = _schema("codex-hook-invocation-receipt.schema.json")
    assert Draft202012Validator(schema).is_valid(payload)
    normalized = payload["normalized_outcome"]
    assert normalized["event"] == event.value
    assert all(normalized[key] == value for key, value in expected.items())
    if event is HookEvent.POST_TOOL_USE:
        assert normalized["reason"] == "verify"
    encoded = json.dumps(payload)
    assert "C:/private.txt" not in encoded
    assert "must-not-serialize" not in encoded
    assert "raw_stdout" not in encoded
    assert payload["handler_runs"][0]["handler_revision"] == "handler-v1"


def test_receipt_serializer_uses_schema_safe_nonce_when_scope_ids_are_at_max_length() -> None:
    snapshot = _snapshot("policy-r1", _manifest("safe-handler"))
    receipt = CodexHookHost(
        catalog=HookPolicyCatalog(snapshot), runner=lambda _manifest, _payload: _run(),
    ).invoke(HookEvent.PRE_TOOL_USE, {})
    max_id = "a" * 160

    payload = hook_invocation_receipt_to_payload(
        receipt,
        turn_id=max_id,
        project_id=max_id,
        policy_snapshot_ref="crp://default/hooks/snapshots/hook-snapshot-r1",
    )

    assert len(payload["invocation_id"]) <= 160
    assert Draft202012Validator(_schema("codex-hook-invocation-receipt.schema.json")).is_valid(payload)


def test_receipt_serializer_rejects_handler_not_bound_to_frozen_snapshot() -> None:
    snapshot = _snapshot("policy-r1", _manifest("actual"))
    host = CodexHookHost(
        catalog=HookPolicyCatalog(snapshot),
        runner=lambda _manifest, _payload: _run(),
    )
    receipt = host.invoke(HookEvent.PRE_TOOL_USE, {})
    other = _snapshot("policy-r1", _manifest("different"))

    with pytest.raises(hook_runtime.HookRuntimeError, match="present in the frozen snapshot"):
        hook_invocation_receipt_to_payload(
            receipt,
            turn_id=None,
            project_id=None,
            policy_snapshot_ref="crp://default/hooks/snapshots/hook-snapshot-r1",
            snapshot=other,
        )


def _schema(filename: str) -> dict[str, object]:
    return json.loads((ROOT / "core-contracts" / "ai" / filename).read_text(encoding="utf-8"))
