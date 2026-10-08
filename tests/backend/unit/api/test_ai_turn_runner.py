from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from threading import Barrier, Lock, Thread
from time import monotonic
from datetime import datetime, timedelta, timezone

import pytest

from backend.api.ai_turn_runner import AITurnRunner, AITurnRunnerCapacityError
from backend.api.ai_turn_runner import shutdown_ai_turn_runner
from backend.api.mcp_runtime import shutdown_ai_mcp_runtime
from backend.api.mcp_runtime import MCPConnectionManager
from core.ai_kernel import (
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    InMemoryTurnStateStore,
    SQLiteAITurnStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
    TurnReceipt,
    RunLeaseToken,
    RunLeaseRevoked,
)


ROOT = Path(__file__).resolve().parents[4]


class _Runtime:
    def __init__(self, *, fail: bool = False) -> None:
        self.gate = Event()
        self.started = Event()
        self.fail = fail
        self.failed_turn_ids: list[str] = []
        self.leases: dict[str, tuple[str, int]] = {}
        self.receipts: dict[str, TurnReceipt] = {}
        self.cancel_requests: list[tuple[str, object, str | None]] = []
        self.renewals = 0
        self.run_calls = 0

    def accept_turn(self, payload):
        receipt = TurnReceipt(str(payload["turn_id"]), str(payload["session_id"]), str(payload["operation_id"]), "accepted", 1, False)
        self.receipts[receipt.turn_id] = receipt
        return receipt

    def run_accepted_turn(self, _turn_id, _run_lease=None):
        self.run_calls += 1
        self.started.set()
        self.gate.wait(2)
        if self.fail:
            raise RuntimeError("unexpected host failure")
        receipt = TurnReceipt(_turn_id, "session-test", "op-test", "completed", 2, False)
        self.receipts[receipt.turn_id] = receipt
        return receipt

    def fail_accepted_turn(self, turn_id, _run_lease=None):
        self.failed_turn_ids.append(turn_id)
        receipt = TurnReceipt(turn_id, "session-test", "op-test", "failed", 2, False)
        self.receipts[receipt.turn_id] = receipt
        return receipt
        return TurnReceipt(turn_id, "session-00000000000000000000000000000001", "op-0000000000000001", "failed", 2, False)

    def try_acquire_run_lease(self, turn_id, owner_id, *, now, stale_after):
        if turn_id in self.leases:
            return None
        token = RunLeaseToken(turn_id, owner_id, 1)
        self.leases[turn_id] = token
        return token

    def release_strict_run_lease(self, token):
        if self.leases.get(token.turn_id) == token:
            self.leases.pop(token.turn_id)

    def renew_run_lease(self, token, *, now, stale_after):
        self.renewals += 1
        return object() if self.leases.get(token.turn_id) == token else None

    def request_background_cancel(self, _turn_id, _run_lease=None, *, reason=None):
        self.cancel_requests.append((_turn_id, _run_lease, reason))
        return False

    def receipt_for(self, turn_id, *, replayed=False):
        del replayed
        return self.receipts[turn_id]


def test_runner_is_bounded_deduplicates_and_shutdown_waits() -> None:
    runtime = _Runtime()
    runner = AITurnRunner(runtime, max_workers=1, max_pending=1)
    request = _request()
    receipt = runner.accept_and_submit(request)
    assert receipt.status == "accepted" and runtime.started.wait(1)
    assert runner.accept_and_submit(request).replayed is True
    next_request = _request()
    next_request.update({"turn_id": "turn-ffffffffffffffffffffffffffffffff", "operation_id": "op-capacity-0000001", "idempotency_key": "capacity-key-000001"})
    with pytest.raises(AITurnRunnerCapacityError):
        runner.accept_and_submit(next_request)
    runtime.gate.set()
    runner.shutdown()
    assert runner.active_turn_ids == ()


def test_runner_converges_unexpected_worker_exception() -> None:
    runtime = _Runtime(fail=True)
    runtime.gate.set()
    runner = AITurnRunner(runtime, max_workers=1)
    request = _request()
    runner.accept_and_submit(request)
    runner.shutdown()
    assert runtime.failed_turn_ids == [request["turn_id"]]


