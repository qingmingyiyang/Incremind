from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from core.product_core.retention import (
    RetentionBackupEvidence,
    RetentionCandidate,
    RetentionRecord,
    RetentionReference,
)

from core.storage_provider.runtime import (
    JsonObjectStore,
    read_json_object_store_collection,
)
from core.storage_provider.vault_backup_restore import (
    VaultBackupRestoreError,
    compare_vault_to_backup,
    fingerprint_vault_root,
)


_SOURCE_OWNED_COLLECTIONS = {
    "sources",
    "source_content_reads",
    "source_structures",
    "source_series_assignments",
    "jobs",
    "source_outputs",
    "source_asset_links",
    "media_processing_jobs",
    "media_processing_outputs",
    "audio_asset_refs",
    "tag_index",
    "assets",
}
_DOCUMENT_OWNED_COLLECTIONS = {
    "documents",
    "document_revisions",
    "document_markdown",
}


class RetentionInventoryError(ValueError):
    """Raised when a read-only retention inventory cannot be trusted."""


@dataclass(frozen=True, slots=True)
class RetentionExternalRecord:
    authority: str
    collection: str
    object_id: str
    payload: Mapping[str, object]
    revision: int | None = None


class VaultRetentionInventory:
    """Scan lifecycle candidates without exposing a physical deletion operation."""

    def __init__(
        self,
        *,
        active_vault_root: Path,
        source_store: JsonObjectStore,
        document_repository: object,
        document_authority: str,
        additional_records: Sequence[RetentionExternalRecord] = (),
        reference_catalog_complete: bool,
    ) -> None:
        if document_authority not in {"json_document", "sqlite_document"}:
            raise RetentionInventoryError("Document authority must be explicit")
        self._active_vault_root = active_vault_root
        self._source_store = source_store
        self._documents = document_repository
        self._document_authority = document_authority
        self._additional = tuple(additional_records)
        self._catalog_complete = reference_catalog_complete

    def scan(self) -> tuple[RetentionCandidate, ...]:
        json_records = self._json_records()
        documents, document_complete = self._document_records()
        all_records = (*json_records, *documents, *self._additional)
        observed = fingerprint_vault_root(self._active_vault_root)
        candidates: list[RetentionCandidate] = []
        for record in json_records:
            if record.collection != "sources":
                continue
            lifecycle = record.payload.get("library_lifecycle")
            if not isinstance(lifecycle, Mapping) or lifecycle.get("status") != "deleted":
                continue
            candidates.append(
                self._source_candidate(
                    record,
                    lifecycle=lifecycle,
                    records=all_records,
                    observed_fingerprint=observed,
                    inventory_complete=self._catalog_complete and document_complete,
                )
            )
        for record in documents:
            if record.collection != "documents" or record.payload.get("status") != "archived":
                continue
            candidates.append(
                self._document_candidate(
                    record,
                    records=all_records,
                    observed_fingerprint=observed,
                    inventory_complete=self._catalog_complete and document_complete,
                )
            )
        return tuple(sorted(candidates, key=lambda item: (item.aggregate_type, item.object_id)))

    def _json_records(self) -> tuple[RetentionExternalRecord, ...]:
        records: list[RetentionExternalRecord] = []
        for collection in self._source_store.collection_names():
            for item in read_json_object_store_collection(
                self._source_store.root,
                namespace_id=self._source_store.namespace_id,
                collection=collection,
            ):
                records.append(
                    RetentionExternalRecord(
                        authority=(
                            "json_document"
                            if self._document_authority == "json_document"
                            and collection in _DOCUMENT_OWNED_COLLECTIONS
                            else "json_object_store"
                        ),
                        collection=collection,
                        object_id=item.object_id,
                        payload=dict(item.payload),
                        revision=self._source_store.revision(collection, item.object_id),
                    )
                )
        return tuple(records)

    def _document_records(self) -> tuple[tuple[RetentionExternalRecord, ...], bool]:
        list_documents = getattr(self._documents, "list", None)
        revisions = getattr(self._documents, "revisions", None)
        markdown = getattr(self._documents, "markdown", None)
        if not callable(list_documents):
            raise RetentionInventoryError("active Document repository cannot list archived documents")
        complete = callable(revisions) and callable(markdown)
        records: list[RetentionExternalRecord] = []
        for payload in list_documents(include_archived=True):
            if not isinstance(payload, Mapping):
                raise RetentionInventoryError("Document inventory contains an invalid payload")
            object_id = _required_str(payload, "id")
            revision = _required_revision(payload.get("revision"))
            records.append(
                RetentionExternalRecord(
                    authority=self._document_authority,
                    collection="documents",
                    object_id=object_id,
                    payload=dict(payload),
                    revision=revision,
                )
            )
            if not complete:
                continue
            for revision_payload in revisions(object_id):
                if not isinstance(revision_payload, Mapping):
                    raise RetentionInventoryError("Document revision inventory is invalid")
                revision_number = _required_revision(revision_payload.get("revision"))
                revision_id = _required_str(revision_payload, "id")
                records.append(
                    RetentionExternalRecord(
                        authority=self._document_authority,
                        collection="document_revisions",
                        object_id=revision_id,
                        payload=dict(revision_payload),
                        revision=revision_number,
                    )
                )
                markdown_payload = markdown(object_id, revision=revision_number)
                if not isinstance(markdown_payload, str):
                    complete = False
                    continue
                records.append(
                    RetentionExternalRecord(
                        authority=self._document_authority,
                        collection="document_markdown",
                        object_id=f"{object_id}~r{revision_number}",
                        payload={
                            "document_id": object_id,
                            "revision": revision_number,
                            "markdown_present": True,
                        },
                        revision=revision_number,
                    )
                )
        return tuple(records), complete

    def _source_candidate(
        self,
        record: RetentionExternalRecord,
        *,
        lifecycle: Mapping[str, object],
        records: Sequence[RetentionExternalRecord],
        observed_fingerprint: str,
        inventory_complete: bool,
    ) -> RetentionCandidate:
        owned: list[RetentionRecord] = []
        inbound: list[RetentionReference] = []
        for other in records:
            paths = _reference_paths(other.payload, key="source_id", value=record.object_id)
            is_self = other.authority == record.authority and other.collection == "sources" and other.object_id == record.object_id
            if is_self or (other.collection in _SOURCE_OWNED_COLLECTIONS and paths):
                owned.append(_owned(other))
            elif paths:
                inbound.extend(_references(other, paths))
        return RetentionCandidate(
            aggregate_type="source",
            object_id=record.object_id,
            authority="json_object_store",
            revision=_required_revision(record.revision),
            lifecycle_status=str(lifecycle.get("status", "")),
            lifecycle_at=_optional_str(lifecycle.get("deleted_at")),
            undo_expires_at=_optional_str(lifecycle.get("undo_expires_at")),
            observed_vault_fingerprint=observed_fingerprint,
            inventory_complete=inventory_complete,
            owned_records=_dedupe_records(owned),
            inbound_references=_dedupe_references(inbound),
        )

    def _document_candidate(
        self,
        record: RetentionExternalRecord,
        *,
        records: Sequence[RetentionExternalRecord],
        observed_fingerprint: str,
        inventory_complete: bool,
    ) -> RetentionCandidate:
        owned: list[RetentionRecord] = []
        inbound: list[RetentionReference] = []
        for other in records:
            paths = _reference_paths(other.payload, key="document_id", value=record.object_id)
            same_authority_owned = (
                other.authority == record.authority
                and other.collection in _DOCUMENT_OWNED_COLLECTIONS
                and (other.object_id == record.object_id or bool(paths))
            )
            if same_authority_owned:
                owned.append(_owned(other))
            elif (
                other.collection in _DOCUMENT_OWNED_COLLECTIONS
                and other.object_id == record.object_id
                and other.authority != record.authority
            ):
                inbound.append(
                    RetentionReference(
                        authority=other.authority,
                        collection=other.collection,
                        object_id=other.object_id,
                        field_path="$.authority_conflict",
                    )
                )
            elif paths:
                inbound.extend(_references(other, paths))
        return RetentionCandidate(
            aggregate_type="document",
            object_id=record.object_id,
            authority=self._document_authority,
            revision=_required_revision(record.revision),
            lifecycle_status=str(record.payload.get("status", "")),
            lifecycle_at=_optional_str(record.payload.get("updated_at")),
            undo_expires_at=None,
            observed_vault_fingerprint=observed_fingerprint,
            inventory_complete=inventory_complete,
            owned_records=_dedupe_records(owned),
            inbound_references=_dedupe_references(inbound),
        )


