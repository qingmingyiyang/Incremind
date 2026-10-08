from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from uuid import uuid4

import pytest

from core.ai_kernel import (
    AIKernelRuntimeError,
    RunLeaseRevoked,
    ScopedCapabilityRegistry,
    SQLiteAITurnStore,
    SynchronousAIRuntime,
    TurnEventConflict,
)


ROOT = Path(__file__).resolve().parents[2]


def _request():
    return json.loads((ROOT / "core-contracts/ai/fixtures/turn-request/valid-project-answer.json").read_text(encoding="utf-8"))


def _route(control, store):
    route_ref = store.put(control.turn_id, "test-model-route", {"provider": "openai"})
    control.model_call_routed(
        snapshot_ref=route_ref, snapshot_revision="a" * 64,
        prompt_cache_scope_identity="b" * 64, provider="openai",
        model="gpt-5.4-mini", execution_location="remote",
    )
    control.model_call_started(provider="openai", model="gpt-5.4-mini")
    return control.begin_model_wire_attempt()


class _CompletePlanner:
    def plan(self, *_args, **_kwargs):
        return {"type": "complete", "summary": "done", "evidence_refs": []}


class _Owner:
    def __init__(self, tmp_path):
        self.database = tmp_path / "turns.sqlite3"
        self.store = SQLiteAITurnStore(self.database)
        self.runtime = SynchronousAIRuntime(
            planner=_CompletePlanner(), registry=ScopedCapabilityRegistry(),
            events=self.store, payloads=self.store, state=self.store,
        )
        self.request = _request()
        self.runtime.accept_turn(self.request)
        now = datetime.now(timezone.utc)
        self.lease = self.store.try_acquire_run_lease(
            self.request["turn_id"], "checkpoint-owner", now=now,
            stale_after=now + timedelta(seconds=120),
        )
        assert self.lease is not None
        self.context_token = self.runtime._run_lease_context.set(self.lease)
        self.control = self.runtime._begin_planner_control(
            self.request["turn_id"], step_id="step-" + uuid4().hex,
            model_request_id="model-request-" + uuid4().hex,
        )
        self.handle = _route(self.control, self.store)

    def commit(self, cursor=None, **overrides):
        arguments = {
            "dispatch_payload": self.handle.dispatch,
            "dispatch_payload_ref": self.handle.dispatch_ref,
            "cursor": {"response_id": "resp_checkpoint", "sequence_number": 0} if cursor is None else cursor,
            "expected_previous_ref": None,
            "run_lease": self.lease,
        }
        arguments.update(overrides)
        return self.store.commit_model_provider_checkpoint(**arguments)

    def facts(self):
        with sqlite3.connect(self.database) as connection:
            return tuple(connection.execute(query).fetchall() for query in (
                "SELECT * FROM ai_model_attempt_reservations ORDER BY attempt_id",
                "SELECT * FROM effect ORDER BY operation_id",
                "SELECT * FROM ai_turn_events ORDER BY turn_id,sequence",
                "SELECT * FROM ai_turn_payloads ORDER BY payload_ref",
                "SELECT * FROM ai_turn_immutable_payloads ORDER BY payload_ref",
            ))

    def run(self, action):
        def handler():
            result = action()
            self.handle.succeeded(usage={"input_tokens": 3, "output_tokens": 2}, cache_observation=None)
            return result
        return self.handle.invoke_wire(handler)


@pytest.fixture
def owner(tmp_path):
    result = _Owner(tmp_path)
    try:
        yield result
    finally:
        result.runtime._run_lease_context.reset(result.context_token)


