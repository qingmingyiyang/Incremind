from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .runtime import JsonObjectStore, ObjectStoreRevisionError
from .object_locks import source_locked
from .sqlite_uow import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
)


_ASSET_COLLECTION = "workbench_original_assets"
_LINK_COLLECTION = "source_asset_links"
_SQLITE_ASSET_COLLECTION = "original_assets"
_BLOB_COLLECTION = "asset_blobs"
_ROUTED_COLLECTIONS = {_ASSET_COLLECTION, _LINK_COLLECTION}


class SourceAssetRuntimeAuthorityError(ValueError):
    """Raised when a Source Asset operation has no single writable authority."""


class SourceAssetRuntimeStore:
    """Route Source Asset mutations to the one selected compound authority.

    Every collection outside the Source Asset aggregate remains on the legacy
    JSON store.  This keeps the cutover narrow while making existing upload,
    import and retention services authority-aware without duplicating routing
    decisions in each business workflow.
    """

    def __init__(
        self,
        *,
        json_store: JsonObjectStore,
        sqlite_records: SQLiteStructuredRecordStore | None,
        library_root: Path,
        authority_identity: str,
        authority_error: BaseException | None = None,
        publication_records: SQLiteStructuredRecordStore | None = None,
    ) -> None:
        self._json = json_store
        self._records = sqlite_records
        self._publication_records = publication_records
        self._library_root = library_root.expanduser().resolve(strict=False)
        self.authority_identity = authority_identity
        self._authority_error = authority_error

    @property
    def sqlite_active(self) -> bool:
        return self._records is not None

    @property
    def root(self) -> Path:
        return self._json.root

    @property
    def namespace_id(self) -> str:
        return self._json.namespace_id

    def __getattr__(self, name: str) -> Any:
        # Preserve the wider JsonObjectStore operational surface used by
        # lifecycle inventories for all non-routed collections.
        return getattr(self._json, name)

    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None:
        self._guard_source_authority(collection)
        if not self._is_sqlite_collection(collection):
            value = self._json.read(collection, object_id)
            return value if self._source_published(collection, value) else None
        record = self._records_or_raise().read(
            self._sqlite_collection(collection), object_id
        )
        return self._legacy_payload(collection, record) if record is not None else None

    def list(self, collection: str) -> Sequence[Mapping[str, object]]:
        self._guard_source_authority(collection)
        if not self._is_sqlite_collection(collection):
            return tuple(value for value in self._json.list(collection)
                         if self._source_published(collection, value))
        return tuple(
            self._legacy_payload(collection, record)
            for record in self._records_or_raise().list(
                self._sqlite_collection(collection)
            )
        )

    def read_including_deleted(
        self, collection: str, object_id: str
    ) -> Mapping[str, object] | None:
        if self._is_sqlite_collection(collection):
            return self.read(collection, object_id)
        value = self._json.read_including_deleted(collection, object_id)
        return value if self._source_published(collection, value) else None

    def list_including_deleted(
        self, collection: str
    ) -> Sequence[Mapping[str, object]]:
        if self._is_sqlite_collection(collection):
            return self.list(collection)
        return tuple(value for value in self._json.list_including_deleted(collection)
                     if self._source_published(collection, value))

    def _source_published(self, collection: str, value: Mapping[str, object] | None) -> bool:
        if value is None:
            return False
        if collection != "sources" or value.get("identity_method") != "workspace_confirmation":
            return True
        operation_id = value.get("confirmation_operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            return False
        records = self._publication_records if self._publication_records is not None else SQLiteStructuredRecordStore(self._json.root / "structured-records.sqlite3")
        operation = records.read("workspace_confirmation_operations", operation_id)
        return bool(operation is not None and operation.payload.get("state") == "committed"
                    and operation.payload.get("source_id") == value.get("id")
                    and operation.payload.get("workspace_item_id") == value.get("workspace_item_id"))

    def revision(self, collection: str, object_id: str) -> int:
        self._guard_source_authority(collection)
        if not self._is_sqlite_collection(collection):
            return self._json.revision(collection, object_id)
        record = self._records_or_raise().read(
            self._sqlite_collection(collection), object_id
        )
        return record.revision if record is not None else 0

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        if collection != 'sources':
            return self._write(collection, object_id, payload, expected_revision)
        from .source_retrieval_index import source_mutation, refresh_source
        with source_mutation(self._json, object_id) as tx:
            self._guard_source_authority(collection)
            existing = self._json.read_including_deleted(collection, object_id)
            if existing is not None and existing.get('identity_method') == 'workspace_confirmation':
                if dict(existing) != dict(payload):
                    raise SourceAssetRuntimeAuthorityError(
                        'confirmed workspace Source is immutable; edit its Document')
                current = self._json.revision(collection, object_id)
                if expected_revision is not None and current != expected_revision:
                    raise ObjectStoreRevisionError('confirmed Source revision conflicted')
                return current
            revision, incarnation, token = self._json._write_object(
                collection, object_id, payload, expected_revision, source_tx=tx)
        refresh_source(self._json, object_id, revision, incarnation, token)
        return revision

    @source_locked
    def _write(self, collection, object_id, payload, expected_revision):
        self._guard_source_authority(collection)
        if not self._is_sqlite_collection(collection):
            return self._json.write(
                collection, object_id, payload, expected_revision
            )
        if collection == _ASSET_COLLECTION:
            return self._write_asset(object_id, payload, expected_revision)
        return self._write_link(object_id, payload, expected_revision)

    def delete(self, collection: str, object_id: str) -> bool:
        self._guard_source_authority(collection)
        if not self._is_sqlite_collection(collection):
            return self._json.delete(collection, object_id)
        records = self._records_or_raise()
        sqlite_collection = self._sqlite_collection(collection)
        with records.begin() as uow:
            current = uow.read(sqlite_collection, object_id)
            if current is None:
                uow.rollback()
                return False
            sha256 = (
                str(current.payload.get("blob_sha256") or "")
                if collection == _ASSET_COLLECTION
                else ""
            )
            uow.delete(
                sqlite_collection,
                object_id,
                expected_revision=current.revision,
            )
            if collection == _ASSET_COLLECTION and not any(
                item.object_id != object_id
                and item.payload.get("blob_sha256") == sha256
                for item in uow.list(_SQLITE_ASSET_COLLECTION)
            ):
                blob = uow.read(_BLOB_COLLECTION, sha256)
                if blob is not None:
                    uow.delete(
                        _BLOB_COLLECTION,
                        sha256,
                        expected_revision=blob.revision,
                    )
            uow.commit()
        return True

    def is_revision_conflict(self, error: BaseException) -> bool:
        return isinstance(error, (ObjectStoreRevisionError, SQLiteUnitOfWorkConflict))

    def asset_location(
        self,
        *,
        sha256: str,
        legacy_filename: str,
    ) -> tuple[str, Path]:
        """Return the active byte location without probing the inactive layout."""

        if self._authority_error is not None:
            raise SourceAssetRuntimeAuthorityError(str(self._authority_error)) from self._authority_error
        if self._records is None:
            vault_ref = f"assets/originals/{sha256[:2]}/{legacy_filename}"
        else:
            vault_ref = f"assets/blobs/{sha256[:2]}/{sha256}"
        return vault_ref, self._library_root / Path(vault_ref)

    def link_source_asset(
        self,
        *,
        asset_ref: str,
        link_id: str,
        link_payload: Mapping[str, object],
        source_id: str,
    ) -> Mapping[str, object]:
        """Atomically create one link and project its state onto the asset."""

        if self._records is None:
            raise RuntimeError("atomic SQLite link requested while JSON is active")
        records = self._records_or_raise()
        with records.begin() as uow:
            assets = uow.list(_SQLITE_ASSET_COLLECTION)
            current = next(
                (
                    item
                    for item in assets
                    if item.payload.get("legacy_asset_ref") == asset_ref
                ),
                None,
            )
            if current is None:
                raise ValueError("referenced original asset was not found")
            asset = self._legacy_asset_in_uow(uow, current)
            existing_link = uow.read(_LINK_COLLECTION, link_id)
            normalized_link = self._sqlite_link_payload(link_id, link_payload)
            if existing_link is None:
                uow.put(
                    _LINK_COLLECTION,
                    link_id,
                    normalized_link,
                    expected_revision=0,
                )
            elif dict(existing_link.payload) != normalized_link:
                raise ValueError("source asset link identity conflicts")
            updated = dict(asset)
            source_ids = list(updated.get("linked_source_ids") or [])
            if source_id not in source_ids:
                source_ids.append(source_id)
            updated.update(
                {
                    "linked_source_ids": source_ids,
                    "link_status": "linked",
                    "orphan_reason": None,
                    "orphaned_at": None,
                }
            )
            uow.put(
                _SQLITE_ASSET_COLLECTION,
                current.object_id,
                self._sqlite_asset_payload(updated),
                expected_revision=current.revision,
            )
            uow.commit()
        return updated

    def _legacy_asset_in_uow(self, uow, record: SQLiteStructuredRecord) -> dict[str, object]:
        value = record.payload
        runtime = value.get("runtime_payload")
        if isinstance(runtime, Mapping):
            result = dict(runtime)
        else:
            metadata = dict(value.get("metadata") or {})
            linked_source_ids = [
                str(link.payload.get("source_id") or "")
                for link in uow.list(_LINK_COLLECTION)
                if link.payload.get("asset_id") == record.object_id
            ]
            result = {
                "schema_version": "1.0.0",
                "id": value.get("legacy_asset_id", record.object_id),
                "kind": "workbench_original_asset",
                "status": "stored",
                "asset_ref": value.get("legacy_asset_ref"),
                "display_name": metadata.get("original_filename", ""),
                "media_type": metadata.get(
                    "media_type", "application/octet-stream"
                ),
                "byte_count": value.get("byte_count"),
                "sha256": value.get("blob_sha256"),
                "storage_mode": "stored_original",
                "availability": "available",
                "availability_reason": "source_asset_sqlite_authority",
                "link_status": "linked" if linked_source_ids else "orphaned",
                "linked_source_ids": linked_source_ids,
                "orphan_reason": (
                    None if linked_source_ids else "no_active_source_asset_links"
                ),
                "metadata": metadata,
            }
        blob = uow.read(_BLOB_COLLECTION, str(value.get("blob_sha256") or ""))
        if blob is None:
            raise ValueError("Source Asset SQLite asset references a missing blob")
        result["vault_ref"] = blob.payload.get("canonical_vault_ref")
        return result

    def _write_asset(
        self,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        records = self._records_or_raise()
        normalized = self._sqlite_asset_payload(payload)
        sha256 = str(normalized["blob_sha256"])
        with records.begin() as uow:
            current = uow.read(_SQLITE_ASSET_COLLECTION, object_id)
            expected = self._expected_revision(current, expected_revision)
            if (
                current is not None
                and current.payload.get("blob_sha256") != sha256
            ):
                raise ValueError("original asset blob identity cannot change")
            blob = uow.read(_BLOB_COLLECTION, sha256)
            canonical = f"assets/blobs/{sha256[:2]}/{sha256}"
            legacy_ref = str(normalized["legacy_vault_ref"])
            if blob is None:
                uow.put(
                    _BLOB_COLLECTION,
                    sha256,
                    {
                        "schema_version": "1.0.0",
                        "sha256": sha256,
                        "canonical_vault_ref": canonical,
                        "legacy_vault_refs": [legacy_ref],
                    },
                    expected_revision=0,
                )
            else:
                blob_payload = dict(blob.payload)
                if blob_payload.get("canonical_vault_ref") != canonical:
                    raise ValueError("Source Asset canonical blob reference drifted")
                refs = list(blob_payload.get("legacy_vault_refs") or [])
                if legacy_ref not in refs:
                    refs.append(legacy_ref)
                    blob_payload["legacy_vault_refs"] = sorted(refs)
                    uow.put(
                        _BLOB_COLLECTION,
                        sha256,
                        blob_payload,
                        expected_revision=blob.revision,
                    )
            written = uow.put(
                _SQLITE_ASSET_COLLECTION,
                object_id,
                normalized,
                expected_revision=expected,
            )
            uow.commit()
        return written.revision

    def _write_link(
        self,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        records = self._records_or_raise()
        with records.begin() as uow:
            current = uow.read(_LINK_COLLECTION, object_id)
            expected = self._expected_revision(current, expected_revision)
            written = uow.put(
                _LINK_COLLECTION,
                object_id,
                self._sqlite_link_payload(object_id, payload),
                expected_revision=expected,
            )
            uow.commit()
        return written.revision

    @staticmethod
    def _expected_revision(
        current: SQLiteStructuredRecord | None, expected_revision: int | None
    ) -> int:
        return (
            current.revision
            if expected_revision is None and current is not None
            else (0 if expected_revision is None else expected_revision)
        )

    @staticmethod
    def _sqlite_asset_payload(payload: Mapping[str, object]) -> dict[str, object]:
        asset_id = str(payload.get("id") or "")
        asset_ref = str(payload.get("asset_ref") or "")
        sha256 = str(payload.get("sha256") or "")
        vault_ref = str(payload.get("vault_ref") or "")
        byte_count = payload.get("byte_count")
        if not asset_id or not asset_ref or len(sha256) != 64 or not vault_ref:
            raise ValueError("Source Asset runtime asset identity is invalid")
        if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
            raise ValueError("Source Asset runtime byte_count is invalid")
        return {
            "schema_version": "1.1.0",
            "legacy_asset_id": asset_id,
            "legacy_asset_ref": asset_ref,
            "legacy_vault_ref": vault_ref,
            "blob_sha256": sha256,
            "byte_count": byte_count,
            "metadata": dict(payload.get("metadata") or {}),
            "runtime_payload": dict(payload),
        }

    @staticmethod
    def _sqlite_link_payload(
        object_id: str, payload: Mapping[str, object]
    ) -> dict[str, object]:
        return {
            "schema_version": "1.1.0",
            "legacy_link_id": object_id,
            "source_id": str(payload.get("source_id") or ""),
            "source_uri": str(payload.get("source_uri") or ""),
            "asset_id": str(payload.get("asset_id") or ""),
            "asset_ref": str(payload.get("asset_ref") or ""),
            "content_hash": str(payload.get("content_hash") or ""),
            "size_bytes": payload.get("size_bytes"),
            "role": str(payload.get("role") or ""),
            "created_at": str(payload.get("created_at") or ""),
            "provenance": str(payload.get("provenance") or ""),
        }

    def _legacy_payload(
        self, collection: str, record: SQLiteStructuredRecord
    ) -> Mapping[str, object]:
        value = record.payload
        if collection == _LINK_COLLECTION:
            return {
                "schema_version": value.get("schema_version", "1.0.0"),
                "id": value.get("legacy_link_id", record.object_id),
                "source_id": value.get("source_id"),
                "source_uri": value.get("source_uri", ""),
                "asset_id": value.get("asset_id"),
                "asset_ref": value.get("asset_ref"),
                "content_hash": value.get("content_hash"),
                "size_bytes": value.get("size_bytes"),
                "role": value.get("role"),
                "created_at": value.get("created_at", ""),
                "provenance": value.get("provenance"),
            }
        runtime = value.get("runtime_payload")
        if isinstance(runtime, Mapping):
            result = dict(runtime)
        else:
            metadata = dict(value.get("metadata") or {})
            linked_source_ids = [
                str(link.payload.get("source_id") or "")
                for link in self._records_or_raise().list(_LINK_COLLECTION)
                if link.payload.get("asset_id") == record.object_id
            ]
            result = {
                "schema_version": "1.0.0",
                "id": value.get("legacy_asset_id", record.object_id),
                "kind": "workbench_original_asset",
                "status": "stored",
                "asset_ref": value.get("legacy_asset_ref"),
                "display_name": metadata.get("original_filename", ""),
                "media_type": metadata.get(
                    "media_type", "application/octet-stream"
                ),
                "byte_count": value.get("byte_count"),
                "sha256": value.get("blob_sha256"),
                "storage_mode": "stored_original",
                "availability": "available",
                "availability_reason": "source_asset_sqlite_authority",
                "link_status": "linked" if linked_source_ids else "orphaned",
                "linked_source_ids": linked_source_ids,
                "orphan_reason": (
                    None if linked_source_ids else "no_active_source_asset_links"
                ),
                "metadata": metadata,
            }
        blob = self._records_or_raise().read(
            _BLOB_COLLECTION, str(value.get("blob_sha256") or "")
        )
        if blob is None:
            raise ValueError("Source Asset SQLite asset references a missing blob")
        result["vault_ref"] = blob.payload.get("canonical_vault_ref")
        return result

    def _is_sqlite_collection(self, collection: str) -> bool:
        return self._records is not None and collection in _ROUTED_COLLECTIONS

    def _guard_source_authority(self, collection: str) -> None:
        if collection in _ROUTED_COLLECTIONS and self._authority_error is not None:
            raise SourceAssetRuntimeAuthorityError(str(self._authority_error)) from self._authority_error

    @staticmethod
    def _sqlite_collection(collection: str) -> str:
        return (
            _SQLITE_ASSET_COLLECTION
            if collection == _ASSET_COLLECTION
            else collection
        )

    def _records_or_raise(self) -> SQLiteStructuredRecordStore:
        if self._records is None:  # pragma: no cover - guarded by callers
            raise RuntimeError("Source Asset SQLite authority is inactive")
        return self._records
