from __future__ import annotations

import sqlite3

import pytest

from core.effect_log import EffectLog
from core.product_core.index_rebuild_effect_adapter import (
    ARTIFACT_COLLECTION,
    IndexRebuildEffectAdapter,
)
from core.product_core.index_rebuild_effect_admission import (
    IndexRebuildEffectAdmissionFactory,
    SQLiteIndexRebuildEffectAdmission,
)
from core.search_and_recall import (
    IndexRebuildRequest,
    RecallIndexEntry,
    build_recall_authority_ledger,
    create_sqlite_fts5_manifest,
    select_default_recall_backend_policy,
    source_ledger_fingerprint,
    sqlite_fts5_manifest_payload,
)
from core.storage_provider import JsonObjectStore


def _entry(content: str = "effect adapter lighthouse") -> RecallIndexEntry:
    return RecallIndexEntry(
        object_id="source-a", project_id="project-a", layer="l0_source", content=content,
        source_refs=("source-a#rev:1",), trust_status="user_confirmed", base_score=0.5,
    )


def _prepared(tmp_path, entries):
    store = JsonObjectStore(tmp_path / "objects", legacy_root=tmp_path / "library")
    ledger_rows = build_recall_authority_ledger(entries)
    fingerprint = source_ledger_fingerprint(ledger_rows)
    request = IndexRebuildRequest(
        "sqlite_fts5", "source_changed", fingerprint, len(ledger_rows), ("source-a#rev:1",), "2026-08-30T00:00:00+00:00",
    )
    candidate = create_sqlite_fts5_manifest(
        rebuild_request=request, backend_selection=select_default_recall_backend_policy(), manifest_id="candidate-a",
    )
    manifest = {
        "id": candidate.manifest_id, "backend_kind": candidate.backend_kind,
        "source_fingerprint": candidate.source_fingerprint,
    }
    ledger = {"id": "ledger-a", "source_fingerprint": fingerprint, "entry_count": len(ledger_rows)}
    store.write("recall_index_manifests", candidate.manifest_id, sqlite_fts5_manifest_payload(candidate), expected_revision=None)
    admission = IndexRebuildEffectAdmissionFactory(admitted_at=100).build(
        request={"id": "request-a", "backend_kind": "sqlite_fts5", "reason": "source_changed", "source_refs": ["source-a#rev:1"]},
        manifest=manifest, ledger=ledger,
    )
    log = EffectLog(tmp_path / "effects.sqlite")
    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        effect, _ = SQLiteIndexRebuildEffectAdmission(log).admit_in_connection(connection, admission, now=100)
        connection.commit()
    adapter = IndexRebuildEffectAdapter(store, tmp_path, lambda: tuple(entries))
    return store, log, effect, adapter


def test_real_adapter_builds_verifies_persists_artifact_and_activates_without_job_state(tmp_path) -> None:
    store, _log, effect, adapter = _prepared(tmp_path, (_entry(),))
    receipt = adapter.handler(tmp_path / "effects.sqlite").handle(effect)

    artifact = store.read(ARTIFACT_COLLECTION, effect.operation_id)
    active = store.read("recall_index_manifests", "active")
    assert receipt.receipt_ref.endswith(effect.operation_id)
    assert artifact is not None and artifact["artifact_revision"].startswith("index-rebuild-artifact:sha256:")
    assert active is not None and active["verified_operation_id"] == effect.operation_id
    assert active["source"] == "verified_index_rebuild_effect"
    assert "verified_job_id" not in active
    assert adapter.probe(tmp_path / "effects.sqlite").probe(effect)[0].value == "SETTLED_OK"


def test_artifact_before_receipt_crash_replays_from_active_artifact_without_rebuilding(tmp_path) -> None:
    store, _log, effect, adapter = _prepared(tmp_path, (_entry(),))
    crashing = adapter.handler(tmp_path / "effects.sqlite")
    object.__setattr__(crashing, "after_domain_write", lambda: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        crashing.handle(effect)
    artifact = store.read(ARTIFACT_COLLECTION, effect.operation_id)
    assert artifact is not None

    receipt = adapter.handler(tmp_path / "effects.sqlite").handle(effect)
    assert receipt.receipt_ref.endswith(effect.operation_id)
    assert store.read(ARTIFACT_COLLECTION, effect.operation_id) == artifact


def test_artifact_before_activation_crash_replays_activation_without_rebuilding(tmp_path) -> None:
    store, _log, effect, adapter = _prepared(tmp_path, (_entry(),))
    object.__setattr__(
        adapter, "after_artifact_write",
        lambda: (_ for _ in ()).throw(RuntimeError("artifact persisted")),
    )
    with pytest.raises(RuntimeError, match="artifact persisted"):
        adapter.handler(tmp_path / "effects.sqlite").handle(effect)
    artifact = store.read(ARTIFACT_COLLECTION, effect.operation_id)
    assert artifact is not None
    assert store.read("recall_index_manifests", "active") is None

    object.__setattr__(adapter, "after_artifact_write", None)
    receipt = adapter.handler(tmp_path / "effects.sqlite").handle(effect)
    active = store.read("recall_index_manifests", "active")
    assert receipt.receipt_ref.endswith(effect.operation_id)
    assert active is not None
    assert active["verified_operation_id"] == effect.operation_id
    assert store.read(ARTIFACT_COLLECTION, effect.operation_id) == artifact


def test_completion_query_fails_closed_when_authority_or_active_manifest_drifts(tmp_path) -> None:
    entries = [_entry()]
    store, _log, effect, adapter = _prepared(tmp_path, entries)
    adapter.handler(tmp_path / "effects.sqlite").handle(effect)
    with sqlite3.connect(tmp_path / "effects.sqlite") as connection:
        domain = connection.execute(
            "SELECT request_json FROM index_rebuild_effect_request WHERE operation_id=?", (effect.operation_id,),
        ).fetchone()[0]
    import json
    payload = json.loads(domain)
    payload["operation_id"] = effect.operation_id
    assert adapter.query_completion(payload).state == "completed"
    active = store.read("recall_index_manifests", "active")
    assert active is not None
    drifted_active = dict(active)
    drifted_active["database_uri"] = "file:///drift.sqlite3"
    store.write("recall_index_manifests", "active", drifted_active, expected_revision=None)
    assert adapter.query_completion(payload).state == "unknown"
    store.write("recall_index_manifests", "active", active, expected_revision=None)
    entries[0] = _entry("drifted recall authority")
    assert adapter.query_completion(payload).state == "unknown"
