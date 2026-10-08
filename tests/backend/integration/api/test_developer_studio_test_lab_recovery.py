from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.api import developer_studio_test_lab_ai_runtime as test_lab
from backend.api.ai_turn_recovery_startup import scan_due_ai_turn_recovery
from backend.api.ai_turn_runner import AITurnRunner
from core.ai_kernel import ScopedCapabilityRegistry, SQLiteAITurnStore, SynchronousAIRuntime


class _InterruptedGateway:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        request.metadata_sink.model_call_routed(
            snapshot_ref="crp://session/turn-test-lab-recovery-0001/turn-model-routing-snapshot-v1/frozen",
            snapshot_revision="a" * 64,
            prompt_cache_scope_identity="b" * 64,
            provider="provider-test",
            model="model-test",
            execution_location="remote",
        )
        request.metadata_sink.model_call_started(
            provider="provider-test", model="model-test",
        )
        request.wire_attempt_sink.begin_model_wire_attempt()
        raise RuntimeError("connection ended after request dispatch")


class _CrashAfterDispatchGateway(_InterruptedGateway):
    """Emulates process-style abort after the durable wire-attempt marker."""

    def invoke(self, request):
        self.calls += 1
        request.metadata_sink.model_call_routed(
            snapshot_ref="crp://session/turn-test-lab-recovery-0001/turn-model-routing-snapshot-v1/frozen",
            snapshot_revision="a" * 64,
            prompt_cache_scope_identity="b" * 64,
            provider="provider-test",
            model="model-test",
            execution_location="remote",
        )
        request.metadata_sink.model_call_started(provider="provider-test", model="model-test")
        request.wire_attempt_sink.begin_model_wire_attempt()
        raise SystemExit("simulated process crash")


def _snapshot() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "kind": test_lab.DEVELOPER_STUDIO_TEST_LAB_SNAPSHOT_KIND,
        "project_id": "project-a",
        "test_type": "prompt",
        "input": "private recovery input",
        "system_prompt": "private recovery prompt",
        "model_capability": "structured",
        "route": {
            "route_key": "intake.classification",
            "route_revision": 3,
            "provider_id": "provider-test",
            "provider_revision": "provider-revision-2",
            "model_name": "model-test",
            "runtime_revision": 4,
            "evidence_ref": "crp://default/model-routes/intake.classification",
        },
        "prompt": {
            "prompt_id": "pt-input-understanding",
            "source": "active",
            "config_revision": 7,
            "evidence_ref": "crp://default/prompts/pt-input-understanding",
        },
        "recipe": None,
    }


def _turn() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "turn_id": "turn-test-lab-recovery-0001",
        "session_id": "session-test-lab-recovery-0001",
        "operation_id": "operation-test-lab-recovery-0001",
        "idempotency_key": "idempotency-test-lab-recovery-0001",
        "scope": {"kind": "project", "project_id": "project-a", "series_id": None},
        "input": {"kind": "text", "text": json.dumps(_snapshot()), "refs": []},
        "desired_outcome": test_lab.DEVELOPER_STUDIO_TEST_LAB_OUTCOME,
        "privacy": {
            "mode": "remote_allowed", "allow_remote": True, "pii": "possible",
            "consent_refs": ["crp://default/consent/provider-egress-policy"],
            "retention": "local_durable",
        },
        "capability_policy": {
            "allowed": [test_lab.DEVELOPER_STUDIO_TEST_LAB_CAPABILITY],
            "denied": [],
            "require_approval": [test_lab.DEVELOPER_STUDIO_TEST_LAB_CAPABILITY],
        },
        "context_policy": {
            "include_project_skill": False, "include_memory": False,
            "include_session_history": False, "max_context_bytes": 32768,
        },
        "approval_policy": {"mode": "explicit", "auto_approve_read_only": True},
        "created_at": "2026-08-26T00:00:00+00:00",
    }


def _runtime(store: SQLiteAITurnStore, gateway: _InterruptedGateway) -> SynchronousAIRuntime:
    registry = ScopedCapabilityRegistry()
    registry.register(
        test_lab.developer_studio_test_lab_capability_definition(),
        test_lab.DeveloperStudioTestLabCapability(gateway=gateway, receipt_store=store),
    )
    return SynchronousAIRuntime(
        planner=test_lab.DeveloperStudioTestLabTurnPlanner(),
        registry=registry,
        events=store,
        payloads=store,
        state=store,
    )


