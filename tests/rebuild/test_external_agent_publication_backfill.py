from __future__ import annotations

from core.storage_provider import SQLiteStructuredRecordStore
from core.storage_provider.external_agent_publication_backfill import (
    backfill_external_agent_publication_outbox,
    publication_backfill_audit_collection,
)
from core.storage_provider.external_agent_publication_change import (
    publication_outbox_collection,
)


_TIME = "2026-08-29T08:00:00+00:00"


def _scenario_authority(
    records: SQLiteStructuredRecordStore,
    *,
    publication_id: str = "memory-publication-scenario-001",
    object_id: str = "scenario-001",
    project_id: str = "project-a",
    status: str = "published",
) -> None:
    transition_id = f"transition-{publication_id}"
    with records.begin() as uow:
        uow.put(
            "memory_scenarios", object_id,
            {
                "id": object_id, "revision": 1,
                "trust_status": "user_confirmed", "project_id": project_id,
            }, expected_revision=0,
        )
        uow.put(
            "memory_transitions", transition_id,
            {
                "id": transition_id, "object_type": "scenario",
                "object_id": object_id, "created_at": _TIME,
            }, expected_revision=0,
        )
        uow.put(
            "memory_publications", publication_id,
            {
                "schema_version": "1.0.0", "id": publication_id,
                "publication_id": publication_id, "status": status,
                "layer": "scenario", "object_type": "scenario",
                "published_object_id": object_id, "published_revision": 1,
                "published_at": _TIME,
                "transition_ref": f"crp://default/memory-transitions/{transition_id}.json",
            }, expected_revision=0,
        )
        uow.commit()


def _project_skill_authority(
    records: SQLiteStructuredRecordStore,
    *,
    publication_id: str = "memory-publication-project-skill-001",
    skill_id: str = "skill-001",
    project_id: str = "project-a",
) -> None:
    transition_id = f"transition-{publication_id}"
    with records.begin() as uow:
        uow.put(
            "project_skills", skill_id,
            {
                "id": skill_id, "project_id": project_id, "revision": 3,
                "status": "active", "trust_status": "user_confirmed",
            }, expected_revision=0,
        )
        uow.put(
            "memory_transitions", transition_id,
            {
                "id": transition_id, "object_type": "project_skill",
                "object_id": skill_id, "created_at": _TIME,
            }, expected_revision=0,
        )
        uow.put(
            "memory_publications", publication_id,
            {
                "schema_version": "1.0.0", "id": publication_id,
                "status": "published", "layer": "project_skill",
                "object_type": "project_skill", "project_id": project_id,
                "published_object_id": skill_id, "published_revision": 3,
                "created_at": _TIME,
                "transition_ref": f"crp://default/memory-transitions/{transition_id}.json",
            }, expected_revision=0,
        )
        uow.commit()


def test_backfill_uses_current_formal_memory_and_project_skill_authorities(tmp_path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    _scenario_authority(records)
    _project_skill_authority(records)

    result = backfill_external_agent_publication_outbox(records)

    assert result.scanned == 2
    assert {(item.publication_identity, item.project_id, item.disposition) for item in result.receipts} == {
        ("memory-publication-scenario-001", "project-a", "enqueued"),
        ("memory-publication-project-skill-001", "project-a", "enqueued"),
    }
    assert result.issues == ()
    scenario = records.read(
        publication_outbox_collection("project-a"), "memory-publication-scenario-001",
    )
    assert scenario is not None and scenario.payload["event"] == {
        "publication_identity": "memory-publication-scenario-001",
        "project_id": "project-a", "change_type": "memory.published",
        "object_ref": "crp://memory/project-a/scenario-001",
        "object_revision": "r1", "occurred_at": _TIME,
    }
    skill = records.read(
        publication_outbox_collection("project-a"), "memory-publication-project-skill-001",
    )
    assert skill is not None and skill.payload["event"] == {
        "publication_identity": "memory-publication-project-skill-001",
        "project_id": "project-a", "change_type": "project_skill.published",
        "object_ref": "crp://skills/project-a/skill-001",
        "object_revision": "project-skill-r3", "occurred_at": _TIME,
    }
    audit = records.read(
        publication_backfill_audit_collection("project-a"),
        "memory-publication-scenario-001",
    )
    assert audit is not None
    assert audit.payload["publication_record_revision"] == 1
    assert audit.payload["event"]["occurred_at"] == _TIME


def test_backfill_is_restart_idempotent_even_after_drain_changes_outbox_state(tmp_path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    _scenario_authority(records)

    first = backfill_external_agent_publication_outbox(records)
    outbox = records.read(
        publication_outbox_collection("project-a"), "memory-publication-scenario-001",
    )
    assert outbox is not None
    with records.begin() as uow:
        uow.put(
            outbox.collection, outbox.object_id,
            {**dict(outbox.payload), "state": "delivered", "change_cursor": 7},
            expected_revision=outbox.revision,
        )
        uow.commit()

    replay = backfill_external_agent_publication_outbox(records)

    assert first.receipts[0].disposition == "enqueued"
    assert [(item.publication_identity, item.project_id, item.disposition) for item in replay.receipts] == [
        ("memory-publication-scenario-001", "project-a", "replayed"),
    ]
    assert replay.issues == ()
    assert len(records.list(publication_outbox_collection("project-a"))) == 1


def test_rolled_back_and_bad_authorities_do_not_block_later_valid_backfill(tmp_path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    _scenario_authority(
        records, publication_id="a-bad-authority", object_id="bad-scenario",
    )
    with records.begin() as uow:
        record = uow.read("memory_publications", "a-bad-authority")
        assert record is not None
        uow.put(
            record.collection, record.object_id,
            {**dict(record.payload), "published_at": "not-a-time"},
            expected_revision=record.revision,
        )
        uow.commit()
    _scenario_authority(
        records, publication_id="b-rolled-back", object_id="rolled-back", status="rolled_back",
    )
    _scenario_authority(
        records, publication_id="z-valid-authority", object_id="valid-scenario",
    )

    result = backfill_external_agent_publication_outbox(records)

    assert result.scanned == 3
    assert [(item.publication_identity, item.code) for item in result.issues] == [
        ("a-bad-authority", "publication_transition_conflicts"),
    ]
    assert [(item.publication_identity, item.project_id) for item in result.receipts] == [
        ("z-valid-authority", "project-a"),
    ]
    assert records.read(publication_outbox_collection("project-a"), "b-rolled-back") is None


def test_backfill_limit_bounds_scan_without_fabricating_later_authorities(tmp_path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    _scenario_authority(records, publication_id="a-first", object_id="first")
    _scenario_authority(records, publication_id="b-second", object_id="second")

    result = backfill_external_agent_publication_outbox(records, limit=1)

    assert result.scanned == 1
    assert [item.publication_identity for item in result.receipts] == ["a-first"]
    assert records.read(publication_outbox_collection("project-a"), "b-second") is None
