from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from time import sleep
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.api.ai_turn_recovery_startup import scan_due_ai_turn_recovery
from backend.api.ai_turn_recovery_worker import AIRecoveryWorker, run_safe_ai_turn_recovery
from backend.api.app import create_app
from core.ai_kernel import (
    InMemoryTurnStateStore,
    RecoveryDecision,
    RecoveryQueueItem,
    RunLeaseToken,
    RunLeaseRevoked,
    SQLiteAITurnStore,
    classify_recovery,
)
from core.ai_kernel.tool_invocation import ToolInvocationOutcome, outcome_to_payload


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


def test_classifier_terminal_waiting_model_and_tool_boundaries() -> None:
    accepted = _event(1, "turn.accepted", "accepted")
    assert classify_recovery("turn-x", 1, (accepted, _event(2, "turn.completed", "completed"))).disposition == "terminal_noop"
    waiting = (
        accepted,
        _event(2, "tool.requested", "running", tool="t1", capability="document.write"),
        _event(3, "approval.required", "waiting_approval", tool="t1", capability="document.write", payload_ref="crp://approval/1"),
    )
    assert classify_recovery("turn-x", 1, waiting).disposition == "waiting_noop"
    assert classify_recovery("turn-x", 1, (accepted, _event(2, "model.requested", "running", model="m1"))).disposition == "quarantine"
    started = (
        accepted,
        _event(2, "tool.requested", "running", tool="t1", capability="memory.read"),
        _event(3, "tool.intent.recorded", "running", tool="t1", capability="memory.read"),
        _event(4, "tool.dispatch.claimed", "running", tool="t1", capability="memory.read"),
        _event(5, "tool.started", "running", tool="t1", capability="memory.read"),
    )
    assert classify_recovery("turn-x", 1, started).disposition == "quarantine"
    assert classify_recovery("turn-x", 1, started[:-1]).disposition == "safe_resume"


def test_classifier_keeps_durable_mcp_request_state_waiting_without_resume() -> None:
    """A restart must retain the explicit user-action boundary without wire work."""
    events = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "tool.requested", "running", tool="mcp-call", capability="calendar.read"),
        _event(3, "tool.intent.recorded", "running", tool="mcp-call", capability="calendar.read"),
        _event(4, "tool.dispatch.claimed", "running", tool="mcp-call", capability="calendar.read"),
        _event(5, "tool.started", "running", tool="mcp-call", capability="calendar.read"),
        _event(6, "mcp.continuation.required", "waiting_approval", tool="mcp-call", capability="calendar.read", payload_ref="crp://session/turn-x/mcp-request-state-v1-1/frozen"),
    )

    decision = classify_recovery("turn-x", 1, events)

    assert decision.disposition == "waiting_noop"
    assert decision.reason_code == "ai.recovery_waiting_mcp_continuation"


def test_hook_invocation_receipt_is_a_known_no_effect_recovery_event() -> None:
    events = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "hook.invoked", "running"),
    )

    decision = classify_recovery("turn-x", 1, events)

    assert decision.disposition == "safe_resume"
    assert decision.reason_code == "ai.recovery_no_effect_started"


def test_classifier_rejects_unknown_history_fake_waiting_and_orphan_model_terminal() -> None:
    fake_waiting = _event(1, "future.side_effect", "waiting_approval")
    assert classify_recovery("turn-x", 1, (fake_waiting,)).reason_code == "ai.recovery_event_identity_unknown"
    events = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "future.side_effect", "running"),
        _event(3, "turn.resumed", "running"),
    )
    assert classify_recovery("turn-x", 1, events).disposition == "quarantine"
    orphan = (_event(1, "turn.accepted", "accepted"), _event(2, "model.completed", "running", model="m1"))
    assert classify_recovery("turn-x", 1, orphan).reason_code == "ai.recovery_model_incomplete"


def test_classifier_accepts_only_structurally_valid_standalone_model_routing() -> None:
    routed = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "model.routed", "running", model="m1"),
    )
    assert classify_recovery("turn-x", 1, routed).disposition == "safe_resume"

    # A request without a terminal is never replayable, including after the
    # durable pre-egress route marker.
    requested_after_routing = routed + (
        _event(3, "model.requested", "running", model="m1"),
    )
    assert classify_recovery("turn-x", 1, requested_after_routing).disposition == "quarantine"


