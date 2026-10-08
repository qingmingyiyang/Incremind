from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
import math

import pytest

from core.effect_log import (
    EffectClass,
    EffectHandlerAbandoned,
    EffectHandlerDeferred,
    EffectIntent,
    EffectLeaseFence,
    EffectLog,
    EffectPurpose,
    EffectHandlerRegistration,
    EffectHandlerRegistry,
    EffectReaper,
    EffectRecoveryRegistration,
    EffectRuntime,
    EffectRunner,
    EffectState,
    EffectReceipt,
    GateDecision,
    GateDecisionFact,
    EFFECT_V2,
    V2_REVISION_KEYS,
    NOT_APPLICABLE,
    InvalidEffectTransition,
    build_effect_runtime,
)
from core.effect_log.runtime import EffectLeaseCheckpoint


def _v2_intent(**changes) -> EffectIntent:
    revisions = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
    revisions.update(policy="policy-v2", handler="handler-v2", budget="budget-v2")
    values = dict(
        session_id="session-v2", root_id="root-v2", step_key="step-v2", kind="v2-call",
        effect_class=EffectClass.IDEMPOTENT, intent_ref="intent:v2", gate_decision_id="gate:v2",
        rev_set=revisions, payload={"secret_ref": "lease:v2", "input_ref": "facts:input-v2"},
        contract_version=EFFECT_V2, intent_schema_version="intent/v2",
        expected_receipt_kind="v2-call.receipt", expected_receipt_schema_version="receipt/v2",
    )
    values.update(changes)
    if "step_key" in changes and "gate_decision_id" not in changes:
        values["gate_decision_id"] = f"gate:{changes['step_key']}"
    return EffectIntent(**values)


def _v2_gate(decision: GateDecision = GateDecision.ALLOW, **changes) -> GateDecisionFact:
    values = dict(decision=decision, rule_ref="rule:v2", scope_ref="scope:v2", budget_after={},
                  secret_scope="scope:secret-v2", policy_revision="policy-v2")
    values.update(changes)
    return GateDecisionFact(**values)