def test_runner_requests_only_its_active_turn_cancel_and_forwards_reason() -> None:
    class CancellationRuntime(_Runtime):
        def request_background_cancel(self, turn_id, run_lease=None, *, reason=None):
            self.cancel_requests.append((turn_id, run_lease, reason))
            return True

    runtime = CancellationRuntime()
    runner = AITurnRunner(runtime, max_workers=1)
    request = _request()
    runner.accept_and_submit(request)
    assert runtime.started.wait(1)
    assert runner.request_turn_cancel(str(request["turn_id"]), reason="parent cancelled child") is True
    assert runtime.cancel_requests == [
        (request["turn_id"], runtime.leases[request["turn_id"]], "parent cancelled child"),
    ]
    assert runner.request_turn_cancel("turn-not-owned") is False
    runtime.gate.set()
    runner.shutdown()


def test_runner_cancel_reason_falls_back_to_legacy_runtime_signature() -> None:
    class LegacyRuntime(_Runtime):
        def request_background_cancel(self, turn_id, run_lease=None):
            self.cancel_requests.append((turn_id, run_lease, None))
            return True

    runtime = LegacyRuntime()
    runner = AITurnRunner(runtime, max_workers=1)
    request = _request()
    runner.accept_and_submit(request)
    assert runtime.started.wait(1)
    assert runner.request_turn_cancel(str(request["turn_id"]), reason="legacy reason") is True
    assert runtime.cancel_requests == [(request["turn_id"], runtime.leases[request["turn_id"]], None)]
    runtime.gate.set()
    runner.shutdown()


def test_runner_waits_for_durable_terminal_receipt_and_notifies_subscribers() -> None:
    runtime = _Runtime()
    observed: list[TurnReceipt] = []
    runner = AITurnRunner(runtime, max_workers=1, terminal_observer=lambda receipt: observed.append(receipt))
    subscriber: list[TurnReceipt] = []
    unsubscribe = runner.subscribe_terminal(lambda receipt: subscriber.append(receipt))
    request = _request()
    runner.accept_and_submit(request)
    assert runtime.started.wait(1)
    outcome: list[TurnReceipt | None] = []
    waiter = Thread(
        target=lambda: outcome.append(
            runner.wait_for_terminal(str(request["turn_id"]), timeout_seconds=1),
        ),
    )
    waiter.start()
    runtime.gate.set()
    waiter.join(1)
    assert outcome and outcome[0] is not None
    assert outcome[0].status == "completed"
    assert runner.terminal_receipt(str(request["turn_id"])).status == "completed"
    assert [receipt.status for receipt in observed] == ["completed"]
    assert [receipt.status for receipt in subscriber] == ["completed"]
    unsubscribe()
    runner.shutdown()


def test_local_terminal_wait_returns_only_after_terminal_observers_finish() -> None:
    runtime = _Runtime()
    observer_entered, observer_release = Event(), Event()

    def observe(_receipt):
        observer_entered.set()
        observer_release.wait(1)

    runner = AITurnRunner(runtime, max_workers=1, terminal_observer=observe)
    request = _request()
    runner.accept_and_submit(request)
    assert runtime.started.wait(1)
    outcome: list[TurnReceipt | None] = []
    waiter = Thread(
        target=lambda: outcome.append(
            runner.wait_for_terminal(str(request["turn_id"]), timeout_seconds=1),
        ),
    )
    waiter.start()
    runtime.gate.set()
    assert observer_entered.wait(1)
    assert outcome == []

    observer_release.set()
    waiter.join(1)

    assert outcome and outcome[0] is not None
    assert outcome[0].status == "completed"
    runner.shutdown()


def test_runner_terminal_wait_times_out_for_nonterminal_turn_and_validates_inputs() -> None:
    runtime = _Runtime()
    runner = AITurnRunner(runtime, max_workers=1)
    request = _request()
    receipt = runtime.accept_turn(request)
    assert receipt.status == "accepted"
    assert runner.wait_for_terminal(str(request["turn_id"]), timeout_seconds=0) is None
    with pytest.raises(ValueError, match="timeout"):
        runner.wait_for_terminal(str(request["turn_id"]), timeout_seconds=-1)
    with pytest.raises(ValueError, match="reason"):
        runner.request_turn_cancel(str(request["turn_id"]), reason=" ")
    runner.shutdown()


