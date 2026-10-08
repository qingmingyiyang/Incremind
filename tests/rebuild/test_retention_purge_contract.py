from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.document_engine import DocumentDraft, SQLiteDocumentRepository
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.product_core import (
    BuildRetentionDryRun,
    DeleteLibraryItem,
    RetentionBackupEvidence,
    RetentionPolicy,
    serialize_retention_dry_run,
)
from core.storage_provider import (
    JsonObjectStore,
    SQLiteStructuredRecordStore,
    create_vault_backup,
)
from core.retention_inventory import (
    RetentionExternalRecord,
    VaultRetentionInventory,
    build_retention_backup_evidence,
)
from core.storage_provider.vault_backup_restore import fingerprint_vault_root


_DELETED_AT = datetime(2026, 5, 1, tzinfo=UTC)


def _runtime(tmp_path: Path, *, document_now: str = "2026-05-01T00:00:00Z"):
    active = tmp_path / "active"
    (active / "library").mkdir(parents=True)
    (active / "library" / "marker.txt").write_text("active-v1", encoding="utf-8")
    store = JsonObjectStore(active / ".rebuild-data", legacy_root=active / "library")
    records = SQLiteStructuredRecordStore(active / ".rebuild-data" / "records.sqlite3")
    documents = SQLiteDocumentRepository(records, now=document_now)
    return active, store, documents


def _deleted_source(store: JsonObjectStore, *, source_id_suffix: str = "one") -> str:
    ObjectStoreSourceRegistrar(store).register(
        SourceSubmission(
            kind="text",
            title=f"Retention source {source_id_suffix}",
            content=f"retention fixture {source_id_suffix}",
        )
    )
    source = store.list("sources")[-1]
    source_id = str(source["id"])
    result = DeleteLibraryItem(
        store,
        clock=lambda: _DELETED_AT,
        operation_id_factory=lambda: f"delete-{source_id_suffix}",
    ).execute(item_type="source", item_id=source_id)
    assert result.status == "deleted"
    return source_id


def _archived_document(
    documents: SQLiteDocumentRepository,
    *,
    source_id: str | None = None,
) -> str:
    refs = (
        {
            "source_id": source_id or "source-retention-evidence",
            "locator": "fixture://source",
        },
    )
    created = documents.create(
        DocumentDraft(
            title=f"Retention document {source_id or 'standalone'}",
            document_type="note",
            markdown="# Archived\n\nContent",
            source_refs=refs,
        )
    )
    archived = documents.archive(str(created["id"]), expected_revision=1)
    assert archived["status"] == "archived"
    return str(archived["id"])


def _scan(
    *,
    active: Path,
    store: JsonObjectStore,
    documents: SQLiteDocumentRepository,
    complete: bool = True,
    additional: tuple[RetentionExternalRecord, ...] = (),
):
    return VaultRetentionInventory(
        active_vault_root=active,
        source_store=store,
        document_repository=documents,
        document_authority="sqlite_document",
        additional_records=additional,
        reference_catalog_complete=complete,
    ).scan()


def _verified_backup(tmp_path: Path, active: Path, snapshot_id: str):
    snapshot = create_vault_backup(
        source_root=active,
        backups_root=tmp_path / "backups",
        snapshot_id=snapshot_id,
    )
    evidence = build_retention_backup_evidence(
        snapshot_root=snapshot.snapshot_root,
        active_vault_root=active,
    )
    assert evidence.status == "verified"
    return snapshot, evidence


def _report(candidates, evidence, *, now: datetime):
    return BuildRetentionDryRun().execute(
        candidates=candidates,
        backup_evidence=evidence,
        evaluated_at=now,
    )


def test_expired_deleted_source_with_owned_records_and_backup_is_dry_run_eligible(
    tmp_path: Path,
) -> None:
    active, store, documents = _runtime(tmp_path)
    source_id = _deleted_source(store)
    store.write(
        "source_outputs",
        f"output-{source_id}",
        {"id": f"output-{source_id}", "source_id": source_id},
        expected_revision=None,
    )
    for collection, object_id in (
        ("media_processing_jobs", f"job-{source_id}"),
        ("media_processing_outputs", f"media-{source_id}"),
        ("audio_asset_refs", f"audio-{source_id}"),
        ("source_asset_links", f"asset-link-{source_id}"),
    ):
        store.write(
            collection,
            object_id,
            {"id": object_id, "source_id": source_id},
            expected_revision=None,
        )
    candidates = _scan(active=active, store=store, documents=documents)
    _snapshot, evidence = _verified_backup(tmp_path, active, "retention-source")

    report = _report(candidates, evidence, now=_DELETED_AT + timedelta(days=31))

    assert report.execution_supported is False
    assert report.approval_token is None
    assert len(report.items) == 1
    item = report.items[0]
    assert item.aggregate_type == "source"
    assert item.object_id == source_id
    assert item.eligible is True
    assert item.blockers == ()
    assert {record.collection for record in item.owned_records} == {
        "sources",
        "source_outputs",
        "media_processing_jobs",
        "media_processing_outputs",
        "audio_asset_refs",
        "source_asset_links",
    }
    assert store.read_including_deleted("sources", source_id) is not None
    assert store.read("source_outputs", f"output-{source_id}") is not None


