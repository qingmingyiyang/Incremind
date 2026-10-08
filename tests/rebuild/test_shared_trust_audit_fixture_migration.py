from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.memory_core import build_manual_publication_context, build_manual_publication_record
from core.project_skill_core import (
    ObjectStoreProjectSkillRepository,
    ProjectSkillUpdate,
    SQLiteProjectSkillRepository,
)
from core.shared_trust_audit_fixture_migration import (
    SharedTrustAuditFixtureMigrationError,
    compare_shared_trust_audit_fixture,
    execute_shared_trust_audit_fixture_migration,
    plan_shared_trust_audit_fixture_migration_dry_run,
    scan_shared_trust_audit_fixture_inventory,
)
from core.storage_provider import (
    JsonObjectStore,
    SQLiteMigrationLedger,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
)

ROOT = Path(__file__).resolve().parents[2]
SKILL_FIXTURE = (
    ROOT
    / "core-contracts"
    / "rebuild"
    / "fixtures"
    / "project_skill"
    / "valid-active-skill.json"
)
MEMORY_LAYERS = (
    ("atom", "memory_atoms", "memory_atom_revisions"),
    ("scenario", "memory_scenarios", "memory_scenario_revisions"),
    ("series_memory", "memory_series_memory", "memory_series_memory_revisions"),
)
MEMBERS = {
    "memory_atoms",
    "memory_publications",
    "memory_scenarios",
    "memory_series_memory",
    "memory_transitions",
    "project_skills",
}


def _fixture(tmp_path: Path, *, project_id: str = "project-alpha"):
    root = tmp_path / "source" / ".rebuild-data"
    store = JsonObjectStore(root, legacy_root=tmp_path / "source" / "library")
    for layer, current_collection, revisions_collection in MEMORY_LAYERS:
        _write_memory_layer(store, layer, current_collection, revisions_collection)
    project_id, skill_id, project_publication_id, project_transition_id = _write_project_skill(
        store,
        project_id=project_id,
    )
    inventory = scan_shared_trust_audit_fixture_inventory(root, namespace_id="default")
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    dry_run = plan_shared_trust_audit_fixture_migration_dry_run(
        ledger=ledger,
        migration_id="shared-trust-audit-fixture-v1",
        target_schema_version=1,
        inventory=inventory,
        rollback_pointer="snapshot:shared-trust-audit-fixture-v1",
    )
    return (
        root,
        store,
        inventory,
        ledger,
        dry_run,
        project_id,
        skill_id,
        project_publication_id,
        project_transition_id,
    )


