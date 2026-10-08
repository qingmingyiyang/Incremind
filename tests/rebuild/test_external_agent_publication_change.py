from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

import pytest

from core.ai_kernel import SQLiteAITurnStore, TurnStateConflict
from core.storage_provider import SQLiteStructuredRecordStore
from core.storage_provider.external_agent_publication_change import (
    ExternalAgentPublicationChangeError,
    ExternalAgentPublicationChangeOutbox,
    publication_outbox_collection,
)


NOW = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)


def _event(**overrides: str) -> dict[str, str]:
    event = {
        "publication_identity": "memory-publication-001",
        "project_id": "project-a",
        "change_type": "memory.published",
        "object_ref": "crp://memory/project-a/memory-001",
        "object_revision": "r1",
        "occurred_at": "2026-08-29T08:00:00+00:00",
    }
    event.update(overrides)
    return event


def _outbox(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "publications.sqlite3")
    turns = SQLiteAITurnStore(tmp_path / "ai-turns.sqlite3")
    return records, turns, ExternalAgentPublicationChangeOutbox(records, turns, now=lambda: NOW)


def _enqueue(records: SQLiteStructuredRecordStore, **overrides: str) -> None:
    with records.begin() as uow:
        ExternalAgentPublicationChangeOutbox.enqueue(uow, **_event(**overrides))
        uow.commit()


def test_publication_outbox_delivers_only_privacy_safe_metadata_and_marks_cas_delivered(tmp_path) -> None:
    records, turns, outbox = _outbox(tmp_path)
    _enqueue(records)

    delivered = outbox.drain()

    assert len(delivered) == 1
    assert delivered[0].publication_identity == "memory-publication-001"
    assert delivered[0].project_id == "project-a"
    assert delivered[0].change_cursor == 1
    assert delivered[0].replayed is False
    assert turns.project_events_after("project-a") == ({
        "cursor": 1,
        "change_type": "memory.published",
        "object_ref": "crp://memory/project-a/memory-001",
        "object_revision": "r1",
        "occurred_at": "2026-08-29T08:00:00+00:00",
    },)
    record = records.read(
        publication_outbox_collection("project-a"),
        "memory-publication-001",
    )
    assert record is not None
    assert record.payload["state"] == "delivered"
    assert record.payload["change_cursor"] == 1
    assert outbox.drain() == ()


def test_post_delivery_crash_recovers_through_inbox_idempotency(tmp_path, monkeypatch) -> None:
    records, turns, outbox = _outbox(tmp_path)
    _enqueue(records)

    def crash(*_args, **_kwargs):
        raise OSError("simulated process stop")

    monkeypatch.setattr(outbox, "_mark_delivered", crash)
    assert outbox.drain() == ()
    assert turns.project_event_cursor("project-a") == 1
    pending = records.read(
        publication_outbox_collection("project-a"),
        "memory-publication-001",
    )
    assert pending is not None and pending.payload["state"] == "pending"

    restarted = ExternalAgentPublicationChangeOutbox(records, turns, now=lambda: NOW)
    delivered = restarted.drain()
    assert len(delivered) == 1
    assert delivered[0].replayed is True
    assert turns.project_event_cursor("project-a") == 1
    completed = records.read(
        publication_outbox_collection("project-a"),
        "memory-publication-001",
    )
    assert completed is not None and completed.payload["state"] == "delivered"


def test_inbox_rejects_identity_drift_without_appending_a_second_project_event(tmp_path) -> None:
    _records, turns, _outbox_instance = _outbox(tmp_path)
    first = turns.ingest_external_agent_publication_change(_event())
    assert first["replayed"] is False

    with pytest.raises(TurnStateConflict, match="publication identity conflict"):
        turns.ingest_external_agent_publication_change(_event(object_revision="r2"))

    assert turns.project_event_cursor("project-a") == 1


def test_outbox_rejects_duplicate_identity_with_changed_event_in_the_same_publication_uow(tmp_path) -> None:
    records, _turns, _outbox_instance = _outbox(tmp_path)
    _enqueue(records)

    with pytest.raises(Exception, match="outbox identity conflict"):
        _enqueue(records, object_revision="r2")