def test_classifier_rejects_invalid_model_routing_order_duplicates_and_terminals() -> None:
    accepted = _event(1, "turn.accepted", "accepted")
    terminal_before_routed = (
        accepted,
        _event(2, "model.requested", "running", model="m1"),
        _event(3, "model.completed", "running", model="m1"),
        _event(4, "model.routed", "running", model="m1"),
    )
    assert classify_recovery("turn-x", 1, terminal_before_routed).disposition == "quarantine"

    duplicate_routing = (
        accepted,
        _event(2, "model.requested", "running", model="m1"),
        _event(3, "model.routed", "running", model="m1"),
        _event(4, "model.routed", "running", model="m1"),
    )
    assert classify_recovery("turn-x", 1, duplicate_routing).disposition == "quarantine"

    orphan_terminal_after_routing = (
        accepted,
        _event(2, "model.routed", "running", model="m1"),
        _event(3, "model.completed", "running", model="m1"),
    )
    assert classify_recovery("turn-x", 1, orphan_terminal_after_routing).disposition == "quarantine"


def test_classifier_accepts_closed_requested_routed_model_lifecycle() -> None:
    events = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "model.requested", "running", model="m1"),
        _event(3, "model.routed", "running", model="m1"),
        _event(4, "model.completed", "running", model="m1"),
    )
    assert classify_recovery("turn-x", 1, events).disposition == "safe_resume"


def test_classifier_accepts_only_a_closed_durable_model_wire_attempt() -> None:
    dispatch_ref = "crp://session/turn-x/model-wire-attempt-dispatch/one"
    receipt_ref = "crp://session/turn-x/model-wire-attempt-receipt/one"
    dispatch = _wire_dispatch()
    receipt = _wire_receipt(dispatch)
    payloads = {dispatch_ref: dispatch, receipt_ref: receipt}
    events = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "model.requested", "running", model="m1"),
        _event(3, "model.routed", "running", model="m1"),
        _event(4, "model.attempt.dispatched", "running", model="m1", payload_ref=dispatch_ref),
        _event(5, "model.attempt.terminal", "completed", model="m1", receipt_ref=receipt_ref),
        _event(6, "model.completed", "running", model="m1"),
    )

    decision = classify_recovery(
        "turn-x", 1, events, payload_loader=payloads.__getitem__,
    )

    assert decision.disposition == "safe_resume"


@pytest.mark.parametrize("shape", ["unfinished", "orphan_terminal", "duplicate_terminal"])
def test_classifier_quarantines_unknown_or_drifted_model_wire_attempt(shape: str) -> None:
    dispatch_ref = "crp://session/turn-x/model-wire-attempt-dispatch/one"
    receipt_ref = "crp://session/turn-x/model-wire-attempt-receipt/one"
    dispatch = _wire_dispatch()
    payloads = {dispatch_ref: dispatch, receipt_ref: _wire_receipt(dispatch)}
    events = [
        _event(1, "turn.accepted", "accepted"),
        _event(2, "model.requested", "running", model="m1"),
        _event(3, "model.routed", "running", model="m1"),
    ]
    if shape != "orphan_terminal":
        events.append(_event(4, "model.attempt.dispatched", "running", model="m1", payload_ref=dispatch_ref))
    if shape != "unfinished":
        events.append(_event(len(events) + 1, "model.attempt.terminal", "completed", model="m1", receipt_ref=receipt_ref))
    if shape == "duplicate_terminal":
        events.append(_event(len(events) + 1, "model.attempt.terminal", "completed", model="m1", receipt_ref=receipt_ref))
    events.append(_event(len(events) + 1, "model.completed", "running", model="m1"))

    decision = classify_recovery(
        "turn-x", 1, tuple(events), payload_loader=payloads.__getitem__,
    )

    assert decision.disposition == "quarantine"
    assert decision.reason_code == "ai.recovery_model_incomplete"


