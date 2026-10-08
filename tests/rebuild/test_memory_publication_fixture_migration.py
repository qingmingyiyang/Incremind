from __future__ import annotations

from pathlib import Path

import pytest

from core.memory_core import (
    MemoryPublicationFixtureMigrationError,
    build_manual_publication_context,
    build_manual_publication_record,
    build_manual_publication_replacement_record,
    execute_memory_publication_fixture_migration,
    plan_memory_publication_fixture_migration_dry_run,
    scan_memory_publication_fixture_inventory,
)
from core.storage_provider import (
    JsonObjectStore,
    SQLiteMigrationLedger,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
)


_LAYERS = (
    ("atom", "memory_atoms", "memory_atom_revisions"),
    ("scenario", "memory_scenarios", "memory_scenario_revisions"),
    ("series_memory", "memory_series_memory", "memory_series_memory_revisions"),
)


def _fixture(tmp_path: Path):
    root = tmp_path / "source" / ".rebuild-data"
    store = JsonObjectStore(root, legacy_root=tmp_path / "source" / "library")
    for layer, current_collection, revisions_collection in _LAYERS:
        _write_canonical_layer(store, layer, current_collection, revisions_collection)
    inventory = scan_memory_publication_fixture_inventory(root, namespace_id="default")
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    dry_run = plan_memory_publication_fixture_migration_dry_run(
        ledger=ledger,
        migration_id="fixture-memory-publication-v1",
        target_schema_version=1,
        inventory=inventory,
        rollback_pointer="snapshot:fixture-memory-publication-v1",
    )
    return root, store, inventory, ledger, dry_run


