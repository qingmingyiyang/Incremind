from __future__ import annotations

from datetime import datetime, timezone

from core.ai_kernel import SQLiteAITurnStore
from core.product_core.memory_lifecycle import GovernedMemoryLifecycle
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore
from core.storage_provider.external_agent_publication_change import ExternalAgentPublicationChangeOutbox
from core.storage_provider.memory_invalidation_outbox import dispatch_memory_invalidations


NOW = "2026-08-28T10:00:00+00:00"


def test_memory_invalidation_reaches_external_agent_cursor_and_replays(tmp_path):
    json_store = JsonObjectStore(tmp_path / "vault", legacy_root=tmp_path / "legacy")
    json_store.write("memory_scenarios", "memory-a", {
        "schema_version": "1.0.0", "id": "memory-a", "project_id": "project-a",
        "revision": 1, "summary": "old", "trust_status": "user_confirmed",
        "source_refs": [{"source_id": "source-a", "locator": "line:1"}],
    }, expected_revision=0)
    GovernedMemoryLifecycle(json_store).supersede(
        layer="scenario", object_id="memory-a", project_id="project-a",
        expected_revision=1, expected_storage_revision=1, changes={"summary": "new"},
        reason="corrected", occurred_at=NOW, recorded_at=NOW, confirm=True,
    )
    records = SQLiteStructuredRecordStore(tmp_path / "structured.sqlite3")

    first = dispatch_memory_invalidations(json_store, records)
    second = dispatch_memory_invalidations(json_store, records)

    assert first.enqueued == 1 and first.failed == 0
    assert second.scanned == 0
    turn_store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    delivered = ExternalAgentPublicationChangeOutbox(
        records, turn_store, now=lambda: datetime(2026, 8, 28, 10, tzinfo=timezone.utc),
    ).drain()
    assert len(delivered) == 1 and delivered[0].change_cursor == 1
    connection = turn_store._connect()  # noqa: SLF001 - durable cursor Gate inspection
    try:
        row = connection.execute(
            "SELECT change_type,object_ref,object_revision FROM ai_project_event_feed "
            "WHERE project_id='project-a'"
        ).fetchone()
    finally:
        connection.close()
    assert tuple(row) == ("memory.invalidated", "crp://memory/project-a/memory-a", "r2")
