from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
from time import sleep

import pytest

from core.effect_log import EffectLog, EffectReaper, EffectRunner, EffectState
from core.ai_kernel import (
    AIKernelContractError,
    CapabilityDefinition,
    CodexHookHost,
    HookHandlerManifest,
    HookPolicyCatalog,
    HookPolicySnapshot,
    RevisionPinnedHookRunner,
    ScopedCapabilityRegistry,
    SQLiteAITurnStore,
    SynchronousAIRuntime,
    TurnEventConflict,
    RunLeaseRevoked,
    ToolExecutionBoundaryDecision,
    ToolProviderFailure,
    classify_recovery,
)
from core.ai_kernel.codex_hook_parity import HookEvent, HookRun
from backend.api.ai_turn_recovery_startup import scan_due_ai_turn_recovery
from backend.api.ai_turn_runner import AITurnRunner
from core.ai_tooling import ToolConnectionIdentity, ToolDefinition, ToolRetryPolicy
from core.mcp_host import TurnPayloadMCPReceiptStore


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def model_terminal_clock_matches_lease_fixtures(monkeypatch):
    """Historical lease fixtures must use the same wall clock as terminal writes."""
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)
    monkeypatch.setattr("core.ai_kernel.sqlite_store.datetime", Clock)


def test_verified_none_replay_claim_has_one_cross_process_winner(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    SQLiteAITurnStore(database).claim_turn({
        "turn_id": "turn-replay", "session_id": "session-replay",
        "operation_id": "operation-replay", "idempotency_key": "turn-replay-key",
    })
    start = tmp_path / "start"
    worker = """
import sys, time
from pathlib import Path
from core.ai_kernel import SQLiteAITurnStore
from core.mcp_host import TurnPayloadMCPReceiptStore
database, ready, start = map(Path, sys.argv[1:4])
store = TurnPayloadMCPReceiptStore(SQLiteAITurnStore(database))
ready.write_text('ready', encoding='utf-8')
deadline = time.monotonic() + 10
while not start.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(3)
    time.sleep(0.01)
claim = {
    'schema_version': '1.0.0', 'turn_id': 'turn-replay',
    'invocation_id': 'call-replay', 'operation_id': 'operation-replay',
    'idempotency_key': 'operation-replay:call-replay',
    'server_id': 'calendar-server', 'tool_name': 'calendar.create',
    'tool_id': 'calendar.create', 'effect_certainty': 'confirmed_none',
    'attempt': 1,
}
print('1' if store.reserve_verified_none_replay(claim) else '0', flush=True)
"""
    environment = dict(os.environ)
    source_path = str(ROOT / "src")
    environment["PYTHONPATH"] = source_path + os.pathsep + environment.get("PYTHONPATH", "")
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", worker, str(database), str(tmp_path / f"ready-{index}"), str(start)],
            cwd=ROOT, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for index in range(2)
    ]
    deadline = datetime.now(timezone.utc) + timedelta(seconds=10)
    while not all((tmp_path / f"ready-{index}").exists() for index in range(2)):
        if datetime.now(timezone.utc) >= deadline:
            for process in processes:
                process.kill()
            raise AssertionError("cross-process replay workers did not become ready")
        sleep(0.01)
    start.write_text("go", encoding="utf-8")
    results = [process.communicate(timeout=15) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], results
    assert sorted(stdout.strip() for stdout, _stderr in results) == ["0", "1"]


def test_mcp_side_effect_uses_effect_for_unknown_verified_replay_and_settlement(tmp_path: Path) -> None:
    database = tmp_path / "mcp-effects.sqlite3"
    turn_store = SQLiteAITurnStore(database)
    turn_store.claim_turn({
        "turn_id": "turn-mcp", "session_id": "session-mcp",
        "operation_id": "operation-mcp", "idempotency_key": "turn-mcp-key",
    })
    receipts = TurnPayloadMCPReceiptStore(turn_store)
    intent = {
        "schema_version": "1.0.0", "turn_id": "turn-mcp",
        "invocation_id": "call-mcp", "operation_id": "operation-mcp",
        "idempotency_key": "operation-mcp:call-mcp", "attempt": 1,
        "server_id": "calendar-server", "tool_id": "calendar.create",
        "tool_name": "calendar.create", "protocol_version": "2025-11-25",
        "lease_ttl_seconds": 305,
    }
    assert receipts.reserve_side_effect(intent) is True
    effect = EffectLog(database).get("mcp-effect-call-mcp")
    assert effect.parent_id == "call-mcp"
    assert effect.lease_expires_at is not None
    assert effect.lease_expires_at - effect.recorded_at == 305
    assert turn_store.verify_mcp_call_effect(effect) == (
        EffectState.UNKNOWN, "mcp.remote_status_required",
    )
    recovered = EffectReaper(turn_store.effect_runner.log).recover_expired(
        now=2_000_000_000,
        probes={"mcp_call": turn_store.verify_mcp_call_effect},
        verifiers={"mcp_call": turn_store.verify_mcp_call_effect},
    )
    assert recovered[0].state is EffectState.UNKNOWN
    assert recovered[0].reason == "verifier_resolved"
    claim = {
        "schema_version": "1.0.0", "turn_id": "turn-mcp",
        "invocation_id": "call-mcp", "operation_id": "operation-mcp",
        "idempotency_key": "operation-mcp:call-mcp", "attempt": 1,
        "server_id": "calendar-server", "tool_id": "calendar.create",
        "tool_name": "calendar.create", "effect_certainty": "confirmed_none",
    }
    assert receipts.reserve_verified_none_replay(claim) is True
    receipt = {
        "schema_version": "1.0.0", "receipt_id": "mcp-tool-receipt-call-mcp",
        "server_id": "calendar-server", "protocol_version": "2025-11-25",
        "manifest_revision": 1, "transport_generation": 1, "catalog_revision": 1,
        "tool_schema_revision": "schema-1", "tool_id": "calendar.create",
        "tool_name": "calendar.create", "invocation_id": "call-mcp",
        "turn_id": "turn-mcp", "operation_id": "operation-mcp",
        "idempotency_key": "operation-mcp:call-mcp", "attempt": 1,
        "status": "completed", "effect_certainty": "confirmed_applied",
        "started_at": "2026-08-28T00:00:00+00:00",
        "finished_at": "2026-08-28T00:00:01+00:00",
        "remote_operation_id": "remote-operation-mcp",
        "raw_input_recorded": False, "raw_output_recorded": False,
    }
    receipt_ref = receipts.write_metadata(receipt)
    settled = EffectLog(database).get("mcp-effect-call-mcp")
    assert turn_store.verify_mcp_call_effect(settled) == (
        EffectState.SETTLED_OK, receipt_ref,
    )

    with sqlite3.connect(database) as connection:
        effect = connection.execute(
            "SELECT kind,effect_class,state,attempt,probe_ref,result_ref,error_ref "
            "FROM effect WHERE operation_id='mcp-effect-call-mcp'"
        ).fetchone()
        binding = connection.execute(
            "SELECT receipt_ref,receipt_kind FROM effect_receipt "
            "WHERE operation_id='mcp-effect-call-mcp'"
        ).fetchone()
    assert effect[:4] == ("mcp_call", "QUERYABLE", "SETTLED_OK", 2)
    assert isinstance(effect[4], str) and "mcp-verified-none-replay" in effect[4]
    assert effect[5:] == (receipt_ref, "mcp.remote_status_required")
    assert binding == (receipt_ref, "mcp-call-receipt")


def test_model_attempt_dispatch_bundle_has_one_cross_process_winner(tmp_path: Path) -> None:
    database = tmp_path / "model-attempt-race.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request()
    turn_id, created = store.claim_turn(request)
    assert created
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)
    start = tmp_path / "start"
    worker = """
import json
import sys
import time
from pathlib import Path
from core.ai_kernel import SQLiteAITurnStore, TurnEventConflict

database, ready, start, root = map(Path, sys.argv[1:5])
request = json.loads((root / 'core-contracts' / 'ai' / 'fixtures' / 'turn-request' / 'valid-project-answer.json').read_text(encoding='utf-8'))
dispatch = {
    'schema_version': '1.0.0',
    'attempt_id': 'model-wire-attempt-0123456789abcdef0123456789abcdef',
    'turn_id': request['turn_id'],
    'model_request_id': 'model-request-0123456789abcdef0123456789abcdef',
    'attempt_number': 1,
    'routing_snapshot_revision': 'a' * 64,
    'provider_id': 'openai',
    'model_id': 'gpt-5.4-mini',
    'dispatched_at': '2026-08-25T00:00:00+00:00',
    'input_stored': False,
    'output_stored': False,
}
event = {
    'schema_version': '1.0.0',
    'event_id': 'event-00000000000000000000000000000002',
    'turn_id': request['turn_id'],
    'session_id': request['session_id'],
    'sequence': 2,
    'type': 'model.attempt.dispatched',
    'actor': 'kernel',
    'correlation': {
        'step_id': None, 'tool_call_id': None,
        'model_request_id': dispatch['model_request_id'],
        'operation_id': request['operation_id'],
    },
    'data': {
        'status': 'running', 'summary': 'dispatched', 'capability_id': None,
        'payload_ref': None, 'receipt_ref': None, 'evidence_refs': [],
        'error_code': None, 'retryable': False,
    },
    'occurred_at': '2026-08-25T00:00:00+00:00',
}
ready.write_text('ready', encoding='utf-8')
deadline = time.monotonic() + 10
while not start.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(3)
    time.sleep(0.01)
try:
    receipt = SQLiteAITurnStore(database).commit_model_attempt_dispatch_bundle(
        event, expected_sequence=1, dispatch_payload=dispatch,
    )
    print('committed:' + receipt.dispatch_payload_ref, flush=True)
except TurnEventConflict:
    print('conflict', flush=True)
"""
    environment = dict(os.environ)
    source_path = str(ROOT / "src")
    environment["PYTHONPATH"] = source_path + os.pathsep + environment.get("PYTHONPATH", "")
    processes = [
        subprocess.Popen(
            [
                sys.executable, "-c", worker, str(database), str(tmp_path / f"ready-{index}"),
                str(start), str(ROOT),
            ],
            cwd=ROOT, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for index in range(2)
    ]
    deadline = datetime.now(timezone.utc) + timedelta(seconds=10)
    while not all((tmp_path / f"ready-{index}").exists() for index in range(2)):
        if datetime.now(timezone.utc) >= deadline:
            for process in processes:
                process.kill()
            raise AssertionError("cross-process model attempt workers did not become ready")
        sleep(0.01)
    start.write_text("go", encoding="utf-8")
    results = [process.communicate(timeout=15) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], results
    outputs = tuple(stdout.strip() for stdout, _stderr in results)
    assert outputs.count("conflict") == 1
    assert sum(item.startswith("committed:crp://session/") for item in outputs) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_model_attempt_reservations WHERE turn_id=?", (turn_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_turn_events WHERE turn_id=?", (turn_id,),
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_turn_payloads WHERE turn_id=? AND kind='model-wire-attempt-dispatch'",
            (turn_id,),
        ).fetchone()[0] == 1