def test_runtime_handle_persists_body_free_cursor_only_in_real_active_effect(tmp_path):
    database = tmp_path / "public-runtime.sqlite3"
    store = SQLiteAITurnStore(database)
    observations = {}

    class Planner:
        def plan(self, request, _events, _capabilities, _payloads, execution_control=None):
            handle = _route(execution_control, store)
            observations["handle"] = handle

            def provider():
                before_events = tuple(store.events_after(request["turn_id"]))
                with sqlite3.connect(database) as connection:
                    before_reservation = connection.execute("SELECT * FROM ai_model_attempt_reservations").fetchall()
                    before_effect = connection.execute("SELECT * FROM effect").fetchall()
                refs = []
                for number in (0, 1):
                    refs.append(handle.observe_provider_checkpoint({"response_id": "resp_checkpoint", "sequence_number": number}))
                assert tuple(store.events_after(request["turn_id"])) == before_events
                with sqlite3.connect(database) as connection:
                    assert connection.execute("SELECT * FROM ai_model_attempt_reservations").fetchall() == before_reservation
                    assert connection.execute("SELECT * FROM effect").fetchall() == before_effect
                payloads = [store.get(ref) for ref in refs]
                observations.update(refs=refs, payloads=payloads)
                handle.succeeded(usage={"input_tokens": 3, "output_tokens": 2}, cache_observation=None)
                return "provider-value"

            assert handle.invoke_wire(provider) == "provider-value"
            execution_control.model_call_completed(usage={"input_tokens": 3, "output_tokens": 2})
            return {"type": "complete", "summary": "done", "evidence_refs": []}

    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(), events=store, payloads=store, state=store)
    request = _request()
    runtime.accept_turn(request)
    now = datetime.now(timezone.utc)
    lease = store.try_acquire_run_lease(request["turn_id"], "public-owner", now=now, stale_after=now + timedelta(seconds=120))
    assert lease is not None
    receipt = runtime.run_accepted_turn(request["turn_id"], lease)
    assert receipt.status == "completed"
    first, second = observations["payloads"]
    for number, payload in enumerate((first, second)):
        assert set(payload) == {"schema_version", "dispatch", "dispatch_ref", "cursor", "previous_ref", "run_lease", "effect_lease"}
        assert payload["dispatch"] == dict(observations["handle"].dispatch)
        assert payload["dispatch_ref"] == observations["handle"].dispatch_ref
        assert payload["cursor"] == {"response_id": "resp_checkpoint", "sequence_number": number}
        assert payload["run_lease"] == {"owner_id": lease.owner_id, "generation": lease.generation}
        assert set(payload["effect_lease"]) == {"owner_id", "attempt"}
        assert payload["effect_lease"]["attempt"] == 1
    assert first["previous_ref"] is None
    assert second["previous_ref"] == observations["refs"][0]
    events = tuple(runtime.events_after(request["turn_id"]))
    assert sum(event["type"] == "model.attempt.dispatched" for event in events) == 1
    assert sum(event["type"] == "model.attempt.terminal" for event in events) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT state FROM effect WHERE kind='model_call'").fetchall() == [("SETTLED_OK",)]
    with pytest.raises(AIKernelRuntimeError):
        observations["handle"].observe_provider_checkpoint({"response_id": "resp_checkpoint", "sequence_number": 2})


def test_checkpoint_requires_real_handler_context_even_with_live_run_token(owner):
    before = owner.facts()
    with pytest.raises(RunLeaseRevoked):
        owner.commit()
    with pytest.raises(RunLeaseRevoked):
        owner.handle.observe_provider_checkpoint({"response_id": "resp_checkpoint", "sequence_number": 0})
    assert owner.facts() == before


@pytest.mark.parametrize("cursor", [
    {}, {"response_id": "resp_checkpoint"},
    {"response_id": "foreign", "sequence_number": 0},
    {"response_id": "resp_checkpoint\n", "sequence_number": 0},
    {"response_id": True, "sequence_number": 0},
    {"response_id": "resp_checkpoint", "sequence_number": True},
    {"response_id": "resp_checkpoint", "sequence_number": -1},
    {"response_id": "resp_checkpoint", "sequence_number": 1.0},
    {"response_id": "resp_checkpoint", "sequence_number": 9_007_199_254_740_992},
    {"response_id": "resp_checkpoint", "sequence_number": 0, "body": "not allowed"},
])
def test_checkpoint_rejects_non_body_free_or_invalid_cursor_without_writes(owner, cursor):
    def provider():
        before = owner.facts()
        with pytest.raises(ValueError):
            owner.commit(cursor)
        assert owner.facts() == before
    owner.run(provider)


