from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

import pytest

from core.effect_log import EFFECT_V2, EffectLog, EffectReaper, EffectRunner, EffectState
from core.product_core.memory_projection_rebuild_effect_admission import (
    RECEIPT_TABLE, RESERVATION_TABLE, MemoryProjectionRebuildEffectAdmissionFactory,
    SQLiteMemoryProjectionRebuildEffectAdmission,
)
from core.product_core.memory_projection_rebuild_effect_execution import (
    MemoryProjectionRebuildEffectExecutionHandler,
    MemoryProjectionRebuildEffectExecutionProbe,
)
import core.product_core.memory_projection_rebuild_effect_execution as execution
from core.product_core.memory_projection_authority_contract import MemoryProjectionAuthoritySnapshot
from core.product_core.memory_projection_repository import ObjectStoreMemoryProjectionRepository
from core.storage_provider import JsonObjectStore


def _snapshot(*, overview: str = "derived projection") -> MemoryProjectionAuthoritySnapshot:
    return MemoryProjectionAuthoritySnapshot(
        project_id="project-1", authority_identity="memory-authority-v1",
        series_memories=({"id": "series-1", "series_id": "series-1", "overview": overview, "scenario_ids": ["scenario-1"], "source_refs": [{"source_id": "source-1", "locator": "section:one"}], "project_ids": ["project-1"], "stale": False, "revision": 1, "trust_status": "user_confirmed"},),
        scenarios=({"id": "scenario-1", "title": "title", "summary": "summary", "atom_ids": ["atom-1"], "source_refs": [{"source_id": "source-1", "locator": "section:two"}], "tags": ["tag"], "series_id": "series-1", "project_id": "project-1", "stale": False, "revision": 1, "trust_status": "trusted"},),
        atoms=({"id": "atom-1", "source_id": "source-1", "content": "derived authority remains read-only", "atom_type": "decision", "tags": ["tag"], "source_refs": [{"source_id": "source-1", "locator": "section:three"}], "revision": 1, "trust_status": "trusted"},), project_skills=(),
    )


@dataclass
class _Authority:
    snapshot: MemoryProjectionAuthoritySnapshot

    def load(self, project_id: str) -> MemoryProjectionAuthoritySnapshot:
        assert project_id == self.snapshot.project_id
        return self.snapshot


def _admitted(tmp_path):
    log = EffectLog(tmp_path / "effects.sqlite3")
    admission = MemoryProjectionRebuildEffectAdmissionFactory(admitted_at=100).build(_snapshot())
    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        effect, _ = SQLiteMemoryProjectionRebuildEffectAdmission(log).admit_in_connection(connection, admission)
        connection.commit()
    repo = ObjectStoreMemoryProjectionRepository(JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "library"))
    authority = _Authority(_snapshot())
    return log, effect, authority, repo


def test_handler_builds_stages_activates_and_returns_receipt_while_core_settles(tmp_path) -> None:
    log, effect, authority, repo = _admitted(tmp_path)
    handler = MemoryProjectionRebuildEffectExecutionHandler(log.database, authority, repo)
    settled = EffectRunner(log, owner_id="worker", lease_seconds=10).execute_planned(effect.operation_id, handler, now=101)
    assert settled.state is EffectState.SETTLED_OK
    assert settled.result_ref == f"receipt:memory-projection-rebuild/{effect.operation_id}"
    assert repo.load_current(project_id="project-1", authority_identity="memory-authority-v1", authority_fingerprint=authority.snapshot.build(generated_at="2000-01-01T00:00:00+00:00").authority_fingerprint).status == "fresh"
    assert MemoryProjectionRebuildEffectExecutionProbe(log.database, authority, repo)(settled) == (EffectState.SETTLED_OK, settled.result_ref)