def test_outbox_rejects_unrecognized_change_type_before_publication_uow_can_commit(tmp_path) -> None:
    records, _turns, _outbox_instance = _outbox(tmp_path)

    with records.begin() as uow, pytest.raises(ExternalAgentPublicationChangeError, match="change type"):
        ExternalAgentPublicationChangeOutbox.enqueue(uow, **_event(change_type="memory.deleted"))


@pytest.mark.parametrize(("change_type", "object_ref"), [
    ("memory.invalidated", "crp://memory/project-a/memory-001"),
    ("project_skill.invalidated", "crp://skills/project-a/skill-001"),
])
def test_outbox_accepts_project_scoped_invalidation_metadata_only(
    tmp_path, change_type: str, object_ref: str,
) -> None:
    records, turns, outbox = _outbox(tmp_path)
    _enqueue(records, change_type=change_type, object_ref=object_ref)

    assert outbox.drain()[0].change_cursor == 1
    event = turns.project_events_after("project-a")[0]
    assert event["change_type"] == change_type
    assert event["object_ref"] == object_ref


def test_same_publication_identity_is_isolated_by_project_in_outbox_and_inbox(tmp_path) -> None:
    records, turns, outbox = _outbox(tmp_path)
    _enqueue(records)
    _enqueue(
        records, project_id="project-b",
        object_ref="crp://memory/project-b/memory-001",
    )

    delivered = outbox.drain()
    assert {(item.project_id, item.publication_identity) for item in delivered} == {
        ("project-a", "memory-publication-001"),
        ("project-b", "memory-publication-001"),
    }
    assert turns.project_event_cursor("project-a") == 1
    assert turns.project_event_cursor("project-b") == 1


def test_failed_head_rotates_after_recording_attempt_and_allows_later_pending_event(tmp_path, monkeypatch) -> None:
    records, turns, outbox = _outbox(tmp_path)
    _enqueue(records, publication_identity="broken-first")
    _enqueue(records, publication_identity="ready-second", object_ref="crp://memory/project-a/memory-002")
    original = turns.ingest_external_agent_publication_change

    def flaky(event):
        if event["publication_identity"] == "broken-first":
            raise OSError("offline")
        return original(event)

    monkeypatch.setattr(turns, "ingest_external_agent_publication_change", flaky)
    assert outbox.drain(limit=1) == ()
    first = records.read(publication_outbox_collection("project-a"), "broken-first")
    assert first is not None and first.payload["attempt_count"] == 1
    delivered = outbox.drain(limit=1)
    assert len(delivered) == 1
    assert delivered[0].publication_identity == "ready-second"


@pytest.mark.parametrize("field,value", [
    ("object_ref", "C:\\private\\memory.json"),
    ("object_revision", "cookie=session-secret"),
])
def test_publication_change_uses_external_agent_unsafe_projection_canary(tmp_path, field, value) -> None:
    records, _turns, _outbox_instance = _outbox(tmp_path)
    unsafe = _event(**{field: value})

    with records.begin() as uow, pytest.raises(Exception, match="unsafe|ref|revision"):
        ExternalAgentPublicationChangeOutbox.enqueue(uow, **unsafe)


def test_existing_global_publication_inbox_migrates_to_project_scoped_identity(tmp_path) -> None:
    database = tmp_path / "legacy-ai-turns.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE ai_external_agent_publication_inbox("
            "publication_identity TEXT PRIMARY KEY,project_id TEXT NOT NULL,change_type TEXT NOT NULL,"
            "object_ref TEXT NOT NULL,object_revision TEXT NOT NULL,occurred_at TEXT NOT NULL,change_cursor INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO ai_external_agent_publication_inbox VALUES(?,?,?,?,?,?,?)",
            ("legacy-publication", "project-a", "memory.published",
             "crp://memory/project-a/old", "r1", "2026-08-29T08:00:00+00:00", 1),
        )
        connection.commit()
    finally:
        connection.close()

    turns = SQLiteAITurnStore(database)
    replay = turns.ingest_external_agent_publication_change(_event(
        publication_identity="legacy-publication", object_ref="crp://memory/project-a/old",
    ))
    assert replay["replayed"] is True
    assert turns.ingest_external_agent_publication_change(_event(
        publication_identity="legacy-publication", project_id="project-b",
        object_ref="crp://memory/project-b/memory-001",
    ))["replayed"] is False