@pytest.mark.parametrize("field,value", [
    ("turn_id", "turn-" + "f" * 32),
    ("attempt_id", "model-wire-attempt-" + "f" * 32),
    ("attempt_number", 2),
    ("model_request_id", "model-request-" + "f" * 32),
    ("routing_snapshot_revision", "f" * 64),
    ("provider_id", "deepseek"), ("model_id", "other-model"),
    ("execution_location", "local_loopback"),
    ("dispatched_at", "2026-01-01T00:00:00+00:00"),
])
def test_checkpoint_rejects_other_dispatch_or_route_without_writes(owner, field, value):
    def provider():
        dispatch = dict(owner.handle.dispatch, **{field: value})
        before = owner.facts()
        with pytest.raises((TurnEventConflict, RunLeaseRevoked)):
            owner.commit(dispatch_payload=dispatch)
        assert owner.facts() == before
    owner.run(provider)


def test_checkpoint_rejects_other_dispatch_ref_without_writes(owner):
    def provider():
        before = owner.facts()
        with pytest.raises(TurnEventConflict):
            owner.commit(dispatch_payload_ref="crp://session/foreign/dispatch")
        assert owner.facts() == before
    owner.run(provider)


@pytest.mark.parametrize("case", ["missing-token", "expired-run", "takeover", "expired-effect", "wrong-effect-owner", "wrong-effect-attempt"])
def test_checkpoint_requires_current_run_and_effect_leases(owner, case):
    class ProviderStopped(Exception):
        pass

    def provider():
        if case == "expired-run":
            with sqlite3.connect(owner.database) as connection:
                connection.execute("UPDATE ai_turn_run_leases SET stale_after=? WHERE turn_id=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), owner.lease.turn_id))
        elif case == "takeover":
            now = datetime.now(timezone.utc) + timedelta(seconds=130)
            assert owner.store.mark_run_lease_stale(owner.lease, now=now) is not None
            replacement = owner.store.takeover_run_lease(owner.lease.turn_id, expected_generation=owner.lease.generation, owner_id="new-owner", now=now, stale_after=now + timedelta(seconds=120), disposition="safe")
            assert replacement is not None
        elif case == "expired-effect":
            with sqlite3.connect(owner.database) as connection:
                connection.execute("UPDATE effect SET lease_expires_at=? WHERE operation_id=?", (datetime.now(timezone.utc).timestamp() - 1, owner.handle.dispatch["attempt_id"]))
        elif case == "wrong-effect-owner":
            with sqlite3.connect(owner.database) as connection:
                connection.execute("UPDATE effect SET lease_owner=? WHERE operation_id=?", ("other-owner", owner.handle.dispatch["attempt_id"]))
        elif case == "wrong-effect-attempt":
            with sqlite3.connect(owner.database) as connection:
                connection.execute("UPDATE effect SET attempt=attempt+1 WHERE operation_id=?", (owner.handle.dispatch["attempt_id"],))
        before = owner.facts()
        with pytest.raises(RunLeaseRevoked):
            owner.commit(**({"run_lease": None} if case == "missing-token" else {}))
        assert owner.facts() == before
        raise ProviderStopped()

    with pytest.raises(ProviderStopped):
        owner.handle.invoke_wire(provider)
    with sqlite3.connect(owner.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_turn_immutable_payloads").fetchone()[0] == 0


@pytest.mark.parametrize("cursor,previous", [
    ({"response_id": "resp_other", "sequence_number": 1}, "latest"),
    ({"response_id": "resp_checkpoint", "sequence_number": 0}, "latest"),
    ({"response_id": "resp_checkpoint", "sequence_number": 1}, None),
    ({"response_id": "resp_checkpoint", "sequence_number": 1}, "foreign"),
])
def test_checkpoint_cas_and_response_binding_reject_stale_or_changed_cursor(owner, cursor, previous):
    def provider():
        first = owner.handle.observe_provider_checkpoint({"response_id": "resp_checkpoint", "sequence_number": 0})
        expected = first if previous == "latest" else ("crp://session/foreign/checkpoint" if previous == "foreign" else None)
        before = owner.facts()
        with pytest.raises(TurnEventConflict):
            owner.commit(cursor, expected_previous_ref=expected)
        assert owner.facts() == before
        second = owner.handle.observe_provider_checkpoint({"response_id": "resp_checkpoint", "sequence_number": 1})
        assert owner.store.get(second)["previous_ref"] == first
    owner.run(provider)


def test_sqlite_rejecting_checkpoint_insert_leaves_authoritative_facts_unchanged(owner):
    def provider():
        with sqlite3.connect(owner.database) as connection:
            connection.execute("CREATE TRIGGER reject_checkpoint BEFORE INSERT ON ai_turn_immutable_payloads BEGIN SELECT RAISE(ABORT, 'checkpoint unavailable'); END")
        before = owner.facts()
        with pytest.raises(sqlite3.IntegrityError, match="checkpoint unavailable"):
            owner.handle.observe_provider_checkpoint({"response_id": "resp_checkpoint", "sequence_number": 0})
        assert owner.facts() == before
        with sqlite3.connect(owner.database) as connection:
            connection.execute("DROP TRIGGER reject_checkpoint")
        ref = owner.handle.observe_provider_checkpoint({"response_id": "resp_checkpoint", "sequence_number": 0})
        assert owner.store.get(ref)["previous_ref"] is None
    owner.run(provider)


def test_checkpoint_after_terminal_rejected_and_failed_effect_remains_unknown(owner):
    def provider():
        ref = owner.handle.observe_provider_checkpoint({"response_id": "resp_checkpoint", "sequence_number": 0})
        owner.handle.failed_transport(error_code="ai.model_stalled")
        before = owner.facts()
        with pytest.raises(TurnEventConflict):
            owner.commit({"response_id": "resp_checkpoint", "sequence_number": 1}, expected_previous_ref=ref)
        with pytest.raises(AIKernelRuntimeError):
            owner.handle.observe_provider_checkpoint({"response_id": "resp_checkpoint", "sequence_number": 1})
        assert owner.facts() == before
        raise ConnectionError("provider closed after interruption")
    with pytest.raises(ConnectionError, match="provider closed after interruption"):
        owner.handle.invoke_wire(provider)
    with sqlite3.connect(owner.database) as connection:
        assert connection.execute("SELECT state FROM effect").fetchall() == [("UNKNOWN",)]
        assert connection.execute("SELECT status,terminal_status FROM ai_model_attempt_reservations").fetchall() == [("terminal", "failed_transport")]
        assert connection.execute("SELECT COUNT(*) FROM ai_turn_immutable_payloads").fetchone()[0] == 1


def test_checkpoint_checks_lease_time_after_waiting_for_actual_sqlite_write_lock(owner):
    active = Event()
    locked = Event()
    checkpoint_entered = Event()
    observations = {}

    class ProviderStopped(Exception):
        pass

    def provider():
        active.set()
        assert locked.wait(5)
        checkpoint_entered.set()
        try:
            observations["ref"] = owner.commit()
        except RunLeaseRevoked:
            observations["revoked"] = True
        raise ProviderStopped()

    def run():
        try:
            owner.handle.invoke_wire(provider)
        except ProviderStopped:
            observations["stopped"] = True
        except BaseException as error:
            observations["error"] = error

    before_events = tuple(owner.store.events_after(owner.lease.turn_id))
    thread = Thread(target=run)
    thread.start()
    try:
        assert active.wait(5)
        with sqlite3.connect(owner.database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=0.3)
            connection.execute("UPDATE ai_turn_run_leases SET stale_after=? WHERE turn_id=?", (expires_at.isoformat(), owner.lease.turn_id))
            locked.set()
            assert checkpoint_entered.wait(5)
            deadline = monotonic() + 2
            while datetime.now(timezone.utc) <= expires_at:
                assert monotonic() < deadline
                sleep(0.01)
            connection.commit()
    finally:
        locked.set()
        thread.join(5)
    assert not thread.is_alive()
    assert "error" not in observations, observations.get("error")
    assert observations == {"revoked": True, "stopped": True}
    assert tuple(owner.store.events_after(owner.lease.turn_id)) == before_events
    with sqlite3.connect(owner.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_turn_immutable_payloads").fetchone()[0] == 0
