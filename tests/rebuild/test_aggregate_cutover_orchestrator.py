from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.aggregate_cutover_orchestrator import (
    AggregateCutoverOrchestratorError,
    stage_document_skill_cutover_fixture,
)
from core.aggregate_repository_factory import AUTHORITY_DATABASE_NAME, STRUCTURED_DATABASE_NAME
from core.composition import build_document_repository, build_project_skill_repository
from core.document_engine import (
    DocumentDraft,
    ObjectStoreDocumentRepository,
    SQLiteDocumentRepository,
    plan_document_migration_dry_run,
    scan_document_migration_inventory,
)
from core.project_skill_core import (
    ObjectStoreProjectSkillRepository,
    ProjectSkillUpdate,
    SQLiteProjectSkillRepository,
    plan_project_skill_migration_dry_run,
    scan_project_skill_migration_inventory,
)
from core.storage_provider import (
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteMigrationLedger,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
)


ROOT = Path(__file__).resolve().parents[2]
SKILL_FIXTURE = ROOT / "core-contracts" / "rebuild" / "fixtures" / "project_skill" / "valid-active-skill.json"


def _fixture(tmp_path: Path):
    rebuild_root = tmp_path / ".rebuild-data"
    store = JsonObjectStore(rebuild_root, legacy_root=tmp_path / "library")
    documents = ObjectStoreDocumentRepository(store)
    document = documents.create(
        DocumentDraft(
            title="Combined cutover document",
            document_type="project_doc",
            markdown="Combined cutover keeps JSON active while staged.",
            source_refs=(
                {
                    "source_id": "source-combined-001",
                    "locator": "char:0-48",
                    "quote": "Combined cutover keeps JSON active while staged.",
                },
            ),
        )
    )
    skills = ObjectStoreProjectSkillRepository(store)
    structured = json.loads(SKILL_FIXTURE.read_text(encoding="utf-8"))
    skill = skills.save(
        ProjectSkillUpdate(
            project_id=str(structured["project_id"]),
            markdown="# Combined Skill",
            structured=structured,
            expected_revision=0,
            reason="user confirmed combined fixture",
        )
    )
    document_inventory = scan_document_migration_inventory(rebuild_root, namespace_id="default")
    skill_inventory = scan_project_skill_migration_inventory(rebuild_root, namespace_id="default")
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    document_plan = plan_document_migration_dry_run(
        ledger=ledger,
        migration_id="combined-documents-v1",
        target_schema_version=2,
        inventory=document_inventory,
        rollback_pointer="snapshot:combined-documents",
    )
    skill_plan = plan_project_skill_migration_dry_run(
        ledger=ledger,
        migration_id="combined-project-skills-v1",
        target_schema_version=2,
        inventory=skill_inventory,
        rollback_pointer="snapshot:combined-project-skills",
    )
    return store, document, skill, document_inventory, skill_inventory, ledger, document_plan, skill_plan


def _stage(tmp_path: Path, ledger, document_plan, skill_plan):
    return stage_document_skill_cutover_fixture(
        runtime_root=tmp_path,
        ledger=ledger,
        document_dry_run=document_plan,
        project_skill_dry_run=skill_plan,
    )


def test_orchestrator_copies_both_aggregates_and_stages_authority_without_activation(tmp_path: Path) -> None:
    _store, document, skill, _di, _si, ledger, document_plan, skill_plan = _fixture(tmp_path)

    result = _stage(tmp_path, ledger, document_plan, skill_plan)

    target = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    assert result.target_created is True
    assert result.document_object_count == 3
    assert result.project_skill_object_count == 5
    assert authority.get("default", "documents").state == "sqlite_staged"
    assert authority.get("default", "project_skills").state == "sqlite_staged"
    assert SQLiteDocumentRepository(target).read(str(document["id"])) == document
    assert SQLiteProjectSkillRepository(target).load(str(skill["project_id"])) == skill
    assert type(build_document_repository(ROOT, runtime_root=tmp_path)) is ObjectStoreDocumentRepository
    assert type(build_project_skill_repository(ROOT, runtime_root=tmp_path)) is ObjectStoreProjectSkillRepository


def test_staged_orchestrator_is_idempotent_and_explicit_activation_enables_factory(tmp_path: Path) -> None:
    _store, document, skill, _di, _si, ledger, document_plan, skill_plan = _fixture(tmp_path)
    first = _stage(tmp_path, ledger, document_plan, skill_plan)
    repeated = _stage(tmp_path, ledger, document_plan, skill_plan)
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)

    assert first.document_authority_revision == repeated.document_authority_revision == 2
    assert first.project_skill_authority_revision == repeated.project_skill_authority_revision == 2
    assert repeated.target_created is False
    for aggregate in ("documents", "project_skills"):
        current = authority.get("default", aggregate)
        authority.transition(
            namespace_id="default",
            aggregate=aggregate,
            expected_revision=current.revision,
            to_state="sqlite_active",
            evidence=current.evidence,
            reason="explicit test activation after staged verification",
        )

    documents = build_document_repository(ROOT, runtime_root=tmp_path)
    skills = build_project_skill_repository(ROOT, runtime_root=tmp_path)
    assert type(documents) is SQLiteDocumentRepository
    assert type(skills) is SQLiteProjectSkillRepository
    assert documents.read(str(document["id"])) == document
    assert skills.load(str(skill["project_id"])) == skill


def test_copy_failure_removes_new_target_and_does_not_create_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, _document, _skill, document_inventory, skill_inventory, ledger, document_plan, skill_plan = _fixture(tmp_path)
    original_put = SQLiteStructuredRecordUnitOfWork.put
    count = 0

    def _fail_second(self, *args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("injected combined copy failure")
        return original_put(self, *args, **kwargs)

    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", _fail_second)
    with pytest.raises(AggregateCutoverOrchestratorError, match="injected combined"):
        _stage(tmp_path, ledger, document_plan, skill_plan)

    target = tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    assert target.exists() is False
    assert not (tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME).exists()
    assert scan_document_migration_inventory(tmp_path / ".rebuild-data", namespace_id="default") == document_inventory
    assert scan_project_skill_migration_inventory(tmp_path / ".rebuild-data", namespace_id="default") == skill_inventory


def test_drift_or_existing_mismatched_target_fails_closed(tmp_path: Path) -> None:
    store, document, _skill, _di, _si, ledger, document_plan, skill_plan = _fixture(tmp_path)
    current = store.read("documents", str(document["id"]))
    current["title"] = "drift after planning"
    store.write("documents", str(document["id"]), current, expected_revision=1)
    with pytest.raises(AggregateCutoverOrchestratorError, match="fingerprint changed"):
        _stage(tmp_path, ledger, document_plan, skill_plan)

    fresh = tmp_path / "fresh"
    _store, _document, _skill, _di, _si, ledger, document_plan, skill_plan = _fixture(fresh)
    target = SQLiteStructuredRecordStore(fresh / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    with target.begin() as uow:
        uow.put("unrelated", "keep", {"id": "keep"}, expected_revision=0)
        uow.commit()
    with pytest.raises(AggregateCutoverOrchestratorError, match="object set mismatch|marker mismatch"):
        _stage(fresh, ledger, document_plan, skill_plan)
    assert target.read("unrelated", "keep").payload == {"id": "keep"}
