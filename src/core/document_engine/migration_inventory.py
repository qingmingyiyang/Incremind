from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from core.storage_provider import (
    InventoryCollection,
    JsonObjectStoreInventory,
    MigrationRecord,
    ObjectStorePathError,
    SQLiteMigrationLedger,
    read_json_object_store_collection,
)


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_DOCUMENT_COLLECTIONS = (
    "document_markdown",
    "document_revisions",
    "documents",
)


class DocumentMigrationInventoryError(ValueError):
    """Raised when a Document inventory cannot be planned safely."""


@dataclass(frozen=True, slots=True)
class DocumentInventoryIssue:
    code: str
    collection: str
    object_id: str
    document_id: str | None = None
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class DocumentMigrationInventory:
    inventory: JsonObjectStoreInventory
    issues: tuple[DocumentInventoryIssue, ...]


def scan_document_migration_inventory(
    object_store_root: Path,
    *,
    namespace_id: str,
) -> DocumentMigrationInventory:
    """Return a path-free digest and relational health report for Documents."""

    _require_segment("namespace_id", namespace_id)
    namespace_root = object_store_root.expanduser().resolve(strict=False) / "objects" / namespace_id
    records: dict[str, dict[str, dict[str, object]]] = {}
    collections: list[InventoryCollection] = []
    for collection in _DOCUMENT_COLLECTIONS:
        collection_records, collection_inventory = _read_collection(
            namespace_root,
            collection,
        )
        records[collection] = collection_records
        collections.append(collection_inventory)
    ordered_collections = tuple(collections)
    inventory = JsonObjectStoreInventory(
        namespace_id=namespace_id,
        collections=ordered_collections,
        object_count=sum(item.object_count for item in ordered_collections),
        fingerprint=_inventory_fingerprint(namespace_id, ordered_collections),
    )
    issues = _document_issues(
        records["documents"],
        records["document_revisions"],
        records["document_markdown"],
    )
    return DocumentMigrationInventory(inventory=inventory, issues=issues)


def plan_document_migration_dry_run(
    *,
    ledger: SQLiteMigrationLedger,
    migration_id: str,
    target_schema_version: int,
    inventory: DocumentMigrationInventory,
    rollback_pointer: str,
) -> MigrationRecord:
    if inventory.issues:
        codes = ", ".join(sorted({issue.code for issue in inventory.issues}))
        raise DocumentMigrationInventoryError(
            f"document inventory is inconsistent: {codes}"
        )
    return ledger.plan_dry_run(
        migration_id=migration_id,
        target_schema_version=target_schema_version,
        inventory=inventory.inventory,
        rollback_pointer=rollback_pointer,
    )


def _read_collection(
    namespace_root: Path,
    collection: str,
) -> tuple[dict[str, dict[str, object]], InventoryCollection]:
    directory = namespace_root / collection
    records: dict[str, dict[str, object]] = {}
    fingerprint_pairs: list[tuple[str, str]] = []
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise DocumentMigrationInventoryError(
                "document inventory collection root must be a directory"
            )
        for path in directory.iterdir():
            if path.name.endswith(".meta.json"):
                if path.is_symlink() or not path.is_file():
                    raise DocumentMigrationInventoryError(
                        "document inventory metadata entry is invalid"
                    )
                continue
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise DocumentMigrationInventoryError(
                    "document inventory contains an unexpected entry"
                )
    try:
        stored = read_json_object_store_collection(
            namespace_root.parent.parent,
            namespace_id=namespace_root.name,
            collection=collection,
        )
    except ObjectStorePathError as exc:
        raise DocumentMigrationInventoryError(str(exc)) from exc
    for record in stored:
        records[record.object_id] = dict(record.payload)
        fingerprint_pairs.append((f"{record.object_id}.json", _sha256(record.payload_bytes)))
    return records, InventoryCollection(
        collection=collection,
        object_count=len(records),
        fingerprint=_fingerprint_pairs(fingerprint_pairs),
    )


