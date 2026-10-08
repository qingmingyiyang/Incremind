"""Rehearse the offline Document cutover after exact record merge.

Stop every application writer first. This does not move original source files.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from backup_guard import require_backup

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME, TARGET_IDENTITY, AggregateRepositoryFactory,
)
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import (
    AggregateAuthorityEvidence, SQLiteAggregateAuthorityStore, SQLiteStructuredRecordStore,
)


COLLECTIONS = ("documents", "document_revisions", "document_markdown")
MIGRATION_ID = "agent-os-document-union-20260924"
MARKER_ID = "default~documents"
MARKER_COLLECTION = "aggregate_authority_targets"


def rows(path: Path, collection: str) -> dict[str, tuple[str, int]]:
    connection = sqlite3.connect("file:" + path.resolve(strict=True).as_posix() + "?mode=ro", uri=True)
    try:
        return {object_id: (payload_json, revision) for object_id, payload_json, revision in connection.execute(
            "SELECT object_id,payload_json,revision FROM crp_structured_records WHERE collection=?", (collection,)
        )}
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    if args.apply:
        require_backup(args.backup_dir)
    root = args.runtime_root.resolve(strict=True)
    source_path = root / "recognition.sqlite3"
    target_path = root / ".rebuild-data" / "structured-records.sqlite3"
    counts = {}
    for collection in COLLECTIONS:
        source = rows(source_path, collection)
        target = rows(target_path, collection)
        if source != target:
            raise RuntimeError(f"document_collection_not_exact:{collection}")
        if list((root / ".rebuild-data" / "objects" / "default" / collection).glob("*.json")):
            raise RuntimeError(f"old_json_document_records_present:{collection}")
        counts[collection] = len(source)
    count = sum(counts.values())
    evidence = AggregateAuthorityEvidence(
        migration_id=MIGRATION_ID,
        source_fingerprint=None,
        target_fingerprint=None,
        target_identity=TARGET_IDENTITY,
        verification_method="exact_records",
        verified_record_count=count,
    )
    marker = {"namespace_id": "default", "aggregate": "documents",
              "migration_id": MIGRATION_ID, "target_identity": TARGET_IDENTITY,
              "verification_method": "exact_records", "verified_record_count": count,
              "source_collection_counts": counts}
    records = SQLiteStructuredRecordStore(target_path)
    authority = SQLiteAggregateAuthorityStore(root / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    current = authority.get("default", "documents")
    existing_marker = records.read(MARKER_COLLECTION, MARKER_ID)
    if existing_marker is not None and dict(existing_marker.payload) != marker:
        raise RuntimeError("target_marker_conflict")
    if current is not None and current.evidence is not None and current.evidence != evidence:
        raise RuntimeError("authority_evidence_conflict")
    if args.apply:
        if current is None:
            current = authority.create_json_active(namespace_id="default", aggregate="documents",
                                                   reason="old JSON Document collection is empty")
        if current.state == "json_active":
            current = authority.transition(namespace_id="default", aggregate="documents",
                                           expected_revision=current.revision, to_state="sqlite_staged",
                                           evidence=evidence, reason="exact-record merge verified")
        if existing_marker is None:
            with records.begin() as tx:
                tx.put(MARKER_COLLECTION, MARKER_ID, marker, expected_revision=0)
                tx.commit()
        if current.state == "sqlite_staged":
            current = authority.transition(namespace_id="default", aggregate="documents",
                                           expected_revision=current.revision, to_state="sqlite_active",
                                           evidence=evidence, reason="single SQLite Document authority active")
        store, settings = build_rebuild_object_store(root, repository_root=args.repository_root)
        resolved = AggregateRepositoryFactory(root, settings.namespace_id, store).document_repository_resolution()
        if not isinstance(resolved.repository, SQLiteDocumentRepository) or resolved.repository.records.database_path != target_path:
            raise RuntimeError("factory_document_authority_mismatch")
    print(json.dumps({"apply": args.apply, "record_counts": counts,
                      "authority_state": current.state if current else "absent"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
