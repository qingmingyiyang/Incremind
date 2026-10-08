"""Recoverable publication-change handoff for external Agent context sessions.

Publication code adds the outbox record through its *existing* structured-record
Unit of Work.  A separate drainer then hands only privacy-safe metadata to the
AI Turn database.  The two stores deliberately do not pretend to be one
transaction: the immutable publication identity makes post-crash replay safe.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
import re

from core.ai_kernel.external_agent_context import validate_external_agent_safe_projection
from core.ai_kernel.sqlite_store import SQLiteAITurnStore

from .sqlite_uow import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
    SQLiteUnitOfWorkConflict,
)


PUBLICATION_CHANGE_OUTBOX_COLLECTION = "external_agent_publication_outbox"
_COLLECTION_PREFIX = f"{PUBLICATION_CHANGE_OUTBOX_COLLECTION}."
_SCHEMA = "external-agent-publication-outbox-v1"
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_REF = re.compile(r"^crp://[A-Za-z0-9._~-]{1,64}/[A-Za-z0-9._~/-]{1,384}$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:~-]{0,159}$")
_NAMESPACES = {
    "document.published": "documents",
    "memory.published": "memory",
    "memory.invalidated": "memory",
    "project_skill.published": "skills",
    "project_skill.invalidated": "skills",
}
_EVENT_FIELDS = (
    "publication_identity", "project_id", "change_type", "object_ref",
    "object_revision", "occurred_at",
)


class ExternalAgentPublicationChangeError(ValueError):
    """The outbox payload is not a privacy-safe publication change."""


@dataclass(frozen=True, slots=True)
class PublicationChangeDelivery:
    publication_identity: str
    project_id: str
    change_cursor: int
    replayed: bool


class ExternalAgentPublicationChangeOutbox:
    """Publication-owned SQLite outbox with retryable delivery to AI Turn inbox."""

    def __init__(
        self, records: SQLiteStructuredRecordStore,
        turn_store: SQLiteAITurnStore,
        *, now: Callable[[], datetime],
    ) -> None:
        self._records = records
        self._turn_store = turn_store
        self._now = now

    @staticmethod
    def enqueue(
        uow: SQLiteStructuredRecordUnitOfWork,
        *,
        publication_identity: str,
        project_id: str,
        change_type: str,
        object_ref: str,
        object_revision: str,
        occurred_at: str,
    ) -> SQLiteStructuredRecord:
        """Write a pending outbox entry in the caller's publication UoW.

        The caller must invoke this only after its normal authority transition
        has established the referenced publication.  Replaying the exact same
        identity is a no-op; a drift fails before the UoW can commit.
        """
        event = _event(
            publication_identity=publication_identity, project_id=project_id,
            change_type=change_type, object_ref=object_ref,
            object_revision=object_revision, occurred_at=occurred_at,
        )
        collection = publication_outbox_collection(event["project_id"])
        payload = {
            "schema": _SCHEMA, "state": "pending", "event": event,
            "attempt_count": 0, "last_attempt_at": None,
        }
        existing = uow.read(collection, publication_identity)
        if existing is None:
            return uow.put(
                collection, publication_identity, payload,
                expected_revision=0,
            )
        if _outbox_event(existing) != event:
            raise ExternalAgentPublicationChangeError("publication outbox identity conflict")
        return existing

    def pending(self, *, limit: int = 32) -> tuple[SQLiteStructuredRecord, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 128:
            raise ExternalAgentPublicationChangeError("publication outbox limit is invalid")
        candidates: list[tuple[tuple[int, str, str], SQLiteStructuredRecord]] = []
        for record in self._records.list_all():
            if not record.collection.startswith(_COLLECTION_PREFIX):
                continue
            try:
                if _outbox_state(record) == "pending":
                    candidates.append((_pending_sort_key(record), record))
            except ExternalAgentPublicationChangeError:
                # Corrupt historical data is not a queue head-of-line lock.
                continue
        return tuple(record for _key, record in sorted(candidates, key=lambda item: item[0])[:limit])

    def drain(self, *, limit: int = 32) -> tuple[PublicationChangeDelivery, ...]:
        """Deliver pending records; failures stay pending for a later invocation."""
        delivered: list[PublicationChangeDelivery] = []
        for record in self.pending(limit=limit):
            try:
                delivered.append(self.deliver(record))
            except SQLiteUnitOfWorkConflict:
                # Another drainer may have completed the CAS.  Its next pass
                # observes the durable delivered state; never overwrite it.
                continue
            except Exception:
                # The pending record is the recovery contract.  Do not store
                # exception text because an adapter may include sensitive data.
                try:
                    self._mark_attempt(record)
                except (ExternalAgentPublicationChangeError, SQLiteUnitOfWorkConflict):
                    pass
                continue
        return tuple(delivered)

    def deliver(self, record: SQLiteStructuredRecord) -> PublicationChangeDelivery:
        """Deliver one Core-selected outbox record and persist its projection."""
        event = _outbox_event(record)
        receipt = self._turn_store.ingest_external_agent_publication_change(event)
        change = receipt.get("change")
        if not isinstance(change, Mapping) or not isinstance(change.get("cursor"), int):
            raise ExternalAgentPublicationChangeError("publication inbox receipt is invalid")
        self._mark_delivered(record, change_cursor=int(change["cursor"]))
        return PublicationChangeDelivery(
            publication_identity=event["publication_identity"],
            project_id=str(change["project_id"]),
            change_cursor=int(change["cursor"]),
            replayed=bool(receipt.get("replayed")),
        )

    def _mark_delivered(self, record: SQLiteStructuredRecord, *, change_cursor: int) -> SQLiteStructuredRecord:
        if not isinstance(change_cursor, int) or change_cursor < 1:
            raise ExternalAgentPublicationChangeError("publication inbox cursor is invalid")
        payload = dict(record.payload)
        payload["state"] = "delivered"
        payload["change_cursor"] = change_cursor
        payload["delivered_at"] = _timestamp(self._now())
        with self._records.begin() as uow:
            updated = uow.put(
                record.collection, record.object_id, payload,
                expected_revision=record.revision,
            )
            uow.commit()
            return updated

    def _mark_attempt(self, record: SQLiteStructuredRecord) -> SQLiteStructuredRecord:
        payload = dict(record.payload)
        _outbox_event(record)
        if payload.get("state") != "pending":
            raise ExternalAgentPublicationChangeError("publication outbox is not pending")
        attempts = payload.get("attempt_count", 0)
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            raise ExternalAgentPublicationChangeError("publication outbox attempt count is invalid")
        payload["attempt_count"] = attempts + 1
        payload["last_attempt_at"] = _timestamp(self._now())
        with self._records.begin() as uow:
            updated = uow.put(
                record.collection, record.object_id, payload,
                expected_revision=record.revision,
            )
            uow.commit()
            return updated


def _event(**values: str) -> dict[str, str]:
    if set(values) != set(_EVENT_FIELDS):
        raise ExternalAgentPublicationChangeError("publication change event schema is invalid")
    identity = values["publication_identity"]
    if not _IDENTITY.fullmatch(identity):
        raise ExternalAgentPublicationChangeError("publication identity is invalid")
    for field in _EVENT_FIELDS:
        if not isinstance(values[field], str) or not values[field]:
            raise ExternalAgentPublicationChangeError("publication change field is invalid")
    validate_external_agent_safe_projection(values)
    project_id = values["project_id"].strip()
    if not project_id or len(project_id) > 160:
        raise ExternalAgentPublicationChangeError("publication project identity is invalid")
    change_type = values["change_type"]
    namespace = _NAMESPACES.get(change_type)
    if namespace is None:
        raise ExternalAgentPublicationChangeError("publication change type is invalid")
    object_ref = values["object_ref"]
    if not _REF.fullmatch(object_ref) or not object_ref.startswith(f"crp://{namespace}/{project_id}/"):
        raise ExternalAgentPublicationChangeError("publication change ref is invalid")
    if not _REVISION.fullmatch(values["object_revision"]):
        raise ExternalAgentPublicationChangeError("publication change revision is invalid")
    try:
        observed_at = datetime.fromisoformat(values["occurred_at"])
    except ValueError as error:
        raise ExternalAgentPublicationChangeError("publication change time is invalid") from error
    if observed_at.tzinfo is None:
        raise ExternalAgentPublicationChangeError("publication change time is invalid")
    return {field: values[field] for field in _EVENT_FIELDS}


def publication_outbox_collection(project_id: str) -> str:
    """Project-scoped collection preserves the shared 128-character UoW key contract."""
    collection = f"{_COLLECTION_PREFIX}{project_id}"
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}", project_id)
        or len(collection) > 128
    ):
        raise ExternalAgentPublicationChangeError("publication outbox project identity is not a safe segment")
    return collection


def _outbox_event(record: SQLiteStructuredRecord) -> dict[str, str]:
    payload = record.payload
    if payload.get("schema") != _SCHEMA or not isinstance(payload.get("event"), Mapping):
        raise ExternalAgentPublicationChangeError("publication outbox record is invalid")
    event = payload["event"]
    if set(event) != set(_EVENT_FIELDS) or any(not isinstance(event.get(field), str) for field in _EVENT_FIELDS):
        raise ExternalAgentPublicationChangeError("publication outbox event is invalid")
    normalized = _event(**{field: str(event[field]) for field in _EVENT_FIELDS})
    if publication_outbox_collection(normalized["project_id"]) != record.collection:
        raise ExternalAgentPublicationChangeError("publication outbox project scope drifted")
    if normalized["publication_identity"] != record.object_id:
        raise ExternalAgentPublicationChangeError("publication outbox identity drifted")
    return normalized


def _outbox_state(record: SQLiteStructuredRecord) -> str:
    _outbox_event(record)
    state = record.payload.get("state")
    if state not in {"pending", "delivered"}:
        raise ExternalAgentPublicationChangeError("publication outbox state is invalid")
    return state


def _pending_sort_key(record: SQLiteStructuredRecord) -> tuple[int, str, str]:
    payload = record.payload
    attempts = payload.get("attempt_count", 0)
    last_attempt = payload.get("last_attempt_at")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise ExternalAgentPublicationChangeError("publication outbox attempt count is invalid")
    if last_attempt is not None and not isinstance(last_attempt, str):
        raise ExternalAgentPublicationChangeError("publication outbox last attempt is invalid")
    return (attempts, last_attempt or "", record.object_id)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ExternalAgentPublicationChangeError("publication delivery time is invalid")
    return value.isoformat()
