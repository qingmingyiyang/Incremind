from __future__ import annotations

from pathlib import Path

import pytest

from core.memory_core import (
    MemoryPublicationFixtureCompatibilityError,
    build_manual_publication_context,
    build_manual_publication_record,
    compare_memory_publication_fixture,
    execute_memory_publication_fixture_migration,
    plan_memory_publication_fixture_migration_dry_run,
    scan_memory_publication_fixture_inventory,
)
from core.storage_provider import JsonObjectStore, SQLiteMigrationLedger, SQLiteStructuredRecordStore


_LAYERS = (
    ("atom", "memory_atoms", "memory_atom_revisions"),
    ("scenario", "memory_scenarios", "memory_scenario_revisions"),
    ("series_memory", "memory_series_memory", "memory_series_memory_revisions"),
)


def _fixture(tmp_path: Path):
    root = tmp_path / "source" / ".rebuild-data"
    store = JsonObjectStore(root, legacy_root=tmp_path / "source" / "library")
    for layer, current_collection, revisions_collection in _LAYERS:
        _write_layer(store, layer, current_collection, revisions_collection)
    inventory = scan_memory_publication_fixture_inventory(root, namespace_id="default")
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    dry_run = plan_memory_publication_fixture_migration_dry_run(
        ledger=ledger,
        migration_id="fixture-memory-compatibility-v1",
        target_schema_version=1,
        inventory=inventory,
        rollback_pointer="snapshot:fixture-memory-compatibility-v1",
    )
    target = tmp_path / "target" / "memory-publication.sqlite3"
    execute_memory_publication_fixture_migration(
        object_store_root=root,
        target_database_path=target,
        ledger=ledger,
        dry_run=dry_run,
    )
    return root, store, target


def _write_layer(store: JsonObjectStore, layer: str, current_collection: str, revisions_collection: str) -> None:
    object_id = f"{layer}-compatibility"
    refs = [{"source_id": f"source-{layer}", "locator": "char:0-24"}]
    current = {
        "schema_version": "1.0.0",
        "id": object_id,
        "revision": 1,
        "trust_status": "user_confirmed",
        "source_refs": refs,
        "created_at": "2026-07-12T12:00:00+08:00",
        "updated_at": "2026-07-12T12:05:00+08:00",
    }
    context = build_manual_publication_context(
        namespace_id="default",
        layer=layer,
        draft_id=object_id,
        candidate_id=f"candidate-{layer}-compatibility",
        reviewed_at="2026-07-12T12:01:00+08:00",
        review_reason="用户确认 compatibility fixture。",
        source_refs=refs,
        evidence_refs=refs,
    )
    publication = build_manual_publication_record(
        context=context,
        namespace_id="default",
        layer=layer,
        draft_id=object_id,
        revision=1,
        published_at="2026-07-12T12:05:00+08:00",
    )
    transition_id = str(publication["transition_ref"]).rsplit("/", 1)[-1].removesuffix(".json")
    transition = {
        "schema_version": "1.0.0",
        "id": transition_id,
        "object_type": layer,
        "object_id": object_id,
        "transition_type": "confirm",
        "from_trust_status": "system_generated",
        "to_trust_status": "user_confirmed",
        "from_revision": 0,
        "to_revision": 1,
        "actor": "user",
        "reason": publication["reason"],
        "evidence_refs": [{"object_type": layer, "object_id": object_id, "source_refs": refs}],
        "created_at": publication["published_at"],
    }
    revision = {
        "schema_version": "1.0.0",
        "id": f"{object_id}~r1",
        "layer": layer,
        "object_id": object_id,
        "revision": 1,
        "state": "published",
        "trust_status": "user_confirmed",
        "publication_id": publication["publication_id"],
        "source_candidate_id": publication["source_candidate_id"],
        "review_ref": publication["review_ref"],
        "reviewer": publication["reviewer"],
        "reviewed_at": publication["reviewed_at"],
        "policy_id": publication["policy_id"],
        "source_refs": refs,
        "evidence_refs": refs,
        "published_at": publication["published_at"],
        "payload": current,
    }
    store.write(current_collection, object_id, current, expected_revision=0)
    store.write(revisions_collection, str(revision["id"]), revision, expected_revision=0)
    store.write("memory_publications", str(publication["id"]), publication, expected_revision=0)
    store.write("memory_transitions", transition_id, transition, expected_revision=0)


def test_compare_reads_all_canonical_collections_without_mutating_target(tmp_path: Path) -> None:
    root, _store, target = _fixture(tmp_path)
    before = target.stat().st_mtime_ns

    report = compare_memory_publication_fixture(
        object_store_root=root,
        target_database_path=target,
        namespace_id="default",
    )

    assert len(report.compared_collections) == 8
    assert report.object_count == 12
    assert target.stat().st_mtime_ns == before


def test_compare_rejects_source_inventory_drift(tmp_path: Path) -> None:
    root, store, target = _fixture(tmp_path)
    store.write("staging_atoms", "atom-pending", {"id": "atom-pending"}, expected_revision=0)

    with pytest.raises(MemoryPublicationFixtureCompatibilityError, match="source inventory is inconsistent"):
        compare_memory_publication_fixture(
            object_store_root=root,
            target_database_path=target,
            namespace_id="default",
        )


def test_compare_rejects_target_payload_and_sqlite_revision_drift(tmp_path: Path) -> None:
    root, _store, target = _fixture(tmp_path)
    records = SQLiteStructuredRecordStore(target)
    with records.begin() as transaction:
        atom = transaction.read("memory_atoms", "atom-compatibility")
        assert atom is not None
        changed = dict(atom.payload)
        changed["updated_at"] = "2026-07-12T13:00:00+08:00"
        transaction.put("memory_atoms", atom.object_id, changed, expected_revision=atom.revision)
        transaction.commit()

    with pytest.raises(MemoryPublicationFixtureCompatibilityError, match="SQLite revision mismatch"):
        compare_memory_publication_fixture(
            object_store_root=root,
            target_database_path=target,
            namespace_id="default",
        )


def test_compare_rejects_missing_target_collection_and_missing_target(tmp_path: Path) -> None:
    root, _store, target = _fixture(tmp_path)
    records = SQLiteStructuredRecordStore(target)
    with records.begin() as transaction:
        transition = transaction.read("memory_transitions", "transition-memory-publication-atom-atom-compatibility")
        assert transition is not None
        transaction.delete("memory_transitions", transition.object_id, expected_revision=transition.revision)
        transaction.commit()

    with pytest.raises(MemoryPublicationFixtureCompatibilityError, match="object set mismatch"):
        compare_memory_publication_fixture(
            object_store_root=root,
            target_database_path=target,
            namespace_id="default",
        )
    with pytest.raises(MemoryPublicationFixtureCompatibilityError, match="target is missing"):
        compare_memory_publication_fixture(
            object_store_root=root,
            target_database_path=tmp_path / "missing.sqlite3",
            namespace_id="default",
        )