class _Planner:
    def __init__(self, capability_id: str) -> None:
        self.capability_id = capability_id

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        if any(event["type"] == "tool.completed" for event in events):
            return {"type": "complete", "summary": "completed", "evidence_refs": []}
        return {"type": "tool", "capability_id": self.capability_id, "arguments": {"query": "question"}}


class _RecordedPlanner(_Planner):
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        assert execution_control is not None
        execution_control.model_call_started(provider="openai", model="durable-model")
        execution_control.model_call_completed(usage={})
        return super().plan(request, events, capabilities, payloads, execution_control)


class _Provider:
    def __init__(self, *, receipt: bool) -> None:
        self.receipt = receipt
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        assert request["operation_id"] == "op-project-answer-0001"
        return {
            "summary": "tool completed",
            "result": {"answer": "durable"},
            "receipt_ref": "crp://default/operations/op-durable-0001" if self.receipt else None,
            "evidence_refs": ["crp://default/memory/atom-1"],
        }


class _AtomicReceiptProvider(_Provider):
    def __init__(self) -> None:
        super().__init__(receipt=False)

    def invoke(self, request):
        result = super().invoke(request)
        result["operation_receipt"] = {
            "schema_version": "1.0.0",
            "operation": "document.draft",
            "status": "completed",
        }
        return result


class _CrashBeforeEffectSettleRunner(EffectRunner):
    def settle_ok(self, *args, **kwargs):
        raise SystemExit("receipt durable before Effect settle")


class _CrashOnceProvider:
    def __init__(self, *, receipt: bool) -> None:
        self.receipt = receipt
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        if self.calls == 1:
            raise SystemExit("simulated process interruption")
        return {
            "summary": "recovered tool completed",
            "result": {"answer": "recovered"},
            "receipt_ref": "crp://default/operations/op-recovered-0001" if self.receipt else None,
            "evidence_refs": [],
        }


class _SideEffectThenCrashProvider:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        raise SystemExit("side effect may already have happened")


class _DurableReceiptThenCrashProvider:
    def __init__(
        self, store: SQLiteAITurnStore, *, crash: bool, crash_recovery: bool = False,
    ) -> None:
        self.store = store
        self.crash = crash
        self.crash_recovery = crash_recovery
        self.calls = 0
        self.recovery_calls = 0
        self.recovery_had_execution_context = False

    def invoke(self, request):
        self.calls += 1
        receipt = {
            "turn_id": request["turn_id"],
            "invocation_id": request["tool_call_id"],
            "operation_id": request["operation_id"],
            "status": "completed",
        }
        self.store.get_or_create_immutable_payload(
            request["turn_id"], f"test-tool-receipt-{request['tool_call_id']}", receipt,
        )
        if self.crash:
            raise SystemExit("simulated crash after durable provider receipt")
        return self.recover_completed_invocation(request)

    def recover_completed_invocation(self, request):
        self.recovery_calls += 1
        context = request.get("execution_context")
        self.recovery_had_execution_context = callable(getattr(context, "checkpoint", None))
        if self.crash_recovery:
            self.crash_recovery = False
            raise SystemExit("simulated crash during local receipt recovery")
        completed = self.store.get_immutable_payload(
            request["turn_id"], f"test-tool-receipt-{request['tool_call_id']}",
        )
        if completed is None:
            return None
        receipt_ref, _receipt = completed
        return {
            "summary": "durable provider completion recovered",
            "receipt_ref": receipt_ref,
            "evidence_refs": [],
        }


class _DeniedDurableRecoveryProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.recovery_calls = 0

    def invoke(self, _request):
        self.calls += 1
        raise AssertionError("recovery must not enter the ordinary provider wire path")

    def recover_completed_invocation(self, _request):
        self.recovery_calls += 1
        raise ToolProviderFailure(
            "mcp.tool_contract_drift", effect_certainty="confirmed_none"
        )


class _TimeoutOnceProvider:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("temporary read timeout")
        return {
            "summary": "retry recovered",
            "result": {"answer": "recovered"},
            "receipt_ref": None,
            "evidence_refs": [],
        }


class _CrashDuringRetryWaitRuntime(SynchronousAIRuntime):
    def _wait_for_retry(self, turn_id: str, delay_ms: int) -> bool:
        raise SystemExit("simulated crash during retry backoff")


class _CrashBeforeDispatchRuntime(SynchronousAIRuntime):
    def _execute_tool_intent(self, *_args, **_kwargs):
        raise SystemExit("simulated crash before dispatch")


class _CrashApprovedBeforeIntentRuntime(SynchronousAIRuntime):
    def _invoke_tool(self, *_args, **_kwargs):
        raise SystemExit("simulated crash after approval resolution")


class _CrashAfterApprovalEventRuntime(SynchronousAIRuntime):
    armed = False

    def _append(self, turn_id, event_type, status, summary, **kwargs):
        event = super()._append(turn_id, event_type, status, summary, **kwargs)
        if self.armed and event_type == "approval.resolved":
            raise SystemExit("simulated crash after approval event")
        return event


class _CrashAfterToolOutcomeRuntime(SynchronousAIRuntime):
    armed = True

    def _append_tool_outcome_bundle(self, *args, **kwargs):
        receipt = super()._append_tool_outcome_bundle(*args, **kwargs)
        if self.armed:
            self.armed = False
            raise SystemExit("simulated crash after durable tool outcome")
        return receipt


class _TimeoutThenCrashProvider:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("temporary read timeout")
        raise SystemExit("simulated interruption during retry attempt")


class _NativeCrashProvider:
    def __init__(self, *, crash: bool) -> None:
        self.crash = crash
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        if self.crash:
            raise SystemExit("simulated native MCP interruption")
        return {"summary": "must not run", "result": {"events": []}, "evidence_refs": []}


def test_completed_turn_replays_after_store_and_runtime_restart(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    provider = _Provider(receipt=False)
    registry = _registry("memory.recall", "read", False, "read_only", provider)
    first_store = SQLiteAITurnStore(database)
    first = SynchronousAIRuntime(planner=_RecordedPlanner("memory.recall"), registry=registry, events=first_store, payloads=first_store, state=first_store)
    request = _request()
    completed = first.submit_turn(request)
    assert completed.status == "completed" and provider.calls == 1

    restarted_store = SQLiteAITurnStore(database)
    restarted = SynchronousAIRuntime(planner=_RecordedPlanner("memory.recall"), registry=registry, events=restarted_store, payloads=restarted_store, state=restarted_store)
    replay = restarted.submit_turn(request)
    assert replay.turn_id == completed.turn_id and replay.replayed is True
    assert provider.calls == 1
    tool_event = next(event for event in restarted.events_after(completed.turn_id) if event["type"] == "tool.completed")
    assert restarted_store.get(tool_event["data"]["payload_ref"]) == {"answer": "durable"}
    model_event = next(
        event for event in restarted.events_after(completed.turn_id)
        if event["type"] == "model.completed"
    )
    model_receipt = restarted_store.get(model_event["data"]["receipt_ref"])
    assert model_receipt["status"] == "completed"
    assert model_receipt["model_request_id"] == model_event["correlation"]["model_request_id"]


def test_pending_approval_and_action_idempotency_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    provider = _Provider(receipt=True)
    registry = _registry("document.draft", "write", True, "receipt_required", provider)
    store = SQLiteAITurnStore(database)
    first = SynchronousAIRuntime(planner=_Planner("document.draft"), registry=registry, events=store, payloads=store, state=store)
    request = _request()
    request["capability_policy"] = {"allowed": ["document.draft"], "denied": [], "require_approval": ["document.draft"]}
    waiting = first.submit_turn(request)
    approval_event = tuple(first.events_after(waiting.turn_id))[-1]
    store.clear_pending(waiting.turn_id)  # simulate loss after the durable approval event

    restarted_store = SQLiteAITurnStore(database)
    restarted = SynchronousAIRuntime(planner=_Planner("document.draft"), registry=registry, events=restarted_store, payloads=restarted_store, state=restarted_store)
    action = _action(waiting.turn_id, waiting.current_sequence, str(approval_event["event_id"]))
    completed = restarted.apply_action(action)
    assert completed.status == "completed" and provider.calls == 1

    second_restart_store = SQLiteAITurnStore(database)
    second_restart = SynchronousAIRuntime(planner=_Planner("document.draft"), registry=registry, events=second_restart_store, payloads=second_restart_store, state=second_restart_store)
    replay = second_restart.apply_action(action)
    assert replay.status == "completed" and replay.replayed is True
    assert provider.calls == 1


def test_sqlite_restart_keeps_mcp_request_state_waiting_without_a_second_wire_call(tmp_path: Path) -> None:
    """The startup scanner must leave an explicit requestState pause untouched."""
    class InputRequiredProvider:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, _request):
            self.calls += 1
            raise ToolProviderFailure(
                "mcp.input_required", effect_certainty="unknown",
                continuation_state=b"opaque-request-state",
            )

    database = tmp_path / "mcp-request-state.sqlite3"
    provider = InputRequiredProvider()
    registry = ScopedCapabilityRegistry()
    registry.register(_native_mcp_definition("calendar-server"), provider)
    first_store = SQLiteAITurnStore(database)
    first = SynchronousAIRuntime(
        planner=_Planner("calendar.read"), registry=registry,
        events=first_store, payloads=first_store, state=first_store,
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["calendar.read"], "denied": [], "require_approval": [],
    }

    waiting = first.submit_turn(request)
    assert waiting.status == "waiting_approval" and provider.calls == 1
    assert tuple(first.events_after(waiting.turn_id))[-1]["type"] == "mcp.continuation.required"

    restarted_store = SQLiteAITurnStore(database)
    stale_now = NOW - timedelta(minutes=2)
    assert restarted_store.try_acquire_run_lease(
        waiting.turn_id, "interrupted-host", now=stale_now,
        stale_after=stale_now + timedelta(seconds=1),
    ) is not None

    assert scan_due_ai_turn_recovery(restarted_store, clock=lambda: NOW) == 0
    assert provider.calls == 1
    assert restarted_store.get_pending(waiting.turn_id) is not None
    assert restarted_store.list_recovery_reviews() == ()
    assert restarted_store.claim_safe_recovery_queue(
        now=NOW, stale_after=NOW + timedelta(seconds=30), limit=1,
    ) == ()