def test_probe_distinguishes_planned_reserved_unknown_authority_drift_and_bad_receipt(tmp_path) -> None:
    log, effect, authority, repo = _admitted(tmp_path)
    probe = MemoryProjectionRebuildEffectExecutionProbe(log.database, authority, repo)
    assert probe(effect)[0] is EffectState.PLANNED
    with sqlite3.connect(log.database) as connection:
        connection.execute(f"INSERT INTO {RESERVATION_TABLE}(operation_id,request_digest,reserved_at) VALUES(?,?,?)", (effect.operation_id, "x", 100))
        connection.commit()
    assert probe(effect) == (EffectState.UNKNOWN, "error:memory-projection-rebuild-reserved")
    authority.snapshot = _snapshot(overview="changed")
    assert probe(effect) == (EffectState.UNKNOWN, "error:memory-projection-rebuild-evidence-drift")


def test_domain_receipt_rejects_mutation_after_insert(tmp_path) -> None:
    log, effect, authority, repo = _admitted(tmp_path)
    handler = MemoryProjectionRebuildEffectExecutionHandler(log.database, authority, repo)
    EffectRunner(log, owner_id="worker", lease_seconds=10).execute_planned(effect.operation_id, handler, now=101)
    with sqlite3.connect(log.database) as connection:
        receipt = json.loads(connection.execute(f"SELECT receipt_json FROM {RECEIPT_TABLE} WHERE operation_id=?", (effect.operation_id,)).fetchone()[0])
        receipt["artifact_id"] = "other-artifact"
        with pytest.raises(sqlite3.IntegrityError, match="insert-or-verify"):
            connection.execute(f"UPDATE {RECEIPT_TABLE} SET receipt_json=? WHERE operation_id=?", (json.dumps(receipt), effect.operation_id))
        with pytest.raises(sqlite3.IntegrityError, match="insert-or-verify"):
            connection.execute(f"DELETE FROM {RECEIPT_TABLE} WHERE operation_id=?", (effect.operation_id,))


def test_handler_rejects_request_digest_and_intent_payload_tampering(tmp_path) -> None:
    log, effect, authority, repo = _admitted(tmp_path)
    handler = MemoryProjectionRebuildEffectExecutionHandler(log.database, authority, repo)
    with sqlite3.connect(log.database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="insert-or-verify"):
            connection.execute("UPDATE memory_projection_rebuild_effect_request SET request_digest='bad' WHERE operation_id=?", (effect.operation_id,))
    with sqlite3.connect(log.database) as connection:
        connection.execute("UPDATE effect_intent_fact SET payload_json='{}' WHERE operation_id=?", (effect.operation_id,))
        connection.commit()
    with pytest.raises(ValueError, match="intent payload drifted"):
        handler(effect)


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("effect_intent_fact", "intent_ref", "intent:memory-projection-rebuild/other"),
        ("effect_intent_fact", "intent_digest", "0" * 64),
        ("effect_intent_fact", "payload_json", "{}"),
        ("effect_intent_fact", "schema_version", "other-v2"),
        ("effect_gate_fact", "decision", "DENY"),
        ("effect_gate_fact", "rule_ref", "rule:other"),
        ("effect_gate_fact", "scope_ref", "scope:memory-projection/other"),
        ("effect_gate_fact", "budget_after", "{}"),
        ("effect_gate_fact", "secret_scope", "scope:other"),
        ("effect_gate_fact", "policy_revision", "other-policy"),
        ("effect_gate_fact", "mutated_intent_digest", "0" * 64),
        ("effect_gate_fact", "decision_digest", "0" * 64),
    ],
)
def test_probe_fails_closed_for_every_core_fact_binding_drift(tmp_path, table, column, value) -> None:
    log, effect, authority, repo = _admitted(tmp_path)
    where_column = "operation_id" if table == "effect_intent_fact" else "decision_id"
    identity = effect.operation_id if table == "effect_intent_fact" else effect.gate_decision_id
    with sqlite3.connect(log.database) as connection:
        connection.execute(f"UPDATE {table} SET {column}=? WHERE {where_column}=?", (value, identity))
        connection.commit()
    assert MemoryProjectionRebuildEffectExecutionProbe(log.database, authority, repo)(effect) == (
        EffectState.UNKNOWN, "error:memory-projection-rebuild-evidence-drift",
    )