def test_source_before_existing_undo_expiry_is_blocked(tmp_path: Path) -> None:
    active, store, documents = _runtime(tmp_path)
    _deleted_source(store)
    candidates = _scan(active=active, store=store, documents=documents)
    _snapshot, evidence = _verified_backup(tmp_path, active, "retention-too-early")

    report = _report(candidates, evidence, now=_DELETED_AT + timedelta(days=6))

    assert report.items[0].eligible is False
    assert "retention_not_elapsed" in report.items[0].blockers


def test_active_sqlite_document_reference_blocks_source_candidate(tmp_path: Path) -> None:
    active, store, documents = _runtime(tmp_path)
    source_id = _deleted_source(store)
    document_id = _archived_document(documents, source_id=source_id)
    candidates = _scan(active=active, store=store, documents=documents)
    _snapshot, evidence = _verified_backup(tmp_path, active, "retention-source-reference")

    report = _report(candidates, evidence, now=_DELETED_AT + timedelta(days=60))
    source = next(item for item in report.items if item.aggregate_type == "source")

    assert source.eligible is False
    assert "inbound_references_present" in source.blockers
    assert any(
        reference.authority == "sqlite_document" and reference.object_id == document_id
        for reference in source.inbound_references
    )


def test_archived_sqlite_document_requires_thirty_days_and_then_becomes_eligible(
    tmp_path: Path,
) -> None:
    active, store, documents = _runtime(tmp_path)
    document_id = _archived_document(documents)
    candidates = _scan(active=active, store=store, documents=documents)
    _snapshot, evidence = _verified_backup(tmp_path, active, "retention-document")

    early = _report(candidates, evidence, now=datetime(2026, 5, 20, tzinfo=UTC))
    mature = _report(candidates, evidence, now=datetime(2026, 6, 15, tzinfo=UTC))

    assert early.items[0].object_id == document_id
    assert early.items[0].eligible is False
    assert "retention_not_elapsed" in early.items[0].blockers
    assert mature.items[0].eligible is True
    assert {record.collection for record in mature.items[0].owned_records} == {
        "documents",
        "document_revisions",
        "document_markdown",
    }


def test_document_external_reference_blocks_only_document_candidate(tmp_path: Path) -> None:
    active, store, documents = _runtime(tmp_path)
    source_id = _deleted_source(store)
    document_id = _archived_document(documents)
    external = RetentionExternalRecord(
        authority="sqlite_memory",
        collection="memory_candidates",
        object_id="candidate-1",
        payload={"id": "candidate-1", "document_id": document_id},
        revision=1,
    )
    candidates = _scan(
        active=active,
        store=store,
        documents=documents,
        additional=(external,),
    )
    _snapshot, evidence = _verified_backup(tmp_path, active, "retention-mixed")

    report = _report(candidates, evidence, now=datetime(2026, 7, 1, tzinfo=UTC))
    source = next(item for item in report.items if item.object_id == source_id)
    document = next(item for item in report.items if item.object_id == document_id)

    assert source.eligible is True
    assert document.eligible is False
    assert document.inbound_references[0].authority == "sqlite_memory"


def test_stale_json_document_projection_blocks_active_sqlite_candidate(tmp_path: Path) -> None:
    active, store, documents = _runtime(tmp_path)
    document_id = _archived_document(documents)
    store.write(
        "documents",
        document_id,
        {"id": document_id, "status": "archived", "revision": 99},
        expected_revision=None,
    )
    candidates = _scan(active=active, store=store, documents=documents)
    _snapshot, evidence = _verified_backup(tmp_path, active, "retention-authority-conflict")

    report = _report(candidates, evidence, now=datetime(2026, 7, 1, tzinfo=UTC))
    document = next(item for item in report.items if item.object_id == document_id)

    assert document.authority == "sqlite_document"
    assert document.eligible is False
    assert "inbound_references_present" in document.blockers
    assert any(
        reference.field_path == "$.authority_conflict"
        for reference in document.inbound_references
    )


