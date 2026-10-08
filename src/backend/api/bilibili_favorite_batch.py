from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re

from core.storage_provider import ObjectStorePort, ObjectStoreRevisionError


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_SCHEMA_VERSION = "1.1.0"


class BilibiliFavoriteBatchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BilibiliFavoriteBatch:
    payload: Mapping[str, object]
    replayed: bool


class BilibiliFavoriteBatchRepository:
    collection = "bilibili_favorite_batches"

    def __init__(self, object_store: ObjectStorePort, *, namespace_id: str) -> None:
        if not isinstance(namespace_id, str) or not namespace_id:
            raise BilibiliFavoriteBatchError("batch namespace is invalid")
        if getattr(object_store, "namespace_id", None) != namespace_id:
            raise BilibiliFavoriteBatchError("batch namespace mismatch")
        self.object_store = object_store
        self.namespace_id = namespace_id

    def create(
        self,
        *,
        batch_id: str,
        project_id: str,
        snapshot_ref: str,
        snapshot_revision: str,
        items: list[Mapping[str, object]],
        created_at: str,
    ) -> BilibiliFavoriteBatch:
        _identifier(batch_id, "batch_id")
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "kind": "bilibili_favorite_batch",
            "batch_id": batch_id,
            "project_id": project_id,
            "snapshot_ref": snapshot_ref,
            "snapshot_revision": snapshot_revision,
            "status": "pending",
            "admission_status": "not_started",
            "processing_status": "not_started",
            "command": None,
            "revision": 1,
            "created_at": created_at,
            "updated_at": created_at,
            "items": [
                {
                    "ordinal": item["ordinal"],
                    "bvid": item["bvid"],
                    "url": item["url"],
                    "title": item["title"],
                    "state": "pending",
                    "attempts": 0,
                    "manifest_ref": "",
                    "job_id": "",
                    "error": "",
                }
                for item in items
            ],
        }
        normalized = _validate(payload)
        existing = self.get(batch_id)
        if existing is not None:
            if (
                existing.payload.get("project_id") != project_id
                or existing.payload.get("snapshot_ref") != snapshot_ref
                or existing.payload.get("snapshot_revision") != snapshot_revision
            ):
                raise BilibiliFavoriteBatchError("batch request conflicts")
            return BilibiliFavoriteBatch(existing.payload, replayed=True)
        try:
            revision = self.object_store.write(
                self.collection, batch_id, normalized, expected_revision=0
            )
        except ObjectStoreRevisionError as error:
            existing = self.get(batch_id)
            if existing is None:
                raise
            if (
                existing.payload.get("project_id") != project_id
                or existing.payload.get("snapshot_ref") != snapshot_ref
            ):
                raise BilibiliFavoriteBatchError("batch request conflicts") from error
            return BilibiliFavoriteBatch(existing.payload, replayed=True)
        if revision != 1:
            raise BilibiliFavoriteBatchError("batch storage revision is invalid")
        return BilibiliFavoriteBatch(normalized, replayed=False)

    def get(self, batch_id: str) -> BilibiliFavoriteBatch | None:
        _identifier(batch_id, "batch_id")
        payload = self.object_store.read(self.collection, batch_id)
        if payload is None:
            return None
        return BilibiliFavoriteBatch(_validate(payload), replayed=True)

    def save(self, payload: Mapping[str, object]) -> BilibiliFavoriteBatch:
        current = _validate(payload)
        expected_revision = int(current["revision"])
        updated = dict(current)
        updated["revision"] = expected_revision + 1
        try:
            stored_revision = self.object_store.write(
                self.collection,
                str(current["batch_id"]),
                updated,
                expected_revision=expected_revision,
            )
        except ObjectStoreRevisionError as error:
            raise BilibiliFavoriteBatchError("batch revision conflict") from error
        if stored_revision != updated["revision"]:
            raise BilibiliFavoriteBatchError("batch storage revision drifted")
        return BilibiliFavoriteBatch(_validate(updated), replayed=False)

    def list(self, *, project_id: str | None = None) -> tuple[BilibiliFavoriteBatch, ...]:
        """Return durable batch discovery records, newest first.

        This is deliberately a repository read rather than an in-memory queue:
        startup coordination can discover work after the HTTP client disappears.
        """
        records = []
        for raw in self.object_store.list(self.collection):
            item = _validate(raw)
            if project_id is None or item["project_id"] == project_id:
                records.append(BilibiliFavoriteBatch(item, replayed=True))
        return tuple(sorted(records, key=lambda record: (
            str(record.payload["updated_at"]), str(record.payload["batch_id"]),
        ), reverse=True))


