from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from core.project_skill_core import (
    ObjectStoreProjectSkillRepository,
    ProjectSkillMigrationInventoryError,
    ProjectSkillUpdate,
    plan_project_skill_migration_dry_run,
    scan_project_skill_migration_inventory,
)
from core.storage_provider import (
    JsonObjectStore,
    MigrationLedgerConflict,
    SQLiteMigrationLedger,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    ROOT
    / "core-contracts"
    / "rebuild"
    / "fixtures"
    / "project_skill"
    / "valid-active-skill.json"
)


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / ".rebuild-data", tmp_path / "library"


def _store(tmp_path: Path) -> JsonObjectStore:
    rebuild_root, library_root = _roots(tmp_path)
    return JsonObjectStore(rebuild_root, legacy_root=library_root)


def _update(
    structured: dict[str, object],
    *,
    expected_revision: int,
    markdown: str,
) -> ProjectSkillUpdate:
    return ProjectSkillUpdate(
        project_id=str(structured["project_id"]),
        markdown=markdown,
        structured=structured,
        expected_revision=expected_revision,
        reason="user confirmed inventory fixture",
    )


def _healthy_fixture(tmp_path: Path, *, skill_id: str | None = None):
    store = _store(tmp_path)
    repository = ObjectStoreProjectSkillRepository(store)
    structured = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if skill_id is not None:
        structured["id"] = skill_id
    first = repository.save(
        _update(
            structured,
            expected_revision=0,
            markdown="# Private Skill\n\nFirst private rule.",
        )
    )
    second_input = copy.deepcopy(first)
    second_input["style_preferences"]["voice"] = "Private voice must not enter inventory"
    second = repository.save(
        _update(
            second_input,
            expected_revision=1,
            markdown="# Private Skill\n\nSecond private rule.",
        )
    )
    return store, str(second["project_id"]), str(second["id"])


def _scan(tmp_path: Path):
    rebuild_root, _library_root = _roots(tmp_path)
    return scan_project_skill_migration_inventory(
        rebuild_root,
        namespace_id="default",
    )


def _root_for_short_object_fixture(tmp_path: Path, collection: str, object_id: str) -> Path:
    short_stem = "~h-" + hashlib.sha256(object_id.encode("utf-8")).hexdigest()
    for depth in range(121):
        base = tmp_path / ("d" * depth)
        root = base / ".rebuild-data"
        legacy_meta = root / "objects" / "default" / collection / f"{object_id}.meta.json"
        short_meta = root / "objects" / "default" / collection / f"{short_stem}.meta.json"
        if len(str(legacy_meta)) > 259 and len(str(short_meta)) <= 259:
            return base
    raise AssertionError("could not construct Project Skill short filename fixture")


def test_project_skill_inventory_is_path_free_deterministic_and_ledger_idempotent(
    tmp_path: Path,
) -> None:
    _healthy_fixture(tmp_path)

    first = _scan(tmp_path)
    repeated = _scan(tmp_path)
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    plan = plan_project_skill_migration_dry_run(
        ledger=ledger,
        migration_id="json-to-sqlite-project-skills-v1",
        target_schema_version=2,
        inventory=first,
        rollback_pointer="snapshot:project-skills-v1",
    )

    assert first == repeated
    assert first.issues == ()
    assert first.inventory.object_count == 8
    assert [item.collection for item in first.inventory.collections] == [
        "project_skill_index",
        "project_skill_json",
        "project_skill_markdown",
        "project_skill_revisions",
        "project_skills",
    ]
    assert [item.object_count for item in first.inventory.collections] == [1, 2, 2, 2, 1]
    assert str(tmp_path) not in repr(first)
    assert "Private Skill" not in repr(first)
    assert "Private voice" not in repr(first)
    assert plan == plan_project_skill_migration_dry_run(
        ledger=ledger,
        migration_id="json-to-sqlite-project-skills-v1",
        target_schema_version=2,
        inventory=repeated,
        rollback_pointer="snapshot:project-skills-v1",
    )


