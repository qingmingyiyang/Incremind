from __future__ import annotations

import sqlite3

import pytest

from core.effect_log import EFFECT_V2, NOT_APPLICABLE, V2_REVISION_KEYS, EffectClass, EffectLog, GateDecision
from core.product_core.index_rebuild_effect_admission import (
    EFFECT_KIND, RECEIPT_KIND, RECEIPT_SCHEMA, REQUEST_TABLE, IndexRebuildEffectAdmissionFactory,
    SQLiteIndexRebuildEffectAdmission,
)


def _request(**changes):
    value = {"id": "request-1", "backend_kind": "sqlite_fts5", "reason": "freshness", "source_refs": ["source-a#rev:1"]}
    value.update(changes)
    return value


def _manifest(**changes):
    value = {"id": "manifest-1", "backend_kind": "sqlite_fts5", "source_fingerprint": "fp-1"}
    value.update(changes)
    return value


def _ledger(**changes):
    value = {"id": "ledger-1", "source_fingerprint": "fp-1", "entry_count": 2}
    value.update(changes)
    return value


def test_builds_frozen_queryable_v2_gate_intent_and_request() -> None:
    admitted = IndexRebuildEffectAdmissionFactory(admitted_at=100).build(request=_request(), manifest=_manifest(), ledger=_ledger())
    assert admitted.intent.contract_version == EFFECT_V2
    assert admitted.intent.kind == EFFECT_KIND
    assert admitted.intent.effect_class is EffectClass.QUERYABLE
    assert admitted.gate_fact.decision is GateDecision.ALLOW
    assert admitted.intent.expected_receipt_kind == RECEIPT_KIND
    assert admitted.intent.expected_receipt_schema_version == RECEIPT_SCHEMA
    assert admitted.request["request_ref"] == "facts:index-rebuild/request/request-1"
    assert admitted.intent.payload["manifest_ref"] == admitted.request["manifest_ref"]
    assert admitted.intent.rev_set["context_manifest"] == admitted.request["manifest_revision"]
    assert admitted.intent.rev_set["budget"] == admitted.request["ledger_revision"]
    assert set(admitted.intent.rev_set) == set(V2_REVISION_KEYS)
    for key in ("provider", "model_route", "secret"):
        assert admitted.intent.rev_set[key] == NOT_APPLICABLE


@pytest.mark.parametrize("kwargs", [
    {"request": _request(backend_kind="vector")},
    {"request": _request(source_refs=[])},
    {"request": _request(extra="no")},
    {"manifest": {"backend_kind": "sqlite_fts5"}},
    {"ledger": {"id": "ledger bad"}},
])
def test_rejects_noncanonical_or_untraceable_admission_evidence(kwargs) -> None:
    arguments = {"request": _request(), "manifest": _manifest(), "ledger": _ledger()}
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        IndexRebuildEffectAdmissionFactory(admitted_at=100).build(**arguments)


def test_volatile_object_revisions_do_not_change_execution_identity() -> None:
    factory = IndexRebuildEffectAdmissionFactory(admitted_at=100)
    first = factory.build(request=_request(), manifest=_manifest(), ledger=_ledger())
    replay = factory.build(
        request=_request(), manifest=_manifest(source_fingerprint="fp-2"),
        ledger=_ledger(source_fingerprint="fp-2"),
    )
    assert replay.intent.operation_id == first.intent.operation_id
    assert replay.intent.rev_set["context_manifest"] != first.intent.rev_set["context_manifest"]


@pytest.mark.parametrize("field,value", [
    ("reason", "Bearer token-value"),
    ("source_refs", ["C:\\private\\source"]),
    ("source_refs", [{"source": "whole document"}]),
])
def test_rejects_credentials_paths_and_nested_source_content(field, value) -> None:
    request = _request(**{field: value})
    with pytest.raises(ValueError):
        IndexRebuildEffectAdmissionFactory(admitted_at=100).build(
            request=request, manifest=_manifest(), ledger=_ledger(),
        )


def test_atomic_admission_bootstraps_domain_schema_and_rolls_everything_back(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite")
    admission = IndexRebuildEffectAdmissionFactory(admitted_at=100).build(
        request=_request(), manifest=_manifest(), ledger=_ledger(),
    )
    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        SQLiteIndexRebuildEffectAdmission(log).admit_in_connection(connection, admission, now=100)
        connection.rollback()
    with sqlite3.connect(log.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (REQUEST_TABLE,),
        ).fetchone()[0] == 0


def test_admission_rejects_clock_and_authority_replay_drift_atomically(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite")
    command = SQLiteIndexRebuildEffectAdmission(log)
    first = IndexRebuildEffectAdmissionFactory(admitted_at=100).build(
        request=_request(), manifest=_manifest(), ledger=_ledger(),
    )
    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        command.admit_in_connection(connection, first, now=100)
        connection.commit()

    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="time drifted"):
            command.admit_in_connection(connection, first, now=101)
        connection.rollback()

    drifted = IndexRebuildEffectAdmissionFactory(admitted_at=100).build(
        request=_request(), manifest=_manifest(source_fingerprint="fp-2"),
        ledger=_ledger(source_fingerprint="fp-2"),
    )
    assert drifted.intent.operation_id == first.intent.operation_id
    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="operation id collision"):
            command.admit_in_connection(connection, drifted, now=100)
        connection.rollback()
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 1
        assert connection.execute(f"SELECT COUNT(*) FROM {REQUEST_TABLE}").fetchone()[0] == 1


def test_domain_fact_tables_reject_update_and_delete(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite")
    admission = IndexRebuildEffectAdmissionFactory(admitted_at=100).build(
        request=_request(), manifest=_manifest(), ledger=_ledger(),
    )
    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        effect, _ = SQLiteIndexRebuildEffectAdmission(log).admit_in_connection(
            connection, admission, now=100,
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                f"UPDATE {REQUEST_TABLE} SET request_json='{{}}' WHERE operation_id=?",
                (effect.operation_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                f"DELETE FROM {REQUEST_TABLE} WHERE operation_id=?", (effect.operation_id,),
            )
