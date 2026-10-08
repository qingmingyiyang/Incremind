from __future__ import annotations

import sqlite3

import pytest

from core.effect_log import EFFECT_V2, EffectLog
from core.product_core.memory_projection_rebuild_effect_admission import (
    REQUEST_TABLE,
    MemoryProjectionRebuildEffectAdmissionFactory,
    SQLiteMemoryProjectionRebuildEffectAdmission,
)
from core.product_core.memory_projection_authority_contract import MemoryProjectionAuthoritySnapshot


def _snapshot(*, overview: str = "derived projection") -> MemoryProjectionAuthoritySnapshot:
    return MemoryProjectionAuthoritySnapshot(
        project_id="project-1", authority_identity="memory-authority-v1",
        series_memories=({"id": "series-1", "series_id": "series-1", "overview": overview, "scenario_ids": ["scenario-1"], "source_refs": [{"source_id": "source-1", "locator": "section:one"}], "project_ids": ["project-1"], "stale": False, "revision": 1, "trust_status": "user_confirmed"},),
        scenarios=({"id": "scenario-1", "title": "title", "summary": "summary", "atom_ids": ["atom-1"], "source_refs": [{"source_id": "source-1", "locator": "section:two"}], "tags": ["tag"], "series_id": "series-1", "project_id": "project-1", "stale": False, "revision": 1, "trust_status": "trusted"},),
        atoms=({"id": "atom-1", "source_id": "source-1", "content": "derived authority remains read-only", "atom_type": "decision", "tags": ["tag"], "source_refs": [{"source_id": "source-1", "locator": "section:three"}], "revision": 1, "trust_status": "trusted"},),
        project_skills=(),
    )


def test_admission_persists_gate_intent_effect_and_request_in_one_caller_transaction(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite3")
    admission = MemoryProjectionRebuildEffectAdmissionFactory(admitted_at=100).build(_snapshot())
    command = SQLiteMemoryProjectionRebuildEffectAdmission(log)
    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        effect, created = command.admit_in_connection(connection, admission)
        connection.commit()
    assert created is True
    assert effect.contract_version == EFFECT_V2
    assert effect.state.value == "PLANNED"
    with sqlite3.connect(log.database) as connection:
        assert connection.execute(f"SELECT COUNT(*) FROM {REQUEST_TABLE}").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM effect_gate_fact").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM effect_intent_fact").fetchone()[0] == 1


def test_admission_rolls_back_effect_and_request_when_caller_rolls_back(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite3")
    admission = MemoryProjectionRebuildEffectAdmissionFactory(admitted_at=100).build(_snapshot())
    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        SQLiteMemoryProjectionRebuildEffectAdmission(log).admit_in_connection(connection, admission)
        connection.rollback()
    with sqlite3.connect(log.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0
        # The domain table may be created by the attempted command; it must not
        # retain an executable request after the transaction rolls back.
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(f"SELECT COUNT(*) FROM {REQUEST_TABLE}").fetchone()


def test_admission_freezes_authority_fingerprint_and_policy_metadata() -> None:
    admission = MemoryProjectionRebuildEffectAdmissionFactory(admitted_at=100).build(_snapshot())
    assert admission.intent.rev_set["context_manifest"].startswith("projection-authority:")
    assert admission.request["projection_version"] == "progressive-memory-r0-r1-v1"
    assert admission.request["generator_policy_id"] == "deterministic-r0-r1-builder-v1"
    assert "job_runner" not in SQLiteMemoryProjectionRebuildEffectAdmission.__module__


def test_admission_rejects_request_that_does_not_match_frozen_intent(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite3")
    admission = MemoryProjectionRebuildEffectAdmissionFactory(admitted_at=100).build(_snapshot())
    request = dict(admission.request)
    request["request_ref"] = "facts:memory-projection-rebuild/request/other"
    object.__setattr__(admission, "request", request)
    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="request reference drifted"):
            SQLiteMemoryProjectionRebuildEffectAdmission(log).admit_in_connection(connection, admission)
        connection.rollback()