def test_classifier_accepts_closed_nested_model_lifecycle_inside_parent_tool() -> None:
    events = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "tool.requested", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(3, "tool.intent.recorded", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(4, "tool.dispatch.claimed", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(5, "tool.started", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(6, "model.requested", "running", model="m1", tool="t1", step="step-1"),
        _event(7, "model.routed", "running", model="m1", tool="t1", step="step-1"),
        _event(8, "model.completed", "completed", model="m1", tool="t1", step="step-1"),
        _event(9, "tool.outcome.recorded", "running", tool="t1", capability="vision.analyze", step="step-1", payload_ref="crp://outcome/t1"),
    )
    outcome = outcome_to_payload(ToolInvocationOutcome(
        invocation_id="t1", turn_id="turn-x", capability_id="vision.analyze", attempt=1,
        status="completed", effect_certainty="confirmed_none", payload_ref="crp://result/t1",
        receipt_ref=None, evidence_refs=(), error_code=None, retryable=False,
    ))
    assert classify_recovery("turn-x", 1, events, payload_loader=lambda _ref: outcome).disposition == "safe_resume"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda events: events[5]["correlation"].update({"step_id": "other-step"}),
        lambda events: events[5]["correlation"].update({"tool_call_id": "other-tool"}),
        lambda events: events[8]["correlation"].update({"step_id": "other-step"}),
    ],
)
def test_classifier_rejects_nested_model_with_wrong_parent_correlation(mutate) -> None:
    events = [
        _event(1, "turn.accepted", "accepted"),
        _event(2, "tool.requested", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(3, "tool.intent.recorded", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(4, "tool.dispatch.claimed", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(5, "tool.started", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(6, "model.requested", "running", model="m1", tool="t1", step="step-1"),
        _event(7, "model.routed", "running", model="m1", tool="t1", step="step-1"),
        _event(8, "model.completed", "completed", model="m1", tool="t1", step="step-1"),
        _event(9, "tool.outcome.recorded", "running", tool="t1", capability="vision.analyze", step="step-1", payload_ref="crp://outcome/t1"),
    ]
    mutate(events)
    outcome = outcome_to_payload(ToolInvocationOutcome(
        invocation_id="t1", turn_id="turn-x", capability_id="vision.analyze", attempt=1,
        status="completed", effect_certainty="confirmed_none", payload_ref="crp://result/t1",
        receipt_ref=None, evidence_refs=(), error_code=None, retryable=False,
    ))
    assert classify_recovery("turn-x", 1, tuple(events), payload_loader=lambda _ref: outcome).disposition == "quarantine"


def test_classifier_rejects_nested_model_terminal_after_parent_outcome() -> None:
    events = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "tool.requested", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(3, "tool.intent.recorded", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(4, "tool.dispatch.claimed", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(5, "tool.started", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(6, "model.requested", "running", model="m1", tool="t1", step="step-1"),
        _event(7, "model.routed", "running", model="m1", tool="t1", step="step-1"),
        _event(8, "tool.outcome.recorded", "running", tool="t1", capability="vision.analyze", step="step-1", payload_ref="crp://outcome/t1"),
        _event(9, "model.completed", "completed", model="m1", tool="t1", step="step-1"),
    )
    outcome = outcome_to_payload(ToolInvocationOutcome(
        invocation_id="t1", turn_id="turn-x", capability_id="vision.analyze", attempt=1,
        status="completed", effect_certainty="confirmed_none", payload_ref="crp://result/t1",
        receipt_ref=None, evidence_refs=(), error_code=None, retryable=False,
    ))
    assert classify_recovery("turn-x", 1, events, payload_loader=lambda _ref: outcome).disposition == "quarantine"


def test_classifier_rejects_nested_model_before_parent_tool_started() -> None:
    events = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "tool.requested", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(3, "tool.intent.recorded", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(4, "tool.dispatch.claimed", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(5, "model.requested", "running", model="m1", tool="t1", step="step-1"),
        _event(6, "tool.started", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(7, "model.routed", "running", model="m1", tool="t1", step="step-1"),
        _event(8, "model.completed", "completed", model="m1", tool="t1", step="step-1"),
        _event(9, "tool.outcome.recorded", "running", tool="t1", capability="vision.analyze", step="step-1", payload_ref="crp://outcome/t1"),
    )
    outcome = outcome_to_payload(ToolInvocationOutcome(
        invocation_id="t1", turn_id="turn-x", capability_id="vision.analyze", attempt=1,
        status="completed", effect_certainty="confirmed_none", payload_ref="crp://result/t1",
        receipt_ref=None, evidence_refs=(), error_code=None, retryable=False,
    ))
    assert classify_recovery("turn-x", 1, events, payload_loader=lambda _ref: outcome).disposition == "quarantine"


def test_classifier_quarantines_incomplete_nested_model_even_when_parent_tool_is_open() -> None:
    events = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "tool.requested", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(3, "tool.intent.recorded", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(4, "tool.dispatch.claimed", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(5, "tool.started", "running", tool="t1", capability="vision.analyze", step="step-1"),
        _event(6, "model.requested", "running", model="m1", tool="t1", step="step-1"),
    )
    assert classify_recovery("turn-x", 1, events).disposition == "quarantine"


def test_classifier_rejects_known_but_malformed_terminal_and_tool_order() -> None:
    malformed_terminal = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "tool.started", "running", tool="t1", capability="memory.read"),
        _event(3, "turn.completed", "completed"),
    )
    assert classify_recovery("turn-x", 1, malformed_terminal).disposition == "quarantine"

    outcome_before_start = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "tool.requested", "running", tool="t1", capability="memory.read"),
        _event(3, "tool.intent.recorded", "running", tool="t1", capability="memory.read"),
        _event(4, "tool.outcome.recorded", "running", tool="t1", capability="memory.read", payload_ref="crp://outcome/1"),
        _event(5, "tool.dispatch.claimed", "running", tool="t1", capability="memory.read"),
        _event(6, "tool.started", "running", tool="t1", capability="memory.read"),
    )
    assert classify_recovery("turn-x", 1, outcome_before_start, payload_loader=lambda _ref: {}).disposition == "quarantine"

    duplicate_start = outcome_before_start[:5] + (
        _event(6, "tool.started", "running", tool="t1", capability="memory.read"),
        _event(7, "tool.started", "running", tool="t1", capability="memory.read"),
    )
    assert classify_recovery("turn-x", 1, duplicate_start, payload_loader=lambda _ref: {}).disposition == "quarantine"


def test_classifier_rejects_unknown_tool_outcome_even_when_outcome_event_exists() -> None:
    outcome_ref = "crp://session/turn-x/tool-invocation-outcome/outcome-1"
    outcome = outcome_to_payload(ToolInvocationOutcome(
        invocation_id="t1", turn_id="turn-x", capability_id="document.write",
        attempt=1, status="unknown_effect", effect_certainty="unknown",
        payload_ref=None, receipt_ref=None, evidence_refs=(),
        error_code="ai.tool_outcome_unknown", retryable=False,
    ))
    events = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "tool.requested", "running", tool="t1", capability="document.write"),
        _event(3, "tool.intent.recorded", "running", tool="t1", capability="document.write"),
        _event(4, "tool.dispatch.claimed", "running", tool="t1", capability="document.write"),
        _event(5, "tool.started", "running", tool="t1", capability="document.write"),
        _event(6, "tool.outcome.recorded", "running", tool="t1", capability="document.write", payload_ref=outcome_ref),
    )
    decision = classify_recovery(
        "turn-x", 1, events,
        payload_loader=lambda ref: outcome if ref == outcome_ref else None,
    )
    assert decision.disposition == "quarantine"
    assert decision.reason_code == "ai.recovery_tool_outcome_unsafe"


def test_classifier_allows_verified_confirmed_none_tool_outcome() -> None:
    outcome_ref = "crp://session/turn-x/tool-invocation-outcome/outcome-safe"
    outcome = outcome_to_payload(ToolInvocationOutcome(
        invocation_id="t1", turn_id="turn-x", capability_id="memory.read",
        attempt=1, status="completed", effect_certainty="confirmed_none",
        payload_ref="crp://result/1", receipt_ref=None, evidence_refs=(),
        error_code=None, retryable=False,
    ))
    events = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "tool.requested", "running", tool="t1", capability="memory.read"),
        _event(3, "tool.intent.recorded", "running", tool="t1", capability="memory.read"),
        _event(4, "tool.dispatch.claimed", "running", tool="t1", capability="memory.read"),
        _event(5, "tool.started", "running", tool="t1", capability="memory.read"),
        _event(6, "tool.outcome.recorded", "running", tool="t1", capability="memory.read", payload_ref=outcome_ref),
    )
    decision = classify_recovery("turn-x", 1, events, payload_loader=lambda _ref: outcome)
    assert decision.disposition == "safe_resume"


def test_classifier_allows_confirmed_none_outcome_before_provider_started() -> None:
    outcome_ref = "crp://session/turn-x/tool-invocation-outcome/cancelled"
    outcome = outcome_to_payload(ToolInvocationOutcome(
        invocation_id="t1", turn_id="turn-x", capability_id="memory.read",
        attempt=1, status="cancelled", effect_certainty="confirmed_none",
        payload_ref=None, receipt_ref=None, evidence_refs=(),
        error_code="ai.tool_cancelled", retryable=False,
    ))
    events = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "tool.requested", "running", tool="t1", capability="memory.read"),
        _event(3, "tool.intent.recorded", "running", tool="t1", capability="memory.read"),
        _event(4, "tool.outcome.recorded", "running", tool="t1", capability="memory.read", payload_ref=outcome_ref),
    )
    assert classify_recovery("turn-x", 1, events, payload_loader=lambda _ref: outcome).disposition == "safe_resume"


