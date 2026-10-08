from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.project_skill_core import (
    ObjectStoreProjectSkillRepository,
    ProjectSkillMigrationExecutionError,
    ProjectSkillUpdate,
    SQLiteProjectSkillRepository,
    execute_project_skill_fixture_migration,
    execute_project_skill_publication_fixture_migration,
    plan_project_skill_publication_migration_dry_run,
    plan_project_skill_migration_dry_run,
    scan_project_skill_migration_inventory,
    scan_project_skill_publication_fixture_inventory,
    compare_project_skill_publication_fixture,
    ProjectSkillPublicationCompatibilityError,
)
from core.storage_provider import (
    JsonObjectStore,
    SQLiteMigrationLedger,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "core-contracts" / "rebuild" / "fixtures" / "project_skill" / "valid-active-skill.json"


def _fixture(tmp_path: Path):
    rebuild_root = tmp_path / "legacy" / ".rebuild-data"
    store = JsonObjectStore(rebuild_root, legacy_root=tmp_path / "legacy" / "library")
    repository = ObjectStoreProjectSkillRepository(store)
    structured = json.loads(FIXTURE.read_text(encoding="utf-8"))
    first = repository.save(ProjectSkillUpdate(str(structured["project_id"]), "# Skill\n\nFirst.", structured, 0, "first"))
    updated = copy.deepcopy(first)
    updated["style_preferences"]["voice"] = "preserve user voice"
    second = repository.save(ProjectSkillUpdate(str(updated["project_id"]), "# Skill\n\nSecond.", updated, 1, "second"))
    inventory = scan_project_skill_migration_inventory(rebuild_root, namespace_id="default")
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    dry_run = plan_project_skill_migration_dry_run(
        ledger=ledger,
        migration_id="fixture-project-skills-v1",
        target_schema_version=2,
        inventory=inventory,
        rollback_pointer="snapshot:fixture-project-skills-v1",
    )
    return rebuild_root, store, repository, str(second["project_id"]), str(second["id"]), inventory, ledger, dry_run


def test_executor_copies_five_collections_and_reopens_history(tmp_path: Path) -> None:
    rebuild_root, _store, source, project_id, _skill_id, inventory, ledger, dry_run = _fixture(tmp_path)
    target = tmp_path / "target" / "project-skills.sqlite3"

    result = execute_project_skill_fixture_migration(
        object_store_root=rebuild_root,
        target_database_path=target,
        ledger=ledger,
        dry_run=dry_run,
    )

    migrated = SQLiteProjectSkillRepository(SQLiteStructuredRecordStore(target))
    assert result.project_count == 1
    assert result.object_count == 8
    assert result.input_fingerprint == inventory.inventory.fingerprint
    assert migrated.load(project_id) == source.load(project_id)
    assert migrated.revisions(project_id) == source.revisions(project_id)
    assert migrated.markdown(project_id, revision=1) == source.markdown(project_id, revision=1)
    assert migrated.markdown(project_id, revision=2) == source.markdown(project_id, revision=2)
    assert migrated.structured(project_id, revision=2) == source.structured(project_id, revision=2)
    assert scan_project_skill_migration_inventory(rebuild_root, namespace_id="default") == inventory


def test_executor_rejects_input_drift_before_target_creation(tmp_path: Path) -> None:
    rebuild_root, store, _source, _project_id, skill_id, _inventory, ledger, dry_run = _fixture(tmp_path)
    skill = store.read("project_skills", skill_id)
    assert skill is not None
    skill["purpose"] = "drift after dry-run"
    store.write("project_skills", skill_id, skill, expected_revision=2)
    target = tmp_path / "target" / "project-skills.sqlite3"

    with pytest.raises(ProjectSkillMigrationExecutionError, match="inventory is inconsistent|fingerprint changed"):
        execute_project_skill_fixture_migration(object_store_root=rebuild_root, target_database_path=target, ledger=ledger, dry_run=dry_run)
    assert target.exists() is False


def test_executor_rejects_existing_or_nested_target(tmp_path: Path) -> None:
    rebuild_root, _store, _source, _project_id, _skill_id, _inventory, ledger, dry_run = _fixture(tmp_path)
    existing = tmp_path / "target" / "project-skills.sqlite3"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"keep")
    with pytest.raises(ProjectSkillMigrationExecutionError, match="already exist"):
        execute_project_skill_fixture_migration(object_store_root=rebuild_root, target_database_path=existing, ledger=ledger, dry_run=dry_run)
    assert existing.read_bytes() == b"keep"

    nested = rebuild_root / "nested.sqlite3"
    with pytest.raises(ProjectSkillMigrationExecutionError, match="inside source root"):
        execute_project_skill_fixture_migration(object_store_root=rebuild_root, target_database_path=nested, ledger=ledger, dry_run=dry_run)
    assert nested.exists() is False