def test_concurrent_mcp_continue_actions_dispatch_one_continuation_and_replay_idempotently(tmp_path: Path) -> None:
    class InitialProvider:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, _request):
            self.calls += 1
            raise ToolProviderFailure(
                "mcp.input_required", effect_certainty="unknown",
                continuation_state=b"opaque-request-state",
            )

        def continue_request_state(self, _request, _state):
            raise AssertionError("the reconnect must replace the revoked provider")

    class ContinuedProvider:
        def __init__(self) -> None:
            self.calls = 0
            self.started = Event()
            self.release = Event()

        def invoke(self, _request):
            raise AssertionError("continuation must use the dedicated provider method")

        def continue_request_state(self, request, state):
            self.calls += 1
            assert state == b"opaque-request-state"
            assert request["arguments"] == {"query": "question"}
            self.started.set()
            assert self.release.wait(2)
            return {"summary": "continued", "result": {"answer": "continued"}, "evidence_refs": []}

    database = tmp_path / "mcp-concurrent.sqlite3"
    initial = InitialProvider()
    continued = ContinuedProvider()
    registry = ScopedCapabilityRegistry()
    definition = _native_mcp_definition("calendar-server")
    registration = registry.register(definition, initial)
    replacement: list[object] = []

    def reconnect(server_id: str) -> None:
        assert server_id == "calendar-server"
        registration.close()
        replacement.append(registry.register(definition, continued))

    store = SQLiteAITurnStore(database)
    runtime = SynchronousAIRuntime(
        planner=_Planner("calendar.read"), registry=registry,
        events=store, payloads=store, state=store,
        mcp_continuation_reconnector=reconnect,
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["calendar.read"], "denied": [], "require_approval": [],
    }
    waiting = runtime.submit_turn(request)
    event = tuple(runtime.events_after(waiting.turn_id))[-1]
    action = {
        "schema_version": "1.0.0", "action_id": "action-mcp-continue-000000000000001", "turn_id": waiting.turn_id,
        "type": "mcp_continue", "target_event_id": event["event_id"], "reason": "continue stateless MCP request",
        "actor": "user", "expected_sequence": waiting.current_sequence,
        "idempotency_key": "mcp-continue-concurrent-0001", "created_at": "2026-08-26T00:00:00Z",
    }
    first = AITurnRunner(runtime, max_workers=1)
    second = AITurnRunner(runtime, max_workers=1)
    barrier = Barrier(3)
    outcomes: list[object] = []
    outcome_lock = Lock()

    def apply(runner: AITurnRunner) -> None:
        barrier.wait()
        try:
            value: object = runner.apply_action_and_wait(action)
        except Exception as error:  # one durable lease winner is expected
            value = error
        with outcome_lock:
            outcomes.append(value)

    threads = (Thread(target=apply, args=(first,)), Thread(target=apply, args=(second,)))
    for thread in threads:
        thread.start()
    barrier.wait()
    assert continued.started.wait(1)
    continued.release.set()
    for thread in threads:
        thread.join(2)
    try:
        assert continued.calls == 1
        assert len([item for item in outcomes if getattr(item, "status", None) == "completed"]) == 1
        replay = first.apply_action_and_wait(action)
        assert replay.status == "completed" and replay.replayed is True
        assert continued.calls == 1
        recovery = classify_recovery(
            waiting.turn_id, 1, tuple(store.events_after(waiting.turn_id)),
            payload_loader=store.get,
        )
        assert recovery.reason_code == "ai.recovery_terminal", recovery
        assert recovery.disposition == "terminal_noop", (
            recovery,
            [(event["type"], event["correlation"]["tool_call_id"], event["data"]["summary"]) for event in store.events_after(waiting.turn_id)],
        )
    finally:
        first.shutdown()
        second.shutdown()


def test_mcp_continue_rechecks_current_boundary_before_continuation_wire(tmp_path: Path) -> None:
    class Boundary:
        def __init__(self) -> None:
            self.calls = 0

        def evaluate(self, _request, _definition, decision):
            self.calls += 1
            outcome = "allow" if self.calls == 1 else "deny"
            return ToolExecutionBoundaryDecision(
                outcome, ("test_boundary",), (), self.calls, False, False,
                dict(decision["arguments"]),
            )

    runtime, provider, reconnect, request = _request_state_runtime(tmp_path, continuation="complete")
    runtime._execution_boundary = Boundary()  # type: ignore[attr-defined]
    waiting = runtime.submit_turn(request)
    action = _mcp_action(waiting, runtime)

    failed = runtime.apply_action(action)

    assert failed.status == "failed"
    assert reconnect["calls"] == 1
    assert provider.continuation_calls == 0
    assert runtime._execution_boundary.calls == 2  # type: ignore[attr-defined]


def test_expired_mcp_request_state_terminates_without_reconnect_or_wire(tmp_path: Path) -> None:
    runtime, provider, reconnect, request = _request_state_runtime(tmp_path, continuation="complete")
    waiting = runtime.submit_turn(request)
    state_event = tuple(runtime.events_after(waiting.turn_id))[-1]
    state_ref = state_event["data"]["payload_ref"]
    assert isinstance(state_ref, str)
    with sqlite3.connect(tmp_path / "request-state.sqlite3") as connection:
        payload = json.loads(connection.execute(
            "SELECT payload_json FROM ai_turn_immutable_payloads WHERE payload_ref=?", (state_ref,),
        ).fetchone()[0])
        payload["expires_at"] = "2020-01-01T00:00:00+00:00"
        connection.execute(
            "UPDATE ai_turn_immutable_payloads SET payload_json=? WHERE payload_ref=?",
            (json.dumps(payload, separators=(",", ":")), state_ref),
        )
        connection.commit()

    failed = runtime.apply_action(_mcp_action(waiting, runtime))

    assert failed.status == "failed"
    assert reconnect["calls"] == 0
    assert provider.continuation_calls == 0


@pytest.mark.parametrize(
    ("continuation", "expected_code"),
    (("input_required", "mcp.continuation_round_exhausted"), ("unknown", "ai.tool_outcome_unknown")),
)
def test_mcp_continuation_unconfirmed_or_second_input_never_creates_another_waiting_state(
    tmp_path: Path, continuation: str, expected_code: str,
) -> None:
    runtime, provider, _reconnect, request = _request_state_runtime(tmp_path, continuation=continuation)
    waiting = runtime.submit_turn(request)

    failed = runtime.apply_action(_mcp_action(waiting, runtime))

    events = tuple(runtime.events_after(waiting.turn_id))
    assert failed.status == "failed"
    assert provider.continuation_calls == 1
    assert sum(event["type"] == "mcp.continuation.required" for event in events) == 1
    outcome_event = next(event for event in events if event["type"] == "tool.outcome.recorded")
    outcome = runtime._payloads.get(outcome_event["data"]["payload_ref"])
    assert outcome["status"] == "unknown_effect"
    assert outcome["error_code"] == expected_code
    recovery = classify_recovery(
        waiting.turn_id, 1, events, payload_loader=runtime._payloads.get,
    )
    assert recovery.disposition == "quarantine", recovery
    assert recovery.reason_code == "ai.recovery_tool_outcome_unsafe"


def test_sqlite_and_memory_payload_stores_reject_sensitive_material(tmp_path: Path) -> None:
    store = SQLiteAITurnStore(tmp_path / "ai-turns.sqlite3")
    store.claim_turn(_request())
    with pytest.raises(AIKernelContractError, match="sensitive field"):
        store.put("turn-0123456789abcdef0123456789abcdef", "tool-result", {"api_key": "forbidden"})