@pytest.mark.parametrize("resolution", ["approve", "reject"])
def test_classifier_allows_well_formed_approval_resolution(resolution: str) -> None:
    required = _event(
        3, "approval.required", "waiting_approval", tool="t1",
        capability="document.write", payload_ref="crp://approval/decision",
    )
    resolved = _event(
        4, "approval.resolved", "running", capability="document.write",
        payload_ref="crp://approval/action",
    )
    resolved["data"]["summary"] = resolution  # type: ignore[index]
    events = (
        _event(1, "turn.accepted", "accepted"),
        _event(2, "tool.requested", "running", tool="t1", capability="document.write"),
        required,
        resolved,
    )
    assert classify_recovery("turn-x", 1, events).disposition == "safe_resume"


def test_due_claim_replays_after_crash_before_audit_and_record_is_idempotent() -> None:
    store = InMemoryTurnStateStore()
    store.claim_turn({"turn_id": "turn-x", "idempotency_key": "key-x"})
    token = store.try_acquire_run_lease("turn-x", "owner", now=NOW, stale_after=NOW)
    assert token is not None
    first = store.claim_due_run_leases(now=NOW, limit=1)
    assert len(first) == 1
    assert store.claim_due_run_leases(now=NOW, limit=1) == first
    decision = classify_recovery("turn-x", token.generation, (_event(1, "turn.accepted", "accepted"),))
    assert store.record_recovery_decision(decision, observed_at=NOW)
    assert not store.record_recovery_decision(decision, observed_at=NOW)