def test_unknown_effect_persists_across_runtime_restart_without_model_replay(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        test_lab,
        "load_turn_model_routing_binding",
        lambda *_args, **_kwargs: SimpleNamespace(
            snapshot_ref="crp://session/turn-test-lab-recovery-0001/turn-model-routing-snapshot-v1/frozen",
            snapshot_revision="a" * 64,
            snapshot={"selected": {
                "route_key": "intake.classification", "route_revision": 3,
                "provider_id": "provider-test", "provider_revision": "provider-revision-2",
                "model_name": "model-test",
            }},
            parameters=lambda: {"_routing": "frozen"},
        ),
    )
    database = tmp_path / "ai-turns.sqlite3"
    first_gateway = _InterruptedGateway()
    first = _runtime(SQLiteAITurnStore(database), first_gateway)
    waiting = first.submit_turn(_turn())
    approval = tuple(first.events_after(waiting.turn_id))[-1]

    failed = first.apply_action({
        "schema_version": "1.0.0",
        "action_id": "action-test-lab-recovery-0001",
        "turn_id": waiting.turn_id,
        "type": "approve",
        "target_event_id": approval["event_id"],
        "reason": "execute recovery fault injection",
        "actor": "user",
        "expected_sequence": waiting.current_sequence,
        "idempotency_key": "approve-test-lab-recovery-0001",
        "created_at": "2026-08-26T00:00:01+00:00",
    })

    assert failed.status == "failed"
    assert first_gateway.calls == 1
    events = tuple(first.events_after(failed.turn_id))
    assert sum(event["type"] == "model.attempt.dispatched" for event in events) == 1
    assert any(
        event["type"] == "tool.failed"
        and event["data"]["error_code"] == "ai.tool_outcome_unknown"
        for event in events
    )

    restarted_gateway = _InterruptedGateway()
    restarted = _runtime(SQLiteAITurnStore(database), restarted_gateway)
    replay = restarted.submit_turn(_turn())

    assert replay.status == "failed"
    assert replay.replayed is True
    assert restarted_gateway.calls == 0
    restarted_events = tuple(restarted.events_after(replay.turn_id))
    assert sum(event["type"] == "model.attempt.dispatched" for event in restarted_events) == 1


def test_approval_runner_crash_keeps_lease_and_startup_quarantines_incomplete_model_attempt(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        test_lab,
        "load_turn_model_routing_binding",
        lambda *_args, **_kwargs: SimpleNamespace(
            snapshot_ref="crp://session/turn-test-lab-recovery-0001/turn-model-routing-snapshot-v1/frozen",
            snapshot_revision="a" * 64,
            snapshot={"selected": {
                "route_key": "intake.classification", "route_revision": 3,
                "provider_id": "provider-test", "provider_revision": "provider-revision-2",
                "model_name": "model-test",
            }},
            parameters=lambda: {"_routing": "frozen"},
        ),
    )
    database = tmp_path / "ai-turns.sqlite3"
    store = SQLiteAITurnStore(database)
    first_gateway = _CrashAfterDispatchGateway()
    first = _runtime(store, first_gateway)
    waiting = first.submit_turn(_turn())
    approval = tuple(first.events_after(waiting.turn_id))[-1]
    action = {
        "schema_version": "1.0.0", "action_id": "action-test-lab-crash-0001",
        "turn_id": waiting.turn_id, "type": "approve", "target_event_id": approval["event_id"],
        "reason": "execute crash fault injection", "actor": "user",
        "expected_sequence": waiting.current_sequence, "idempotency_key": "approve-test-lab-crash-0001",
        "created_at": "2026-08-26T00:00:01+00:00",
    }
    runner = AITurnRunner(first, max_workers=1, lease_ttl=timedelta(seconds=0.05), heartbeat_interval_seconds=0.01)
    with pytest.raises(SystemExit, match="simulated process crash"):
        runner.apply_action_and_wait(action)
    runner.shutdown(timeout_seconds=0)
    events = tuple(first.events_after(waiting.turn_id))
    assert first_gateway.calls == 1
    assert sum(event["type"] == "model.attempt.dispatched" for event in events) == 1
    assert events[-1]["type"] == "model.attempt.dispatched"

    scanned = scan_due_ai_turn_recovery(
        SQLiteAITurnStore(database),
        clock=lambda: datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert scanned == 1
    review = SQLiteAITurnStore(database).list_recovery_reviews(limit=10)[0]
    assert review.reason_code == "ai.recovery_model_incomplete"

    restarted_gateway = _CrashAfterDispatchGateway()
    restarted = _runtime(SQLiteAITurnStore(database), restarted_gateway)
    replay = restarted.submit_turn(_turn())
    assert replay.status == "running"
    assert replay.replayed is True
    assert restarted_gateway.calls == 0