def _write_memory_layer(
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


def _write_project_skill(
    store: JsonObjectStore,
    *,
    project_id: str = "project-alpha",
) -> tuple[str, str, str, str]:
    structured = json.loads(SKILL_FIXTURE.read_text(encoding="utf-8"))
    structured.update(
        {
            "id": f"skill-{project_id}",
            "project_id": project_id,
            "name": f"{project_id} Project Skill",
            "markdown_uri": f"crp://default/projects/{project_id}/project-skill.md",
            "json_uri": f"crp://default/projects/{project_id}/project-skill.json",
        }
    )
    repository = ObjectStoreProjectSkillRepository(store)
    first = repository.save(
        ProjectSkillUpdate(
            str(structured["project_id"]),
            "# Skill\n\nFirst.",
            structured,
            0,
            "first",
        )
    )
    updated = copy.deepcopy(first)
    updated["style_preferences"]["voice"] = "preserve user voice"
    second = repository.save(
        ProjectSkillUpdate(
            str(updated["project_id"]),
            "# Skill\n\nSecond.",
            updated,
            1,
            "second",
        )
    )
    draft_id = "project-skill-publication-draft-aaaaaaaaaaaaaaaaaaaaaaaa"
    transition_id = f"transition-project-skill-publication-{draft_id}"
    publication_id = f"memory-publication-project-skill-{draft_id}"
    store.write(
        "memory_transitions",
        transition_id,
        {"id": transition_id, "object_type": "project_skill", "object_id": second["id"]},
        expected_revision=0,
    )
    store.write(
        "memory_publications",
        publication_id,
        {
            "id": publication_id,
            "object_type": "project_skill",
            "project_id": second["project_id"],
            "published_object_id": second["id"],
            "published_revision": 2,
            "status": "published",
            "draft_digest": "a" * 64,
            "review_ref": "crp://default/memory-candidates/candidate-alpha.json#review",
            "transition_ref": f"crp://default/memory-transitions/{transition_id}.json",
        },
        expected_revision=0,
    )
    return str(second["project_id"]), str(second["id"]), publication_id, transition_id


def test_mixed_fixture_copies_once_and_returns_activation_compatible_proof(tmp_path: Path) -> None:
    root, _store, inventory, ledger, dry_run, project_id, _skill_id, _, _ = _fixture(tmp_path)
    target = tmp_path / "target" / "shared.sqlite3"

    result = execute_shared_trust_audit_fixture_migration(
        object_store_root=root,
        target_database_path=target,
        ledger=ledger,
        dry_run=dry_run,
    )
    compared = compare_shared_trust_audit_fixture(
        object_store_root=root,
        target_database_path=target,
        namespace_id="default",
        migration_id=dry_run.migration_id,
    )

    assert inventory.is_migratable is True
    assert result == compared
    assert result.object_count == inventory.inventory.object_count
    assert set(result.member_migrations) == MEMBERS
    assert set(result.member_migrations.values()) == {dry_run.migration_id}
    assert SQLiteProjectSkillRepository(SQLiteStructuredRecordStore(target)).load(project_id) is not None
    evidence = result.activation_evidence(namespace_id="default", activation_id="fixture-activation-v1")
    assert evidence.member_migrations == result.member_migrations
    assert evidence.source_fingerprint == inventory.inventory.fingerprint
    assert evidence.target_fingerprint == result.target_fingerprint


@pytest.mark.parametrize("collection", ["memory_publications", "memory_transitions"])
def test_inventory_rejects_unknown_shared_audit_owner(tmp_path: Path, collection: str) -> None:
    root, store, *_ = _fixture(tmp_path)
    store.write(
        collection,
        f"unknown-{collection}",
        {"id": f"unknown-{collection}", "object_type": "document"},
        expected_revision=0,
    )

    inventory = scan_shared_trust_audit_fixture_inventory(root, namespace_id="default")

    assert inventory.is_migratable is False
    assert any(f"shared:unknown_owner:{collection}" in issue for issue in inventory.issues)


def test_inventory_rejects_project_skill_cross_reference_drift(tmp_path: Path) -> None:
    root, store, *_prefix, publication_id, _transition_id = _fixture(tmp_path)
    publication = store.read("memory_publications", publication_id)
    assert publication is not None
    publication["transition_ref"] = "crp://default/memory-transitions/missing.json"
    store.write("memory_publications", publication_id, publication, expected_revision=1)

    inventory = scan_shared_trust_audit_fixture_inventory(root, namespace_id="default")

    assert inventory.is_migratable is False
    assert any("project:publication_transition_missing_or_mismatch" in issue for issue in inventory.issues)


def test_inventory_rejects_shared_audit_owner_collision(tmp_path: Path) -> None:
    root, store, *_ = _fixture(tmp_path)
    collision_id = "transition-project-skill-collides-with-atom"
    store.write(
        "memory_transitions",
        collision_id,
        {"id": collision_id, "object_type": "project_skill", "object_id": "atom-fixture"},
        expected_revision=0,
    )

    inventory = scan_shared_trust_audit_fixture_inventory(root, namespace_id="default")

    assert inventory.is_migratable is False
    assert any("shared:owner_collision" in issue for issue in inventory.issues)


def test_executor_rejects_source_drift_and_preserves_existing_target(tmp_path: Path) -> None:
    root, store, _inventory, ledger, dry_run, *_ = _fixture(tmp_path)
    atom = store.read("memory_atoms", "atom-fixture")
    assert atom is not None
    atom["updated_at"] = "2026-07-13T00:00:00+08:00"
    store.write("memory_atoms", "atom-fixture", atom, expected_revision=1)
    target = tmp_path / "target" / "drift.sqlite3"

    with pytest.raises(SharedTrustAuditFixtureMigrationError, match="inconsistent|fingerprint"):
        execute_shared_trust_audit_fixture_migration(
            object_store_root=root,
            target_database_path=target,
            ledger=ledger,
            dry_run=dry_run,
        )
    assert not target.exists()

    existing = tmp_path / "existing.sqlite3"
    existing.write_bytes(b"keep")
    with pytest.raises(SharedTrustAuditFixtureMigrationError, match="already exist"):
        execute_shared_trust_audit_fixture_migration(
            object_store_root=root,
            target_database_path=existing,
            ledger=ledger,
            dry_run=dry_run,
        )
    assert existing.read_bytes() == b"keep"

    root, _store, _inventory, ledger, dry_run, *_ = _fixture(tmp_path / "nested")
    with pytest.raises(SharedTrustAuditFixtureMigrationError, match="inside source root"):
        execute_shared_trust_audit_fixture_migration(
            object_store_root=root,
            target_database_path=root / "not-the-runtime-target.sqlite3",
            ledger=ledger,
            dry_run=dry_run,
        )


def test_executor_rolls_back_and_cleans_mid_copy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _store, _inventory, ledger, dry_run, *_ = _fixture(tmp_path)
    target = tmp_path / "target" / "failure.sqlite3"
    original_put = SQLiteStructuredRecordUnitOfWork.put
    calls = 0

    def fail_during_copy(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 5:
            raise RuntimeError("injected composite copy failure")
        return original_put(self, *args, **kwargs)

    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", fail_during_copy)
    with pytest.raises(SharedTrustAuditFixtureMigrationError, match="injected composite"):
        execute_shared_trust_audit_fixture_migration(
            object_store_root=root,
            target_database_path=target,
            ledger=ledger,
            dry_run=dry_run,
        )
    assert not target.exists()
    assert not Path(f"{target}-wal").exists()
    assert not Path(f"{target}-shm").exists()


def test_compatibility_is_read_only_and_rejects_target_mismatch(tmp_path: Path) -> None:
    root, _store, _inventory, ledger, dry_run, *_ = _fixture(tmp_path)
    target = tmp_path / "target" / "shared.sqlite3"
    execute_shared_trust_audit_fixture_migration(
        object_store_root=root,
        target_database_path=target,
        ledger=ledger,
        dry_run=dry_run,
    )
    before = (target.stat().st_size, target.stat().st_mtime_ns)
    compare_shared_trust_audit_fixture(
        object_store_root=root,
        target_database_path=target,
        namespace_id="default",
        migration_id=dry_run.migration_id,
    )
    assert (target.stat().st_size, target.stat().st_mtime_ns) == before

    records = SQLiteStructuredRecordStore(target)
    with records.begin() as transaction:
        atom = transaction.read("memory_atoms", "atom-fixture")
        assert atom is not None
        transaction.delete("memory_atoms", "atom-fixture", expected_revision=atom.revision)
        transaction.commit()
    with pytest.raises(SharedTrustAuditFixtureMigrationError, match="collection set mismatch|object set mismatch"):
        compare_shared_trust_audit_fixture(
            object_store_root=root,
            target_database_path=target,
            namespace_id="default",
            migration_id=dry_run.migration_id,
        )