def test_v2_plan_binds_gate_and_intent_facts_atomically(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    intent = _v2_intent()
    effect, created = log.plan_v2(intent, gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1)
    assert created and effect.contract_version == EFFECT_V2
    with log._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect_gate_fact").fetchone()[0] == 1
        assert connection.execute("SELECT payload_json FROM effect_intent_fact").fetchone()[0] == (
            '{"input_ref":"facts:input-v2","secret_ref":"lease:v2"}'
        )


def test_v2_execution_facts_require_complete_frozen_authority(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    intent = _v2_intent()
    gate = _v2_gate()
    planned, _created = log.plan_v2(
        intent, gate_decision_id="gate:v2", gate_fact=gate, now=1,
    )

    assert log.require_v2_execution_facts(
        intent, gate_decision_id="gate:v2", gate_fact=gate,
    ) == planned


@pytest.mark.parametrize(
    ("statement", "message"),
    (
        ("UPDATE effect SET purpose='aux'", "different frozen intent"),
        (
            "UPDATE effect_intent_fact SET payload_json='{}'",
            "immutable intent fact drifted",
        ),
        (
            "UPDATE effect_gate_fact SET rule_ref='rule:drifted'",
            "immutable Gate fact drifted",
        ),
    ),
)
def test_v2_execution_facts_reject_any_persisted_fact_drift(
    tmp_path, statement: str, message: str,
) -> None:
    log = EffectLog(tmp_path / "effects.db")
    intent = _v2_intent()
    gate = _v2_gate()
    log.plan_v2(intent, gate_decision_id="gate:v2", gate_fact=gate, now=1)
    with log._connect() as connection:
        connection.execute(statement)

    with pytest.raises(RuntimeError, match=message):
        log.require_v2_execution_facts(
            intent, gate_decision_id="gate:v2", gate_fact=gate,
        )


def test_v2_plan_in_connection_commits_with_caller_facts(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    intent = _v2_intent()
    with log._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE caller_fact(ref TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO caller_fact(ref) VALUES('facts:job-plan')")
        effect, created = log.plan_v2_in_connection(
            connection, intent, gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1,
        )
        assert created
        connection.commit()
    with log._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM caller_fact").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM effect_gate_fact").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM effect_intent_fact").fetchone()[0] == 1
        assert log.get_in_connection(connection, effect.operation_id).operation_id == effect.operation_id


def test_v2_plan_in_connection_rolls_back_all_v2_facts_with_caller(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    with log._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        log.plan_v2_in_connection(
            connection, _v2_intent(), gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1,
        )
        connection.rollback()
    with log._connect() as connection:
        for table in ("effect", "effect_gate_fact", "effect_intent_fact"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("decision", [GateDecision.DENY, GateDecision.ASK])
def test_v2_plan_in_connection_rejects_deny_or_ask(tmp_path, decision) -> None:
    log = EffectLog(tmp_path / "effects.db")
    with log._connect() as connection, pytest.raises(InvalidEffectTransition, match="cannot plan"):
        connection.execute("BEGIN IMMEDIATE")
        log.plan_v2_in_connection(
            connection, _v2_intent(), gate_decision_id="gate:v2", gate_fact=_v2_gate(decision), now=1,
        )
        connection.rollback()


def test_v2_plan_in_connection_rejects_gate_and_intent_drift_and_is_idempotent(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    intent = _v2_intent()
    with log._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        first, created = log.plan_v2_in_connection(
            connection, intent, gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1,
        )
        second, repeated = log.plan_v2_in_connection(
            connection, intent, gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=2,
        )
        assert created and not repeated and first.operation_id == second.operation_id
        with pytest.raises(ValueError, match="drifted from intent"):
            log.plan_v2_in_connection(
                connection, intent, gate_decision_id="gate:other", gate_fact=_v2_gate(), now=2,
            )
        with pytest.raises(RuntimeError, match="immutable Gate fact drifted"):
            log.plan_v2_in_connection(
                connection, intent, gate_decision_id="gate:v2",
                gate_fact=_v2_gate(scope_ref="scope:changed"), now=2,
            )
        connection.commit()


@pytest.mark.parametrize("decision", [GateDecision.DENY, GateDecision.ASK])
def test_v2_deny_or_ask_cannot_plan(tmp_path, decision) -> None:
    log = EffectLog(tmp_path / "effects.db")
    with pytest.raises(InvalidEffectTransition, match="cannot plan"):
        log.plan_v2(_v2_intent(), gate_decision_id="gate:v2", gate_fact=_v2_gate(decision), now=1)


def test_v2_mutation_receipt_and_direct_success_contract(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    intent = _v2_intent()
    fact = _v2_gate(GateDecision.MUTATE, mutated_intent_digest=intent.intent_digest)
    effect, _ = log.plan_v2(intent, gate_decision_id="gate:v2", gate_fact=fact, now=1)
    inflight = log.transition(effect.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT,
                              now=2, lease_owner="runner", lease_expires_at=3)
    with pytest.raises(InvalidEffectTransition, match="Receipt binding API"):
        log.transition(inflight.operation_id, expected=EffectState.INFLIGHT, target=EffectState.SETTLED_OK,
                       now=2, result_ref="receipt:bad")
    settled = log.settle_ok_with_receipt(inflight.operation_id, expected=EffectState.INFLIGHT,
                                         receipt_ref="receipt:ok", receipt_kind="v2-call.receipt",
                                         receipt_schema_version="receipt/v2", intent_schema_version="intent/v2", now=2)
    assert settled.state is EffectState.SETTLED_OK


def test_receipt_binding_cannot_settle_a_planned_effect(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    effect, _ = log.plan_v2(_v2_intent(), gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1)
    with pytest.raises(InvalidEffectTransition, match="INFLIGHT or UNKNOWN"):
        log.settle_ok_with_receipt(
            effect.operation_id, expected=EffectState.PLANNED, receipt_ref="receipt:planned",
            receipt_kind="v2-call.receipt", receipt_schema_version="receipt/v2",
            intent_schema_version="intent/v2", now=2,
        )


def test_v2_receipt_ref_rejects_url_and_reaper_uses_existing_receipt_first(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    effect, _ = log.plan_v2(_v2_intent(effect_class=EffectClass.QUERYABLE), gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1)
    log.transition(effect.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT,
                   now=1, lease_owner="dead", lease_expires_at=2)
    with pytest.raises(ValueError, match="internal opaque"):
        log.settle_ok_with_receipt(effect.operation_id, expected=EffectState.INFLIGHT,
                                  receipt_ref="https://host/receipt?token=canary", receipt_kind="v2-call.receipt",
                                  receipt_schema_version="receipt/v2", intent_schema_version="intent/v2", now=2)
    with log._connect() as connection:
        connection.execute("INSERT INTO effect_receipt(operation_id,receipt_ref,receipt_kind,receipt_schema_version,recorded_at) VALUES(?,?,?,?,?)",
                           (effect.operation_id, "receipt:already-bound", "v2-call.receipt", "receipt/v2", 2))
        connection.commit()
    outcome = EffectReaper(log).recover_expired(
        now=3, probes={("v2-call", EFFECT_V2): lambda _: pytest.fail("probe must not run")},
    )[0]
    assert outcome.reason == "receipt_already_bound"
    assert log.get(effect.operation_id).state is EffectState.SETTLED_OK


def test_v2_secret_payload_and_registry_contract_fail_closed(tmp_path) -> None:
    with pytest.raises(ValueError, match="reference-only envelope"):
        _v2_intent(payload={"api_key": "plaintext"})
    runtime = build_effect_runtime(tmp_path / "effects.db", owner_id="runner")
    runtime.handlers.register(EffectHandlerRegistration(
        kind="v2-call", effect_class=EffectClass.IDEMPOTENT, contract_version=EFFECT_V2,
        intent_schema_version="intent/v2", receipt_kind="v2-call.receipt", receipt_schema_version="receipt/v2",
        handler=lambda _: EffectReceipt("receipt:v2", "v2-call.receipt", "receipt/v2", "intent/v2"),
    ))
    intent = _v2_intent()
    effect, _ = runtime.log.plan_v2(intent, gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1)
    assert runtime.dispatch_operation(effect.operation_id, now=2).state is EffectState.SETTLED_OK


@pytest.mark.parametrize("key", [
    "Authorization", "access-key", "client.secret", "PASSWORD", "token",
    "cookie", "private key",
])
def test_v2_secret_semantics_reject_nested_plaintext_and_unsafe_refs(key) -> None:
    with pytest.raises(ValueError):
        _v2_intent(payload={"data": {key: "Bearer actual-secret"}})
    with pytest.raises(ValueError, match="opaque"):
        _v2_intent(payload={"data": {f"{key}_ref": "Bearer actual-secret"}})
    with pytest.raises(ValueError, match="opaque reference"):
        _v2_intent(payload={"data": {"label": "arbitrary plaintext"}})
    assert _v2_intent(payload={"data_ref": "facts:payload-1", "max_tokens": 32}).payload["max_tokens"] == 32
    with pytest.raises(ValueError, match="internal opaque"):
        _v2_intent(payload={"data_ref": "https://host/path?token=secret"})
    with pytest.raises(ValueError, match="internal opaque"):
        _v2_intent(intent_ref="https://host/path?token=secret")


@pytest.mark.parametrize("payload", [
    {"provider_revision": "revision?secret=1"},
    {"provider_id": "https:external"},
    {"max_cost_micros": -1},
    {"duration_milliseconds": math.inf},
    {"elapsed_micros": math.nan},
])
def test_v2_revision_tokens_and_budget_scalars_fail_closed(payload) -> None:
    with pytest.raises(ValueError):
        _v2_intent(payload=payload)
    assert _v2_intent(payload={"max_cost_micros": 0, "duration_milliseconds": 1.5}).payload["max_cost_micros"] == 0


def test_v2_identity_ignores_authority_drift_but_frozen_plan_rejects_it(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    original = _v2_intent()
    log.plan_v2(original, gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1)
    revisions = dict(original.rev_set)
    revisions["boundary"] = "boundary-v2"
    drifted = _v2_intent(rev_set=revisions)
    assert drifted.operation_id == original.operation_id
    with pytest.raises(RuntimeError, match="different frozen intent"):
        log.plan_v2(drifted, gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=2)
    with pytest.raises(ValueError, match="forbids operation_id_override"):
        _v2_intent(operation_id_override="eff2_spoof")
    with pytest.raises(ValueError, match="non-empty strings"):
        _v2_intent(rev_set={**original.rev_set, "budget": 1})
    with pytest.raises(ValueError, match="only mutate"):
        _v2_gate(mutated_intent_digest="not-allowed")
    with pytest.raises(ValueError, match="BLAKE3"):
        _v2_gate(GateDecision.MUTATE, mutated_intent_digest="abc")
    with pytest.raises(ValueError, match="opaque token"):
        _v2_intent(session_id="https:bad")


def test_v2_identity_depends_on_exactly_four_protocol_fields() -> None:
    original = _v2_intent()
    assert _v2_intent(turn_id="other", parent_id="other", idem_key="other").operation_id == original.operation_id
    assert _v2_intent(rev_set={**original.rev_set, "boundary": "boundary-v3"}).operation_id == original.operation_id
    for name, value in (("session_id", "s2"), ("root_id", "r2"), ("step_key", "k2")):
        assert _v2_intent(**{name: value}).operation_id != original.operation_id
    assert _v2_intent(payload={"secret_ref": "lease:other"}).operation_id != original.operation_id


def test_v2_unknown_three_user_decisions_require_receipt_or_decision_refs(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    intent = _v2_intent(effect_class=EffectClass.AT_MOST_ONCE)
    effect, _ = log.plan_v2(intent, gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1)
    log.transition(effect.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT, now=1, lease_owner="dead", lease_expires_at=2)
    unknown = EffectReaper(log).recover_expired(now=3)[0]
    assert unknown.state is EffectState.UNKNOWN
    runner = EffectRunner(log, owner_id="runner")
    with pytest.raises(ValueError, match="decision"):
        runner.reauthorize_unknown(log.get(effect.operation_id), probe_ref="facts:not-a-decision", now=4)
    resumed = runner.reauthorize_unknown(log.get(effect.operation_id), probe_ref="decision:not-completed", now=4)
    assert resumed.probe_ref == "decision:not-completed"
    abandon_intent = _v2_intent(step_key="abandon", effect_class=EffectClass.AT_MOST_ONCE)
    abandon, _ = log.plan_v2(abandon_intent, gate_decision_id="gate:abandon", gate_fact=_v2_gate(scope_ref="scope:abandon"), now=1)
    log.transition(abandon.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT, now=1, lease_owner="dead", lease_expires_at=2)
    EffectReaper(log).recover_expired(now=3)
    with pytest.raises(ValueError, match="decision"):
        runner.abandon_unknown(log.get(abandon.operation_id), decision_ref="facts:abandon", now=4)
    assert runner.abandon_unknown(log.get(abandon.operation_id), decision_ref="decision:abandon", now=4).state is EffectState.ABANDONED
    done_intent = _v2_intent(step_key="done", effect_class=EffectClass.AT_MOST_ONCE)
    done, _ = log.plan_v2(done_intent, gate_decision_id="gate:done", gate_fact=_v2_gate(scope_ref="scope:done"), now=1)
    log.transition(done.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT, now=1, lease_owner="dead", lease_expires_at=2)
    EffectReaper(log).recover_expired(now=3)
    assert runner.settle_verified_ok(log.get(done.operation_id), receipt_ref="receipt:user-confirmed", now=4).state is EffectState.SETTLED_OK


def test_v1_schema_upgrade_gets_legacy_facts_and_v2_persists_new_facts(tmp_path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE effect(operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT, root_id TEXT NOT NULL, parent_id TEXT, step_key TEXT NOT NULL, kind TEXT NOT NULL, effect_class TEXT NOT NULL, purpose TEXT NOT NULL, intent_ref TEXT NOT NULL, intent_digest TEXT NOT NULL, gate_decision_id TEXT NOT NULL, rev_set TEXT NOT NULL, idem_key TEXT, state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_expires_at INTEGER, probe_ref TEXT, result_ref TEXT, error_ref TEXT, occurred_at INTEGER NOT NULL, recorded_at INTEGER NOT NULL, settled_at INTEGER)")
        connection.execute("INSERT INTO effect VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "legacy", "s", None, "r", None, "step", "kind", "IDEMPOTENT", "primary", "intent:legacy",
            "digest", "gate", "{\"policy\":\"p\"}", None, "PLANNED", 0, None, None, None, None, None, 1, 1, None,
        ))
    log = EffectLog(database)
    legacy = log.get("legacy")
    assert legacy.identity_algorithm == "blake2b-160"
    assert legacy.revision_schema_version == "legacy-v1"
    v2, _ = log.plan_v2(_v2_intent(step_key="new-v2"), gate_decision_id="gate:new-v2", gate_fact=_v2_gate(scope_ref="scope:new-v2"), now=2)
    assert v2.identity_algorithm == "blake3-256"
    assert v2.revision_schema_version == "effect-authority-v2"


def test_future_schema_is_rejected_before_any_schema_mutation(tmp_path) -> None:
    database = tmp_path / "future.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE effect_contract_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.execute("INSERT INTO effect_contract_meta VALUES('schema_version','999')")
        before = tuple(connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
    with pytest.raises(RuntimeError, match="newer"):
        EffectLog(database)
    with sqlite3.connect(database) as connection:
        after = tuple(connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
        assert after == before
        assert connection.execute("SELECT value FROM effect_contract_meta WHERE key='schema_version'").fetchone()[0] == "999"


def test_v2_queryable_probe_and_existing_receipt_recovery_use_frozen_contract(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    intent = _v2_intent(effect_class=EffectClass.QUERYABLE, kind="v2-query")
    effect, _ = log.plan_v2(intent, gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1)
    log.transition(effect.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT,
                   now=1, lease_owner="dead", lease_expires_at=2)
    outcome = EffectReaper(log).recover_expired(
        now=3, probes={("v2-query", EFFECT_V2): lambda _: (EffectState.SETTLED_OK, "receipt:probe")},
    )[0]
    assert outcome.state is EffectState.SETTLED_OK
    with log._connect() as connection:
        assert tuple(connection.execute(
            "SELECT receipt_kind,receipt_schema_version FROM effect_receipt WHERE operation_id=?",
            (effect.operation_id,),
        ).fetchone()) == ("v2-call.receipt", "receipt/v2")

    second = _v2_intent(step_key="receipt-crash")
    planned, _ = log.plan_v2(second, gate_decision_id="gate:receipt-crash", gate_fact=_v2_gate(scope_ref="scope:second"), now=4)
    log.transition(planned.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT,
                   now=4, lease_owner="runner", lease_expires_at=5)
    with log._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO effect_receipt(operation_id,receipt_ref,receipt_kind,receipt_schema_version,recorded_at) VALUES(?,?,?,?,?)",
            (planned.operation_id, "receipt:crashed", "v2-call.receipt", "receipt/v2", 4),
        )
        connection.commit()
    recovered = log.settle_ok_with_receipt(
        planned.operation_id, expected=EffectState.INFLIGHT, receipt_ref="receipt:crashed",
        receipt_kind="v2-call.receipt", receipt_schema_version="receipt/v2", intent_schema_version="intent/v2", now=5,
    )
    assert recovered.state is EffectState.SETTLED_OK


def test_same_kind_dual_contract_queryable_effects_use_their_own_probes(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.db", owner_id="runner")
    calls: list[str] = []
    runtime.handlers.register(EffectHandlerRegistration(
        kind="shared-query", effect_class=EffectClass.QUERYABLE, handler=lambda _: "receipt:v1",
        probe=lambda _: calls.append("v1") or (EffectState.SETTLED_OK, "receipt:v1"),
    ))
    runtime.handlers.register(EffectHandlerRegistration(
        kind="shared-query", effect_class=EffectClass.QUERYABLE, contract_version=EFFECT_V2,
        intent_schema_version="intent/v2", receipt_kind="v2-call.receipt", receipt_schema_version="receipt/v2",
        handler=lambda _: EffectReceipt("receipt:v2", "v2-call.receipt", "receipt/v2", "intent/v2"),
        probe=lambda _: calls.append("v2") or (EffectState.SETTLED_OK, "receipt:v2"),
    ))
    legacy, _ = runtime.log.plan(_intent(EffectClass.QUERYABLE, kind="shared-query"), now=1)
    v2, _ = runtime.log.plan_v2(
        _v2_intent(kind="shared-query", effect_class=EffectClass.QUERYABLE), gate_decision_id="gate:v2",
        gate_fact=_v2_gate(), now=1,
    )
    for effect in (legacy, v2):
        runtime.log.transition(effect.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT,
                               now=1, lease_owner="dead", lease_expires_at=2)
    outcomes = runtime.recover_expired(now=3)
    assert {outcome.operation_id for outcome in outcomes} == {legacy.operation_id, v2.operation_id}
    assert sorted(calls) == ["v1", "v2"]


def test_v2_queryable_never_falls_back_to_legacy_kind_probe(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    effect, _ = log.plan_v2(_v2_intent(effect_class=EffectClass.QUERYABLE), gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1)
    log.transition(effect.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT,
                   now=1, lease_owner="dead", lease_expires_at=2)
    calls: list[str] = []
    outcome = EffectReaper(log).recover_expired(
        now=3, probes={"v2-call": lambda _: calls.append("legacy") or (EffectState.SETTLED_OK, "receipt:wrong")},
    )[0]
    assert outcome.state is EffectState.UNKNOWN
    assert calls == []


def test_v2_queryable_planned_probe_requires_and_persists_evidence(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    effect, _ = log.plan_v2(_v2_intent(effect_class=EffectClass.QUERYABLE), gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1)
    log.transition(effect.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT, now=1, lease_owner="dead", lease_expires_at=2)
    outcome = EffectReaper(log).recover_expired(now=3, probes={("v2-call", EFFECT_V2): lambda _: (EffectState.PLANNED, "facts:not-completed")})[0]
    assert outcome.state is EffectState.PLANNED
    assert log.get(effect.operation_id).probe_ref == "facts:not-completed"
    for index, evidence in enumerate((None, "https://host/evidence?secret=x")):
        next_effect, _ = log.plan_v2(_v2_intent(step_key=f"evidence-{index}", effect_class=EffectClass.QUERYABLE), gate_decision_id=f"gate:evidence-{index}", gate_fact=_v2_gate(scope_ref=f"scope:evidence-{index}"), now=4)
        log.transition(next_effect.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT, now=4, lease_owner="dead", lease_expires_at=5)
        outcome = EffectReaper(log).recover_expired(now=6, probes={("v2-call", EFFECT_V2): lambda _: (EffectState.PLANNED, evidence)})[0]
        assert outcome.state is EffectState.UNKNOWN


def test_v2_concurrent_runners_execute_structured_receipt_once(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    intent = _v2_intent()
    gate = _v2_gate()
    calls: list[str] = []
    def handler(effect):
        calls.append(effect.operation_id)
        return EffectReceipt("receipt:concurrent", "v2-call.receipt", "receipt/v2", "intent/v2")
    runners = (EffectRunner(log, owner_id="a"), EffectRunner(log, owner_id="b"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda runner: runner.execute_v2(intent, handler, gate_decision_id="gate:v2", gate_fact=gate, now=1), runners))
    assert calls == [intent.operation_id]
    assert any(outcome.state is EffectState.SETTLED_OK for outcome in outcomes)
    assert log.get(intent.operation_id).state is EffectState.SETTLED_OK


def test_recovery_registry_versions_same_kind_without_cross_talk(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.db", owner_id="runner")
    calls: list[str] = []
    runtime.recoveries.register(EffectRecoveryRegistration(
        kind="reauth-shared", effect_class=EffectClass.NEEDS_REAUTH,
        reauthorize=lambda _: calls.append("v1") or (EffectState.PLANNED, "legacy-evidence"),
    ))
    runtime.recoveries.register(EffectRecoveryRegistration(
        kind="reauth-shared", effect_class=EffectClass.NEEDS_REAUTH, contract_version=EFFECT_V2,
        reauthorize=lambda _: calls.append("v2") or (EffectState.PLANNED, "decision:reauthorized"),
    ))
    legacy, _ = runtime.log.plan(_intent(EffectClass.NEEDS_REAUTH, kind="reauth-shared"), now=1)
    v2_intent = _v2_intent(kind="reauth-shared", effect_class=EffectClass.NEEDS_REAUTH)
    v2, _ = runtime.log.plan_v2(v2_intent, gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1)
    for effect in (legacy, v2):
        runtime.log.transition(effect.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT,
                               now=1, lease_owner="dead", lease_expires_at=2)
    outcomes = runtime.recover_expired(now=3)
    assert {outcome.state for outcome in outcomes} == {EffectState.PLANNED}
    assert sorted(calls) == ["v1", "v2"]
    assert runtime.log.get(v2.operation_id).probe_ref == "decision:reauthorized"

    rejected_intent = _v2_intent(step_key="bad-reauth", kind="reauth-bad", effect_class=EffectClass.NEEDS_REAUTH)
    runtime.recoveries.register(EffectRecoveryRegistration(
        kind="reauth-bad", effect_class=EffectClass.NEEDS_REAUTH, contract_version=EFFECT_V2,
        reauthorize=lambda _: (EffectState.PLANNED, "facts:not-a-decision"),
    ))
    rejected, _ = runtime.log.plan_v2(rejected_intent, gate_decision_id="gate:bad-reauth", gate_fact=_v2_gate(scope_ref="scope:bad-reauth"), now=4)
    runtime.log.transition(rejected.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT,
                           now=4, lease_owner="dead", lease_expires_at=5)
    assert runtime.recover_expired(now=6)[0].state is EffectState.UNKNOWN


@pytest.mark.parametrize("effect_class", [EffectClass.QUERYABLE, EffectClass.NEEDS_REAUTH])
def test_v2_reaper_exception_persists_only_digest_error_ref(tmp_path, effect_class) -> None:
    log = EffectLog(tmp_path / f"{effect_class.value}.db")
    effect, _ = log.plan_v2(
        _v2_intent(effect_class=effect_class), gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1,
    )
    log.transition(effect.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT,
                   now=1, lease_owner="dead", lease_expires_at=2)

    def explode(_effect):
        raise RuntimeError("Bearer secret-canary-must-not-persist")

    kwargs = ({"probes": {("v2-call", EFFECT_V2): explode}}
              if effect_class is EffectClass.QUERYABLE else
              {"reauthorizers": {("v2-call", EFFECT_V2): explode}})
    outcome = EffectReaper(log).recover_expired(now=3, **kwargs)[0]
    recovered = log.get(effect.operation_id)
    assert outcome.state is EffectState.UNKNOWN
    assert recovered.error_ref is not None
    assert __import__("re").fullmatch(r"error:[0-9a-f]{64}", recovered.error_ref)
    assert "secret-canary" not in recovered.error_ref


@pytest.mark.parametrize(
    "bad_outcome",
    [
        ("NOT_A_STATE", "receipt:bad-state"),
        (EffectState.SETTLED_OK, "https://example.invalid/receipt"),
        (EffectState.UNKNOWN, "https://example.invalid/error"),
    ],
    ids=("invalid-state", "invalid-receipt-ref", "invalid-error-ref"),
)
def test_v2_reaper_isolates_invalid_strategy_contract_and_continues_scan(tmp_path, bad_outcome) -> None:
    log = EffectLog(tmp_path / "effects.db")
    bad, _ = log.plan_v2(
        _v2_intent(
            kind="v2-bad-probe", step_key="bad", effect_class=EffectClass.QUERYABLE,
            gate_decision_id="gate:bad",
        ),
            gate_decision_id="gate:bad",
        gate_fact=_v2_gate(scope_ref="scope:bad-probe"),
        now=1,
    )
    good, _ = log.plan_v2(
        _v2_intent(
            kind="v2-good-probe", step_key="good", effect_class=EffectClass.QUERYABLE,
            gate_decision_id="gate:good-probe",
        ),
        gate_decision_id="gate:good-probe",
        gate_fact=_v2_gate(scope_ref="scope:good-probe"),
        now=1,
    )
    log.transition(
        bad.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT,
        now=1, lease_owner="dead-bad", lease_expires_at=2,
    )
    log.transition(
        good.operation_id, expected=EffectState.PLANNED, target=EffectState.INFLIGHT,
        now=1, lease_owner="dead-good", lease_expires_at=3,
    )

    outcomes = EffectReaper(log).recover_expired(
        now=4,
        probes={
            ("v2-bad-probe", EFFECT_V2): lambda _effect: bad_outcome,
            ("v2-good-probe", EFFECT_V2): lambda _effect: (EffectState.SETTLED_OK, "receipt:good-probe"),
        },
    )

    by_operation = {outcome.operation_id: outcome for outcome in outcomes}
    assert by_operation[bad.operation_id].state is EffectState.UNKNOWN
    assert by_operation[bad.operation_id].reason == "recovery_contract_invalid"
    assert __import__("re").fullmatch(r"error:[0-9a-f]{64}", log.get(bad.operation_id).error_ref or "")
    assert by_operation[good.operation_id].state is EffectState.SETTLED_OK
    assert log.get(good.operation_id).result_ref == "receipt:good-probe"


def test_v2_handler_string_result_is_rejected_without_terminal_settlement(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.db", owner_id="runner")
    runtime.handlers.register(EffectHandlerRegistration(
        kind="v2-call", effect_class=EffectClass.IDEMPOTENT, contract_version=EFFECT_V2,
        intent_schema_version="intent/v2", receipt_kind="v2-call.receipt", receipt_schema_version="receipt/v2",
        handler=lambda _: "receipt:not-structured",
    ))
    intent = _v2_intent()
    with pytest.raises(TypeError, match="immutable EffectReceipt"):
        runtime.execute_v2(intent, gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1)
    assert runtime.log.get(intent.operation_id).state is EffectState.INFLIGHT


def test_runtime_execute_v2_requires_gate_fact_and_uses_registered_contract(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.db", owner_id="runner")
    runtime.handlers.register(EffectHandlerRegistration(
        kind="v2-call", effect_class=EffectClass.IDEMPOTENT, contract_version=EFFECT_V2,
        intent_schema_version="intent/v2", receipt_kind="v2-call.receipt",
        receipt_schema_version="receipt/v2",
        handler=lambda _: EffectReceipt(
            "receipt:v2", "v2-call.receipt", "receipt/v2", "intent/v2",
        ),
    ))
    intent = _v2_intent()
    with pytest.raises(ValueError, match="execute_v2"):
        runtime.execute(intent, now=1)
    settled = runtime.execute_v2(
        intent, gate_decision_id="gate:v2", gate_fact=_v2_gate(), now=1,
    )
    assert settled.state is EffectState.SETTLED_OK


def _intent(effect_class: EffectClass = EffectClass.IDEMPOTENT, *, kind: str = "tool_call") -> EffectIntent:
    return EffectIntent(
        session_id="session-1",
        turn_id="turn-1",
        root_id="root-1",
        parent_id=None,
        step_key="acquire:source-1",
        kind=kind,
        effect_class=effect_class,
        purpose=EffectPurpose.PRIMARY,
        intent_ref="payload:intent-1",
        gate_decision_id="gate-1",
        rev_set={"policy": "p1", "capability": "c1"},
        payload={"source_ref": "source-1", "output": "asset-1"},
        idem_key="idem-1",
    )


def test_operation_id_is_deterministic_and_plan_is_single_flight(tmp_path):
    log = EffectLog(tmp_path / "effects.db")
    intent = _intent()

    with ThreadPoolExecutor(max_workers=20) as pool:
        rows = list(pool.map(lambda _: log.plan(intent, now=100), range(100)))

    assert len({effect.operation_id for effect, _ in rows}) == 1
    assert sum(created for _, created in rows) == 1
    assert log.get(intent.operation_id).state is EffectState.PLANNED


def test_same_operation_rejects_gate_or_revision_drift(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    original = _intent()
    log.plan(original, now=100)

    with pytest.raises(RuntimeError, match="different frozen intent"):
        log.plan(EffectIntent(
            session_id=original.session_id,
            turn_id=original.turn_id,
            root_id=original.root_id,
            parent_id=original.parent_id,
            step_key=original.step_key,
            kind=original.kind,
            effect_class=original.effect_class,
            purpose=original.purpose,
            intent_ref=original.intent_ref,
            gate_decision_id="gate-2",
            rev_set={"policy": "p2", "capability": "c1"},
            payload=original.payload,
            idem_key=original.idem_key,
            operation_id_override=original.operation_id,
        ), now=101)


def test_cancellation_request_is_idempotent_coordination_not_effect_state(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    effect, _ = log.plan(_intent(), now=100)

    first = log.request_cancellation(
        effect.operation_id, request_ref="api://cancel/1", now=101,
    )
    repeated = log.request_cancellation(
        effect.operation_id, request_ref="api://cancel/2", now=102,
    )

    assert first.state is EffectState.PLANNED
    assert repeated.state is EffectState.PLANNED
    assert log.cancellation_requested(effect.operation_id) is True
    assert log.get(effect.operation_id).state is EffectState.PLANNED


def test_terminal_effect_rejects_new_cancellation_request(tmp_path) -> None:
    runner = EffectRunner(EffectLog(tmp_path / "effects.db"), owner_id="runner-a")
    settled = runner.execute(_intent(), lambda _: "receipt:done", now=100)

    with pytest.raises(InvalidEffectTransition, match="terminal Effect"):
        runner.log.request_cancellation(
            settled.operation_id, request_ref="api://cancel/late", now=101,
        )


def test_runner_persists_intent_before_effect_and_replays_terminal_result(tmp_path):
    log = EffectLog(tmp_path / "effects.db")
    runner = EffectRunner(log, owner_id="runner-a", lease_seconds=10)
    observed: list[EffectState] = []

    def handler(effect):
        observed.append(log.get(effect.operation_id).state)
        return "asset:1"

    first = runner.execute(_intent(), handler, now=100)
    replay = runner.execute(_intent(), handler, now=101)

    assert observed == [EffectState.INFLIGHT]
    assert first.state is EffectState.SETTLED_OK
    assert first.result_ref == "asset:1"
    assert replay == first


def test_runner_executes_existing_planned_effect_and_reaper_owns_failure(tmp_path):
    log = EffectLog(tmp_path / "effects.db")
    runner = EffectRunner(log, owner_id="runner-a", lease_seconds=1)
    planned, _ = log.plan(_intent(), now=100)

    def fail(_effect):
        raise RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        runner.execute_planned(planned.operation_id, fail, now=100)

    assert log.get(planned.operation_id).state is EffectState.INFLIGHT
    assert EffectReaper(log).recover_expired(now=102)[0].state is EffectState.PLANNED

    settled = runner.execute_planned(
        planned.operation_id,
        lambda _effect: "crp://receipts/final",
        now=103,
        receipt_kind="custom-receipt",
    )
    assert settled.state is EffectState.SETTLED_OK
    assert settled.result_ref == "crp://receipts/final"


def test_execute_planned_never_reinvokes_an_already_inflight_handler(tmp_path):
    log = EffectLog(tmp_path / "effects.db")
    planned, _ = log.plan(_intent(), now=100)
    runner = EffectRunner(log, owner_id="runner-a", lease_seconds=10)
    claimed, won = runner.claim_planned(planned.operation_id, now=100)
    calls: list[str] = []

    replay = runner.execute_planned(
        planned.operation_id,
        lambda effect: calls.append(effect.operation_id) or "receipt:duplicate",
        now=101,
    )

    assert won is True
    assert claimed.state is EffectState.INFLIGHT
    assert replay.state is EffectState.INFLIGHT
    assert calls == []


def test_runner_owns_abandoned_transition_after_handler_compensation(tmp_path):
    log = EffectLog(tmp_path / "effects.db")
    runner = EffectRunner(log, owner_id="runner-a")
    planned, _ = log.plan(_intent(), now=100)

    abandoned = runner.execute_planned(
        planned.operation_id,
        lambda _effect: (_ for _ in ()).throw(
            EffectHandlerAbandoned("review_evidence_conflict")
        ),
        now=100,
    )

    assert abandoned.state is EffectState.ABANDONED
    assert abandoned.error_ref == "review_evidence_conflict"


def test_runner_releases_deferred_handler_without_settling_or_replaying(tmp_path):
    log = EffectLog(tmp_path / "effects.db")
    runner = EffectRunner(log, owner_id="runner-a")
    planned, _ = log.plan(_intent(), now=100)

    deferred = runner.execute_planned(
        planned.operation_id,
        lambda _effect: (_ for _ in ()).throw(
            EffectHandlerDeferred("capability_temporarily_unavailable")
        ),
        now=100,
    )

    assert deferred.state is EffectState.PLANNED
    assert deferred.probe_ref == "capability_temporarily_unavailable"
    assert deferred.result_ref is None


def test_core_binds_one_receipt_to_one_effect_and_replays_exactly(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    effect, _ = log.plan(_intent(), now=100)
    log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="runner-a",
        lease_expires_at=110,
    )

    settled = log.settle_ok_with_receipt(
        effect.operation_id,
        expected=EffectState.INFLIGHT,
        receipt_ref="crp://receipts/one",
        receipt_kind="test-receipt",
        now=101,
    )
    replayed = log.settle_ok_with_receipt(
        effect.operation_id,
        expected=EffectState.INFLIGHT,
        receipt_ref="crp://receipts/one",
        receipt_kind="test-receipt",
        now=102,
    )

    assert settled.state is EffectState.SETTLED_OK
    assert settled.result_ref == "crp://receipts/one"
    assert replayed == settled


def test_receipt_binding_conflict_rolls_back_without_terminal_drift(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    first, _ = log.plan(_intent(), now=100)
    log.transition(
        first.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="runner-a",
        lease_expires_at=110,
    )
    log.settle_ok_with_receipt(
        first.operation_id,
        expected=EffectState.INFLIGHT,
        receipt_ref="crp://receipts/shared",
        receipt_kind="test-receipt",
        now=101,
    )
    other_intent = replace(
        _intent(),
        step_key="other-step",
        operation_id_override="effect-other",
    )
    second, _ = log.plan(other_intent, now=102)
    log.transition(
        second.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=102,
        lease_owner="runner-b",
        lease_expires_at=112,
    )

    with pytest.raises(InvalidEffectTransition, match="another Effect"):
        log.settle_ok_with_receipt(
            second.operation_id,
            expected=EffectState.INFLIGHT,
            receipt_ref="crp://receipts/shared",
            receipt_kind="test-receipt",
            now=103,
        )

    assert log.get(second.operation_id).state is EffectState.INFLIGHT


def test_core_renews_same_lease_and_fences_active_competing_owner(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    effect, _ = log.plan(_intent(), now=100)
    log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="worker-a:token-a",
        lease_expires_at=110,
        increment_attempt=True,
    )
    with log._connect() as connection:  # Core transaction seam used by composed authorities.
        connection.execute("BEGIN IMMEDIATE")
        renewed = log.renew_or_take_over_lease_in_connection(
            connection,
            effect.operation_id,
            lease_owner="worker-a:token-a",
            lease_expires_at=120,
            now=105,
        )
        connection.commit()
    assert renewed.attempt == 1
    assert renewed.lease_expires_at == 120

    with log._connect() as connection, pytest.raises(
        InvalidEffectTransition, match="active lease"
    ):
        connection.execute("BEGIN IMMEDIATE")
        log.renew_or_take_over_lease_in_connection(
            connection,
            effect.operation_id,
            lease_owner="worker-b:token-b",
            lease_expires_at=121,
            now=106,
        )


def test_core_expired_lease_takeover_increments_attempt(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    effect, _ = log.plan(_intent(), now=100)
    log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="worker-a:token-a",
        lease_expires_at=101,
        increment_attempt=True,
    )
    with log._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        taken = log.renew_or_take_over_lease_in_connection(
            connection,
            effect.operation_id,
            lease_owner="worker-b:token-b",
            lease_expires_at=112,
            now=102,
        )
        connection.commit()
    assert taken.attempt == 2
    assert taken.lease_owner == "worker-b:token-b"


def test_core_expired_same_owner_takeover_starts_a_new_generation(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    effect, _ = log.plan(_intent(), now=100)
    claimed = log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="worker-a:stable-process-id",
        lease_expires_at=101,
        increment_attempt=True,
    )

    with log._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        taken = log.renew_or_take_over_lease_in_connection(
            connection,
            effect.operation_id,
            lease_owner="worker-a:stable-process-id",
            lease_expires_at=112,
            now=102,
        )
        connection.commit()

    assert taken.attempt == claimed.attempt + 1
    assert taken.lease_owner == claimed.lease_owner


def test_stale_generation_cannot_bind_receipt_after_expired_takeover(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    effect, _ = log.plan(_intent(), now=100)
    claimed = log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="worker-a",
        lease_expires_at=101,
        increment_attempt=True,
    )
    stale_fence = EffectLeaseFence.from_effect(claimed)
    with log._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        log.renew_or_take_over_lease_in_connection(
            connection,
            effect.operation_id,
            lease_owner="worker-b",
            lease_expires_at=112,
            now=102,
        )
        connection.commit()

    with pytest.raises(InvalidEffectTransition, match="fence"):
        log.settle_ok_with_receipt(
            effect.operation_id,
            expected=EffectState.INFLIGHT,
            receipt_ref="receipt:stale-worker",
            receipt_kind="test-receipt",
            now=102,
            fence=stale_fence,
        )

    with log._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM effect_receipt WHERE operation_id=?",
            (effect.operation_id,),
        ).fetchone()[0] == 0
    current = log.get(effect.operation_id)
    assert current.state is EffectState.INFLIGHT
    assert current.lease_owner == "worker-b"
    assert current.attempt == claimed.attempt + 1


def test_reaper_cannot_overwrite_a_new_claim_generation(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    effect, _ = log.plan(
        _intent(EffectClass.QUERYABLE, kind="fenced-query"), now=100,
    )
    claimed = log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="expired-worker",
        lease_expires_at=101,
        increment_attempt=True,
    )

    def probe(_effect):
        with log._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            log.renew_or_take_over_lease_in_connection(
                connection,
                effect.operation_id,
                lease_owner="live-worker",
                lease_expires_at=120,
                now=102,
            )
            connection.commit()
        return EffectState.PLANNED, None

    outcome = EffectReaper(log).recover_expired(
        now=102, probes={"fenced-query": probe},
    )[0]

    current = log.get(effect.operation_id)
    assert outcome.reason == "concurrent_reaper_won"
    assert outcome.state is EffectState.INFLIGHT
    assert current.state is EffectState.INFLIGHT
    assert current.lease_owner == "live-worker"
    assert current.attempt == claimed.attempt + 1
    assert current.lease_expires_at == 120


def test_current_generation_can_checkpoint_and_settle(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    runner = EffectRunner(log, owner_id="worker-a", lease_seconds=10)
    effect, _ = log.plan(_intent(), now=100)
    claimed = runner.begin_planned(effect.operation_id, now=100)
    fence = EffectLeaseFence.from_effect(claimed)

    with log._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        checkpoint_owner = log.assert_active_fence_in_connection(
            connection, fence, now=101,
        )
        connection.commit()
    assert checkpoint_owner.attempt == claimed.attempt

    settled = runner.settle_ok(
        claimed,
        receipt_ref="receipt:current-worker",
        receipt_kind="test-receipt",
        now=101,
    )
    assert settled.state is EffectState.SETTLED_OK


def test_domain_checkpoint_accepts_same_generation_renewal_but_rejects_takeover(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "renewed-effects.db", owner_id="worker-a", lease_seconds=10)
    planned, _ = runtime.log.plan(_intent(), now=100)
    claimed = runtime.runner.begin_planned(planned.operation_id, now=100)
    now = [102]
    checkpoint = EffectLeaseCheckpoint.for_claim(runtime, claimed, clock=lambda: now[0])

    with runtime.log._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        renewed = runtime.runner.renew(claimed, lease_expires_at=120, now=101, connection=connection)
        connection.commit()

    checkpoint.checkpoint()
    with runtime.log._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        taken_over = runtime.log.renew_or_take_over_lease_in_connection(
            connection, claimed.operation_id, lease_owner="worker-b", lease_expires_at=130, now=121,
        )
        connection.commit()
    assert taken_over.attempt == renewed.attempt + 1
    now[0] = 122
    with pytest.raises(InvalidEffectTransition, match="stale or expired"):
        checkpoint.checkpoint()


def test_domain_receipt_and_effect_binding_roll_back_together_on_settle_failure(tmp_path) -> None:
    database = tmp_path / "effects.db"
    log = EffectLog(database)
    effect, _ = log.plan(_intent(), now=100)
    log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="runner-a",
        lease_expires_at=110,
    )
    with log._connect() as connection:
        connection.execute("CREATE TABLE domain_receipt(ref TEXT PRIMARY KEY,payload TEXT)")
        connection.commit()
    with pytest.raises(InvalidEffectTransition):
        with log._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO domain_receipt VALUES('crp://receipts/atomic','{}')"
            )
            log.settle_ok_with_receipt_in_connection(
                connection,
                effect.operation_id,
                expected=EffectState.UNKNOWN,
                receipt_ref="crp://receipts/atomic",
                receipt_kind="test-receipt",
                now=101,
            )
    with log._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM domain_receipt").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM effect_receipt").fetchone()[0] == 0
    assert log.get(effect.operation_id).state is EffectState.INFLIGHT


def test_legacy_settled_effect_receipt_is_backfilled_without_payload_copy(tmp_path) -> None:
    database = tmp_path / "effects.db"
    log = EffectLog(database)
    effect, _ = log.plan(_intent(), now=100)
    log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="legacy-runner",
        lease_expires_at=110,
    )
    log.transition(
        effect.operation_id,
        expected=EffectState.INFLIGHT,
        target=EffectState.SETTLED_OK,
        now=101,
        result_ref="crp://receipts/legacy",
    )

    EffectLog(database)

    with log._connect() as connection:
        columns = tuple(
            row[1] for row in connection.execute("PRAGMA table_info(effect_receipt)")
        )
        binding = connection.execute(
            "SELECT receipt_ref,receipt_kind FROM effect_receipt WHERE operation_id=?",
            (effect.operation_id,),
        ).fetchone()
    assert "payload_json" not in columns
    assert tuple(binding) == ("crp://receipts/legacy", "tool_call-legacy-receipt")


def test_two_runners_execute_one_external_handler(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    calls: list[str] = []

    def handler(effect):
        calls.append(effect.operation_id)
        return "receipt:single-winner"

    runners = (
        EffectRunner(log, owner_id="runner-a", lease_seconds=10),
        EffectRunner(log, owner_id="runner-b", lease_seconds=10),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda runner: runner.execute(_intent(), handler, now=100), runners))

    assert calls == [_intent().operation_id]
    assert all(outcome.state in {EffectState.INFLIGHT, EffectState.SETTLED_OK} for outcome in outcomes)
    assert any(outcome.state is EffectState.SETTLED_OK for outcome in outcomes)
    assert log.get(_intent().operation_id).state is EffectState.SETTLED_OK


def test_receipt_created_before_settle_crash_leaves_at_most_once_unknown(tmp_path) -> None:
    class CrashBeforeSettleLog(EffectLog):
        def settle_ok_with_receipt_in_connection(self, connection, operation_id, **kwargs):
            raise BaseException("simulated process death after receipt persistence")

        def transition(self, operation_id, *, expected, target, now, **kwargs):
            return super().transition(
                operation_id,
                expected=expected,
                target=target,
                now=now,
                **kwargs,
            )

    log = CrashBeforeSettleLog(tmp_path / "effects.db")
    receipt_store: list[str] = []

    def handler(_):
        receipt_store.append("receipt:external-1")
        return receipt_store[0]

    with pytest.raises(BaseException, match="simulated process death"):
        EffectRunner(log, owner_id="runner-a", lease_seconds=1).execute(
            _intent(EffectClass.AT_MOST_ONCE), handler, now=100,
        )

    assert receipt_store == ["receipt:external-1"]
    assert log.get(_intent().operation_id).state is EffectState.INFLIGHT
    recovered = EffectReaper(log).recover_expired(now=102)
    assert recovered[0].state is EffectState.UNKNOWN


@pytest.mark.parametrize(
    ("effect_class", "expected", "reason"),
    [
        (EffectClass.PURE, EffectState.PLANNED, "safe_to_retry"),
        (EffectClass.IDEMPOTENT, EffectState.PLANNED, "safe_to_retry"),
        (EffectClass.AT_MOST_ONCE, EffectState.UNKNOWN, "at_most_once_uncertain"),
        (EffectClass.NEEDS_REAUTH, EffectState.UNKNOWN, "reauthorization_required"),
    ],
)
def test_reaper_routes_expired_effects_by_class(tmp_path, effect_class, expected, reason):
    log = EffectLog(tmp_path / f"{effect_class.value}.db")
    effect, _ = log.plan(_intent(effect_class), now=100)
    log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="dead-runner",
        lease_expires_at=101,
        increment_attempt=True,
    )

    outcome = EffectReaper(log).recover_expired(now=102)[0]

    assert outcome.state is expected
    assert outcome.reason == reason


@pytest.mark.parametrize(
    ("probe_state", "result_ref"),
    [
        (EffectState.SETTLED_OK, "remote:done"),
        (EffectState.SETTLED_ERR, "remote:failed"),
        (EffectState.PLANNED, None),
        (EffectState.UNKNOWN, None),
    ],
)
def test_queryable_recovery_uses_probe(tmp_path, probe_state, result_ref):
    log = EffectLog(tmp_path / "queryable.db")
    effect, _ = log.plan(_intent(EffectClass.QUERYABLE, kind="remote_job"), now=100)
    log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="dead-runner",
        lease_expires_at=101,
    )

    outcome = EffectReaper(log).recover_expired(
        now=102,
        probes={"remote_job": lambda _: (probe_state, result_ref)},
    )[0]

    assert outcome.state is probe_state
    recovered = log.get(effect.operation_id)
    if probe_state is EffectState.SETTLED_OK:
        assert recovered.result_ref == result_ref
    elif probe_state is EffectState.SETTLED_ERR:
        assert recovered.error_ref == result_ref


@pytest.mark.parametrize(
    ("reauthorized_state", "expected_error"),
    [
        (EffectState.PLANNED, None),
        (EffectState.UNKNOWN, "lease-revoked"),
    ],
)
def test_needs_reauth_recovery_is_core_scheduled(
    tmp_path, reauthorized_state, expected_error,
):
    runtime = build_effect_runtime(tmp_path / "reauth.db", owner_id="runner")
    runtime.recoveries.register(EffectRecoveryRegistration(
        kind="credentialed-write",
        effect_class=EffectClass.NEEDS_REAUTH,
        reauthorize=lambda _effect: (reauthorized_state, expected_error),
    ))
    effect, _ = runtime.log.plan(
        _intent(EffectClass.NEEDS_REAUTH, kind="credentialed-write"), now=100,
    )
    runtime.log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="dead-runner",
        lease_expires_at=101,
    )

    outcome = runtime.recover_expired(now=102)[0]

    assert outcome.state is reauthorized_state
    assert outcome.reason == "reauthorization_resolved"
    assert runtime.log.get(effect.operation_id).error_ref == expected_error


def test_core_reaper_recovers_expired_effect_after_process_restart(tmp_path):
    database = tmp_path / "restart.db"
    first = build_effect_runtime(database, owner_id="worker-before-crash")
    effect, _ = first.log.plan(_intent(EffectClass.IDEMPOTENT), now=100)
    first.log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="worker-before-crash",
        lease_expires_at=101,
    )

    restarted = build_effect_runtime(database, owner_id="worker-after-restart")
    outcome = restarted.recover_expired(now=102)[0]

    assert outcome.state is EffectState.PLANNED
    assert outcome.reason == "safe_to_retry"


@pytest.mark.parametrize("strategy", ["probe", "verifier"])
def test_reaper_isolates_domain_strategy_failure_as_unknown(tmp_path, strategy):
    log = EffectLog(tmp_path / f"{strategy}.db")
    effect_class = EffectClass.QUERYABLE if strategy == "probe" else EffectClass.AT_MOST_ONCE
    effect, _ = log.plan(_intent(effect_class, kind="remote_job"), now=100)
    log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="dead-runner",
        lease_expires_at=101,
    )

    def fail(_effect):
        raise RuntimeError("domain unavailable")

    kwargs = {"probes": {"remote_job": fail}} if strategy == "probe" else {
        "verifiers": {"remote_job": fail}
    }
    outcome = EffectReaper(log).recover_expired(now=102, **kwargs)[0]

    assert outcome.state is EffectState.UNKNOWN
    assert outcome.reason == f"{strategy}_failed"
    assert log.get(effect.operation_id).error_ref == "RuntimeError:domain unavailable"


def test_reaper_reports_concurrent_winner_without_replaying_strategy(tmp_path):
    log = EffectLog(tmp_path / "concurrent-reaper.db")
    effect, _ = log.plan(_intent(EffectClass.QUERYABLE, kind="remote_job"), now=100)
    log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="dead-runner",
        lease_expires_at=101,
    )
    calls = 0

    def probe(_effect):
        nonlocal calls
        calls += 1
        if calls == 1:
            log.settle_ok_with_receipt(
                effect.operation_id,
                expected=EffectState.INFLIGHT,
                receipt_ref="receipt:remote",
                receipt_kind="remote-job-receipt",
                now=102,
            )
        return EffectState.SETTLED_OK, "receipt:remote"

    outcome = EffectReaper(log).recover_expired(
        now=102, probes={"remote_job": probe},
    )[0]

    assert calls == 1
    assert outcome.state is EffectState.SETTLED_OK
    assert outcome.reason == "concurrent_reaper_won"


def test_unknown_requires_explicit_resolution(tmp_path):
    log = EffectLog(tmp_path / "unknown.db")
    effect, _ = log.plan(_intent(EffectClass.AT_MOST_ONCE), now=100)
    log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="dead-runner",
        lease_expires_at=101,
    )
    EffectReaper(log).recover_expired(now=102)

    with pytest.raises(InvalidEffectTransition):
        log.transition(
            effect.operation_id,
            expected=EffectState.UNKNOWN,
            target=EffectState.INFLIGHT,
            now=103,
        )

    resolved = log.transition(
        effect.operation_id,
        expected=EffectState.UNKNOWN,
        target=EffectState.SETTLED_OK,
        now=103,
        result_ref="user-confirmed",
    )
    assert resolved.state is EffectState.SETTLED_OK


@pytest.mark.parametrize(
    ("target", "result_ref"),
    [
        (EffectState.PLANNED, None),
        (EffectState.ABANDONED, None),
    ],
)
def test_unknown_user_can_confirm_not_completed_or_abandon(
    tmp_path, target, result_ref,
):
    log = EffectLog(tmp_path / f"unknown-{target.value}.db")
    effect, _ = log.plan(_intent(EffectClass.AT_MOST_ONCE), now=100)
    log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="dead-runner",
        lease_expires_at=101,
    )
    EffectReaper(log).recover_expired(now=102)

    resolved = log.transition(
        effect.operation_id,
        expected=EffectState.UNKNOWN,
        target=target,
        now=103,
        result_ref=result_ref,
    )

    assert resolved.state is target


def test_runner_can_settle_unknown_error_from_verified_domain_fact(tmp_path) -> None:
    log = EffectLog(tmp_path / "verified-error.db")
    runner = EffectRunner(log, owner_id="runner-a", lease_seconds=1)
    effect, _ = log.plan(_intent(EffectClass.AT_MOST_ONCE), now=100)
    inflight = runner.begin_planned(effect.operation_id, now=100)
    unknown = runner.mark_unknown(
        inflight, error_ref="worker.crashed", now=100,
    )

    settled = runner.settle_verified_error(
        unknown, error_ref="domain.fact.failed", now=102,
    )

    assert settled.state is EffectState.SETTLED_ERR
    assert settled.error_ref == "domain.fact.failed"


def test_intent_rejects_missing_revision_facts() -> None:
    with pytest.raises(ValueError, match="rev_set"):
        EffectIntent(
            session_id="session-1", root_id="root-1", step_key="step-1",
            kind="tool_call", effect_class=EffectClass.IDEMPOTENT,
            intent_ref="intent:1", gate_decision_id="gate-1", rev_set={}, payload={},
        )


def test_inflight_and_success_require_lease_and_receipt_contract(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.db")
    effect, _ = log.plan(_intent(), now=100)

    with pytest.raises(InvalidEffectTransition, match="lease owner"):
        log.transition(
            effect.operation_id,
            expected=EffectState.PLANNED,
            target=EffectState.INFLIGHT,
            now=100,
            lease_expires_at=110,
        )

    inflight = log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="runner-a",
        lease_expires_at=110,
    )
    with pytest.raises(InvalidEffectTransition, match="receipt reference"):
        log.transition(
            inflight.operation_id,
            expected=EffectState.INFLIGHT,
            target=EffectState.SETTLED_OK,
            now=101,
        )


def test_registry_fails_closed_for_unknown_duplicate_and_class_drift(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.db", owner_id="runner-a")
    assert isinstance(runtime, EffectRuntime)
    with pytest.raises(KeyError, match="not registered"):
        runtime.execute(_intent(), now=100)

    runtime.handlers.register(EffectHandlerRegistration(
        kind="tool_call",
        effect_class=EffectClass.IDEMPOTENT,
        handler=lambda _: "receipt:tool-1",
    ))
    with pytest.raises(ValueError, match="already registered"):
        runtime.handlers.register(EffectHandlerRegistration(
            kind="tool_call",
            effect_class=EffectClass.IDEMPOTENT,
            handler=lambda _: "receipt:tool-2",
        ))
    with pytest.raises(ValueError, match="class drifted"):
        runtime.execute(_intent(EffectClass.AT_MOST_ONCE), now=100)


def test_queryable_registration_requires_probe_and_runtime_recovers(tmp_path) -> None:
    registry = EffectHandlerRegistry()
    with pytest.raises(ValueError, match="require a probe"):
        registry.register(EffectHandlerRegistration(
            kind="remote_job",
            effect_class=EffectClass.QUERYABLE,
            handler=lambda _: "receipt:remote",
        ))

    runtime = build_effect_runtime(tmp_path / "effects.db", owner_id="runner-a", lease_seconds=1)
    runtime.handlers.register(EffectHandlerRegistration(
        kind="remote_job",
        effect_class=EffectClass.QUERYABLE,
        handler=lambda _: "receipt:remote",
        probe=lambda _: (EffectState.SETTLED_OK, "receipt:remote"),
    ))
    effect, _ = runtime.log.plan(_intent(EffectClass.QUERYABLE, kind="remote_job"), now=100)
    runtime.log.transition(
        effect.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=100,
        lease_owner="dead-runner",
        lease_expires_at=101,
    )

    outcome = runtime.recover_expired(now=102)

    assert outcome[0].state is EffectState.SETTLED_OK
    assert runtime.log.get(effect.operation_id).result_ref == "receipt:remote"
