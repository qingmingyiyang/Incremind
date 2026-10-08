from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from core.ai_kernel import (
    CapabilityDefinition,
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    InMemoryTurnStateStore,
    RunLeaseRevoked,
    SQLiteAITurnStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
)


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, _request):
        self.calls += 1
        return {"summary": "read", "result": {}, "evidence_refs": []}


class _RevokingToolPlanner:
    def __init__(self, state, token) -> None:
        self._state = state
        self._token = token

    def plan(self, _request, _events, _capabilities, _payloads, _execution_control=None):
        assert self._state.mark_run_lease_stale(self._token, now=NOW) is not None
        replacement = self._state.takeover_run_lease(
            self._token.turn_id, expected_generation=self._token.generation,
            owner_id="replacement", now=NOW, stale_after=NOW + timedelta(seconds=10),
            disposition="safe",
        )
        assert replacement is not None
        return {"type": "tool", "capability_id": "memory.recall", "arguments": {}}


def test_runtime_old_token_cannot_append_after_safe_takeover() -> None:
    state = InMemoryTurnStateStore()
    runtime = SynchronousAIRuntime(
        planner=_CompletePlanner(), registry=ScopedCapabilityRegistry(),
        events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore(), state=state,
    )
    request = _request()
    runtime.accept_turn(request)
    old = state.try_acquire_run_lease(str(request["turn_id"]), "old", now=NOW, stale_after=NOW)
    assert old is not None
    assert state.mark_run_lease_stale(old, now=NOW) is not None
    assert state.takeover_run_lease(old.turn_id, expected_generation=old.generation, owner_id="new", now=NOW, stale_after=NOW + timedelta(seconds=10), disposition="safe") is not None
    with pytest.raises(RunLeaseRevoked):
        runtime.run_accepted_turn(old.turn_id, old)
    with pytest.raises(RunLeaseRevoked):
        runtime.fail_accepted_turn(old.turn_id, old)
    assert len(tuple(runtime.events_after(old.turn_id))) == 1


def test_runtime_rejects_no_token_path_when_strict_lease_exists() -> None:
    state = InMemoryTurnStateStore()
    runtime = SynchronousAIRuntime(planner=_CompletePlanner(), registry=ScopedCapabilityRegistry(), events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore(), state=state)
    request = _request()
    runtime.accept_turn(request)
    token = state.try_acquire_run_lease(str(request["turn_id"]), "owner", now=NOW, stale_after=NOW + timedelta(seconds=10))
    assert token is not None
    with pytest.raises(RunLeaseRevoked):
        runtime.run_accepted_turn(token.turn_id)
    with pytest.raises(RunLeaseRevoked):
        runtime.fail_accepted_turn(token.turn_id)


def test_revoked_lease_after_started_before_fence_never_invokes_provider() -> None:
    state = InMemoryTurnStateStore()
    provider = _Provider()
    registry = ScopedCapabilityRegistry()
    registry.register(_definition(), provider)
    request = _request()
    state.claim_turn(request)
    token = state.try_acquire_run_lease(str(request["turn_id"]), "old", now=NOW, stale_after=NOW)
    assert token is not None
    class Dispatcher:
        def prepare(self, _invocation_id):
            return None

        def abandon_prepared(self, _invocation_id):
            return None

        def request_cancel(self, _invocation_id):
            return False

        def dispatch(self, _provider, _request, observer):
            observer.claimed()
            observer.started()
            assert state.mark_run_lease_stale(token, now=NOW) is not None
            assert state.takeover_run_lease(token.turn_id, expected_generation=token.generation, owner_id="replacement", now=NOW, stale_after=NOW + timedelta(seconds=10), disposition="safe") is not None
            with observer.fence():
                return _provider.invoke({})

    runtime = SynchronousAIRuntime(
        planner=_ToolPlanner(), registry=registry,
        events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore(), state=state,
        dispatcher=Dispatcher(),
    )
    runtime._append(str(request["turn_id"]), "turn.accepted", "accepted", "turn accepted")
    with pytest.raises(RunLeaseRevoked):
        runtime.run_accepted_turn(token.turn_id, token)
    assert provider.calls == 0


def test_sqlite_fenced_append_rejects_revoked_token_atomically(tmp_path: Path) -> None:
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    request = _request()
    store.claim_turn(request)
    turn_id = str(request["turn_id"])
    token = store.try_acquire_run_lease(turn_id, "old", now=NOW, stale_after=NOW)
    assert token is not None
    accepted = _event(request, 1, "turn.accepted", "accepted")
    store.append(accepted, expected_sequence=0, run_lease=token)
    assert store.mark_run_lease_stale(token, now=NOW) is not None
    assert store.takeover_run_lease(turn_id, expected_generation=token.generation, owner_id="new", now=NOW, stale_after=NOW + timedelta(seconds=10), disposition="safe") is not None
    with pytest.raises(RunLeaseRevoked):
        store.append(_event(request, 2, "context.resolved", "running"), expected_sequence=1, run_lease=token)
    assert len(store.events_after(turn_id)) == 1


class _CompletePlanner:
    def plan(self, *_args, **_kwargs):
        return {"type": "complete", "summary": "done", "evidence_refs": []}


class _ToolPlanner:
    def plan(self, *_args, **_kwargs):
        return {"type": "tool", "capability_id": "memory.recall", "arguments": {}}


def _definition() -> CapabilityDefinition:
    return CapabilityDefinition("memory.recall", 1, "read", False, "read_only", "crp://input", "crp://output")


def _event(request: dict[str, object], sequence: int, event_type: str, status: str) -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "event_id": f"event-{uuid4().hex}", "turn_id": request["turn_id"],
        "session_id": request["session_id"], "sequence": sequence, "type": event_type, "actor": "kernel",
        "correlation": {"step_id": None, "tool_call_id": None, "model_request_id": None, "operation_id": request["operation_id"]},
        "data": {"status": status, "summary": "state", "capability_id": None, "payload_ref": None, "receipt_ref": None, "evidence_refs": [], "error_code": None, "retryable": False},
        "occurred_at": NOW.isoformat(),
    }


def _request() -> dict[str, object]:
    return json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))