def test_project_skill_inventory_recovers_logical_ids_from_short_object_names(
    tmp_path: Path,
) -> None:
    skill_id = "skill-" + "x" * 118
    revision_id = f"{skill_id}~r1"
    base = _root_for_short_object_fixture(tmp_path, "project_skill_revisions", revision_id)
    _store_instance, _project_id, stored_skill_id = _healthy_fixture(base, skill_id=skill_id)

    inventory = _scan(base)

    assert stored_skill_id == skill_id
    assert inventory.issues == ()
    assert inventory.inventory.object_count == 8
    short_stem = "~h-" + hashlib.sha256(revision_id.encode("utf-8")).hexdigest()
    assert (
        base
        / ".rebuild-data"
        / "objects"
        / "default"
        / "project_skill_revisions"
        / f"{short_stem}.json"
    ).exists()


@pytest.mark.parametrize(
    ("case", "expected_code"),
    (
        ("missing-index", "project_index_missing"),
        ("missing-markdown", "skill_markdown_missing"),
        ("orphan-json", "orphan_skill_child"),
        ("pointer-drift", "current_revision_pointer_mismatch"),
        ("current-json-drift", "current_skill_json_mismatch"),
        ("stale-active", "active_context_stale"),
    ),
)
def test_project_skill_inventory_reports_partial_or_unpublishable_state(
    tmp_path: Path,
    case: str,
    expected_code: str,
) -> None:
    case_root = tmp_path / case
    store, project_id, skill_id = _healthy_fixture(case_root)
    if case == "missing-index":
        store.delete("project_skill_index", project_id)
    elif case == "missing-markdown":
        store.delete("project_skill_markdown", f"{skill_id}~r2")
    elif case == "orphan-json":
        store.write(
            "project_skill_json",
            "skill-missing~r1",
            {
                "id": "skill-missing~r1",
                "skill_id": "skill-missing",
                "project_id": "project-missing",
                "revision": 1,
                "structured": {},
            },
            expected_revision=0,
        )
    elif case == "current-json-drift":
        payload = store.read("project_skill_json", f"{skill_id}~r2")
        assert payload is not None
        payload["structured"]["purpose"] = "drifted structured copy"
        store.write("project_skill_json", f"{skill_id}~r2", payload, expected_revision=1)
    else:
        skill = store.read("project_skills", skill_id)
        assert skill is not None
        if case == "pointer-drift":
            skill["markdown_revision"] = 99
        else:
            skill["required_context"][0]["stale"] = True
        store.write("project_skills", skill_id, skill, expected_revision=2)

    inventory = _scan(case_root)

    assert expected_code in {issue.code for issue in inventory.issues}
    assert all(not hasattr(issue, "markdown") for issue in inventory.issues)


def test_inconsistent_project_skill_inventory_cannot_enter_ledger(tmp_path: Path) -> None:
    store, project_id, _skill_id = _healthy_fixture(tmp_path)
    store.delete("project_skill_index", project_id)
    inventory = _scan(tmp_path)
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")

    with pytest.raises(ProjectSkillMigrationInventoryError, match="project_index_missing"):
        plan_project_skill_migration_dry_run(
            ledger=ledger,
            migration_id="json-to-sqlite-project-skills-v1",
            target_schema_version=2,
            inventory=inventory,
            rollback_pointer="snapshot:project-skills-v1",
        )

    assert ledger.list_records() == ()


def test_ledger_rejects_changed_project_skill_inventory_for_same_id(tmp_path: Path) -> None:
    store, _project_id, skill_id = _healthy_fixture(tmp_path)
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    first = _scan(tmp_path)
    plan_project_skill_migration_dry_run(
        ledger=ledger,
        migration_id="json-to-sqlite-project-skills-v1",
        target_schema_version=2,
        inventory=first,
        rollback_pointer="snapshot:project-skills-v1",
    )
    skill = store.read("project_skills", skill_id)
    assert skill is not None
    skill["purpose"] = "changed after dry-run"
    store.write("project_skills", skill_id, skill, expected_revision=2)
    structured = store.read("project_skill_json", f"{skill_id}~r2")
    assert structured is not None
    structured["structured"]["purpose"] = "changed after dry-run"
    store.write("project_skill_json", f"{skill_id}~r2", structured, expected_revision=1)
    changed = _scan(tmp_path)

    assert changed.issues == ()
    with pytest.raises(MigrationLedgerConflict, match="input fingerprint"):
        plan_project_skill_migration_dry_run(
            ledger=ledger,
            migration_id="json-to-sqlite-project-skills-v1",
            target_schema_version=2,
            inventory=changed,
            rollback_pointer="snapshot:project-skills-v1",
        )