def test_executor_rolls_back_and_cleans_new_target_on_mid_copy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuild_root, _store, _source, _project_id, _skill_id, inventory, ledger, dry_run = _fixture(tmp_path)
    target = tmp_path / "target" / "project-skills.sqlite3"
    original_put = SQLiteStructuredRecordUnitOfWork.put
    put_count = 0

    def _fail_second(self, *args, **kwargs):
        nonlocal put_count
        put_count += 1
        if put_count == 2:
            raise RuntimeError("injected second Project Skill write failure")
        return original_put(self, *args, **kwargs)

    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", _fail_second)
    with pytest.raises(ProjectSkillMigrationExecutionError, match="injected second"):
        execute_project_skill_fixture_migration(object_store_root=rebuild_root, target_database_path=target, ledger=ledger, dry_run=dry_run)

    assert target.exists() is False
    assert Path(f"{target}-wal").exists() is False
    assert Path(f"{target}-shm").exists() is False
    assert scan_project_skill_migration_inventory(rebuild_root, namespace_id="default") == inventory


def _publication_fixture(tmp_path: Path):
    rebuild_root, store, source, project_id, skill_id, _inventory, ledger, _dry_run = _fixture(tmp_path)
    draft_id = "project-skill-publication-draft-aaaaaaaaaaaaaaaaaaaaaaaa"
    transition_id = f"transition-project-skill-publication-{draft_id}"
    publication_id = f"memory-publication-project-skill-{draft_id}"
    store.write("memory_transitions", transition_id, {"id": transition_id, "object_type": "project_skill", "object_id": skill_id}, expected_revision=0)
    store.write("memory_publications", publication_id, {"id": publication_id, "object_type": "project_skill", "project_id": project_id, "published_object_id": skill_id, "published_revision": 2, "status": "published", "draft_digest": "a" * 64, "review_ref": "crp://default/memory-candidates/candidate-alpha.json#review", "transition_ref": f"crp://default/memory-transitions/{transition_id}.json"}, expected_revision=0)
    inventory = scan_project_skill_publication_fixture_inventory(rebuild_root, namespace_id="default")
    ledger = SQLiteMigrationLedger(tmp_path / "publication-ledger.sqlite3")
    dry_run = plan_project_skill_publication_migration_dry_run(ledger=ledger, migration_id="fixture-project-skill-publications-v1", target_schema_version=2, inventory=inventory, rollback_pointer="snapshot:fixture-project-skill-publications-v1")
    return rebuild_root, store, source, project_id, skill_id, inventory, ledger, dry_run, publication_id, transition_id


def test_publication_executor_copies_audit_records_and_rejects_publication_drift(tmp_path: Path) -> None:
    rebuild_root, store, _source, project_id, _skill_id, inventory, ledger, dry_run, publication_id, transition_id = _publication_fixture(tmp_path)
    target = tmp_path / "target" / "publication.sqlite3"
    result = execute_project_skill_publication_fixture_migration(object_store_root=rebuild_root, target_database_path=target, ledger=ledger, dry_run=dry_run)
    migrated = SQLiteStructuredRecordStore(target)
    assert result.object_count == inventory.inventory.object_count
    assert migrated.read("memory_publications", publication_id) is not None
    assert migrated.read("memory_transitions", transition_id) is not None
    assert SQLiteProjectSkillRepository(migrated).load(project_id) is not None

    changed = store.read("memory_publications", publication_id)
    assert changed is not None
    changed["reason"] = "source drift"
    store.write("memory_publications", publication_id, changed, expected_revision=1)
    with pytest.raises(ProjectSkillMigrationExecutionError, match="fingerprint changed"):
        execute_project_skill_publication_fixture_migration(object_store_root=rebuild_root, target_database_path=tmp_path / "other" / "publication.sqlite3", ledger=ledger, dry_run=dry_run)


def test_publication_executor_cleans_target_after_mid_copy_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rebuild_root, _store, _source, _project_id, _skill_id, _inventory, ledger, dry_run, _publication_id, _transition_id = _publication_fixture(tmp_path)
    target = tmp_path / "target" / "publication.sqlite3"
    original_put = SQLiteStructuredRecordUnitOfWork.put
    calls = 0

    def _fail(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("injected publication copy failure")
        return original_put(self, *args, **kwargs)

    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", _fail)
    with pytest.raises(ProjectSkillMigrationExecutionError, match="injected publication copy failure"):
        execute_project_skill_publication_fixture_migration(object_store_root=rebuild_root, target_database_path=target, ledger=ledger, dry_run=dry_run)
    assert target.exists() is False
    assert Path(f"{target}-wal").exists() is False
    assert Path(f"{target}-shm").exists() is False


def test_publication_compatibility_compares_all_collections_and_fails_closed(tmp_path: Path) -> None:
    rebuild_root, _store, _source, _project_id, _skill_id, inventory, ledger, dry_run, _publication_id, transition_id = _publication_fixture(tmp_path)
    target = tmp_path / "target" / "publication.sqlite3"
    execute_project_skill_publication_fixture_migration(object_store_root=rebuild_root, target_database_path=target, ledger=ledger, dry_run=dry_run)
    report = compare_project_skill_publication_fixture(object_store_root=rebuild_root, target_database_path=target, namespace_id="default")
    assert report.object_count == inventory.inventory.object_count
    records = SQLiteStructuredRecordStore(target)
    with records.begin() as transaction:
        transition = transaction.read("memory_transitions", transition_id)
        assert transition is not None
        transaction.delete("memory_transitions", transition_id, expected_revision=transition.revision)
        transaction.commit()
    with pytest.raises(ProjectSkillPublicationCompatibilityError, match="memory_transitions"):
        compare_project_skill_publication_fixture(object_store_root=rebuild_root, target_database_path=target, namespace_id="default")