def _write_canonical_layer(
    store: JsonObjectStore,
    layer: str,
    current_collection: str,
    revisions_collection: str,
) -> None:
    object_id = f"{layer}-fixture"
    refs = [{"source_id": f"source-{layer}", "locator": "char:0-24"}]
    current = {
        "schema_version": "1.0.0",
        "id": object_id,
        "revision": 1,
        "trust_status": "user_confirmed",
        "source_refs": refs,
        "created_at": "2026-07-12T12:00:00+08:00",
        "updated_at": "2026-07-12T12:05:00+08:00",
        "layer": layer,
    }
    context = build_manual_publication_context(
        namespace_id="default",
        layer=layer,
        draft_id=object_id,
        candidate_id=f"candidate-{layer}-fixture",
        reviewed_at="2026-07-12T12:01:00+08:00",
        review_reason="用户确认 fixture 候选。",
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


def test_inventory_dry_run_and_executor_copy_all_generic_publication_collections(tmp_path: Path) -> None:
    root, _store, inventory, ledger, dry_run = _fixture(tmp_path)
    target = tmp_path / "target" / "memory-publication.sqlite3"

    assert inventory.is_migratable is True
    result = execute_memory_publication_fixture_migration(
        object_store_root=root,
        target_database_path=target,
        ledger=ledger,
        dry_run=dry_run,
    )

    records = SQLiteStructuredRecordStore(target)
    assert result.object_count == 12
    assert result.input_fingerprint == inventory.inventory.fingerprint
    for _layer, current_collection, revisions_collection in _LAYERS:
        assert len(records.list(current_collection)) == 1
        assert len(records.list(revisions_collection)) == 1
    assert len(records.list("memory_publications")) == 3
    assert len(records.list("memory_transitions")) == 3
    assert scan_memory_publication_fixture_inventory(root, namespace_id="default") == inventory


def test_inventory_migrates_complete_series_replacement_chain(tmp_path: Path) -> None:
    root, store, _inventory, _ledger, _dry_run = _fixture(tmp_path)
    object_id = "series_memory-fixture"
    old_id = f"memory-publication-series-memory-{object_id}"
    old = store.read("memory_publications", old_id)
    current = store.read("memory_series_memory", object_id)
    assert old is not None and current is not None
    refs = [{"source_id": "source-series_memory", "locator": "char:25-48"}]
    context = build_manual_publication_context(
        namespace_id="default", layer="series_memory", draft_id=object_id,
        candidate_id="candidate-series-memory-replacement", reviewed_at="2026-07-12T12:06:00+08:00",
        review_reason="用户确认替换 fixture 系列。", source_refs=refs, evidence_refs=refs,
    )
    replacement = build_manual_publication_replacement_record(
        context=context, namespace_id="default", layer="series_memory", draft_id=object_id,
        revision=2, supersedes_publication=old, published_at="2026-07-12T12:07:00+08:00",
    )
    transition_id = str(replacement["transition_ref"]).rsplit("/", 1)[-1].removesuffix(".json")
    transition = {
        "schema_version": "1.0.0", "id": transition_id, "object_type": "series_memory", "object_id": object_id,
        "transition_type": "confirm", "from_trust_status": "user_confirmed", "to_trust_status": "user_confirmed",
        "from_revision": 1, "to_revision": 2, "actor": "user", "reason": replacement["reason"],
        "evidence_refs": [{"object_type": "series_memory", "object_id": object_id, "source_refs": refs}],
        "created_at": replacement["published_at"],
    }
    updated_current = {**current, "revision": 2, "updated_at": replacement["published_at"], "overview": "replacement"}
    revision = {
        "schema_version": "1.0.0", "id": f"{object_id}~r2", "layer": "series_memory", "object_id": object_id,
        "revision": 2, "state": "published", "trust_status": "user_confirmed", "previous_revision_id": f"{object_id}~r1",
        "publication_id": replacement["id"], "source_candidate_id": replacement["source_candidate_id"],
        "review_ref": replacement["review_ref"], "reviewer": "user", "reviewed_at": replacement["reviewed_at"],
        "policy_id": "local-manual-v1", "source_refs": refs, "evidence_refs": refs,
        "published_at": replacement["published_at"], "payload": updated_current,
    }
    superseded = {**old, "status": "superseded", "superseded_by_publication_id": replacement["id"], "superseded_at": replacement["published_at"], "superseded_revision": 2}
    store.write("memory_series_memory", object_id, updated_current, expected_revision=1)
    store.write("memory_series_memory_revisions", f"{object_id}~r2", revision, expected_revision=0)
    store.write("memory_publications", old_id, superseded, expected_revision=1)
    store.write("memory_publications", str(replacement["id"]), replacement, expected_revision=0)
    store.write("memory_transitions", transition_id, transition, expected_revision=0)

    inventory = scan_memory_publication_fixture_inventory(root, namespace_id="default")
    assert inventory.is_migratable is True
    ledger = SQLiteMigrationLedger(tmp_path / "replacement-ledger.sqlite3")
    dry_run = plan_memory_publication_fixture_migration_dry_run(
        ledger=ledger, migration_id="fixture-memory-publication-replacement-v1", target_schema_version=1,
        inventory=inventory, rollback_pointer="snapshot:fixture-memory-publication-replacement-v1",
    )
    target = tmp_path / "target" / "replacement.sqlite3"
    execute_memory_publication_fixture_migration(
        object_store_root=root, target_database_path=target, ledger=ledger, dry_run=dry_run,
    )
    records = SQLiteStructuredRecordStore(target)
    assert records.read("memory_series_memory", object_id).payload["revision"] == 2
    assert len(records.list("memory_series_memory_revisions")) == 2
    assert len(records.list("memory_publications")) == 4


def test_inventory_refuses_legacy_missing_revision_staging_and_cross_aggregate_audit(tmp_path: Path) -> None:
    root, store, _inventory, _ledger, _dry_run = _fixture(tmp_path)
    store.delete("memory_atom_revisions", "atom-fixture~r1")
    publication = store.read("memory_publications", "memory-publication-scenario-scenario-fixture")
    assert publication is not None
    publication["transition_ref"] = "crp://default/memory-transitions/transition-missing.json"
    store.write(
        "memory_publications",
        "memory-publication-scenario-scenario-fixture",
        publication,
        expected_revision=1,
    )
    store.write("staging_scenarios", "scenario-pending", {"id": "scenario-pending"}, expected_revision=0)
    store.write(
        "memory_publications",
        "memory-publication-project-skill-legacy",
        {"id": "memory-publication-project-skill-legacy", "object_type": "project_skill"},
        expected_revision=0,
    )

    inventory = scan_memory_publication_fixture_inventory(root, namespace_id="default")

    codes = {issue.code for issue in inventory.issues}
    assert inventory.is_migratable is False
    assert "publication_revision_missing_or_mismatch" in codes
    assert "publication_transition_missing_or_mismatch" in codes
    assert "staging_record_present" in codes
    assert "cross_aggregate_or_legacy_publication" in codes
    with pytest.raises(MemoryPublicationFixtureMigrationError, match="inventory is inconsistent"):
        plan_memory_publication_fixture_migration_dry_run(
            ledger=SQLiteMigrationLedger(tmp_path / "legacy-ledger.sqlite3"),
            migration_id="legacy-memory-publication-v1",
            target_schema_version=1,
            inventory=inventory,
            rollback_pointer="snapshot:legacy-memory-publication-v1",
        )


def test_executor_refuses_source_drift_existing_or_nested_target(tmp_path: Path) -> None:
    root, store, _inventory, ledger, dry_run = _fixture(tmp_path)
    atom = store.read("memory_atoms", "atom-fixture")
    assert atom is not None
    atom["updated_at"] = "2026-07-12T13:00:00+08:00"
    store.write("memory_atoms", "atom-fixture", atom, expected_revision=1)
    drift_target = tmp_path / "target" / "drift.sqlite3"
    with pytest.raises(MemoryPublicationFixtureMigrationError, match="fingerprint changed|inventory is inconsistent"):
        execute_memory_publication_fixture_migration(
            object_store_root=root,
            target_database_path=drift_target,
            ledger=ledger,
            dry_run=dry_run,
        )
    assert drift_target.exists() is False

    root, _store, _inventory, ledger, dry_run = _fixture(tmp_path / "fresh")
    existing = tmp_path / "existing" / "memory.sqlite3"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"keep")
    with pytest.raises(MemoryPublicationFixtureMigrationError, match="already exist"):
        execute_memory_publication_fixture_migration(
            object_store_root=root,
            target_database_path=existing,
            ledger=ledger,
            dry_run=dry_run,
        )
    assert existing.read_bytes() == b"keep"
    with pytest.raises(MemoryPublicationFixtureMigrationError, match="inside source root"):
        execute_memory_publication_fixture_migration(
            object_store_root=root,
            target_database_path=root / "nested.sqlite3",
            ledger=ledger,
            dry_run=dry_run,
        )


def test_executor_cleans_new_target_after_mid_copy_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _store, _inventory, ledger, dry_run = _fixture(tmp_path)
    target = tmp_path / "target" / "memory-publication.sqlite3"
    original_put = SQLiteStructuredRecordUnitOfWork.put
    calls = 0

    def _fail(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("injected memory publication copy failure")
        return original_put(self, *args, **kwargs)

    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", _fail)
    with pytest.raises(MemoryPublicationFixtureMigrationError, match="injected memory publication copy failure"):
        execute_memory_publication_fixture_migration(
            object_store_root=root,
            target_database_path=target,
            ledger=ledger,
            dry_run=dry_run,
        )
    assert target.exists() is False
    assert Path(f"{target}-wal").exists() is False
    assert Path(f"{target}-shm").exists() is False
