from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .ports import ObjectStorePort


class MemoryLifecycleError(ValueError):
    pass


class MemoryLifecycleConflict(MemoryLifecycleError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryLifecycleResult:
    action: str
    layer: str
    object_id: str
    previous_revision: int
    revision: int
    status: str
    invalidation_id: str
    invalidated_refs: tuple[str, ...]
    affected_consumers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MemoryBatchItem:
    layer: str
    object_id: str
    expected_revision: int
    expected_storage_revision: int


@dataclass(frozen=True, slots=True)
class MemoryBatchPreview:
    project_id: str
    items: tuple[MemoryBatchItem, ...]
    reason: str
    occurred_at: str
    preview_token: str


_COLLECTIONS = {
    "atom": "memory_atoms",
    "scenario": "memory_scenarios",
    "series_memory": "memory_series_memory",
}
_SAFE_REASON = re.compile(r"^.{1,500}$", re.DOTALL)
_PROTECTED = {
    "id", "project_id", "revision", "lifecycle_status", "supersedes_ref",
    "superseded_at", "redacted_at", "redact_mode", "redact_reason",
}


class GovernedMemoryLifecycle:
    """Revision, redaction and reverse-invalidation authority for published Memory.

    The immutable audit history is separate from the current readable head.  A
    hard redaction deliberately removes every historical payload owned by this
    service and keeps only a metadata tombstone.
    """

    def __init__(
        self,
        store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        change_sink: Callable[[Mapping[str, object]], object] | None = None,
    ) -> None:
        self._store = store
        self._namespace_id = namespace_id
        self._change_sink = change_sink

    def supersede(
        self,
        *,
        layer: str,
        object_id: str,
        project_id: str,
        expected_revision: int,
        expected_storage_revision: int,
        changes: Mapping[str, object],
        reason: str,
        occurred_at: str,
        recorded_at: str,
        confirm: bool,
    ) -> MemoryLifecycleResult:
        if confirm is not True:
            raise MemoryLifecycleError("memory supersede requires confirm=true")
        current, collection = self._current(layer, object_id)
        self._assert_project(current, project_id)
        self._assert_active(current)
        self._assert_revision(current, expected_revision)
        clean_reason = _reason(reason)
        patch = dict(changes)
        if not patch or _PROTECTED.intersection(patch):
            raise MemoryLifecycleError("memory supersede changes are invalid")
        self._assert_storage_revision(collection, object_id, expected_storage_revision)
        self._archive(layer, current, action="supersede", recorded_at=recorded_at)
        updated = {
            **current,
            **patch,
            "revision": expected_revision + 1,
            "lifecycle_status": "active",
            "supersedes_ref": self._memory_ref(current, layer=layer),
            "superseded_at": recorded_at,
            "supersede_reason": clean_reason,
            "occurred_at": occurred_at,
            "recorded_at": recorded_at,
            "updated_at": recorded_at,
        }
        self._store.write(collection, object_id, updated, expected_revision=expected_storage_revision)
        return self._invalidate(
            action="superseded", layer=layer, project_id=project_id, previous=current, current=updated,
            reason=clean_reason, occurred_at=recorded_at,
        )

    def redact(
        self,
        *,
        layer: str,
        object_id: str,
        project_id: str,
        expected_revision: int,
        expected_storage_revision: int,
        reason: str,
        mode: str,
        occurred_at: str,
        confirm: bool,
        hard_confirmation: str | None = None,
    ) -> MemoryLifecycleResult:
        if confirm is not True:
            raise MemoryLifecycleError("memory redact requires confirm=true")
        if mode not in {"soft", "hard"}:
            raise MemoryLifecycleError("memory redact mode is invalid")
        if mode == "hard" and hard_confirmation != f"DELETE {object_id}":
            raise MemoryLifecycleError("hard redact confirmation is invalid")
        current, collection = self._current(layer, object_id)
        self._assert_project(current, project_id)
        self._assert_active(current)
        self._assert_revision(current, expected_revision)
        self._assert_storage_revision(collection, object_id, expected_storage_revision)
        clean_reason = _reason(reason)
        previous_ref = self._memory_ref(current, layer=layer)
        if mode == "soft":
            self._archive(layer, current, action="soft-redact-undo", recorded_at=occurred_at)
            updated = {
                **current,
                "revision": expected_revision + 1,
                "lifecycle_status": "redacted",
                "redacted_at": occurred_at,
                "redact_mode": "soft",
                "redact_reason": clean_reason,
                "updated_at": occurred_at,
            }
        else:
            self._purge_history(layer=layer, object_id=object_id)
            updated = {
                "schema_version": str(current.get("schema_version", "1.0.0")),
                "id": object_id,
                "project_id": project_id,
                "revision": expected_revision + 1,
                "lifecycle_status": "redacted",
                "redacted_at": occurred_at,
                "redact_mode": "hard",
                "redact_reason": clean_reason,
                "previous_ref": previous_ref,
                "payload_digest": _payload_digest(current),
                "updated_at": occurred_at,
            }
        self._store.write(collection, object_id, updated, expected_revision=expected_storage_revision)
        return self._invalidate(
            action=f"{mode}_redacted", layer=layer, project_id=project_id, previous=current, current=updated,
            reason=clean_reason, occurred_at=occurred_at,
        )

    def restore_soft_redaction(
        self,
        *,
        layer: str,
        object_id: str,
        project_id: str,
        expected_revision: int,
        expected_storage_revision: int,
        occurred_at: str,
        confirm: bool,
    ) -> MemoryLifecycleResult:
        if confirm is not True:
            raise MemoryLifecycleError("memory restore requires confirm=true")
        current, collection = self._current(layer, object_id)
        self._assert_project(current, project_id)
        if current.get("lifecycle_status") != "redacted" or current.get("redact_mode") != "soft":
            raise MemoryLifecycleError("memory is not soft redacted")
        self._assert_revision(current, expected_revision)
        self._assert_storage_revision(collection, object_id, expected_storage_revision)
        history = self._history(layer=layer, object_id=object_id)
        undo = next((item for item in reversed(history) if item.get("history_action") == "soft-redact-undo"), None)
        if undo is None or not isinstance(undo.get("payload"), Mapping):
            raise MemoryLifecycleError("soft redaction undo snapshot is unavailable")
        restored = {
            **dict(undo["payload"]),
            "revision": expected_revision + 1,
            "lifecycle_status": "active",
            "restored_at": occurred_at,
            "updated_at": occurred_at,
        }
        self._store.write(collection, object_id, restored, expected_revision=expected_storage_revision)
        return self._invalidate(
            action="restored", layer=layer, project_id=project_id, previous=current, current=restored,
            reason="user restored soft-redacted memory", occurred_at=occurred_at,
        )

    def register_lineage(
        self,
        *,
        project_id: str,
        memory_ref: str,
        consumer_kind: str,
        consumer_ref: str,
        recorded_at: str,
    ) -> str:
        if consumer_kind not in {"compaction", "document", "external_context"}:
            raise MemoryLifecycleError("memory lineage consumer kind is invalid")
        identity = _stable_id("memory-lineage", project_id, memory_ref, consumer_kind, consumer_ref)
        payload = {
            "schema_version": "1.0.0", "id": identity, "project_id": project_id,
            "memory_ref": memory_ref, "consumer_kind": consumer_kind,
            "consumer_ref": consumer_ref, "status": "active", "recorded_at": recorded_at,
        }
        existing = self._store.read("memory_lineage_refs", identity)
        if existing is not None and dict(existing) != payload:
            raise MemoryLifecycleConflict("memory lineage identity drifted")
        if existing is None:
            self._store.write("memory_lineage_refs", identity, payload, expected_revision=0)
        return identity

    def preview_batch_soft_redact(
        self,
        *,
        project_id: str,
        items: Sequence[MemoryBatchItem],
        reason: str,
        occurred_at: str,
    ) -> MemoryBatchPreview:
        """Validate a bounded batch without writing any lifecycle state."""

        if not items or len(items) > 100:
            raise MemoryLifecycleError("memory batch must contain 1 to 100 items")
        clean_reason = _reason(reason)
        canonical = tuple(sorted(items, key=lambda item: (item.layer, item.object_id)))
        if len({(item.layer, item.object_id) for item in canonical}) != len(canonical):
            raise MemoryLifecycleError("memory batch contains duplicate items")
        for item in canonical:
            current, collection = self._current(item.layer, item.object_id)
            self._assert_project(current, project_id)
            self._assert_active(current)
            self._assert_revision(current, item.expected_revision)
            self._assert_storage_revision(collection, item.object_id, item.expected_storage_revision)
        token_payload = {
            "project_id": project_id,
            "items": [
                [item.layer, item.object_id, item.expected_revision, item.expected_storage_revision]
                for item in canonical
            ],
            "reason": clean_reason,
            "occurred_at": occurred_at,
        }
        token = hashlib.sha256(
            json.dumps(token_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return MemoryBatchPreview(project_id, canonical, clean_reason, occurred_at, token)

    def confirm_batch_soft_redact(
        self,
        *,
        preview: MemoryBatchPreview,
        expected_preview_token: str,
        confirm: bool,
    ) -> Mapping[str, object]:
        """Apply a previewed batch with a durable, replayable progress record."""

        if confirm is not True:
            raise MemoryLifecycleError("memory batch redact requires confirm=true")
        if preview.preview_token != expected_preview_token:
            raise MemoryLifecycleConflict("memory batch preview drifted")
        operation_id = f"memory-batch-{preview.preview_token[:20]}"
        operation = self._store.read("memory_lifecycle_batches", operation_id)
        if operation is None:
            operation = {
                "schema_version": "1.0.0", "id": operation_id, "action": "soft_redact",
                "project_id": preview.project_id, "preview_token": preview.preview_token,
                "reason": preview.reason, "occurred_at": preview.occurred_at,
                "status": "applying", "completed": [],
                "items": [
                    {
                        "layer": item.layer, "object_id": item.object_id,
                        "expected_revision": item.expected_revision,
                        "expected_storage_revision": item.expected_storage_revision,
                    }
                    for item in preview.items
                ],
            }
            self._store.write("memory_lifecycle_batches", operation_id, operation, expected_revision=0)
        elif operation.get("preview_token") != preview.preview_token:
            raise MemoryLifecycleConflict("memory batch operation drifted")
        if operation.get("status") in {"completed", "undone"}:
            return dict(operation)

        completed = set(str(item) for item in operation.get("completed", []))
        for item in preview.items:
            identity = f"{item.layer}:{item.object_id}"
            if identity in completed:
                continue
            current, collection = self._current(item.layer, item.object_id)
            already_applied = (
                current.get("revision") == item.expected_revision + 1
                and current.get("lifecycle_status") == "redacted"
                and current.get("redact_mode") == "soft"
                and current.get("redact_reason") == preview.reason
            )
            if not already_applied:
                self.redact(
                    layer=item.layer, object_id=item.object_id, project_id=preview.project_id,
                    expected_revision=item.expected_revision,
                    expected_storage_revision=item.expected_storage_revision,
                    reason=preview.reason, mode="soft", occurred_at=preview.occurred_at, confirm=True,
                )
            completed.add(identity)
            operation = {**dict(operation), "completed": sorted(completed)}
            self._store.write(
                "memory_lifecycle_batches", operation_id, operation,
                expected_revision=self._store.revision("memory_lifecycle_batches", operation_id),
            )
        operation = {**dict(operation), "status": "completed", "completed_at": preview.occurred_at}
        self._store.write(
            "memory_lifecycle_batches", operation_id, operation,
            expected_revision=self._store.revision("memory_lifecycle_batches", operation_id),
        )
        return operation

    def undo_batch_soft_redact(
        self,
        *,
        operation_id: str,
        occurred_at: str,
        confirm: bool,
    ) -> Mapping[str, object]:
        if confirm is not True:
            raise MemoryLifecycleError("memory batch undo requires confirm=true")
        operation = self._store.read("memory_lifecycle_batches", operation_id)
        if operation is None or operation.get("action") != "soft_redact":
            raise MemoryLifecycleError("memory batch operation was not found")
        if operation.get("status") == "undone":
            return dict(operation)
        if operation.get("status") not in {"completed", "undoing"}:
            raise MemoryLifecycleConflict("memory batch is not ready for undo")
        operation = {
            **dict(operation), "status": "undoing", "undo_started_at": occurred_at,
        }
        self._store.write(
            "memory_lifecycle_batches", operation_id, operation,
            expected_revision=self._store.revision("memory_lifecycle_batches", operation_id),
        )
        for raw in operation["items"]:
            item = MemoryBatchItem(
                str(raw["layer"]), str(raw["object_id"]),
                int(raw["expected_revision"]), int(raw["expected_storage_revision"]),
            )
            current, collection = self._current(item.layer, item.object_id)
            if current.get("lifecycle_status") == "active" and current.get("revision") == item.expected_revision + 2:
                continue
            self.restore_soft_redaction(
                layer=item.layer, object_id=item.object_id, project_id=str(operation["project_id"]),
                expected_revision=item.expected_revision + 1,
                expected_storage_revision=self._store.revision(collection, item.object_id),
                occurred_at=occurred_at, confirm=True,
            )
        operation = {**dict(operation), "status": "undone", "undone_at": occurred_at}
        self._store.write(
            "memory_lifecycle_batches", operation_id, operation,
            expected_revision=self._store.revision("memory_lifecycle_batches", operation_id),
        )
        return operation

    def resume_batch_soft_redact(
        self,
        *,
        operation_id: str,
        confirm: bool,
    ) -> Mapping[str, object]:
        if confirm is not True:
            raise MemoryLifecycleError("memory batch resume requires confirm=true")
        operation = self._store.read("memory_lifecycle_batches", operation_id)
        if operation is None or operation.get("action") != "soft_redact":
            raise MemoryLifecycleError("memory batch operation was not found")
        if operation.get("status") in {"completed", "undone"}:
            return dict(operation)
        if operation.get("status") == "undoing":
            return self.undo_batch_soft_redact(
                operation_id=operation_id,
                occurred_at=str(operation.get("undo_started_at") or operation["occurred_at"]),
                confirm=True,
            )
        raw_items = operation.get("items")
        if not isinstance(raw_items, list):
            raise MemoryLifecycleConflict("memory batch operation items drifted")
        try:
            items = tuple(
                MemoryBatchItem(
                    str(item["layer"]), str(item["object_id"]),
                    int(item["expected_revision"]), int(item["expected_storage_revision"]),
                )
                for item in raw_items if isinstance(item, Mapping)
            )
            preview = MemoryBatchPreview(
                str(operation["project_id"]), items, str(operation["reason"]),
                str(operation["occurred_at"]), str(operation["preview_token"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise MemoryLifecycleConflict("memory batch operation drifted") from error
        if len(items) != len(raw_items):
            raise MemoryLifecycleConflict("memory batch operation items drifted")
        return self.confirm_batch_soft_redact(
            preview=preview,
            expected_preview_token=preview.preview_token,
            confirm=True,
        )

    def _invalidate(
        self, *, action: str, layer: str, project_id: str, previous: Mapping[str, object],
        current: Mapping[str, object], reason: str, occurred_at: str,
    ) -> MemoryLifecycleResult:
        old_ref = self._memory_ref(previous, layer=layer)
        new_ref = self._memory_ref(current, layer=layer)
        affected: list[str] = []
        for lineage in self._store.list("memory_lineage_refs"):
            if lineage.get("project_id") != project_id or lineage.get("memory_ref") != old_ref:
                continue
            if lineage.get("status") != "active":
                continue
            lineage_id = _required_text(lineage, "id")
            changed = {
                **dict(lineage), "status": "stale", "invalidated_at": occurred_at,
                "replacement_ref": new_ref, "invalidation_action": action,
            }
            self._store.write(
                "memory_lineage_refs", lineage_id, changed,
                expected_revision=self._store.revision("memory_lineage_refs", lineage_id),
            )
            affected.append(_required_text(lineage, "consumer_ref"))
        invalidation_id = _stable_id("memory-invalidation", project_id, old_ref, new_ref, action)
        invalidation = {
            "schema_version": "1.0.0", "id": invalidation_id, "project_id": project_id,
            "action": action, "invalidated_refs": [old_ref], "replacement_ref": new_ref,
            "affected_consumers": sorted(affected), "reason": reason, "occurred_at": occurred_at,
            "external_event": {
                "publication_identity": invalidation_id,
                "project_id": project_id,
                "change_type": "memory.invalidated",
                "object_ref": f"crp://memory/{project_id}/{previous['id']}",
                "object_revision": f"r{current['revision']}",
                "occurred_at": occurred_at,
            },
        }
        if self._store.read("memory_invalidations", invalidation_id) is None:
            self._store.write("memory_invalidations", invalidation_id, invalidation, expected_revision=0)
        if self._change_sink is not None:
            self._change_sink(dict(invalidation["external_event"]))
        return MemoryLifecycleResult(
            action=action, layer=layer, object_id=_required_text(previous, "id"),
            previous_revision=int(previous["revision"]), revision=int(current["revision"]),
            status=str(current["lifecycle_status"]), invalidation_id=invalidation_id,
            invalidated_refs=(old_ref,), affected_consumers=tuple(sorted(affected)),
        )

    def _archive(self, layer: str, payload: Mapping[str, object], *, action: str, recorded_at: str) -> None:
        identity = _stable_id("memory-history", layer, _required_text(payload, "id"), str(payload["revision"]))
        archive = {
            "schema_version": "1.0.0", "id": identity, "layer": layer,
            "object_id": payload["id"], "revision": payload["revision"],
            "history_action": action, "recorded_at": recorded_at, "payload": dict(payload),
        }
        existing = self._store.read("memory_revision_history", identity)
        if existing is not None and dict(existing) != archive:
            raise MemoryLifecycleConflict("memory revision history drifted")
        if existing is None:
            self._store.write("memory_revision_history", identity, archive, expected_revision=0)

    def _purge_history(self, *, layer: str, object_id: str) -> None:
        for item in self._history(layer=layer, object_id=object_id):
            self._store.delete("memory_revision_history", _required_text(item, "id"))

    def _history(self, *, layer: str, object_id: str) -> list[Mapping[str, object]]:
        return sorted(
            (item for item in self._store.list("memory_revision_history")
             if item.get("layer") == layer and item.get("object_id") == object_id),
            key=lambda item: int(item.get("revision", 0)),
        )

    def _current(self, layer: str, object_id: str) -> tuple[Mapping[str, object], str]:
        collection = _COLLECTIONS.get(layer)
        if collection is None:
            raise MemoryLifecycleError("memory layer is invalid")
        current = self._store.read(collection, object_id)
        if current is None:
            raise MemoryLifecycleError("published memory was not found")
        revision = current.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise MemoryLifecycleError("memory revision is invalid")
        return current, collection

    def _assert_storage_revision(self, collection: str, object_id: str, expected: int) -> None:
        if self._store.revision(collection, object_id) != expected:
            raise MemoryLifecycleConflict("memory storage revision drifted")

    @staticmethod
    def _assert_revision(current: Mapping[str, object], expected: int) -> None:
        if current.get("revision") != expected:
            raise MemoryLifecycleConflict("memory revision drifted")

    @staticmethod
    def _assert_active(current: Mapping[str, object]) -> None:
        if current.get("lifecycle_status", "active") != "active":
            raise MemoryLifecycleConflict("memory is not active")

    @staticmethod
    def _assert_project(current: Mapping[str, object], project_id: str) -> None:
        if not isinstance(project_id, str) or not project_id:
            raise MemoryLifecycleError("memory project_id is invalid")
        stored = current.get("project_id")
        if stored is not None and stored != project_id:
            raise MemoryLifecycleConflict("memory project scope drifted")

    def _memory_ref(self, payload: Mapping[str, object], *, layer: str) -> str:
        return f"crp://{self._namespace_id}/memory/{layer}/{payload['id']}@r{payload['revision']}"


def memory_is_readable(payload: Mapping[str, object]) -> bool:
    return payload.get("lifecycle_status", "active") == "active"


def _payload_digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise MemoryLifecycleError(f"memory {key} is invalid")
    return value


def _reason(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_REASON.fullmatch(value.strip()):
        raise MemoryLifecycleError("memory lifecycle reason is invalid")
    return value.strip()