def test_in_memory_due_claim_rotates_repeated_undecided_rows() -> None:
    store = InMemoryTurnStateStore()
    for index in range(3):
        turn_id = f"turn-{index}"
        store.claim_turn({"turn_id": turn_id, "idempotency_key": f"key-{index}"})
        assert store.try_acquire_run_lease(turn_id, f"owner-{index}", now=NOW, stale_after=NOW) is not None
    first = store.claim_due_run_leases(now=NOW, limit=1)
    second = store.claim_due_run_leases(now=NOW, limit=1)
    assert first[0].token.turn_id != second[0].token.turn_id


def test_sqlite_due_claim_is_bounded_and_rotates_failed_rows(tmp_path: Path) -> None:
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    turn_ids = []
    for index in range(5):
        request = _request(index)
        turn_id = str(request["turn_id"])
        turn_ids.append(turn_id)
        store.claim_turn(request)
        token = store.try_acquire_run_lease(turn_id, f"owner-{index}", now=NOW, stale_after=NOW)
        assert token is not None
        store.append(_sqlite_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0, run_lease=token)

    first = store.claim_due_run_leases(now=NOW, limit=2)
    assert len(first) == 2
    statuses = [store.get_run_lease(turn_id).status for turn_id in turn_ids]  # type: ignore[union-attr]
    assert statuses.count("recovery_required") == 2
    assert statuses.count("active") == 3

    second = store.claim_due_run_leases(now=NOW, limit=2)
    assert len(second) == 2
    assert {item.token.turn_id for item in first}.isdisjoint(item.token.turn_id for item in second)


