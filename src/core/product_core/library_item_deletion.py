from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable
from uuid import uuid4

from .ports import ObjectStorePort


_SOURCE_LIFECYCLE_KEY = "library_lifecycle"
_UNDO_WINDOW = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class LibraryItemDeletionResult:
    status: str
    item_type: str
    item_id: str
    deleted_collections: tuple[str, ...]
    deleted_count: int
    error: str | None
    operation_id: str | None = None
    revision: int | None = None
    undo_expires_at: str | None = None


# 通用删除只拥有 Source lifecycle。Document 使用 authority-aware archive，
# Candidate 使用 review reject，正式 Memory 使用 revisioned rollback。
_SUPPORTED_ITEM_TYPES = ("source",)

# 各类型对应的 collection 名称（atom 类型可能分布在多个 memory collection 中，
# 因为 Library Overview 的 _memory_layer 在无法确定层级时默认返回 "atom"）
class DeleteLibraryItem:
    """Soft-delete Source records without guessing another aggregate authority."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        clock: Callable[[], datetime] | None = None,
        operation_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = object_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._operation_id_factory = operation_id_factory or (lambda: f"library-delete-{uuid4().hex}")

    def execute(self, *, item_type: str, item_id: str) -> LibraryItemDeletionResult:
        clean_type = (item_type or "").strip().lower()
        clean_id = (item_id or "").strip()
        if not clean_type or not clean_id:
            return LibraryItemDeletionResult(
                status="rejected",
                item_type=clean_type,
                item_id=clean_id,
                deleted_collections=(),
                deleted_count=0,
                error="item_type and item_id are required",
            )
        if clean_type not in _SUPPORTED_ITEM_TYPES:
            return LibraryItemDeletionResult(
                status="unsupported",
                item_type=clean_type,
                item_id=clean_id,
                deleted_collections=(),
                deleted_count=0,
                error=f"item_type '{clean_type}' is not supported for deletion; supported: {', '.join(_SUPPORTED_ITEM_TYPES)}",
            )

        return self._soft_delete_source(clean_id)

    def _soft_delete_source(self, clean_id: str) -> LibraryItemDeletionResult:
        source = self._store.read_including_deleted("sources", clean_id)
        if not isinstance(source, Mapping):
            return LibraryItemDeletionResult(
                status="not_found", item_type="source", item_id=clean_id,
                deleted_collections=(), deleted_count=0, error="item not found in any collection",
            )
        lifecycle = source.get(_SOURCE_LIFECYCLE_KEY)
        if isinstance(lifecycle, Mapping) and lifecycle.get("status") == "deleted":
            return LibraryItemDeletionResult(
                status="deleted", item_type="source", item_id=clean_id,
                deleted_collections=("sources",), deleted_count=1, error=None,
                operation_id=_optional_str(lifecycle.get("operation_id")),
                revision=self._store.revision("sources", clean_id),
                undo_expires_at=_optional_str(lifecycle.get("undo_expires_at")),
            )
        now = self._clock().astimezone(UTC)
        operation_id = self._operation_id_factory()
        expected_revision = self._store.revision("sources", clean_id)
        updated = dict(source)
        updated[_SOURCE_LIFECYCLE_KEY] = {
            "status": "deleted",
            "operation_id": operation_id,
            "deleted_at": _iso(now),
            "undo_expires_at": _iso(now + _UNDO_WINDOW),
            "restored_at": None,
        }
        revision = self._store.write("sources", clean_id, updated, expected_revision=expected_revision)
        return LibraryItemDeletionResult(
            status="deleted", item_type="source", item_id=clean_id,
            deleted_collections=("sources",), deleted_count=1, error=None,
            operation_id=operation_id, revision=revision,
            undo_expires_at=_iso(now + _UNDO_WINDOW),
        )

def serialize_library_item_deletion_result(result: LibraryItemDeletionResult) -> dict[str, object]:
    return {
        "status": result.status,
        "item_type": result.item_type,
        "item_id": result.item_id,
        "deleted_collections": list(result.deleted_collections),
        "deleted_count": result.deleted_count,
        "error": result.error,
        "operation_id": result.operation_id,
        "revision": result.revision,
        "undo_expires_at": result.undo_expires_at,
    }


class UndoLibraryItemDeletion:
    def __init__(self, object_store: ObjectStorePort, *, clock: Callable[[], datetime] | None = None) -> None:
        self._store = object_store
        self._clock = clock or (lambda: datetime.now(UTC))

    def execute(
        self,
        *,
        item_type: str,
        item_id: str,
        operation_id: str,
        expected_revision: int,
    ) -> LibraryItemDeletionResult:
        clean_type = (item_type or "").strip().lower()
        clean_id = (item_id or "").strip()
        clean_operation = (operation_id or "").strip()
        if clean_type != "source" or not clean_id or not clean_operation or expected_revision < 1:
            return LibraryItemDeletionResult(
                status="rejected", item_type=clean_type, item_id=clean_id,
                deleted_collections=(), deleted_count=0, error="source undo requires item_id, operation_id and expected_revision",
            )
        source = self._store.read_including_deleted("sources", clean_id)
        if not isinstance(source, Mapping):
            return LibraryItemDeletionResult(
                status="not_found", item_type=clean_type, item_id=clean_id,
                deleted_collections=(), deleted_count=0, error="source not found",
            )
        lifecycle = source.get(_SOURCE_LIFECYCLE_KEY)
        if not isinstance(lifecycle, Mapping) or lifecycle.get("operation_id") != clean_operation:
            return LibraryItemDeletionResult(
                status="conflict", item_type=clean_type, item_id=clean_id,
                deleted_collections=(), deleted_count=0, error="delete operation does not match current source lifecycle",
            )
        current_revision = self._store.revision("sources", clean_id)
        if lifecycle.get("status") == "active":
            return LibraryItemDeletionResult(
                status="restored", item_type=clean_type, item_id=clean_id,
                deleted_collections=("sources",), deleted_count=1, error=None,
                operation_id=clean_operation, revision=current_revision,
                undo_expires_at=_optional_str(lifecycle.get("undo_expires_at")),
            )
        if lifecycle.get("status") != "deleted" or current_revision != expected_revision:
            return LibraryItemDeletionResult(
                status="conflict", item_type=clean_type, item_id=clean_id,
                deleted_collections=(), deleted_count=0, error="source lifecycle revision is stale",
            )
        expires_at = _parse_datetime(lifecycle.get("undo_expires_at"))
        if expires_at is None or self._clock().astimezone(UTC) > expires_at:
            return LibraryItemDeletionResult(
                status="expired", item_type=clean_type, item_id=clean_id,
                deleted_collections=(), deleted_count=0, error="source undo window expired",
            )
        updated = dict(source)
        updated[_SOURCE_LIFECYCLE_KEY] = {**dict(lifecycle), "status": "active", "restored_at": _iso(self._clock())}
        revision = self._store.write("sources", clean_id, updated, expected_revision=expected_revision)
        return LibraryItemDeletionResult(
            status="restored", item_type=clean_type, item_id=clean_id,
            deleted_collections=("sources",), deleted_count=1, error=None,
            operation_id=clean_operation, revision=revision,
            undo_expires_at=_optional_str(lifecycle.get("undo_expires_at")),
        )


def source_is_deleted(source: Mapping[str, object]) -> bool:
    lifecycle = source.get(_SOURCE_LIFECYCLE_KEY)
    return isinstance(lifecycle, Mapping) and lifecycle.get("status") == "deleted"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
