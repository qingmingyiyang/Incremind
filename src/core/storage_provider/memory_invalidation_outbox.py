from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .external_agent_publication_change import ExternalAgentPublicationChangeOutbox
from .runtime import JsonObjectStore
from .sqlite_uow import SQLiteStructuredRecordStore


class MemoryInvalidationOutboxError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryInvalidationDispatchResult:
    scanned: int
    enqueued: int
    replayed: int
    failed: int


def dispatch_memory_invalidations(
    store: JsonObjectStore,
    records: SQLiteStructuredRecordStore,
    *,
    limit: int = 100,
) -> MemoryInvalidationDispatchResult:
    """Copy durable JSON invalidations into the existing SQLite outbox.

    The two stores cannot share a transaction.  The invalidation is written
    first; deterministic publication identity makes a crash and replay safe.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise MemoryInvalidationOutboxError("memory invalidation dispatch limit is invalid")
    pending = [
        item for item in store.list("memory_invalidations")
        if item.get("outbox_state", "pending") == "pending"
    ][:limit]
    enqueued = replayed = failed = 0
    for candidate in pending:
        try:
            invalidation_id = _required(candidate, "id")
            event = candidate.get("external_event")
            if not isinstance(event, Mapping):
                raise MemoryInvalidationOutboxError("memory invalidation event is missing")
            with records.begin() as uow:
                record = ExternalAgentPublicationChangeOutbox.enqueue(
                    uow,
                    publication_identity=_required(event, "publication_identity"),
                    project_id=_required(event, "project_id"),
                    change_type=_required(event, "change_type"),
                    object_ref=_required(event, "object_ref"),
                    object_revision=_required(event, "object_revision"),
                    occurred_at=_required(event, "occurred_at"),
                )
                uow.commit()
            updated = {
                **dict(candidate), "outbox_state": "enqueued",
                "outbox_record_revision": record.revision,
            }
            store.write(
                "memory_invalidations", invalidation_id, updated,
                expected_revision=store.revision("memory_invalidations", invalidation_id),
            )
            if record.revision == 1:
                enqueued += 1
            else:
                replayed += 1
        except Exception:  # durable candidate remains pending for a later bounded pass
            failed += 1
    return MemoryInvalidationDispatchResult(
        scanned=len(pending), enqueued=enqueued, replayed=replayed, failed=failed,
    )


def _required(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise MemoryInvalidationOutboxError(f"memory invalidation {key} is invalid")
    return value
