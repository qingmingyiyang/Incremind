"""Select the recognition record store after the old Document cutover."""

from __future__ import annotations

from pathlib import Path

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.aggregate_repository_factory import AggregateRepositoryFactory, TARGET_IDENTITY
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import AggregateAuthorityEvidence, SQLiteAggregateAuthorityStore, SQLiteStructuredRecordStore


_EMPTY_MIGRATION = "agent-os-empty-document-authority-v1"


def _activate_empty_document_authority(root: Path, namespace_id: str, old_documents: object) -> None:
    """Start a never-used vault with the shared Document authority."""
    rebuild_root = root / ".rebuild-data"
    authority = SQLiteAggregateAuthorityStore(rebuild_root / "aggregate-authority.sqlite3")
    current = authority.get(namespace_id, "documents")
    if current is not None and (current.evidence is None or current.evidence.migration_id != _EMPTY_MIGRATION):
        return
    if (root / "recognition.sqlite3").exists() or old_documents.list(include_archived=True):
        return
    records = SQLiteStructuredRecordStore(rebuild_root / "structured-records.sqlite3")
    if any(records.list(collection) for collection in ("documents", "document_revisions", "document_markdown")):
        return
    evidence = AggregateAuthorityEvidence(
        migration_id=_EMPTY_MIGRATION, source_fingerprint=None, target_fingerprint=None,
        target_identity=TARGET_IDENTITY, verification_method="exact_records", verified_record_count=0,
    )
    if current is None:
        current = authority.create_json_active(namespace_id=namespace_id, aggregate="documents",
                                               reason="new vault has no Document records")
    if current.state == "json_active":
        current = authority.transition(namespace_id=namespace_id, aggregate="documents",
                                       expected_revision=current.revision, to_state="sqlite_staged",
                                       evidence=evidence, reason="empty stores verified")
    marker_id = f"{namespace_id}~documents"
    marker = records.read("aggregate_authority_targets", marker_id)
    if marker is None:
        with records.begin() as tx:
            tx.put("aggregate_authority_targets", marker_id, {
                "namespace_id": namespace_id, "aggregate": "documents", "migration_id": _EMPTY_MIGRATION,
                "target_identity": TARGET_IDENTITY, "verification_method": "exact_records",
                "verified_record_count": 0,
            }, expected_revision=0)
            tx.commit()
    if current.state == "sqlite_staged":
        authority.transition(namespace_id=namespace_id, aggregate="documents",
                             expected_revision=current.revision, to_state="sqlite_active",
                             evidence=evidence, reason="empty shared authority active")


def resolve_recognition_document_store(runtime_root: Path) -> tuple[SQLiteStructuredRecordStore, str]:
    """Use one SQLite Document authority after verified integration activation.

    Existing installations continue using their recognition database until an
    explicit offline migration is activated. A populated old Document authority
    cannot silently coexist with that legacy writer.
    """

    root = Path(runtime_root).resolve()
    store, settings = build_rebuild_object_store(root)
    factory = AggregateRepositoryFactory(
        runtime_root=root, namespace_id=settings.namespace_id, json_store=store
    )
    initial = factory.document_repository_resolution()
    _activate_empty_document_authority(root, settings.namespace_id, initial.repository)
    resolution = factory.document_repository_resolution()
    authority_path = root / ".rebuild-data" / "aggregate-authority.sqlite3"
    authority = (SQLiteAggregateAuthorityStore(authority_path).get(settings.namespace_id, "documents")
                 if authority_path.exists() else None)
    if isinstance(resolution.repository, SQLiteDocumentRepository):
        evidence = authority.evidence if authority is not None else None
        if evidence is None or getattr(evidence, "verification_method", "fingerprint") != "exact_records":
            raise RuntimeError("recognition_document_cutover_evidence_required")
        return resolution.repository.records, settings.namespace_id

    if resolution.repository.list(include_archived=True):
        raise RuntimeError("recognition_document_cutover_required_for_old_documents")
    return SQLiteStructuredRecordStore(root / "recognition.sqlite3"), "recognition"