def test_two_runners_share_durable_lease_and_execute_one_planner(tmp_path: Path) -> None:
    gate = Event()
    started = Event()
    calls = {"count": 0}
    calls_lock = Lock()

    class Planner:
        def plan(self, *_args, **_kwargs):
            with calls_lock:
                calls["count"] += 1
            started.set()
            gate.wait(2)
            return {"type": "complete", "summary": "done", "evidence_refs": []}

    database = tmp_path / "shared.sqlite3"
    first = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(), events=SQLiteAITurnStore(database), payloads=SQLiteAITurnStore(database), state=SQLiteAITurnStore(database))
    second = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(), events=SQLiteAITurnStore(database), payloads=SQLiteAITurnStore(database), state=SQLiteAITurnStore(database))
    runners = (AITurnRunner(first, max_workers=1), AITurnRunner(second, max_workers=1))
    barrier = Barrier(3)
    request = _request()
    threads = [Thread(target=lambda runner=runner: (barrier.wait(), runner.accept_and_submit(request))) for runner in runners]
    for thread in threads:
        thread.start()
    barrier.wait()
    assert started.wait(1)
    gate.set()
    for thread in threads:
        thread.join(2)
    for runner in runners:
        runner.shutdown(timeout_seconds=1)
    assert calls["count"] == 1


def test_shutdown_has_deadline_and_does_not_wait_for_uncooperative_worker() -> None:
    runtime = _Runtime()
    runner = AITurnRunner(runtime, max_workers=1, lease_ttl=timedelta(seconds=0.09), heartbeat_interval_seconds=0.02)
    runner.accept_and_submit(_request())
    assert runtime.started.wait(1)
    started = monotonic()
    still_running = runner.shutdown(timeout_seconds=0.02)
    assert monotonic() - started < 0.5
    assert still_running
    renewals = runtime.renewals
    Event().wait(0.05)
    assert runtime.renewals > renewals
    runtime.gate.set()


def test_bounded_runner_shutdown_allows_following_mcp_shutdown() -> None:
    runtime = _Runtime()
    runner = AITurnRunner(runtime, max_workers=1)
    runner.accept_and_submit(_request())
    assert runtime.started.wait(1)

    class MCP(MCPConnectionManager):
        def __init__(self):
            super().__init__({}, ())
            self.closed = False

        def close_all(self):
            self.closed = True

    mcp = MCP()
    application = type("Application", (), {"state": type("State", (), {
        "ai_turn_runner": runner,
        "ai_mcp_connection_manager": mcp,
    })()})()
    shutdown_ai_turn_runner(application)
    shutdown_ai_mcp_runtime(application)
    assert mcp.closed is True
    runtime.gate.set()


def test_shutdown_cancels_queued_future_and_releases_its_durable_lease() -> None:
    runtime = _Runtime()
    runner = AITurnRunner(runtime, max_workers=1, max_pending=2)
    first = _request()
    second = _request()
    second.update({"turn_id": "turn-ffffffffffffffffffffffffffffffff", "operation_id": "op-queued-00000001", "idempotency_key": "queued-key-000001"})
    runner.accept_and_submit(first)
    assert runtime.started.wait(1)
    runner.accept_and_submit(second)
    assert second["turn_id"] in runtime.leases
    runner.shutdown(timeout_seconds=0)
    for _ in range(50):
        if second["turn_id"] not in runtime.leases:
            break
        Event().wait(0.01)
    assert second["turn_id"] not in runtime.leases
    runtime.gate.set()


def test_double_worker_convergence_failure_keeps_durable_lease_fail_closed() -> None:
    class DoubleFailureRuntime(_Runtime):
        def __init__(self):
            super().__init__(fail=True)
            self.gate.set()
            self.run_calls = 0

        def run_accepted_turn(self, turn_id, _run_lease=None):
            self.run_calls += 1
            raise RuntimeError("worker failed")

        def fail_accepted_turn(self, _turn_id, _run_lease=None):
            raise RuntimeError("durable convergence failed")

    runtime = DoubleFailureRuntime()
    first = AITurnRunner(runtime, max_workers=1)
    request = _request()
    first.accept_and_submit(request)
    for _ in range(50):
        if first.active_turn_ids == ():
            break
        Event().wait(0.01)
    assert request["turn_id"] in runtime.leases
    second = AITurnRunner(runtime, max_workers=1)
    replay = second.accept_and_submit(request)
    assert replay.replayed is True
    assert runtime.run_calls == 1
    runtime.release_strict_run_lease(runtime.leases[request["turn_id"]])
    first.shutdown(timeout_seconds=0)
    second.shutdown(timeout_seconds=0)