def batch_public(payload: Mapping[str, object]) -> dict[str, object]:
    normalized = _validate(payload)
    counts = {key: 0 for key in ("pending", "processing", "admitted", "failed")}
    for item in normalized["items"]:
        counts[str(item["state"])] += 1
    return {
        "batch_id": normalized["batch_id"],
        "project_id": normalized["project_id"],
        "snapshot_ref": normalized["snapshot_ref"],
        "snapshot_revision": normalized["snapshot_revision"],
        "status": normalized["status"],
        # ``status`` is retained for compatibility.  The two explicit values
        # keep admission completion separate from downstream media processing.
        "admission_status": normalized["admission_status"],
        "processing_status": normalized["processing_status"],
        "command": normalized["command"],
        "revision": normalized["revision"],
        "created_at": normalized["created_at"],
        "updated_at": normalized["updated_at"],
        "total": len(normalized["items"]),
        "counts": counts,
        "has_pending": counts["pending"] > 0,
        "has_failed": counts["failed"] > 0,
        "items": normalized["items"],
    }


def _validate(value: Mapping[str, object]) -> dict[str, object]:
    fields = {
        "schema_version", "kind", "batch_id", "project_id", "snapshot_ref",
        "snapshot_revision", "status", "admission_status", "processing_status",
        "command", "revision", "created_at", "updated_at", "items",
    }
    legacy_fields = fields - {"admission_status", "processing_status", "command"}
    if value.get("schema_version") == "1.0.0" and set(value) == legacy_fields:
        # Compatibility reader for frozen batches written before the durable
        # command projection existed.  The next regular save upgrades it.
        legacy = dict(value)
        legacy["schema_version"] = _SCHEMA_VERSION
        legacy["admission_status"] = (
            "complete" if legacy.get("status") in {"completed", "partial_failure"}
            else "not_started"
        )
        legacy["processing_status"] = "not_started"
        legacy["command"] = None
        value = legacy
    if set(value) != fields or value.get("schema_version") != _SCHEMA_VERSION or value.get("kind") != "bilibili_favorite_batch":
        raise BilibiliFavoriteBatchError("batch fields are invalid")
    _identifier(value.get("batch_id"), "batch_id")
    if not isinstance(value.get("project_id"), str) or not value["project_id"]:
        raise BilibiliFavoriteBatchError("batch project is invalid")
    if not isinstance(value.get("snapshot_ref"), str) or not value["snapshot_ref"].startswith("crp://"):
        raise BilibiliFavoriteBatchError("batch snapshot is invalid")
    if value.get("snapshot_revision") != "r1":
        raise BilibiliFavoriteBatchError("batch snapshot revision is invalid")
    revision = value.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise BilibiliFavoriteBatchError("batch revision is invalid")
    if value.get("status") not in {"pending", "running", "completed", "partial_failure"}:
        raise BilibiliFavoriteBatchError("batch status is invalid")
    if value.get("admission_status") not in {
        "not_started", "queued", "running", "awaiting_continue", "complete", "partial_failure", "unknown",
    }:
        raise BilibiliFavoriteBatchError("batch admission status is invalid")
    if value.get("processing_status") not in {
        "not_started", "queued", "running", "complete", "partial_failure", "unknown",
    }:
        raise BilibiliFavoriteBatchError("batch processing status is invalid")
    command = value.get("command")
    if command is not None:
        if not isinstance(command, Mapping) or set(command) != {
            "command_id", "kind", "retry_failed", "operation_id", "submitted_at",
        }:
            raise BilibiliFavoriteBatchError("batch command is invalid")
        _identifier(command.get("command_id"), "batch command id")
        if command.get("kind") not in {"admit", "continue", "retry_failed"}:
            raise BilibiliFavoriteBatchError("batch command kind is invalid")
        if not isinstance(command.get("retry_failed"), bool):
            raise BilibiliFavoriteBatchError("batch command retry flag is invalid")
        if not isinstance(command.get("operation_id"), str) or not command["operation_id"]:
            raise BilibiliFavoriteBatchError("batch command operation is invalid")
        if not isinstance(command.get("submitted_at"), str) or not command["submitted_at"]:
            raise BilibiliFavoriteBatchError("batch command time is invalid")
    items = value.get("items")
    if not isinstance(items, list):
        raise BilibiliFavoriteBatchError("batch items are invalid")
    for ordinal, item in enumerate(items):
        if not isinstance(item, Mapping) or set(item) != {
            "ordinal", "bvid", "url", "title", "state", "attempts",
            "manifest_ref", "job_id", "error",
        }:
            raise BilibiliFavoriteBatchError("batch item is invalid")
        if item.get("ordinal") != ordinal or item.get("state") not in {
            "pending", "processing", "admitted", "failed"
        }:
            raise BilibiliFavoriteBatchError("batch item state is invalid")
        attempts = item.get("attempts")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            raise BilibiliFavoriteBatchError("batch item attempts are invalid")
    return dict(value)


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise BilibiliFavoriteBatchError(f"{label} is invalid")
    return value