def test_probe_rejects_legal_but_noncanonical_intent_and_gate_fact_ids(tmp_path) -> None:
    log, effect, authority, repo = _admitted(tmp_path)
    alternate_id = "other-canonical-id"
    with sqlite3.connect(log.database) as connection:
        connection.execute(
            "UPDATE effect_intent_fact SET intent_ref=? WHERE operation_id=?",
            (f"intent:memory-projection-rebuild/{alternate_id}", effect.operation_id),
        )
        connection.execute(
            "UPDATE effect_gate_fact SET decision_id=? WHERE decision_id=?",
            (f"gate:memory-projection-rebuild/{alternate_id}", effect.gate_decision_id),
        )
        connection.execute(
            "UPDATE effect SET intent_ref=?,gate_decision_id=? WHERE operation_id=?",
            (
                f"intent:memory-projection-rebuild/{alternate_id}",
                f"gate:memory-projection-rebuild/{alternate_id}",
                effect.operation_id,
            ),
        )
        connection.commit()
    drifted = log.get(effect.operation_id)
    assert MemoryProjectionRebuildEffectExecutionProbe(log.database, authority, repo)(drifted) == (
        EffectState.UNKNOWN, "error:memory-projection-rebuild-evidence-drift",
    )


def test_handler_construction_does_not_initialize_domain_schema(tmp_path) -> None:
    database = tmp_path / "no-domain-schema.sqlite3"
    authority = _Authority(_snapshot())
    repo = ObjectStoreMemoryProjectionRepository(JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "library"))
    MemoryProjectionRebuildEffectExecutionHandler(database, authority, repo)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'memory_projection_rebuild_effect_%'"
        ).fetchone()[0] == 0


def test_reaper_recovers_activation_without_domain_receipt_once_and_two_workers_do_not_rebuild(tmp_path, monkeypatch) -> None:
    log, effect, authority, repo = _admitted(tmp_path)
    handler = MemoryProjectionRebuildEffectExecutionHandler(log.database, authority, repo)
    runner = EffectRunner(log, owner_id="worker-a", lease_seconds=10)
    calls = {"stage": 0, "activate": 0}
    original_stage, original_activate = type(repo).stage_projection, type(repo).activate_staged
    def stage(instance, projection):
        calls["stage"] += 1
        return original_stage(instance, projection)
    def activate(instance, **kwargs):
        calls["activate"] += 1
        return original_activate(instance, **kwargs)
    monkeypatch.setattr(type(repo), "stage_projection", stage)
    monkeypatch.setattr(type(repo), "activate_staged", activate)
    original_receipt = execution._write_receipt
    monkeypatch.setattr(execution, "_write_receipt", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash before receipt")))
    with pytest.raises(RuntimeError, match="crash before receipt"):
        runner.execute_planned(effect.operation_id, handler, now=101)
    assert log.get(effect.operation_id).state is EffectState.INFLIGHT
    contender = EffectRunner(log, owner_id="worker-b", lease_seconds=10).execute_planned(effect.operation_id, handler, now=102)
    assert contender.state is EffectState.INFLIGHT
    assert calls == {"stage": 1, "activate": 1}
    monkeypatch.setattr(execution, "_write_receipt", original_receipt)
    probe = MemoryProjectionRebuildEffectExecutionProbe(log.database, authority, repo)
    probe_calls = {"count": 0}
    def counted_probe(candidate):
        probe_calls["count"] += 1
        return probe(candidate)
    outcomes = EffectReaper(log).recover_expired(
        now=112, probes={("memory_projection_rebuild", EFFECT_V2): counted_probe},
    )
    assert [(item.state, item.reason) for item in outcomes] == [(EffectState.SETTLED_OK, "probe_resolved")]
    assert calls == {"stage": 1, "activate": 1}
    assert probe_calls == {"count": 1}
    assert log.get(effect.operation_id).state is EffectState.SETTLED_OK
    assert EffectReaper(log).recover_expired(
        now=113, probes={("memory_projection_rebuild", EFFECT_V2): counted_probe},
    ) == []
    assert probe_calls == {"count": 1}