def test_revoked_worker_does_not_fallback_or_release_strict_lease() -> None:
    class RevokedRuntime(_Runtime):
        def run_accepted_turn(self, _turn_id, _run_lease=None):
            self.run_calls += 1
            raise RunLeaseRevoked()

        def fail_accepted_turn(self, _turn_id, _run_lease=None):
            raise AssertionError("fallback must not run")

    runtime = RevokedRuntime()
    runner = AITurnRunner(runtime, max_workers=1, heartbeat_interval_seconds=0.01)
    request = _request()
    runner.accept_and_submit(request)
    for _ in range(50):
        if runner.active_turn_ids == ():
            break
        Event().wait(0.01)
    assert runtime.run_calls == 1
    assert request["turn_id"] in runtime.leases
    runner.shutdown(timeout_seconds=0)


def test_heartbeat_renews_active_lease_and_stops_after_safe_completion() -> None:
    runtime = _Runtime()
    runner = AITurnRunner(runtime, max_workers=1, lease_ttl=timedelta(seconds=0.09), heartbeat_interval_seconds=0.02)
    runner.accept_and_submit(_request())
    assert runtime.started.wait(1)
    for _ in range(50):
        if runtime.renewals >= 2:
            break
        Event().wait(0.01)
    assert runtime.renewals >= 2
    runtime.gate.set()
    runner.shutdown(timeout_seconds=0.5)
    assert not runner._heartbeat_thread.is_alive()


def test_approval_action_uses_lease_heartbeat_and_releases_only_after_terminal_receipt() -> None:
    class ActionRuntime(_Runtime):
        def __init__(self) -> None:
            super().__init__()
            self.action_started = Event()

        def apply_action(self, action, _run_lease=None):
            self.action_started.set()
            self.gate.wait(2)
            return TurnReceipt(str(action["turn_id"]), "session-test", "op-test", "completed", 3, False)

    runtime = ActionRuntime()
    runner = AITurnRunner(runtime, max_workers=1, lease_ttl=timedelta(seconds=0.09), heartbeat_interval_seconds=0.02)
    outcome: list[TurnReceipt] = []
    thread = Thread(target=lambda: outcome.append(runner.apply_action_and_wait(_action())))
    thread.start()
    assert runtime.action_started.wait(1)
    for _ in range(50):
        if runtime.renewals >= 2:
            break
        Event().wait(0.01)
    assert runtime.renewals >= 2
    runtime.gate.set()
    thread.join(1)
    assert outcome and outcome[0].status == "completed"
    assert _action()["turn_id"] not in runtime.leases
    runner.shutdown()


def test_approval_action_base_exception_keeps_lease_for_startup_recovery() -> None:
    class AbortRuntime(_Runtime):
        def apply_action(self, _action, _run_lease=None):
            raise SystemExit("simulated process abort")

    runtime = AbortRuntime()
    runner = AITurnRunner(runtime, max_workers=1)
    action = _action()
    with pytest.raises(SystemExit, match="simulated process abort"):
        runner.apply_action_and_wait(action)
    for _ in range(50):
        if runner._active_actions == {}:  # noqa: SLF001 - assert durable release boundary.
            break
        Event().wait(0.01)
    assert action["turn_id"] in runtime.leases
    runner.shutdown(timeout_seconds=0)


def test_shutdown_cancels_queued_approval_action_and_releases_its_lease() -> None:
    runtime = _Runtime()
    runner = AITurnRunner(runtime, max_workers=1, max_pending=2)
    runner.accept_and_submit(_request())
    assert runtime.started.wait(1)
    action = _action()
    action["turn_id"] = "turn-ffffffffffffffffffffffffffffffff"
    outcomes: list[BaseException] = []

    def apply_queued_action() -> None:
        try:
            runner.apply_action_and_wait(action)
        except BaseException as error:  # cancelled Future is expected at shutdown
            outcomes.append(error)

    thread = Thread(target=apply_queued_action)
    thread.start()
    for _ in range(50):
        if action["turn_id"] in runtime.leases:
            break
        Event().wait(0.01)
    assert action["turn_id"] in runtime.leases

    runner.shutdown(timeout_seconds=0)
    thread.join(1)
    for _ in range(50):
        if action["turn_id"] not in runtime.leases:
            break
        Event().wait(0.01)
    assert action["turn_id"] not in runtime.leases
    assert outcomes
    runtime.gate.set()


def _request() -> dict[str, object]:
    return json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))


def _action() -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "action_id": "action-11111111111111111111111111111111",
        "turn_id": "turn-0123456789abcdef0123456789abcdef", "type": "approve",
        "target_event_id": "event-abcdef0123456789abcdef0123456789", "reason": "approved",
        "actor": "user", "expected_sequence": 5, "idempotency_key": "approve-document-draft-0001",
        "created_at": "2026-08-23T04:00:02Z",
    }
