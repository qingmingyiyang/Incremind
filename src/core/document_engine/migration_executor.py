from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.storage_provider import (
    MigrationRecord,
    SQLiteMigrationLedger,
    SQLiteStructuredRecordStore,
)

from .migration_inventory import (
    _read_collection,
    scan_document_migration_inventory,
)
from .sqlite_runtime import SQLiteDocumentRepository


class DocumentMigrationExecutionError(ValueError):
    """Raised when a temporary Document fixture migration cannot finish safely."""


@dataclass(frozen=True, slots=True)
class DocumentMigrationExecutionResult:
    document_count: int
    object_count: int
    input_fingerprint: str


def execute_document_fixture_migration(
    *,
    object_store_root: Path,
    target_database_path: Path,
    ledger: SQLiteMigrationLedger,
    dry_run: MigrationRecord,
) -> DocumentMigrationExecutionResult:
    """Copy a verified JSON fixture into a newly-created SQLite target."""

    source_root = object_store_root.expanduser().resolve(strict=False)
    target = target_database_path.expanduser().resolve(strict=False)
    _require_target_outside_source(source_root, target)
    artifacts = _sqlite_artifacts(target)
    if any(path.exists() for path in artifacts):
        raise DocumentMigrationExecutionError(
            "document migration target SQLite artifacts already exist"
        )
    namespace_id = dry_run.inventory.namespace_id
    current = scan_document_migration_inventory(
        object_store_root,
        namespace_id=namespace_id,
    )
    _validate_dry_run(ledger, dry_run, current)
    source_records = _source_records(object_store_root, namespace_id)
    repeated = scan_document_migration_inventory(
        object_store_root,
        namespace_id=namespace_id,
    )
    if repeated != current:
        raise DocumentMigrationExecutionError(
            "document migration source changed while preparing copy"
        )

    records = SQLiteStructuredRecordStore(target)
    try:
        with records.begin() as uow:
            for collection in sorted(source_records):
                for object_id, payload in sorted(source_records[collection].items()):
                    uow.put(
                        collection,
                        object_id,
                        payload,
                        expected_revision=0,
                    )
            uow.commit()
        _validate_target(records, source_records)
        return DocumentMigrationExecutionResult(
            document_count=len(source_records["documents"]),
            object_count=sum(len(items) for items in source_records.values()),
            input_fingerprint=current.inventory.fingerprint,
        )
    except Exception as exc:
        _remove_new_target(artifacts)
        if isinstance(exc, DocumentMigrationExecutionError):
            raise
        raise DocumentMigrationExecutionError(str(exc)) from exc


def _validate_dry_run(
    ledger: SQLiteMigrationLedger,
    dry_run: MigrationRecord,
    current,
) -> None:
    records = {record.migration_id: record for record in ledger.list_records()}
    if records.get(dry_run.migration_id) != dry_run or dry_run.state != "dry_run_ready":
        raise DocumentMigrationExecutionError(
            "document migration dry-run is not ready"
        )
    if current.issues:
        codes = ", ".join(sorted({issue.code for issue in current.issues}))
        raise DocumentMigrationExecutionError(
            f"document migration inventory is inconsistent: {codes}"
        )
    if dry_run.input_fingerprint != current.inventory.fingerprint:
        raise DocumentMigrationExecutionError(
            "document migration input fingerprint changed"
        )


def _source_records(
    object_store_root: Path,
    namespace_id: str,
) -> dict[str, dict[str, dict[str, object]]]:
    namespace_root = object_store_root.expanduser().resolve(strict=False) / "objects" / namespace_id
    return {
        collection: _read_collection(namespace_root, collection)[0]
        for collection in (
            "document_markdown",
            "document_revisions",
            "documents",
        )
    }


def _validate_target(
    records: SQLiteStructuredRecordStore,
    source_records: dict[str, dict[str, dict[str, object]]],
) -> None:
    for collection, objects in source_records.items():
        target_objects = {record.object_id: record for record in records.list(collection)}
        if set(target_objects) != set(objects):
            raise DocumentMigrationExecutionError(
                f"document migration target {collection} object set mismatch"
            )
        for object_id, payload in objects.items():
            target = target_objects[object_id]
            if target.revision != 1 or dict(target.payload) != payload:
                raise DocumentMigrationExecutionError(
                    f"document migration target {collection}/{object_id} mismatch"
                )

    repository = SQLiteDocumentRepository(records)
    for document_id, source in source_records["documents"].items():
        if repository.read(document_id) != source:
            raise DocumentMigrationExecutionError(
                f"document migration current read mismatch: {document_id}"
            )
        revision = source.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise DocumentMigrationExecutionError(
                f"document migration current revision invalid: {document_id}"
            )
        if len(repository.revisions(document_id)) != revision:
            raise DocumentMigrationExecutionError(
                f"document migration revision count mismatch: {document_id}"
            )
        for expected_revision in range(1, revision + 1):
            if repository.revision(document_id, expected_revision) is None:
                raise DocumentMigrationExecutionError(
                    f"document migration revision read missing: {document_id} r{expected_revision}"
                )
            if repository.markdown(document_id, revision=expected_revision) is None:
                raise DocumentMigrationExecutionError(
                    f"document migration Markdown read missing: {document_id} r{expected_revision}"
                )


def _sqlite_artifacts(database_path: Path) -> tuple[Path, ...]:
    return (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
    )


def _require_target_outside_source(source_root: Path, target: Path) -> None:
    try:
        target.relative_to(source_root)
    except ValueError:
        return
    raise DocumentMigrationExecutionError(
        "document migration target cannot be inside source root"
    )


def _remove_new_target(artifacts: tuple[Path, ...]) -> None:
    for path in reversed(artifacts):
        path.unlink(missing_ok=True)