def _document_issues(
    documents: dict[str, dict[str, object]],
    revisions: dict[str, dict[str, object]],
    markdowns: dict[str, dict[str, object]],
) -> tuple[DocumentInventoryIssue, ...]:
    issues: list[DocumentInventoryIssue] = []
    revision_index = _revision_index("document_revisions", revisions, issues)
    markdown_index = _revision_index("document_markdown", markdowns, issues)

    current_revisions: dict[str, int] = {}
    for object_id, document in documents.items():
        document_id = document.get("id")
        if document_id != object_id:
            issues.append(_issue("document_id_mismatch", "documents", object_id))
            continue
        revision = _positive_int(document.get("revision"))
        if revision is None:
            issues.append(_issue("document_revision_invalid", "documents", object_id, object_id))
            continue
        current_revisions[object_id] = revision
        for expected_revision in range(1, revision + 1):
            revision_payload = revision_index.get((object_id, expected_revision))
            markdown_payload = markdown_index.get((object_id, expected_revision))
            revision_object_id = f"{object_id}~r{expected_revision}"
            if revision_payload is None:
                issues.append(
                    _issue(
                        "revision_missing",
                        "document_revisions",
                        revision_object_id,
                        object_id,
                        expected_revision,
                    )
                )
            if markdown_payload is None:
                issues.append(
                    _issue(
                        "markdown_missing",
                        "document_markdown",
                        revision_object_id,
                        object_id,
                        expected_revision,
                    )
                )
            if revision_payload is not None:
                expected_parent = None if expected_revision == 1 else expected_revision - 1
                if revision_payload.get("parent_revision") != expected_parent:
                    issues.append(
                        _issue(
                            "revision_parent_mismatch",
                            "document_revisions",
                            revision_object_id,
                            object_id,
                            expected_revision,
                        )
                    )
            markdown_hash = _markdown_hash(markdown_payload)
            if markdown_payload is not None and markdown_hash is None:
                issues.append(
                    _issue(
                        "markdown_content_hash_mismatch",
                        "document_markdown",
                        revision_object_id,
                        object_id,
                        expected_revision,
                    )
                )
            if revision_payload is not None and markdown_hash is not None:
                if revision_payload.get("new_content_hash") != markdown_hash:
                    issues.append(
                        _issue(
                            "revision_content_hash_mismatch",
                            "document_revisions",
                            revision_object_id,
                            object_id,
                            expected_revision,
                        )
                    )
            if expected_revision == revision and markdown_hash is not None:
                if document.get("content_hash") != markdown_hash:
                    issues.append(
                        _issue(
                            "current_content_hash_mismatch",
                            "documents",
                            object_id,
                            object_id,
                            expected_revision,
                        )
                    )

    for (document_id, revision), payload in revision_index.items():
        if document_id not in current_revisions:
            issues.append(
                _issue("orphan_revision", "document_revisions", _object_id(payload), document_id, revision)
            )
        elif revision > current_revisions[document_id]:
            issues.append(
                _issue("future_revision", "document_revisions", _object_id(payload), document_id, revision)
            )
    for (document_id, revision), payload in markdown_index.items():
        if document_id not in current_revisions:
            issues.append(
                _issue("orphan_markdown", "document_markdown", _object_id(payload), document_id, revision)
            )
        elif revision > current_revisions[document_id]:
            issues.append(
                _issue("future_markdown", "document_markdown", _object_id(payload), document_id, revision)
            )
    return tuple(
        sorted(
            issues,
            key=lambda item: (
                item.code,
                item.document_id or "",
                item.revision or 0,
                item.collection,
                item.object_id,
            ),
        )
    )


def _revision_index(
    collection: str,
    records: dict[str, dict[str, object]],
    issues: list[DocumentInventoryIssue],
) -> dict[tuple[str, int], dict[str, object]]:
    index: dict[tuple[str, int], dict[str, object]] = {}
    for object_id, payload in records.items():
        document_id = payload.get("document_id")
        revision = _positive_int(payload.get("revision"))
        expected_id = (
            f"{document_id}~r{revision}"
            if isinstance(document_id, str) and revision is not None
            else None
        )
        if not isinstance(document_id, str) or not document_id or revision is None:
            issues.append(_issue("revision_identity_invalid", collection, object_id))
            continue
        if expected_id != object_id:
            issues.append(
                _issue("revision_object_id_mismatch", collection, object_id, document_id, revision)
            )
            continue
        index[(document_id, revision)] = payload
    return index


def _markdown_hash(payload: dict[str, object] | None) -> str | None:
    if payload is None:
        return None
    markdown = payload.get("markdown")
    if not isinstance(markdown, str):
        return None
    digest = f"sha256:{hashlib.sha256(markdown.encode('utf-8')).hexdigest()}"
    return digest if payload.get("content_hash") == digest else None


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _object_id(payload: dict[str, object]) -> str:
    document_id = payload.get("document_id")
    revision = payload.get("revision")
    return f"{document_id}~r{revision}"


def _issue(
    code: str,
    collection: str,
    object_id: str,
    document_id: str | None = None,
    revision: int | None = None,
) -> DocumentInventoryIssue:
    return DocumentInventoryIssue(code, collection, object_id, document_id, revision)


def _inventory_fingerprint(
    namespace_id: str,
    collections: tuple[InventoryCollection, ...],
) -> str:
    return _fingerprint_parts(
        (
            namespace_id,
            *(
                f"{item.collection}\0{item.object_count}\0{item.fingerprint}"
                for item in collections
            ),
        )
    )


def _fingerprint_pairs(pairs: list[tuple[str, str]]) -> str:
    return _fingerprint_parts(f"{name}\0{fingerprint}" for name, fingerprint in pairs)


def _fingerprint_parts(parts) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_segment(label: str, value: str) -> None:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise DocumentMigrationInventoryError(f"{label} must be a safe repository segment")
