"""Durable immutable event repository for the Personal Memory World Model."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from core.storage_provider import (
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
    SQLiteUnitOfWorkError,
)

from .models import PersonalWorldModelError, WorldEvent, WorldEventDraft


@dataclass(frozen=True, slots=True)
class WorldEventAppendResult:
    event: WorldEvent
    replayed: bool


class SQLiteWorldEventRepository:
    """Append-only project streams on the existing structured-record UoW.

    The stream cursor is sequencing metadata only.  It does not contain a
    project state snapshot.  The full state remains reproducible from the
    immutable ``world_events`` records.
    """

    events_collection = "world_events"
    streams_collection = "world_event_streams"

    def __init__(self, store: SQLiteStructuredRecordStore) -> None:
        if not isinstance(store, SQLiteStructuredRecordStore):
            raise PersonalWorldModelError("world event repository requires the structured-record store")
        self._store = store

    def append(
        self,
        draft: WorldEventDraft,
        *,
        stream_validator: Callable[[tuple[WorldEvent, ...]], None] | None = None,
    ) -> WorldEventAppendResult:
        if not isinstance(draft, WorldEventDraft):
            raise PersonalWorldModelError("world event draft is invalid")
        try:
            with self._store.begin() as uow:
                existing = uow.read(self.events_collection, draft.event_id)
                if existing is not None:
                    event = WorldEvent.from_record(existing.payload)
                    if event.as_draft().identity_payload() != draft.identity_payload():
                        raise PersonalWorldModelError("world event identity conflicts")
                    uow.rollback()
                    return WorldEventAppendResult(event, True)

                stream = uow.read(self.streams_collection, draft.project_id)
                if stream is None:
                    last_sequence = 0
                    expected_stream_revision = 0
                else:
                    _validate_stream(stream.payload, draft.project_id)
                    last_sequence = int(stream.payload["last_sequence"])
                    expected_stream_revision = stream.revision
                event = WorldEvent(
                    event_id=draft.event_id,
                    project_id=draft.project_id,
                    sequence=last_sequence + 1,
                    kind=draft.kind,
                    actor=draft.actor,
                    source_ref=draft.source_ref,
                    source_revision=draft.source_revision,
                    occurred_at=draft.occurred_at,
                    recorded_at=draft.recorded_at,
                    payload=draft.payload,
                )
                if stream_validator is not None:
                    project_events = tuple(
                        sorted(
                            (
                                *(
                                    WorldEvent.from_record(record.payload)
                                    for record in uow.list(self.events_collection)
                                    if record.payload.get("project_id") == draft.project_id
                                ),
                                event,
                            ),
                            key=lambda item: item.sequence,
                        )
                    )
                    stream_validator(project_events)
                uow.put(
                    self.events_collection,
                    event.event_id,
                    event.to_record(),
                    expected_revision=0,
                )
                uow.put(
                    self.streams_collection,
                    event.project_id,
                    {
                        "schema_version": "1.0.0",
                        "project_id": event.project_id,
                        "last_sequence": event.sequence,
                        "last_event_id": event.event_id,
                        "updated_at": event.recorded_at,
                        "state_projection_persisted": False,
                    },
                    expected_revision=expected_stream_revision,
                )
                uow.commit()
                return WorldEventAppendResult(event, False)
        except PersonalWorldModelError:
            raise
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise PersonalWorldModelError("world event append conflicted") from error

    def get(self, event_id: str) -> WorldEvent | None:
        record = self._store.read(self.events_collection, event_id)
        return None if record is None else WorldEvent.from_record(record.payload)

    def list_project(self, project_id: str) -> tuple[WorldEvent, ...]:
        events = tuple(
            WorldEvent.from_record(record.payload)
            for record in self._store.list(self.events_collection)
            if record.payload.get("project_id") == project_id
        )
        return tuple(sorted(events, key=lambda item: item.sequence))

    def project_ids(self) -> tuple[str, ...]:
        """List durable project streams without projecting mutable state."""

        values: list[str] = []
        for record in self._store.list(self.streams_collection):
            project_id = record.payload.get("project_id")
            if not isinstance(project_id, str) or not project_id:
                raise PersonalWorldModelError("world event stream project is invalid")
            _validate_stream(record.payload, project_id)
            values.append(project_id)
        return tuple(sorted(set(values)))


def _validate_stream(value: object, project_id: str) -> None:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "project_id",
        "last_sequence",
        "last_event_id",
        "updated_at",
        "state_projection_persisted",
    }:
        raise PersonalWorldModelError("world event stream metadata is invalid")
    if (
        value.get("schema_version") != "1.0.0"
        or value.get("project_id") != project_id
        or value.get("state_projection_persisted") is not False
        or not isinstance(value.get("last_sequence"), int)
        or isinstance(value.get("last_sequence"), bool)
        or int(value["last_sequence"]) < 1
        or not isinstance(value.get("last_event_id"), str)
        or not isinstance(value.get("updated_at"), str)
    ):
        raise PersonalWorldModelError("world event stream metadata drifted")