def test_missing_or_corrupt_backup_blocks_without_mutation(tmp_path: Path) -> None:
    active, store, documents = _runtime(tmp_path)
    source_id = _deleted_source(store)
    candidates = _scan(active=active, store=store, documents=documents)
    missing = build_retention_backup_evidence(
        snapshot_root=None,
        active_vault_root=active,
    )
    missing_report = _report(
        candidates,
        missing,
        now=_DELETED_AT + timedelta(days=31),
    )
    snapshot, _evidence = _verified_backup(tmp_path, active, "retention-corrupt")
    (snapshot.snapshot_root / "payload" / "library" / "marker.txt").write_text(
        "tampered", encoding="utf-8"
    )
    corrupt = build_retention_backup_evidence(
        snapshot_root=snapshot.snapshot_root,
        active_vault_root=active,
    )
    corrupt_report = _report(
        candidates,
        corrupt,
        now=_DELETED_AT + timedelta(days=31),
    )

    assert missing_report.items[0].blockers == ("backup_not_verified",)
    assert corrupt.status == "invalid"
    assert "backup_not_verified" in corrupt_report.items[0].blockers
    assert store.read_including_deleted("sources", source_id) is not None


@pytest.mark.parametrize(
    "evidence",
    (
        RetentionBackupEvidence("verified", None, "same", "same", 1),
        RetentionBackupEvidence("verified", "snapshot", None, "same", 1),
        RetentionBackupEvidence("verified", "snapshot", "same", None, 1),
        RetentionBackupEvidence("verified", "snapshot", "same", "same", 0),
        RetentionBackupEvidence("verified", "snapshot", "same", "same", True),
        RetentionBackupEvidence(
            "verified", "snapshot", "same", "same", 1, "unexpected_error"
        ),
    ),
)
def test_malformed_verified_backup_evidence_fails_closed(
    tmp_path: Path,
    evidence: RetentionBackupEvidence,
) -> None:
    active, store, documents = _runtime(tmp_path)
    _deleted_source(store)
    candidates = _scan(active=active, store=store, documents=documents)

    report = _report(candidates, evidence, now=_DELETED_AT + timedelta(days=31))

    assert report.items[0].eligible is False
    assert "backup_evidence_invalid" in report.items[0].blockers


def test_incomplete_reference_catalog_blocks_all_candidates(tmp_path: Path) -> None:
    active, store, documents = _runtime(tmp_path)
    _deleted_source(store)
    _archived_document(documents)
    candidates = _scan(
        active=active,
        store=store,
        documents=documents,
        complete=False,
    )
    _snapshot, evidence = _verified_backup(tmp_path, active, "retention-incomplete")

    report = _report(candidates, evidence, now=datetime(2026, 7, 1, tzinfo=UTC))

    assert len(report.items) == 2
    assert all("reference_catalog_incomplete" in item.blockers for item in report.items)
    assert not any(item.eligible for item in report.items)


def test_inventory_authority_drift_after_backup_is_blocked(tmp_path: Path) -> None:
    active, store, documents = _runtime(tmp_path)
    _deleted_source(store)
    original_candidates = _scan(active=active, store=store, documents=documents)
    _snapshot, evidence = _verified_backup(tmp_path, active, "retention-drift")
    (active / "library" / "marker.txt").write_text("active-v2", encoding="utf-8")
    drifted_candidates = _scan(active=active, store=store, documents=documents)

    original = _report(
        original_candidates,
        evidence,
        now=_DELETED_AT + timedelta(days=31),
    )
    drifted = _report(
        drifted_candidates,
        evidence,
        now=_DELETED_AT + timedelta(days=31),
    )

    assert original.items[0].eligible is True
    assert drifted.items[0].eligible is False
    assert "inventory_authority_drift" in drifted.items[0].blockers


def test_dry_run_is_replay_stable_serializable_and_read_only(tmp_path: Path) -> None:
    active, store, documents = _runtime(tmp_path)
    source_id = _deleted_source(store)
    candidates = _scan(active=active, store=store, documents=documents)
    _snapshot, evidence = _verified_backup(tmp_path, active, "retention-replay")
    before = fingerprint_vault_root(active)
    now = _DELETED_AT + timedelta(days=31)

    first = _report(candidates, evidence, now=now)
    second = _report(candidates, evidence, now=now)
    payload = serialize_retention_dry_run(first)
    after = fingerprint_vault_root(active)

    assert first == second
    assert first.plan_id == second.plan_id
    assert payload["execution_supported"] is False
    assert payload["approval_token"] is None
    assert before == after
    assert store.read_including_deleted("sources", source_id) is not None


def test_document_retention_policy_cannot_lower_safety_floor() -> None:
    with pytest.raises(ValueError, match="at least 30 days"):
        RetentionPolicy(document_archive_min_age_days=29)