def test_core_reaper_settles_tool_effect_from_durable_outcome_after_crash(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    store = SQLiteAITurnStore(database)
    crash_runner = _CrashBeforeEffectSettleRunner(
        store.effect_runner.log, owner_id="tool-crash-runner", lease_seconds=1,
    )
    runtime = SynchronousAIRuntime(
        planner=_Planner("memory.recall"),
        registry=_registry("memory.recall", "read", False, "read_only", _Provider(receipt=False)),
        events=store,
        payloads=store,
        state=store,
        effect_runner=crash_runner,
    )
    request = _request()

    with pytest.raises(SystemExit, match="receipt durable"):
        runtime.submit_turn(request)

    outcome_event = next(
        event for event in store.events_after(str(request["turn_id"]))
        if event["type"] == "tool.outcome.recorded"
    )
    tool_call_id = str(outcome_event["correlation"]["tool_call_id"])
    before = store.effect_runner.log.get(tool_call_id)
    assert before.state is EffectState.INFLIGHT

    recovered = EffectReaper(store.effect_runner.log).recover_expired(
        now=2_000_000_000,
        verifiers={before.kind: store.verify_tool_call_effect},
    )

    assert recovered[0].state is EffectState.SETTLED_OK
    assert store.effect_runner.log.get(tool_call_id).result_ref == outcome_event["data"]["payload_ref"]


def test_unclassified_interrupted_read_does_not_bypass_frozen_retry_codes(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    provider = _CrashOnceProvider(receipt=False)
    registry = _registry("memory.recall", "read", False, "read_only", provider)
    store = SQLiteAITurnStore(database)
    first = SynchronousAIRuntime(
        planner=_Planner("memory.recall"),
        registry=registry,
        events=store,
        payloads=store,
        state=store,
    )
    request = _request()

    with pytest.raises(SystemExit, match="simulated process interruption"):
        first.submit_turn(request)

    interrupted = tuple(first.events_after(str(request["turn_id"])))
    assert [event["type"] for event in interrupted][-3:] == [
        "tool.intent.recorded",
        "tool.dispatch.claimed",
        "tool.started",
    ]
    tool_call_id = interrupted[-1]["correlation"]["tool_call_id"]

    restarted_store = SQLiteAITurnStore(database)
    restarted = SynchronousAIRuntime(
        planner=_Planner("memory.recall"),
        registry=registry,
        events=restarted_store,
        payloads=restarted_store,
        state=restarted_store,
    )
    failed = restarted.apply_action(
        _resume_action(str(request["turn_id"]), len(interrupted))
    )

    assert failed.status == "failed"
    assert provider.calls == 1
    events = tuple(restarted.events_after(str(request["turn_id"])))
    assert sum(event["type"] == "tool.intent.recorded" for event in events) == 1
    assert sum(event["type"] == "tool.started" for event in events) == 1
    assert sum(event["type"] == "tool.outcome.recorded" for event in events) == 1
    assert {
        event["correlation"]["tool_call_id"]
        for event in events
        if event["type"].startswith("tool.") and event["correlation"]["tool_call_id"]
    } == {tool_call_id}
    outcome_event = next(event for event in events if event["type"] == "tool.outcome.recorded")
    outcome = restarted_store.get(outcome_event["data"]["payload_ref"])
    assert outcome["status"] == "failed"
    assert outcome["effect_certainty"] == "confirmed_none"
    assert outcome["error_code"] == "ai.tool_interrupted"


def test_fenced_recovery_resumes_recorded_intent_before_replanning(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    provider = _Provider(receipt=False)
    registry = _registry("memory.recall", "read", False, "read_only", provider)
    store = SQLiteAITurnStore(database)
    first = _CrashBeforeDispatchRuntime(
        planner=_Planner("memory.recall"), registry=registry,
        events=store, payloads=store, state=store,
    )
    request = _request()
    with pytest.raises(SystemExit, match="before dispatch"):
        first.submit_turn(request)
    assert provider.calls == 0

    restarted_store = SQLiteAITurnStore(database)
    token = restarted_store.try_acquire_run_lease(
        str(request["turn_id"]), "recovery-test", now=NOW,
        stale_after=NOW + timedelta(seconds=30),
    )
    assert token is not None
    restarted = SynchronousAIRuntime(
        planner=_Planner("memory.recall"), registry=registry,
        events=restarted_store, payloads=restarted_store, state=restarted_store,
    )
    receipt = restarted.recover_accepted_turn(str(request["turn_id"]), token)
    assert receipt.status == "completed" and provider.calls == 1
    assert sum(event["type"] == "tool.intent.recorded" for event in restarted_store.events_after(str(request["turn_id"]))) == 1


def test_fenced_recovery_converges_durable_tool_outcome_without_second_provider_call(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    provider = _Provider(receipt=False)
    registry = _registry("memory.recall", "read", False, "read_only", provider)
    store = SQLiteAITurnStore(database)
    first = _CrashAfterToolOutcomeRuntime(
        planner=_Planner("memory.recall"), registry=registry,
        events=store, payloads=store, state=store,
    )
    request = _request()

    with pytest.raises(SystemExit, match="after durable tool outcome"):
        first.submit_turn(request)

    interrupted = tuple(store.events_after(str(request["turn_id"])))
    assert interrupted[-1]["type"] == "tool.outcome.recorded"
    assert provider.calls == 1

    restarted_store = SQLiteAITurnStore(database)
    token = restarted_store.try_acquire_run_lease(
        str(request["turn_id"]), "recovery-test", now=NOW,
        stale_after=NOW + timedelta(seconds=30),
    )
    assert token is not None
    restarted = SynchronousAIRuntime(
        planner=_Planner("memory.recall"), registry=registry,
        events=restarted_store, payloads=restarted_store, state=restarted_store,
    )

    receipt = restarted.recover_accepted_turn(str(request["turn_id"]), token)

    events = tuple(restarted_store.events_after(str(request["turn_id"])))
    assert receipt.status == "completed"
    assert provider.calls == 1
    assert sum(event["type"] == "tool.intent.recorded" for event in events) == 1
    assert sum(event["type"] == "tool.outcome.recorded" for event in events) == 1
    assert sum(event["type"] == "tool.completed" for event in events) == 1
    assert events[-1]["type"] == "turn.completed"


def test_fenced_recovery_resumes_approved_pending_decision(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    provider = _Provider(receipt=True)
    registry = _registry("document.draft", "write", True, "receipt_required", provider)
    store = SQLiteAITurnStore(database)
    first = _CrashApprovedBeforeIntentRuntime(
        planner=_Planner("document.draft"), registry=registry,
        events=store, payloads=store, state=store,
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["document.draft"], "denied": [],
        "require_approval": ["document.draft"],
    }
    waiting = first.submit_turn(request)
    approval = tuple(store.events_after(waiting.turn_id))[-1]
    with pytest.raises(SystemExit, match="approval resolution"):
        first.apply_action(_action(waiting.turn_id, waiting.current_sequence, str(approval["event_id"])))
    assert provider.calls == 0

    restarted_store = SQLiteAITurnStore(database)
    token = restarted_store.try_acquire_run_lease(
        waiting.turn_id, "recovery-test", now=NOW,
        stale_after=NOW + timedelta(seconds=30),
    )
    assert token is not None
    restarted = SynchronousAIRuntime(
        planner=_Planner("document.draft"), registry=registry,
        events=restarted_store, payloads=restarted_store, state=restarted_store,
    )
    receipt = restarted.recover_accepted_turn(waiting.turn_id, token)
    assert receipt.status == "completed" and provider.calls == 1
    assert restarted_store.get_pending(waiting.turn_id) is None


def test_fenced_recovery_converges_rejected_pending_without_tool_call(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    provider = _Provider(receipt=True)
    registry = _registry("document.draft", "write", True, "receipt_required", provider)
    store = SQLiteAITurnStore(database)
    first = _CrashAfterApprovalEventRuntime(
        planner=_Planner("document.draft"), registry=registry,
        events=store, payloads=store, state=store,
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["document.draft"], "denied": [],
        "require_approval": ["document.draft"],
    }
    waiting = first.submit_turn(request)
    approval = tuple(store.events_after(waiting.turn_id))[-1]
    action = _action(waiting.turn_id, waiting.current_sequence, str(approval["event_id"]))
    action["type"] = "reject"
    first.armed = True
    with pytest.raises(SystemExit, match="approval event"):
        first.apply_action(action)

    restarted_store = SQLiteAITurnStore(database)
    token = restarted_store.try_acquire_run_lease(
        waiting.turn_id, "recovery-test", now=NOW,
        stale_after=NOW + timedelta(seconds=30),
    )
    assert token is not None
    restarted = SynchronousAIRuntime(
        planner=_Planner("document.draft"), registry=registry,
        events=restarted_store, payloads=restarted_store, state=restarted_store,
    )
    receipt = restarted.recover_accepted_turn(waiting.turn_id, token)
    assert receipt.status == "cancelled" and provider.calls == 0
    assert restarted_store.get_pending(waiting.turn_id) is None


def test_classified_retry_resumes_from_next_attempt_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    provider = _TimeoutOnceProvider()
    registry = _registry("memory.recall", "read", False, "read_only", provider)
    store = SQLiteAITurnStore(database)
    first = _CrashDuringRetryWaitRuntime(
        planner=_Planner("memory.recall"),
        registry=registry,
        events=store,
        payloads=store,
        state=store,
        effect_runner=store.effect_runner,
    )
    request = _request()

    running = first.submit_turn(request)
    assert running.status == "running"

    interrupted = tuple(first.events_after(str(request["turn_id"])))
    assert interrupted[-1]["type"] == "tool.attempt.failed"
    assert interrupted[-1]["data"]["error_code"] == "timeout"
    tool_call_id = interrupted[-1]["correlation"]["tool_call_id"]
    EffectReaper(store.effect_runner.log).recover_expired(now=2_000_000_000)

    restarted_store = SQLiteAITurnStore(database)
    restarted = SynchronousAIRuntime(
        planner=_Planner("memory.recall"),
        registry=registry,
        events=restarted_store,
        payloads=restarted_store,
        state=restarted_store,
        effect_runner=restarted_store.effect_runner,
    )
    completed = restarted.apply_action(
        _resume_action(str(request["turn_id"]), len(interrupted))
    )

    assert completed.status == "completed"
    assert provider.calls == 2
    events = tuple(restarted.events_after(str(request["turn_id"])))
    assert sum(event["type"] == "tool.started" for event in events) == 2
    assert sum(event["type"] == "tool.outcome.recorded" for event in events) == 1
    assert {
        event["correlation"]["tool_call_id"]
        for event in events
        if event["type"].startswith("tool.") and event["correlation"]["tool_call_id"]
    } == {tool_call_id}


def test_resume_rejects_same_version_native_mcp_server_drift(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    original = _NativeCrashProvider(crash=True)
    registry = ScopedCapabilityRegistry()
    lease = registry.register(_native_mcp_definition("calendar-server"), original)
    store = SQLiteAITurnStore(database)
    first = SynchronousAIRuntime(
        planner=_Planner("calendar.read"), registry=registry,
        events=store, payloads=store, state=store,
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["calendar.read"], "denied": [], "require_approval": [],
    }

    with pytest.raises(SystemExit, match="native MCP interruption"):
        first.submit_turn(request)

    interrupted = tuple(first.events_after(str(request["turn_id"])))
    lease.close()
    replacement = _NativeCrashProvider(crash=False)
    registry.register(_native_mcp_definition("different-calendar-server"), replacement)
    restarted_store = SQLiteAITurnStore(database)
    restarted = SynchronousAIRuntime(
        planner=_Planner("calendar.read"), registry=registry,
        events=restarted_store, payloads=restarted_store, state=restarted_store,
    )

    failed = restarted.apply_action(
        _resume_action(str(request["turn_id"]), len(interrupted))
    )

    assert failed.status == "failed"
    assert original.calls == 1 and replacement.calls == 0
    events = tuple(restarted.events_after(str(request["turn_id"])))
    outcome_event = next(event for event in events if event["type"] == "tool.outcome.recorded")
    outcome = restarted_store.get(outcome_event["data"]["payload_ref"])
    assert outcome["error_code"] == "ai.tool_definition_drift"
    assert outcome["effect_certainty"] == "unknown"


def test_crash_during_retry_attempt_does_not_reuse_older_retry_evidence(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    provider = _TimeoutThenCrashProvider()
    registry = _registry("memory.recall", "read", False, "read_only", provider)
    store = SQLiteAITurnStore(database)
    first = SynchronousAIRuntime(
        planner=_Planner("memory.recall"),
        registry=registry,
        events=store,
        payloads=store,
        state=store,
        effect_runner=store.effect_runner,
    )
    request = _request()

    running = first.submit_turn(request)
    assert running.status == "running"
    EffectReaper(store.effect_runner.log).recover_expired(now=2_000_000_000)
    with pytest.raises(SystemExit, match="retry attempt"):
        first.apply_action(
            _resume_action(str(request["turn_id"]), running.current_sequence)
        )

    interrupted = tuple(first.events_after(str(request["turn_id"])))
    assert sum(event["type"] == "tool.started" for event in interrupted) == 2
    assert sum(event["type"] == "tool.attempt.failed" for event in interrupted) == 1

    restarted_store = SQLiteAITurnStore(database)
    EffectReaper(restarted_store.effect_runner.log).recover_expired(now=2_000_000_100)
    restarted = SynchronousAIRuntime(
        planner=_Planner("memory.recall"),
        registry=registry,
        events=restarted_store,
        payloads=restarted_store,
        state=restarted_store,
        effect_runner=restarted_store.effect_runner,
    )
    failed = restarted.apply_action(
        _resume_action(str(request["turn_id"]), len(interrupted))
    )

    assert failed.status == "failed"
    assert provider.calls == 2
    events = tuple(restarted.events_after(str(request["turn_id"])))
    outcome_event = next(event for event in events if event["type"] == "tool.outcome.recorded")
    outcome = restarted_store.get(outcome_event["data"]["payload_ref"])
    assert outcome["error_code"] == "ai.tool_retry_exhausted"
    assert outcome["effect_certainty"] == "confirmed_none"


def test_approval_recovery_never_repeats_uncertain_write_after_crash(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    provider = _SideEffectThenCrashProvider()
    registry = _registry("document.draft", "write", True, "receipt_required", provider)
    store = SQLiteAITurnStore(database)
    first = SynchronousAIRuntime(
        planner=_Planner("document.draft"),
        registry=registry,
        events=store,
        payloads=store,
        state=store,
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["document.draft"],
        "denied": [],
        "require_approval": ["document.draft"],
    }
    waiting = first.submit_turn(request)
    approval = tuple(first.events_after(waiting.turn_id))[-1]
    action = _action(waiting.turn_id, waiting.current_sequence, str(approval["event_id"]))

    with pytest.raises(SystemExit, match="side effect may already have happened"):
        first.apply_action(action)

    restarted_store = SQLiteAITurnStore(database)
    restarted = SynchronousAIRuntime(
        planner=_Planner("document.draft"),
        registry=registry,
        events=restarted_store,
        payloads=restarted_store,
        state=restarted_store,
    )
    failed = restarted.apply_action(action)
    replay = restarted.apply_action(action)

    assert failed.status == "failed"
    assert replay.status == "failed" and replay.replayed is True
    assert provider.calls == 1
    events = tuple(restarted.events_after(waiting.turn_id))
    assert events[-1]["data"]["error_code"] == "ai.tool_outcome_unknown"
    outcome_event = next(event for event in events if event["type"] == "tool.outcome.recorded")
    outcome = restarted_store.get(outcome_event["data"]["payload_ref"])
    assert outcome["status"] == "unknown_effect"


def test_recovery_projects_durable_provider_receipt_without_repeating_side_effect(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    store = SQLiteAITurnStore(database)
    first_provider = _DurableReceiptThenCrashProvider(store, crash=True)
    first = SynchronousAIRuntime(
        planner=_Planner("document.draft"),
        registry=_registry("document.draft", "write", True, "receipt_required", first_provider),
        events=store,
        payloads=store,
        state=store,
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["document.draft"],
        "denied": [],
        "require_approval": ["document.draft"],
    }
    waiting = first.submit_turn(request)
    approval = tuple(first.events_after(waiting.turn_id))[-1]
    action = _action(waiting.turn_id, waiting.current_sequence, str(approval["event_id"]))

    with pytest.raises(SystemExit, match="durable provider receipt"):
        first.apply_action(action)

    restarted_store = SQLiteAITurnStore(database)
    interrupted_recovery_provider = _DurableReceiptThenCrashProvider(
        restarted_store, crash=False, crash_recovery=True,
    )
    interrupted_recovery = SynchronousAIRuntime(
        planner=_Planner("document.draft"),
        registry=_registry(
            "document.draft", "write", True, "receipt_required",
            interrupted_recovery_provider,
        ),
        events=restarted_store,
        payloads=restarted_store,
        state=restarted_store,
    )
    with pytest.raises(SystemExit, match="local receipt recovery"):
        interrupted_recovery.apply_action(action)

    after_recovery_crash = tuple(restarted_store.events_after(waiting.turn_id))
    assert sum(event["type"] == "tool.started" for event in after_recovery_crash) == 1

    final_store = SQLiteAITurnStore(database)
    recovery_provider = _DurableReceiptThenCrashProvider(final_store, crash=False)
    restarted = SynchronousAIRuntime(
        planner=_Planner("document.draft"),
        registry=_registry("document.draft", "write", True, "receipt_required", recovery_provider),
        events=final_store,
        payloads=final_store,
        state=final_store,
    )
    completed = restarted.apply_action(action)

    assert completed.status == "completed"
    assert first_provider.calls == 1
    assert recovery_provider.calls == 0
    assert recovery_provider.recovery_calls == 1
    assert recovery_provider.recovery_had_execution_context is True
    events = tuple(restarted.events_after(waiting.turn_id))
    assert sum(event["type"] == "tool.dispatch.claimed" for event in events) == 1
    assert sum(event["type"] == "tool.started" for event in events) == 1
    assert sum(event["type"] == "tool.completed" for event in events) == 1
    outcome_event = next(event for event in events if event["type"] == "tool.outcome.recorded")
    outcome = restarted_store.get(outcome_event["data"]["payload_ref"])
    assert outcome["status"] == "completed"
    assert outcome["effect_certainty"] == "confirmed_applied"
    assert outcome["receipt_ref"].startswith("crp://session/")


def test_recovery_authority_failure_quarantines_possible_completed_effect(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    store = SQLiteAITurnStore(database)
    first_provider = _DurableReceiptThenCrashProvider(store, crash=True)
    first = SynchronousAIRuntime(
        planner=_Planner("document.draft"),
        registry=_registry("document.draft", "write", True, "receipt_required", first_provider),
        events=store, payloads=store, state=store,
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["document.draft"], "denied": [],
        "require_approval": ["document.draft"],
    }
    waiting = first.submit_turn(request)
    approval = tuple(first.events_after(waiting.turn_id))[-1]
    action = _action(waiting.turn_id, waiting.current_sequence, str(approval["event_id"]))
    with pytest.raises(SystemExit, match="durable provider receipt"):
        first.apply_action(action)

    restarted_store = SQLiteAITurnStore(database)
    denied = _DeniedDurableRecoveryProvider()
    restarted = SynchronousAIRuntime(
        planner=_Planner("document.draft"),
        registry=_registry("document.draft", "write", True, "receipt_required", denied),
        events=restarted_store, payloads=restarted_store, state=restarted_store,
    )
    failed = restarted.apply_action(action)

    assert failed.status == "failed"
    assert denied.calls == 0 and denied.recovery_calls == 1
    events = tuple(restarted.events_after(waiting.turn_id))
    outcome_event = next(event for event in events if event["type"] == "tool.outcome.recorded")
    outcome = restarted_store.get(outcome_event["data"]["payload_ref"])
    assert outcome["status"] == "unknown_effect"
    assert outcome["effect_certainty"] == "unknown"


def test_recovered_provider_receipt_runs_post_tool_stop_once_and_returns_cancelled(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    store = SQLiteAITurnStore(database)
    first_provider = _DurableReceiptThenCrashProvider(store, crash=True)
    first = SynchronousAIRuntime(
        planner=_Planner("document.draft"),
        registry=_registry("document.draft", "write", True, "receipt_required", first_provider),
        events=store, payloads=store, state=store,
        hook_host=_post_tool_stop_host(),
        frozen_hook_authorization_check=lambda *_args: True,
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["document.draft"], "denied": [],
        "require_approval": ["document.draft"],
    }
    waiting = first.submit_turn(request)
    approval = tuple(first.events_after(waiting.turn_id))[-1]
    action = _action(waiting.turn_id, waiting.current_sequence, str(approval["event_id"]))
    with pytest.raises(SystemExit, match="durable provider receipt"):
        first.apply_action(action)

    restarted_store = SQLiteAITurnStore(database)
    recovery_provider = _DurableReceiptThenCrashProvider(restarted_store, crash=False)
    restarted = SynchronousAIRuntime(
        planner=_Planner("document.draft"),
        registry=_registry("document.draft", "write", True, "receipt_required", recovery_provider),
        events=restarted_store, payloads=restarted_store, state=restarted_store,
        hook_host=_post_tool_stop_host(),
        frozen_hook_authorization_check=lambda *_args: True,
    )
    cancelled = restarted.apply_action(action)

    assert cancelled.status == "cancelled"
    assert recovery_provider.calls == 0 and recovery_provider.recovery_calls == 1
    events = tuple(restarted.events_after(waiting.turn_id))
    assert sum(event["type"] == "tool.started" for event in events) == 1
    assert sum(
        event["type"] == "hook.invoked"
        and restarted._hook_event_for(event) is HookEvent.POST_TOOL_USE
        for event in events
    ) == 1
    types = [str(event["type"]) for event in events]
    assert types.index("tool.completed") < types.index("turn.cancelled")


def test_intent_bundle_commits_payload_and_event_together_across_restart(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request()
    turn_id, created = store.claim_turn(request)
    assert created
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)

    committed = store.append_intent_bundle(
        _turn_event(request, 2, "tool.intent.recorded", "intent", capability_id="memory.recall"),
        expected_sequence=1,
        intent_kind="tool-invocation-intent",
        intent_payload={"invocation_id": "call-1", "turn_id": turn_id},
    )

    assert committed.event["data"]["payload_ref"] == committed.intent_payload_ref
    restarted = SQLiteAITurnStore(database)
    assert restarted.get(committed.intent_payload_ref) == {
        "invocation_id": "call-1", "turn_id": turn_id,
    }
    assert tuple(restarted.events_after(turn_id))[-1] == committed.event


def test_intent_bundle_rolls_back_payload_when_event_conflicts(tmp_path: Path) -> None:
    store = SQLiteAITurnStore(tmp_path / "intent-conflict.sqlite3")
    request = _request(); turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)

    with pytest.raises(TurnEventConflict, match="expected sequence"):
        store.append_intent_bundle(
            _turn_event(request, 2, "tool.intent.recorded", "intent", capability_id="memory.recall"),
            expected_sequence=0,
            intent_kind="tool-invocation-intent",
            intent_payload={"invocation_id": "call-rollback", "turn_id": turn_id},
        )

    with sqlite3.connect(tmp_path / "intent-conflict.sqlite3") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM ai_turn_payloads WHERE turn_id=? AND kind='tool-invocation-intent'",
            (turn_id,),
        ).fetchone()[0]
    assert count == 0


def test_hook_receipt_bundle_commits_event_and_receipt_together(tmp_path: Path) -> None:
    database = tmp_path / "hook-receipt.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request(); turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)

    committed = store.append_hook_receipt_bundle(
        _turn_event(request, 2, "hook.invoked", "hook"),
        expected_sequence=1,
        receipt_kind="codex-hook-invocation-receipt",
        receipt_payload={"schema_version": "1.0.0", "event": "PreToolUse"},
    )

    assert committed.event["data"]["receipt_ref"] == committed.hook_receipt_ref
    assert SQLiteAITurnStore(database).get(committed.hook_receipt_ref)["event"] == "PreToolUse"


def test_hook_receipt_bundle_rolls_back_receipt_when_event_conflicts(tmp_path: Path) -> None:
    database = tmp_path / "hook-receipt-conflict.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request(); turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)

    with pytest.raises(TurnEventConflict, match="expected sequence"):
        store.append_hook_receipt_bundle(
            _turn_event(request, 2, "hook.invoked", "hook"),
            expected_sequence=0,
            receipt_kind="codex-hook-invocation-receipt",
            receipt_payload={"schema_version": "1.0.0", "event": "PreToolUse"},
        )

    with sqlite3.connect(database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM ai_turn_payloads WHERE turn_id=? AND kind='codex-hook-invocation-receipt'",
            (turn_id,),
        ).fetchone()[0]
    assert count == 0


def test_model_terminal_bundle_rolls_back_all_receipts_when_event_conflicts(tmp_path: Path) -> None:
    database = tmp_path / "model-terminal-conflict.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request(); turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)
    event = _turn_event(request, 2, "hook.invoked", "model terminal fixture")

    with pytest.raises(TurnEventConflict, match="expected sequence"):
        store.append_model_terminal_bundle(
            event, expected_sequence=0,
            model_receipt_payload={"kind": "model"},
            dispatch_authority_receipt_payload={"kind": "dispatch"},
            prompt_cache_receipt_payload={"kind": "cache"},
        )

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT kind FROM ai_turn_payloads WHERE turn_id=? ORDER BY kind", (turn_id,),
        ).fetchall()
        event_count = connection.execute(
            "SELECT COUNT(*) FROM ai_turn_events WHERE turn_id=?", (turn_id,),
        ).fetchone()[0]
    assert rows == []
    assert event_count == 1


def test_model_attempt_bundles_commit_reservation_dispatch_terminal_and_receipt_together(tmp_path: Path) -> None:
    database = tmp_path / "model-attempt-bundle.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request(); turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)
    lease = store.try_acquire_run_lease(
        turn_id, "model-owner", now=NOW, stale_after=NOW + timedelta(seconds=30),
    )
    assert lease is not None
    dispatch = _model_attempt_dispatch(request)
    committed = store.commit_model_attempt_dispatch_bundle(
        _model_attempt_event(request, 2, "model.attempt.dispatched", "dispatched", dispatch),
        expected_sequence=1,
        dispatch_payload=dispatch,
        run_lease=lease,
    )

    assert store.get(committed.dispatch_payload_ref) == dispatch
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT status,dispatch_payload_ref,terminal_receipt_ref,lease_owner_id,lease_generation "
            "FROM ai_model_attempt_reservations WHERE attempt_id=?",
            (dispatch["attempt_id"],),
        ).fetchone()
        effect = connection.execute(
            "SELECT kind,effect_class,purpose,state,intent_ref,attempt FROM effect WHERE operation_id=?",
            (dispatch["attempt_id"],),
        ).fetchone()
    assert row == (
        "committed", committed.dispatch_payload_ref, None,
        lease.owner_id, lease.generation,
    )
    assert effect == (
        "model_call", "AT_MOST_ONCE", "primary", "PLANNED",
        committed.dispatch_payload_ref, 0,
    )

    receipt = _model_attempt_receipt(dispatch)
    terminal_box = {}

    def handler():
        terminal = store.append_model_attempt_terminal_bundle(
            _model_attempt_event(request, 3, "model.attempt.terminal", "succeeded", receipt),
            expected_sequence=2,
            attempt_receipt_payload=receipt,
            run_lease=lease,
        )
        terminal_box["terminal"] = terminal
        return "provider-value", terminal.attempt_receipt_ref

    assert store.execute_model_attempt_handler(dispatch["attempt_id"], handler) == "provider-value"
    terminal = terminal_box["terminal"]

    assert terminal.event["data"]["receipt_ref"] == terminal.attempt_receipt_ref
    assert terminal.event["data"]["evidence_refs"] == [committed.dispatch_payload_ref]
    assert store.get(terminal.attempt_receipt_ref) == receipt
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT status,dispatch_payload_ref,terminal_receipt_ref,terminal_status "
            "FROM ai_model_attempt_reservations WHERE attempt_id=?",
            (dispatch["attempt_id"],),
        ).fetchone()
        effect = connection.execute(
            "SELECT state,result_ref,error_ref FROM effect WHERE operation_id=?",
            (dispatch["attempt_id"],),
        ).fetchone()
        binding = connection.execute(
            "SELECT receipt_ref,receipt_kind FROM effect_receipt WHERE operation_id=?",
            (dispatch["attempt_id"],),
        ).fetchone()
    assert row == (
        "terminal", committed.dispatch_payload_ref,
        terminal.attempt_receipt_ref, "succeeded",
    )
    assert effect == ("SETTLED_OK", terminal.attempt_receipt_ref, None)
    assert binding == (terminal.attempt_receipt_ref, "model-wire-attempt-receipt")


def test_turn_coordination_heartbeat_does_not_claim_planned_model_effect(tmp_path: Path) -> None:
    database = tmp_path / "model-effect-heartbeat.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request()
    turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)
    lease = store.try_acquire_run_lease(
        turn_id,
        "model-owner",
        now=NOW,
        stale_after=NOW + timedelta(seconds=30),
    )
    assert lease is not None
    dispatch = _model_attempt_dispatch(request)
    dispatch["dispatched_at"] = NOW.isoformat()
    store.commit_model_attempt_dispatch_bundle(
        _model_attempt_event(request, 2, "model.attempt.dispatched", "dispatched", dispatch),
        expected_sequence=1,
        dispatch_payload=dispatch,
        run_lease=lease,
    )
    renewed = store.renew_run_lease(
        lease,
        now=NOW + timedelta(seconds=10),
        stale_after=NOW + timedelta(seconds=70),
    )
    assert renewed is not None
    with sqlite3.connect(database) as connection:
        effect = connection.execute(
            "SELECT lease_owner,lease_expires_at,attempt FROM effect WHERE operation_id=?",
            (dispatch["attempt_id"],),
        ).fetchone()
    assert effect == (None, None, 0)


def test_model_attempt_handler_persists_receipt_before_settle_and_runs_only_inflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "model-handler-settle-fault.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request(); turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)
    lease = store.try_acquire_run_lease(
        turn_id, "model-owner", now=NOW, stale_after=NOW + timedelta(seconds=90),
    )
    assert lease is not None
    dispatch = _model_attempt_dispatch(request)
    store.commit_model_attempt_dispatch_bundle(
        _model_attempt_event(request, 2, "model.attempt.dispatched", "dispatched", dispatch),
        expected_sequence=1, dispatch_payload=dispatch, run_lease=lease,
    )
    receipt = _model_attempt_receipt(dispatch)
    terminal_box = {}

    def fail_settle(*_args, **_kwargs):
        raise RuntimeError("settle interrupted")

    monkeypatch.setattr(store._effect_runner, "settle_ok", fail_settle)

    def handler():
        with sqlite3.connect(database) as connection:
            state = connection.execute(
                "SELECT state FROM effect WHERE operation_id=?", (dispatch["attempt_id"],),
            ).fetchone()[0]
        assert state == "INFLIGHT"
        terminal = store.append_model_attempt_terminal_bundle(
            _model_attempt_event(request, 3, "model.attempt.terminal", "succeeded", receipt),
            expected_sequence=2, attempt_receipt_payload=receipt, run_lease=lease,
        )
        terminal_box["receipt_ref"] = terminal.attempt_receipt_ref
        return "provider-value", terminal.attempt_receipt_ref

    with pytest.raises(RuntimeError, match="settle interrupted"):
        store.execute_model_attempt_handler(
            dispatch["attempt_id"], handler, run_lease=lease,
        )

    with sqlite3.connect(database) as connection:
        effect = connection.execute(
            "SELECT state,result_ref,attempt FROM effect WHERE operation_id=?",
            (dispatch["attempt_id"],),
        ).fetchone()
    assert effect == ("INFLIGHT", None, 1)
    assert store.get(terminal_box["receipt_ref"]) == receipt


def test_model_attempt_bundle_conflict_stale_identity_and_missing_active_lease_leave_no_orphans(tmp_path: Path) -> None:
    database = tmp_path / "model-attempt-conflict.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request(); turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)
    dispatch = _model_attempt_dispatch(request)
    event = _model_attempt_event(request, 2, "model.attempt.dispatched", "dispatched", dispatch)

    with pytest.raises(TurnEventConflict, match="expected sequence"):
        store.commit_model_attempt_dispatch_bundle(
            event, expected_sequence=0, dispatch_payload=dispatch,
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_model_attempt_reservations WHERE turn_id=?", (turn_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_turn_payloads WHERE turn_id=?", (turn_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM effect WHERE turn_id=?", (turn_id,),
        ).fetchone()[0] == 0

    lease = store.try_acquire_run_lease(
        turn_id, "model-owner", now=NOW, stale_after=NOW + timedelta(seconds=30),
    )
    assert lease is not None
    with pytest.raises(RunLeaseRevoked):
        store.commit_model_attempt_dispatch_bundle(
            event, expected_sequence=1, dispatch_payload=dispatch,
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_model_attempt_reservations WHERE turn_id=?", (turn_id,),
        ).fetchone()[0] == 0

    committed = store.commit_model_attempt_dispatch_bundle(
        event, expected_sequence=1, dispatch_payload=dispatch, run_lease=lease,
    )
    duplicate = _model_attempt_event(request, 3, "model.attempt.dispatched", "duplicate", dispatch)
    with pytest.raises(TurnEventConflict, match="reservation identity"):
        store.commit_model_attempt_dispatch_bundle(
            duplicate, expected_sequence=2, dispatch_payload=dispatch, run_lease=lease,
        )
    receipt = _model_attempt_receipt(dispatch)
    receipt["provider_id"] = "other-provider"
    with pytest.raises(TurnEventConflict, match="reservation identity"):
        store.append_model_attempt_terminal_bundle(
            _model_attempt_event(request, 3, "model.attempt.terminal", "bad identity", receipt),
            expected_sequence=2,
            attempt_receipt_payload=receipt,
            run_lease=lease,
        )
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT status,terminal_receipt_ref FROM ai_model_attempt_reservations WHERE attempt_id=?",
            (dispatch["attempt_id"],),
        ).fetchone()
        receipts = connection.execute(
            "SELECT COUNT(*) FROM ai_turn_payloads WHERE turn_id=? AND kind='model-wire-attempt-receipt'",
            (turn_id,),
        ).fetchone()[0]
    assert row == ("committed", None)
    assert receipts == 0
    assert store.get(committed.dispatch_payload_ref) == dispatch


def test_model_attempt_ambiguous_terminal_is_unknown_and_never_looks_retryable(tmp_path: Path) -> None:
    database = tmp_path / "model-attempt-unknown.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request(); turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)
    lease = store.try_acquire_run_lease(
        turn_id, "model-owner", now=NOW, stale_after=NOW + timedelta(seconds=30),
    )
    assert lease is not None
    dispatch = _model_attempt_dispatch(request)
    store.commit_model_attempt_dispatch_bundle(
        _model_attempt_event(request, 2, "model.attempt.dispatched", "dispatched", dispatch),
        expected_sequence=1, dispatch_payload=dispatch, run_lease=lease,
    )
    receipt = _model_attempt_receipt(dispatch)
    receipt["status"] = "failed_transport"
    receipt["error_code"] = "provider.transport_failed"
    def handler():
        store.append_model_attempt_terminal_bundle(
            _model_attempt_event(request, 3, "model.attempt.terminal", "unknown", receipt),
            expected_sequence=2, attempt_receipt_payload=receipt, run_lease=lease,
        )
        raise RuntimeError("provider transport failed")

    with pytest.raises(RuntimeError, match="provider transport failed"):
        store.execute_model_attempt_handler(dispatch["attempt_id"], handler)

    with sqlite3.connect(database) as connection:
        effect = connection.execute(
            "SELECT effect_class,state,result_ref,error_ref FROM effect WHERE operation_id=?",
            (dispatch["attempt_id"],),
        ).fetchone()
    assert effect == (
        "AT_MOST_ONCE", "UNKNOWN", None, "provider.transport_failed",
    )


def test_model_attempt_terminal_rejects_pre_migration_reservation_without_effect(tmp_path: Path) -> None:
    database = tmp_path / "model-attempt-legacy.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request(); turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)
    lease = store.try_acquire_run_lease(
        turn_id, "model-owner", now=NOW, stale_after=NOW + timedelta(seconds=30),
    )
    assert lease is not None
    dispatch = _model_attempt_dispatch(request)
    store.commit_model_attempt_dispatch_bundle(
        _model_attempt_event(request, 2, "model.attempt.dispatched", "dispatched", dispatch),
        expected_sequence=1, dispatch_payload=dispatch, run_lease=lease,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM effect WHERE operation_id=?", (dispatch["attempt_id"],))
        connection.commit()

    with pytest.raises(TurnEventConflict, match="Effect was not found"):
        store.append_model_attempt_terminal_bundle(
            _model_attempt_event(
                request, 3, "model.attempt.terminal", "succeeded", _model_attempt_receipt(dispatch),
            ),
            expected_sequence=2,
            attempt_receipt_payload=_model_attempt_receipt(dispatch),
            run_lease=lease,
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM effect WHERE operation_id=?", (dispatch["attempt_id"],),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT status,terminal_receipt_ref,terminal_status "
            "FROM ai_model_attempt_reservations WHERE attempt_id=?",
            (dispatch["attempt_id"],),
        ).fetchone() == ("committed", None, None)
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_turn_payloads WHERE kind='model-wire-attempt-receipt'"
        ).fetchone()[0] == 0


def test_model_attempt_terminal_rejects_revoked_committing_lease(tmp_path: Path) -> None:
    database = tmp_path / "model-attempt-terminal-revoked.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request(); turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)
    lease = store.try_acquire_run_lease(
        turn_id, "model-owner", now=NOW, stale_after=NOW + timedelta(seconds=30),
    )
    assert lease is not None
    dispatch = _model_attempt_dispatch(request)
    store.commit_model_attempt_dispatch_bundle(
        _model_attempt_event(request, 2, "model.attempt.dispatched", "dispatched", dispatch),
        expected_sequence=1, dispatch_payload=dispatch, run_lease=lease,
    )
    store.release_strict_run_lease(lease)

    with pytest.raises(RunLeaseRevoked):
        store.append_model_attempt_terminal_bundle(
            _model_attempt_event(
                request, 3, "model.attempt.terminal", "terminal", _model_attempt_receipt(dispatch),
            ),
            expected_sequence=2,
            attempt_receipt_payload=_model_attempt_receipt(dispatch),
            run_lease=lease,
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status,terminal_receipt_ref FROM ai_model_attempt_reservations"
        ).fetchone() == ("committed", None)
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_turn_payloads WHERE kind='model-wire-attempt-receipt'"
        ).fetchone()[0] == 0


def test_tool_outcome_bundle_commits_outcome_receipt_and_event_together(tmp_path: Path) -> None:
    database = tmp_path / "tool-outcome.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request(); turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)
    store.append_intent_bundle(
        _turn_event(request, 2, "tool.intent.recorded", "intent", capability_id="memory.recall"),
        expected_sequence=1,
        intent_kind="tool-invocation-intent",
        intent_payload={"invocation_id": "call-1", "turn_id": turn_id},
    )
    store.append(
        _turn_event(request, 3, "tool.started", "started", capability_id="memory.recall"),
        expected_sequence=2,
    )

    committed = store.append_tool_outcome_bundle(
        _turn_event(request, 4, "tool.outcome.recorded", "outcome", capability_id="memory.recall"),
        expected_sequence=3,
        outcome_kind="tool-invocation-outcome",
        outcome_payload={"invocation_id": "call-1", "turn_id": turn_id, "receipt_ref": None},
        operation_receipt_kind="tool-operation-receipt",
        operation_receipt_payload={"operation": "read", "status": "completed"},
    )

    assert committed.event["data"]["payload_ref"] == committed.outcome_payload_ref
    assert committed.event["data"]["receipt_ref"] == committed.operation_receipt_ref
    assert committed.operation_receipt_ref is not None
    restarted = SQLiteAITurnStore(database)
    assert restarted.get(committed.outcome_payload_ref)["receipt_ref"] == committed.operation_receipt_ref
    assert restarted.get(committed.operation_receipt_ref) == {"operation": "read", "status": "completed"}


def test_tool_outcome_bundle_commits_result_with_outcome_in_one_transaction(tmp_path: Path) -> None:
    database = tmp_path / "tool-outcome-result.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request(); turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)

    committed = store.append_tool_outcome_bundle(
        _turn_event(request, 2, "tool.outcome.recorded", "outcome"),
        expected_sequence=1,
        result_kind="tool-result",
        result_payload={"value": "recovered"},
        outcome_kind="tool-invocation-outcome",
        outcome_payload={
            "invocation_id": "call-result", "turn_id": turn_id,
            "payload_ref": None, "receipt_ref": None,
        },
        operation_receipt_kind="tool-operation-receipt",
        operation_receipt_payload={"operation": "plugin_hand", "status": "completed"},
    )

    assert committed.result_payload_ref is not None
    assert store.get(committed.result_payload_ref) == {"value": "recovered"}
    outcome = store.get(committed.outcome_payload_ref)
    assert outcome["payload_ref"] == committed.result_payload_ref
    assert outcome["receipt_ref"] == committed.operation_receipt_ref


def test_tool_outcome_result_bundle_conflict_leaves_no_payload_orphans(tmp_path: Path) -> None:
    database = tmp_path / "tool-outcome-result-conflict.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request(); turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)

    with pytest.raises(TurnEventConflict, match="expected sequence"):
        store.append_tool_outcome_bundle(
            _turn_event(request, 2, "tool.outcome.recorded", "outcome"),
            expected_sequence=0,
            result_kind="tool-result",
            result_payload={"value": "orphan"},
            outcome_kind="tool-invocation-outcome",
            outcome_payload={
                "invocation_id": "call-conflict", "turn_id": turn_id,
                "payload_ref": None, "receipt_ref": None,
            },
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_turn_payloads WHERE turn_id=? AND kind IN "
            "('tool-result', 'tool-invocation-outcome')",
            (turn_id,),
        ).fetchone()[0] == 0


def test_tool_outcome_bundle_conflict_or_stale_lease_leaves_no_orphans(tmp_path: Path) -> None:
    database = tmp_path / "tool-outcome-conflict.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request(); turn_id, _ = store.claim_turn(request)
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)

    with pytest.raises(TurnEventConflict, match="expected sequence"):
        store.append_tool_outcome_bundle(
            _turn_event(request, 2, "tool.outcome.recorded", "outcome"),
            expected_sequence=0,
            outcome_kind="tool-invocation-outcome",
            outcome_payload={"invocation_id": "call-conflict", "turn_id": turn_id, "receipt_ref": None},
            operation_receipt_kind="tool-operation-receipt",
            operation_receipt_payload={"operation": "write"},
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_turn_payloads WHERE turn_id=? AND kind IN ('tool-invocation-outcome', 'tool-operation-receipt')",
            (turn_id,),
        ).fetchone()[0] == 0

    token = store.try_acquire_run_lease(
        turn_id, "outcome-owner", now=NOW, stale_after=NOW + timedelta(seconds=30),
    )
    assert token is not None
    store.release_strict_run_lease(token)
    with pytest.raises(RunLeaseRevoked):
        store.append_tool_outcome_bundle(
            _turn_event(request, 2, "tool.outcome.recorded", "outcome"),
            expected_sequence=1,
            outcome_kind="tool-invocation-outcome",
            outcome_payload={"invocation_id": "call-stale", "turn_id": turn_id, "receipt_ref": None},
            operation_receipt_kind="tool-operation-receipt",
            operation_receipt_payload={"operation": "write"},
            run_lease=token,
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_turn_payloads WHERE turn_id=? AND kind IN ('tool-invocation-outcome', 'tool-operation-receipt')",
            (turn_id,),
        ).fetchone()[0] == 0


def test_runtime_commits_provider_operation_receipt_with_durable_outcome(tmp_path: Path) -> None:
    database = tmp_path / "runtime-operation-receipt.sqlite3"
    provider = _AtomicReceiptProvider()
    registry = _registry("document.draft", "write", False, "receipt_required", provider)
    store = SQLiteAITurnStore(database)
    runtime = SynchronousAIRuntime(
        planner=_Planner("document.draft"), registry=registry,
        events=store, payloads=store, state=store,
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["document.draft"], "denied": [], "require_approval": [],
    }

    completed = runtime.submit_turn(request)

    assert completed.status == "completed" and provider.calls == 1
    outcome_event = next(
        item for item in store.events_after(completed.turn_id)
        if item["type"] == "tool.outcome.recorded"
    )
    outcome_ref = outcome_event["data"]["payload_ref"]
    receipt_ref = outcome_event["data"]["receipt_ref"]
    assert isinstance(outcome_ref, str) and isinstance(receipt_ref, str)
    assert store.get(outcome_ref)["receipt_ref"] == receipt_ref
    assert store.get(receipt_ref)["operation"] == "document.draft"


def test_immutable_payload_append_rolls_back_when_event_cannot_append(tmp_path: Path) -> None:
    store = SQLiteAITurnStore(tmp_path / "ai-turns.sqlite3")
    request = _request()
    turn_id, created = store.claim_turn(request)
    assert created
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)

    with pytest.raises(TurnEventConflict, match="expected sequence"):
        store.append_event_with_immutable_payload(
            _turn_event(request, 2, "context.resolved", "context"),
            expected_sequence=0,
            immutable_kind="frozen-tool-authorization-facts",
            immutable_payload={"turn_id": turn_id, "schema_version": "1.1.0"},
        )

    assert store.get_immutable_payload(turn_id, "frozen-tool-authorization-facts") is None
    assert len(tuple(store.events_after(turn_id))) == 1


def test_approval_bundle_persists_action_and_fact_then_clears_pending(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request()
    turn_id, created = store.claim_turn(request)
    assert created
    store.append(_turn_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0)
    store.put_pending(turn_id, {"capability_id": "document.draft"})

    action = _action(turn_id, 1, "event-00000000000000000000000000000001")
    committed = store.append_approval_bundle(
        _turn_event(request, 2, "approval.resolved", "approved", actor="human", capability_id="document.draft"),
        expected_sequence=1,
        action_kind="turn-action",
        action_payload=action,
        approval_kind="frozen-approval-fact",
        approval_payload={"turn_id": turn_id, "tool_call_id": "call-1", "approved": True},
    )

    restarted = SQLiteAITurnStore(database)
    assert restarted.get_pending(turn_id) == {"capability_id": "document.draft"}
    assert restarted.get(committed.action_payload_ref) == action
    assert restarted.get(committed.approval_payload_ref) == {
        "turn_id": turn_id, "tool_call_id": "call-1", "approved": True,
    }
    assert committed.event["data"]["payload_ref"] == committed.action_payload_ref
    assert committed.approval_payload_ref in committed.event["data"]["evidence_refs"]


class _RequestStateInitialProvider:
    def invoke(self, _request):
        raise ToolProviderFailure(
            "mcp.input_required", effect_certainty="unknown",
            continuation_state=b"opaque-request-state",
        )

    def continue_request_state(self, _request, _state):  # pragma: no cover - reconnect replaces it
        raise AssertionError("continuation must use a fresh provider")


class _RequestStateContinuationProvider:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.continuation_calls = 0

    def invoke(self, _request):  # pragma: no cover - continuation has a dedicated method
        raise AssertionError("initial MCP call must not be replayed")

    def continue_request_state(self, request, request_state):
        self.continuation_calls += 1
        assert request_state == b"opaque-request-state"
        assert request["arguments"] == {"query": "question"}
        if self.outcome == "input_required":
            raise ToolProviderFailure(
                "mcp.input_required", effect_certainty="unknown",
                continuation_state=b"second-opaque-state",
            )
        if self.outcome == "unknown":
            raise ToolProviderFailure("mcp.transport_unconfirmed", effect_certainty="unknown")
        return {"summary": "continued", "result": {"answer": "continued"}, "evidence_refs": []}


def _request_state_runtime(tmp_path: Path, *, continuation: str):
    registry = ScopedCapabilityRegistry()
    definition = _native_mcp_definition("calendar-server")
    registration = registry.register(definition, _RequestStateInitialProvider())
    provider = _RequestStateContinuationProvider(continuation)
    reconnect = {"calls": 0}

    def reconnect_provider(server_id: str) -> None:
        assert server_id == "calendar-server"
        reconnect["calls"] += 1
        registration.close()
        registry.register(definition, provider)

    database = tmp_path / "request-state.sqlite3"
    store = SQLiteAITurnStore(database)
    runtime = SynchronousAIRuntime(
        planner=_Planner("calendar.read"), registry=registry,
        events=store, payloads=store, state=store,
        mcp_continuation_reconnector=reconnect_provider,
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["calendar.read"], "denied": [], "require_approval": [],
    }
    return runtime, provider, reconnect, request


def _mcp_action(waiting, runtime: SynchronousAIRuntime) -> dict[str, object]:
    event = tuple(runtime.events_after(waiting.turn_id))[-1]
    return {
        "schema_version": "1.0.0", "action_id": "action-mcp-request-state-000000001", "turn_id": waiting.turn_id,
        "type": "mcp_continue", "target_event_id": event["event_id"], "reason": "continue stateless MCP request",
        "actor": "user", "expected_sequence": waiting.current_sequence,
        "idempotency_key": "mcp-request-state-action-0001", "created_at": "2026-08-26T00:00:00Z",
    }


def _registry(capability_id: str, mode: str, approval: bool, semantics: str, provider: object) -> ScopedCapabilityRegistry:
    registry = ScopedCapabilityRegistry()
    registry.register(CapabilityDefinition(capability_id, 1, mode, approval, semantics, "crp://default/contracts/in.schema.json", "crp://default/contracts/out.schema.json"), provider)
    return registry


def _post_tool_stop_host() -> CodexHookHost:
    manifest = HookHandlerManifest(
        "post-tool-stop", "post-tool-stop-r1", HookEvent.POST_TOOL_USE, 0,
    )
    return CodexHookHost(
        catalog=HookPolicyCatalog(HookPolicySnapshot("post-tool-policy-r1", (manifest,))),
        runner=RevisionPinnedHookRunner({
            (manifest.hook_id, manifest.revision): lambda _manifest, _payload: HookRun(
                0, 0, True,
                stdout=json.dumps({"continue": False, "stopReason": "stop after recovery"}),
                hook_id=manifest.hook_id,
            ),
        }),
    )


def _native_mcp_definition(owner_id: str) -> CapabilityDefinition:
    tool = ToolDefinition(
        "calendar.read", 2, "Read calendar", "Read events from MCP",
        "mcp", owner_id, "read", ("calendar_event",), "mcp",
        "crp://input", "crp://output", None, "read_only", "parallel",
        (f"mcp:{owner_id}",), "never_retry", ToolRetryPolicy(1, 0, ()),
        None, None, "read_only", "remote", (owner_id,),
        ("calendar_event",), 12_000, ("calendar.read",), ("mcp_server_enabled",),
        connection_identity=ToolConnectionIdentity(
            "mcp", owner_id, "2025-11-25", 2,
            "calendar-local", "personal-calendar", 1, 1, 1,
        ),
    )
    return CapabilityDefinition(
        "calendar.read", 2, "read", False, "read_only",
        "crp://input", "crp://output", tool,
    )


def _request() -> dict[str, object]:
    return json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))


def _turn_event(
    request: dict[str, object],
    sequence: int,
    event_type: str,
    summary: str,
    *,
    actor: str = "kernel",
    capability_id: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "event_id": f"event-{sequence:032x}",
        "turn_id": request["turn_id"],
        "session_id": request["session_id"],
        "sequence": sequence,
        "type": event_type,
        "actor": actor,
        "correlation": {
            "step_id": None,
            "tool_call_id": None,
            "model_request_id": None,
            "operation_id": request["operation_id"],
        },
        "data": {
            "status": "accepted" if event_type == "turn.accepted" else "running",
            "summary": summary,
            "capability_id": capability_id,
            "payload_ref": None,
            "receipt_ref": None,
            "evidence_refs": [],
            "error_code": None,
            "retryable": False,
        },
        "occurred_at": "2026-08-25T00:00:00+00:00",
    }


def _model_attempt_dispatch(request: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "attempt_id": "model-wire-attempt-0123456789abcdef0123456789abcdef",
        "turn_id": request["turn_id"],
        "model_request_id": "model-request-0123456789abcdef0123456789abcdef",
        "attempt_number": 1,
        "routing_snapshot_revision": "a" * 64,
        "provider_id": "openai",
        "model_id": "gpt-5.4-mini",
        "dispatched_at": "2026-08-25T00:00:00+00:00",
        "input_stored": False,
        "output_stored": False,
    }


def _model_attempt_receipt(
    dispatch: dict[str, object], *, attempt_number: int | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "attempt_id": dispatch["attempt_id"],
        "turn_id": dispatch["turn_id"],
        "model_request_id": dispatch["model_request_id"],
        "attempt_number": dispatch["attempt_number"] if attempt_number is None else attempt_number,
        "routing_snapshot_revision": dispatch["routing_snapshot_revision"],
        "provider_id": dispatch["provider_id"],
        "model_id": dispatch["model_id"],
        "status": "succeeded",
        "started_at": dispatch["dispatched_at"],
        "completed_at": "2026-08-25T00:00:01+00:00",
        "duration_ms": 1000,
        "usage_status": "unavailable",
        "usage": None,
        "cache_status": "unavailable",
        "cache_metadata": None,
        "input_stored": False,
        "output_stored": False,
        "error_code": None,
    }


def _model_attempt_event(
    request: dict[str, object],
    sequence: int,
    event_type: str,
    summary: str,
    attempt: dict[str, object],
) -> dict[str, object]:
    event = _turn_event(request, sequence, event_type, summary)
    event["correlation"]["model_request_id"] = attempt["model_request_id"]
    event["data"]["status"] = "running" if event_type == "model.attempt.dispatched" else "completed"
    return event


def _action(turn_id: str, sequence: int, target_event_id: str) -> dict[str, object]:
    return {"schema_version": "1.0.0", "action_id": "action-0123456789abcdef0123456789abcdef", "turn_id": turn_id, "type": "approve", "target_event_id": target_event_id, "reason": "approved", "actor": "user", "expected_sequence": sequence, "idempotency_key": "approve-durable-0001", "created_at": "2026-08-23T06:00:00Z"}


def _resume_action(turn_id: str, sequence: int) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "action_id": "action-ffffffffffffffffffffffffffffffff",
        "turn_id": turn_id,
        "type": "resume",
        "target_event_id": None,
        "reason": "resume interrupted idempotent tool",
        "actor": "user",
        "expected_sequence": sequence,
        "idempotency_key": "resume-durable-0001",
        "created_at": "2026-08-24T06:00:00Z",
    }


def test_immutable_read_cache_copies_values_and_does_not_cache_absence(tmp_path):
    store = SQLiteAITurnStore(tmp_path / "cache.sqlite3", cache_immutable_reads=True)
    request = _request()
    identity = request["turn_id"]
    assert store.get_request(identity) is None
    store.claim_turn(request)
    store.get_request(identity)["scope"]["project_id"] = "changed-copy"
    assert store.get_request(identity)["scope"] == request["scope"]
    assert store.get_immutable_payload(identity, "cache-test") is None
    ref = store.get_or_create_immutable_payload(identity, "cache-test", {"values": [1]})
    store.get(ref)["values"].append(2)
    store.get_immutable_payload(identity, "cache-test")[1]["values"].append(3)
    assert store.get(ref) == {"values": [1]}
    assert store.get_immutable_payload(identity, "cache-test") == (ref, {"values": [1]})