def test_sqlite_record_binds_latest_type_and_preserves_stale_audit(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    store, request, token = _stale_sqlite_turn(database)
    lease = store.claim_due_run_leases(now=NOW, limit=1)[0]
    event = store.events_after(str(request["turn_id"]))[-1]
    forged = RecoveryDecision(
        lease.token.turn_id, lease.token.generation, "safe_resume",
        "ai.recovery_no_effect_started", int(event["sequence"]), str(event["event_id"]),
        "turn.resumed",
    )
    assert not store.record_recovery_decision(forged, observed_at=NOW)
    decision = classify_recovery(lease.token.turn_id, token.generation, tuple(store.events_after(lease.token.turn_id)))
    assert store.record_recovery_decision(decision, observed_at=NOW)

    with sqlite3.connect(database) as connection:
        audit = connection.execute(
            "SELECT old_owner_id,old_acquired_at,old_heartbeat_at,old_stale_after,"
            "classifier_version,scanner_actor FROM ai_turn_recovery_audit"
        ).fetchone()
        queue = connection.execute("SELECT status,attempts FROM ai_turn_recovery_queue").fetchone()
    assert audit == (
        "old-owner", NOW.isoformat(), NOW.isoformat(), NOW.isoformat(),
        "ai-recovery-v1", "startup-scanner",
    )
    assert queue == ("pending", 0)


def test_sqlite_empty_event_stream_is_quarantined_and_audited(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    store = SQLiteAITurnStore(database)
    request = _request(20)
    store.claim_turn(request)
    token = store.try_acquire_run_lease(str(request["turn_id"]), "old-owner", now=NOW, stale_after=NOW)
    assert token is not None
    assert scan_due_ai_turn_recovery(store, limit=1, clock=lambda: NOW) == 1
    assert store.get_run_lease(str(request["turn_id"])).status == "quarantined"  # type: ignore[union-attr]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT last_sequence,last_event_id,last_event_type FROM ai_turn_recovery_audit").fetchone() == (0, "", "")


def test_recovery_candidate_probe_skips_absent_database_without_creating_parent(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "ai-turns.sqlite3"

    assert not SQLiteAITurnStore.has_recovery_or_expert_wait_candidate(database, now=NOW)
    assert not database.parent.exists()


def test_recovery_candidate_probe_reads_existing_schema_without_reinitializing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "turns.sqlite3"
    SQLiteAITurnStore(database)

    monkeypatch.setattr(
        "core.ai_kernel.sqlite_store._initialize_schema",
        lambda _connection: (_ for _ in ()).throw(AssertionError("probe must not initialize schema")),
    )

    assert not SQLiteAITurnStore.has_recovery_or_expert_wait_candidate(database, now=NOW)


def test_recovery_candidate_probe_keeps_legacy_schema_eligible_for_constructor_migration(tmp_path: Path) -> None:
    database = tmp_path / "legacy-turns.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE ai_turn_schema(version INTEGER NOT NULL)")
        connection.execute("INSERT INTO ai_turn_schema(version) VALUES(1)")
        connection.commit()

    assert SQLiteAITurnStore.has_recovery_or_expert_wait_candidate(database, now=NOW)
    migrated = SQLiteAITurnStore(database)
    assert not migrated.has_pending_safe_recovery()


def test_recovery_candidate_probe_detects_due_lease_and_expert_wait(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    _stale_sqlite_turn(database)
    assert SQLiteAITurnStore.has_recovery_or_expert_wait_candidate(database, now=NOW)

    empty = tmp_path / "expert-wait.sqlite3"
    SQLiteAITurnStore(empty)
    with sqlite3.connect(empty) as connection:
        connection.execute(
            "INSERT INTO ai_expert_job_waits(turn_id,job_ref,admission_job_revision,snapshot_ref,status,terminal_job_revision) "
            "VALUES(?,?,?,?,?,?)",
            ("probe-turn", "job://probe", 1, "crp://snapshot/probe", "waiting", None),
        )
        connection.commit()
    assert SQLiteAITurnStore.has_recovery_or_expert_wait_candidate(empty, now=NOW)


@pytest.mark.parametrize("append_first", [True, False])
def test_sqlite_append_and_recovery_claim_are_atomically_fenced(tmp_path: Path, append_first: bool) -> None:
    store, request, token = _stale_sqlite_turn(tmp_path / "turns.sqlite3")
    followup = _sqlite_event(request, 2, "context.resolved", "running")
    if append_first:
        store.append(followup, expected_sequence=1, run_lease=token)
        claimed = store.claim_due_run_leases(now=NOW, limit=1)
        assert len(claimed) == 1
        assert len(store.events_after(token.turn_id)) == 2
    else:
        assert len(store.claim_due_run_leases(now=NOW, limit=1)) == 1
        with pytest.raises(RunLeaseRevoked):
            store.append(followup, expected_sequence=1, run_lease=token)
        assert len(store.events_after(token.turn_id)) == 1


def test_dual_scanner_records_one_audit_and_one_queue_item(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    _stale_sqlite_turn(database)
    first = SQLiteAITurnStore(database)
    second = SQLiteAITurnStore(database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda store: scan_due_ai_turn_recovery(store, limit=1, clock=lambda: NOW), (first, second)))
    assert sum(results) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_turn_recovery_audit").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ai_turn_recovery_queue").fetchone()[0] == 1


def test_safe_recovery_queue_atomically_fences_old_owner_and_runs_once(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    store, request, old = _stale_sqlite_turn(database)
    assert scan_due_ai_turn_recovery(store, limit=1, clock=lambda: NOW) == 1
    assert store.has_pending_safe_recovery()
    assert SQLiteAITurnStore.has_recovery_or_expert_wait_candidate(database, now=NOW)

    class Runtime:
        def __init__(self) -> None: self.calls = []
        def recover_accepted_turn(self, turn_id, token): self.calls.append((turn_id, token)); return SimpleNamespace(status="completed")
        def release_strict_run_lease(self, token): SQLiteAITurnStore(database).release_strict_run_lease(token)
    runtime = Runtime()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda value: run_safe_ai_turn_recovery(value, runtime, limit=1, clock=lambda: NOW), (SQLiteAITurnStore(database), SQLiteAITurnStore(database))))
    assert sum(results) == 1
    assert len(runtime.calls) == 1
    with pytest.raises(RunLeaseRevoked):
        SQLiteAITurnStore(database).append(_sqlite_event(request, 2, "context.resolved", "running"), expected_sequence=1, run_lease=old)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status,attempts FROM ai_turn_recovery_queue").fetchone() == ("completed", 1)
        assert [row[0] for row in connection.execute("SELECT status FROM ai_turn_recovery_queue_audit ORDER BY audit_id")] == ["claimed", "completed"]
    assert SQLiteAITurnStore(database).get_run_lease(str(request["turn_id"])) is None
    assert not SQLiteAITurnStore.has_recovery_or_expert_wait_candidate(database, now=NOW)


def test_recovery_worker_renews_slow_execution_lease(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    store, _request_value, _old = _stale_sqlite_turn(database)
    assert scan_due_ai_turn_recovery(store, limit=1, clock=lambda: NOW) == 1

    class Runtime:
        renewals = 0
        def recover_accepted_turn(self, _turn_id, _token): sleep(0.12); return SimpleNamespace(status="completed")
        def renew_run_lease(self, token, *, now, stale_after):
            self.renewals += 1
            return SQLiteAITurnStore(database).renew_run_lease(token, now=now, stale_after=stale_after)
        def release_strict_run_lease(self, token): SQLiteAITurnStore(database).release_strict_run_lease(token)

    runtime = Runtime()
    assert run_safe_ai_turn_recovery(
        store, runtime, limit=1, lease_ttl=timedelta(milliseconds=60),
    ) == 1
    assert runtime.renewals >= 1


def test_recovery_queue_completion_rejects_forged_token(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    store, _request_value, _old = _stale_sqlite_turn(database)
    assert scan_due_ai_turn_recovery(store, limit=1, clock=lambda: NOW) == 1
    item = store.claim_safe_recovery_queue(now=NOW, stale_after=NOW + timedelta(seconds=30), limit=1)[0]
    forged = replace(item, run_lease=RunLeaseToken(item.turn_id, "recovery-forged", item.run_lease.generation))
    assert not store.complete_recovery_queue(forged, observed_at=NOW)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status FROM ai_turn_recovery_queue").fetchone()[0] == "running"
    assert store.quarantine_recovery_queue(item, reason_code="ai.recovery_execution_unknown", observed_at=NOW)


def test_quarantine_has_no_queue_token_or_provider_call(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    store, request, old_token = _stale_sqlite_turn(database)
    store.append(_sqlite_event(request, 2, "model.requested", "running"), expected_sequence=1, run_lease=old_token)
    assert scan_due_ai_turn_recovery(store, limit=1, clock=lambda: NOW) == 1
    class Runtime:
        calls = 0
        def recover_accepted_turn(self, *_args): self.calls += 1
    runtime = Runtime()
    assert run_safe_ai_turn_recovery(store, runtime, clock=lambda: NOW) == 0
    assert runtime.calls == 0
    assert store.get_run_lease(str(request["turn_id"])).status == "quarantined"  # type: ignore[union-attr]


def test_application_owned_recovery_worker_cancels_active_turn_on_shutdown() -> None:
    token = RunLeaseToken("turn-x", "recovery-owner", 2)
    item = RecoveryQueueItem("turn-x", 1, "ai.recovery_no_effect_started", 1, token)
    started = Event()
    cancelled = Event()

    class Store:
        claimed = False
        def claim_safe_recovery_queue(self, **_kwargs):
            if self.claimed: return ()
            self.claimed = True
            return (item,)
        def complete_recovery_queue(self, *_args, **_kwargs): return True

    class Runtime:
        cancel_calls = 0
        def recover_accepted_turn(self, _turn_id, _token):
            started.set(); cancelled.wait(1); return SimpleNamespace(status="waiting_approval")
        def request_background_cancel(self, _turn_id, _token):
            self.cancel_calls += 1; cancelled.set(); return True
        def release_strict_run_lease(self, _token): pass

    runtime = Runtime()
    worker = AIRecoveryWorker(Store(), runtime, limit=1)
    worker.start()
    assert started.wait(1)
    assert worker.shutdown(timeout_seconds=1)
    assert runtime.cancel_calls == 1


def test_recovery_worker_shutdown_between_stop_check_and_claim_never_calls_provider() -> None:
    token = RunLeaseToken("turn-x", "recovery-owner", 2)
    item = RecoveryQueueItem("turn-x", 1, "ai.recovery_no_effect_started", 1, token)
    claim_entered = Event()
    permit_claim = Event()

    class Store:
        claimed = False
        def claim_safe_recovery_queue(self, **_kwargs):
            claim_entered.set()
            permit_claim.wait(1)
            if self.claimed: return ()
            self.claimed = True
            return (item,)

    class Runtime:
        provider_calls = 0
        cancel_calls = 0
        def recover_accepted_turn(self, _turn_id, _token):
            self.provider_calls += 1
            return SimpleNamespace(status="completed")
        def request_background_cancel(self, _turn_id, _token):
            self.cancel_calls += 1
            return True

    runtime = Runtime()
    worker = AIRecoveryWorker(Store(), runtime, limit=1)
    worker.start()
    assert claim_entered.wait(1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        shutdown = pool.submit(worker.shutdown, timeout_seconds=1)
        sleep(0.02)
        permit_claim.set()
        assert shutdown.result(timeout=1)
    assert runtime.provider_calls == 0
    assert runtime.cancel_calls == 1


def test_application_recovery_worker_wakes_after_becoming_idle() -> None:
    token = RunLeaseToken("turn-x", "recovery-owner", 2)
    item = RecoveryQueueItem("turn-x", 1, "ai.recovery_no_effect_started", 1, token)
    first_scan = Event()
    ready = Event()
    completed = Event()

    class Store:
        claimed = False
        def claim_safe_recovery_queue(self, **_kwargs):
            first_scan.set()
            if not ready.is_set() or self.claimed: return ()
            self.claimed = True
            return (item,)
        def complete_recovery_queue(self, *_args, **_kwargs): return True

    class Runtime:
        def recover_accepted_turn(self, _turn_id, _token):
            completed.set()
            return SimpleNamespace(status="completed")
        def release_strict_run_lease(self, _token): pass

    worker = AIRecoveryWorker(Store(), Runtime(), limit=1)
    worker.start()
    assert first_scan.wait(1)
    ready.set()
    worker.wake()
    assert completed.wait(1)
    assert worker.shutdown(timeout_seconds=1)


def test_scanner_never_invokes_runtime_or_provider_and_isolates_store_failure() -> None:
    class Store:
        def claim_due_run_leases(self, **_kwargs):
            raise RuntimeError("startup recovery unavailable")

        def events_after(self, *_args):
            raise AssertionError("must not read absent claim")

        def get(self, *_args):
            raise AssertionError("must not load absent payload")

        def record_recovery_decision(self, *_args, **_kwargs):
            raise AssertionError("must not record")

    assert scan_due_ai_turn_recovery(Store(), clock=lambda: NOW) == 0


def test_recovery_scanner_exception_does_not_block_application_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.api.app.scan_due_ai_turn_recovery",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("recovery unavailable")),
    )
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        assert client.get("/api/health").status_code == 200


def test_recovery_decision_validation_rejects_invalid_disposition() -> None:
    store = InMemoryTurnStateStore()
    decision = RecoveryDecision("turn-x", 1, "unsafe", "ai.recovery_invalid", 1, "event-1", "turn.accepted")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="disposition"):
        store.record_recovery_decision(decision, observed_at=NOW)

    mismatched = RecoveryDecision(
        "turn-x", 1, "terminal_noop", "ai.recovery_terminal",
        1, "event-1", "turn.accepted",
    )
    with pytest.raises(ValueError, match="does not match"):
        store.record_recovery_decision(mismatched, observed_at=NOW)


def _stale_sqlite_turn(database: Path):
    store = SQLiteAITurnStore(database)
    request = _request(10)
    store.claim_turn(request)
    token = store.try_acquire_run_lease(str(request["turn_id"]), "old-owner", now=NOW, stale_after=NOW)
    assert token is not None
    store.append(_sqlite_event(request, 1, "turn.accepted", "accepted"), expected_sequence=0, run_lease=token)
    return store, request, token


def _request(index: int) -> dict[str, object]:
    request = json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))
    suffix = f"{index:032x}"[-32:]
    request["turn_id"] = f"turn-{suffix}"
    request["session_id"] = f"session-{suffix}"
    request["operation_id"] = f"operation-{suffix}"
    request["idempotency_key"] = f"turn-key-{suffix}"
    return request


def _sqlite_event(request: dict[str, object], sequence: int, kind: str, status: str) -> dict[str, object]:
    event = _event(sequence, kind, status)
    event.update({
        "schema_version": "1.0.0", "turn_id": request["turn_id"],
        "session_id": request["session_id"], "actor": "kernel",
        "occurred_at": NOW.isoformat(),
    })
    event["correlation"]["operation_id"] = request["operation_id"]  # type: ignore[index]
    event["data"].update({  # type: ignore[union-attr]
        "summary": "state", "receipt_ref": None, "evidence_refs": [],
        "error_code": None, "retryable": False,
    })
    return event


def _event(
    sequence: int,
    kind: str,
    status: str,
    *,
    model: str | None = None,
    tool: str | None = None,
    step: str | None = None,
    capability: str | None = None,
    payload_ref: str | None = None,
    receipt_ref: str | None = None,
    turn_id: str = "turn-x",
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "event_id": f"event-{sequence}-{uuid4().hex}",
        "turn_id": turn_id,
        "session_id": "session-x",
        "type": kind,
        "data": {
            "status": status,
            "capability_id": capability,
            "payload_ref": payload_ref,
            "receipt_ref": receipt_ref,
        },
        "correlation": {"model_request_id": model, "tool_call_id": tool, "step_id": step, "operation_id": None},
    }


def _wire_dispatch() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "attempt_id": "model-wire-attempt-01234567",
        "turn_id": "turn-x",
        "model_request_id": "m1",
        "attempt_number": 1,
        "routing_snapshot_revision": "a" * 64,
        "provider_id": "deepseek",
        "model_id": "deepseek-chat",
        "dispatched_at": NOW.isoformat(),
        "input_stored": False,
        "output_stored": False,
    }


def _wire_receipt(dispatch: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "attempt_id": dispatch["attempt_id"],
        "turn_id": dispatch["turn_id"],
        "model_request_id": dispatch["model_request_id"],
        "attempt_number": dispatch["attempt_number"],
        "routing_snapshot_revision": dispatch["routing_snapshot_revision"],
        "provider_id": dispatch["provider_id"],
        "model_id": dispatch["model_id"],
        "status": "succeeded",
        "started_at": dispatch["dispatched_at"],
        "completed_at": (NOW + timedelta(seconds=1)).isoformat(),
        "duration_ms": 1000,
        "usage_status": "unavailable",
        "usage": None,
        "cache_status": "unavailable",
        "cache_metadata": None,
        "input_stored": False,
        "output_stored": False,
        "error_code": None,
    }
