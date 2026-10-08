from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from core.effect_log import EFFECT_V2, NOT_APPLICABLE, V2_REVISION_KEYS, EffectClass, EffectLog, GateDecision
from core.product_core.video_knowledge_index_effect_admission import (
    EFFECT_KIND,
    RECEIPT_KIND,
    RECEIPT_SCHEMA,
    IDENTITY_TABLE,
    REQUEST_TABLE,
    SQLiteVideoKnowledgeIndexEffectAdmission,
    VideoKnowledgeIndexEffectAdmissionFactory,
    allocate_identity_in_connection,
)


def _request(**changes: object) -> dict[str, object]:
    request: dict[str, object] = {
        "id": "video-index-request-1",
        "operation": "upsert_video",
        "series_id": "series-1",
        "video_id": "video-1",
        "source_revision": "source-r1",
        "workspace_revision": "workspace-r1",
        "embedding_profile_revision": "embedding-r1",
        "lancedb_schema_revision": "lancedb-schema-r1",
        "index_generation": "generation-1",
    }
    request.update(changes)
    return request


def test_builds_independent_queryable_v2_admission_with_all_revision_keys() -> None:
    admission = VideoKnowledgeIndexEffectAdmissionFactory(admitted_at=100).build(request=_request())
    assert admission.intent.contract_version == EFFECT_V2
    assert admission.intent.kind == EFFECT_KIND
    assert admission.intent.effect_class is EffectClass.QUERYABLE
    assert admission.gate_fact.decision is GateDecision.ALLOW
    assert admission.intent.expected_receipt_kind == RECEIPT_KIND
    assert admission.intent.expected_receipt_schema_version == RECEIPT_SCHEMA
    assert set(admission.intent.rev_set) == set(V2_REVISION_KEYS)
    assert admission.intent.rev_set["secret"] == NOT_APPLICABLE
    assert admission.intent.rev_set["context_manifest"] == "video-workspace:workspace-r1:source:source-r1"
    assert admission.intent.rev_set["bundle"] == "lancedb-schema-r1"
    assert admission.intent.rev_set["handler"] == "video-knowledge-index-handler-v2"
    assert set(admission.request) == set(_request())
    assert "job_runner" not in SQLiteVideoKnowledgeIndexEffectAdmission.__module__


@pytest.mark.parametrize("candidate", [
    _request(extra="no"),
    _request(operation="refresh"),
    _request(source_revision="C:\\private\\source"),
    _request(video_id="Bearer token-value"),
    _request(video_id={"body": "video transcript"}),
    _request(operation="full_rebuild", series_id="series-1"),
    _request(operation="delete_series", video_id=None),
])
def test_rejects_paths_body_secret_markers_and_invalid_operation_shapes(candidate: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        VideoKnowledgeIndexEffectAdmissionFactory(admitted_at=100).build(request=candidate)


def test_full_rebuild_contains_only_bounded_request_fields() -> None:
    request = _request(operation="full_rebuild")
    request.pop("series_id")
    request.pop("video_id")
    admission = VideoKnowledgeIndexEffectAdmissionFactory(admitted_at=100).build(request=request)
    assert set(admission.request) == {
        "id", "operation", "source_revision", "workspace_revision", "embedding_profile_revision",
        "lancedb_schema_revision", "index_generation",
    }


def test_replay_is_exact_and_authority_drift_fails_closed(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite")
    command = SQLiteVideoKnowledgeIndexEffectAdmission(log)
    first = VideoKnowledgeIndexEffectAdmissionFactory(admitted_at=100).build(request=_request())
    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        effect, created = command.admit_in_connection(connection, first, now=100)
        replay, replay_created = command.admit_in_connection(connection, first, now=100)
        connection.commit()
    assert created is True
    assert replay_created is False
    assert replay.operation_id == effect.operation_id
    with sqlite3.connect(log.database) as connection:
        stored = connection.execute(f"SELECT request_json FROM {REQUEST_TABLE}").fetchone()[0]
    assert json.loads(stored) == _request()

    drifted = VideoKnowledgeIndexEffectAdmissionFactory(admitted_at=100).build(
        request=_request(workspace_revision="workspace-r2"),
    )
    assert drifted.intent.operation_id == effect.operation_id
    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="Gate fact drifted"):
            command.admit_in_connection(connection, drifted, now=100)
        connection.rollback()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("decision", GateDecision.DENY),
        ("rule_ref", "rule:other"),
        ("scope_ref", "scope:other"),
        ("secret_scope", "scope:other"),
        ("policy_revision", "policy-other"),
    ],
)
def test_rejects_every_gate_identity_drift_before_planning(tmp_path, field, value) -> None:
    log = EffectLog(tmp_path / "effects.sqlite")
    admission = VideoKnowledgeIndexEffectAdmissionFactory(admitted_at=100).build(
        request=_request(),
    )
    drifted = replace(admission, gate_fact=replace(admission.gate_fact, **{field: value}))
    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="Gate authority drifted"):
            SQLiteVideoKnowledgeIndexEffectAdmission(log).admit_in_connection(
                connection, drifted, now=100,
            )
        connection.rollback()
    with sqlite3.connect(log.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0


def test_caller_transaction_owns_atomicity_and_request_facts_are_immutable(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite")
    admission = VideoKnowledgeIndexEffectAdmissionFactory(admitted_at=100).build(request=_request())
    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        SQLiteVideoKnowledgeIndexEffectAdmission(log).admit_in_connection(connection, admission, now=100)
        connection.rollback()
    with sqlite3.connect(log.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (REQUEST_TABLE,),
        ).fetchone()[0] == 0

    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        effect, _ = SQLiteVideoKnowledgeIndexEffectAdmission(log).admit_in_connection(connection, admission, now=100)
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(f"UPDATE {REQUEST_TABLE} SET request_json='{{}}' WHERE operation_id=?", (effect.operation_id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(f"DELETE FROM {REQUEST_TABLE} WHERE operation_id=?", (effect.operation_id,))


def test_identity_mapping_rolls_back_with_the_caller_transaction(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite")
    identity = {"operation": "upsert_video", "series_id": "series", "video_id": "video", "source_revision": "source-1", "workspace_revision": "workspace-1", "embedding_profile_revision": "embedding-1", "lancedb_schema_revision": "schema-1"}
    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert allocate_identity_in_connection(connection, identity, now=1) == 1
        connection.rollback()
    with sqlite3.connect(log.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (IDENTITY_TABLE,)).fetchone()[0] == 0