def build_retention_backup_evidence(
    *, snapshot_root: Path | None, active_vault_root: Path
) -> RetentionBackupEvidence:
    if snapshot_root is None:
        return RetentionBackupEvidence("missing", None, None, None, None, "snapshot_missing")
    try:
        result = compare_vault_to_backup(
            snapshot_root=snapshot_root,
            vault_root=active_vault_root,
        )
    except VaultBackupRestoreError as error:
        return RetentionBackupEvidence(
            "invalid",
            None,
            None,
            _safe_active_fingerprint(active_vault_root),
            None,
            type(error).__name__,
        )
    return RetentionBackupEvidence(
        status="verified",
        snapshot_id=result.snapshot_id,
        snapshot_fingerprint=result.source_fingerprint,
        active_fingerprint=result.source_fingerprint,
        file_count=result.file_count,
    )


def _safe_active_fingerprint(active_vault_root: Path) -> str | None:
    try:
        return fingerprint_vault_root(active_vault_root)
    except VaultBackupRestoreError:
        return None


def _reference_paths(payload: object, *, key: str, value: str, prefix: str = "$") -> tuple[str, ...]:
    paths: list[str] = []
    if isinstance(payload, Mapping):
        for field, child in payload.items():
            path = f"{prefix}.{field}"
            if field == key and child == value:
                paths.append(path)
            paths.extend(_reference_paths(child, key=key, value=value, prefix=path))
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for index, child in enumerate(payload):
            paths.extend(
                _reference_paths(child, key=key, value=value, prefix=f"{prefix}[{index}]")
            )
    return tuple(paths)


def _owned(record: RetentionExternalRecord) -> RetentionRecord:
    return RetentionRecord(
        authority=record.authority,
        collection=record.collection,
        object_id=record.object_id,
        revision=record.revision,
    )


def _references(
    record: RetentionExternalRecord, paths: Sequence[str]
) -> tuple[RetentionReference, ...]:
    return tuple(
        RetentionReference(
            authority=record.authority,
            collection=record.collection,
            object_id=record.object_id,
            field_path=path,
        )
        for path in paths
    )


def _dedupe_records(records: Sequence[RetentionRecord]) -> tuple[RetentionRecord, ...]:
    unique = {
        (record.authority, record.collection, record.object_id): record
        for record in records
    }
    return tuple(unique[key] for key in sorted(unique))


def _dedupe_references(
    references: Sequence[RetentionReference],
) -> tuple[RetentionReference, ...]:
    unique = {
        (
            reference.authority,
            reference.collection,
            reference.object_id,
            reference.field_path,
        ): reference
        for reference in references
    }
    return tuple(unique[key] for key in sorted(unique))


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RetentionInventoryError(f"retention inventory requires {key}")
    return value


def _required_revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RetentionInventoryError("retention inventory requires a positive revision")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
