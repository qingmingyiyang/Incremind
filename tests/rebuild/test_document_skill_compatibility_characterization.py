from __future__ import annotations

import json
from pathlib import Path

from core.composition import build_document_repository, build_project_skill_repository
from core.document_engine import (
    DocumentDraft,
    ObjectStoreDocumentRepository,
    SQLiteDocumentRepository,
)
from core.project_skill_core import (
    ObjectStoreProjectSkillRepository,
    ProjectSkillUpdate,
    SQLiteProjectSkillRepository,
)
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


ROOT = Path(__file__).resolve().parents[2]
SKILL_FIXTURE = ROOT / "core-contracts" / "rebuild" / "fixtures" / "project_skill" / "valid-active-skill.json"


def _document_draft() -> DocumentDraft:
    return DocumentDraft(
        title="Authority fixture",
        document_type="project_doc",
        markdown="JSON and SQLite authorities are isolated.",
        source_refs=(
            {
                "source_id": "source-authority-001",
                "locator": "char:0-41",
                "quote": "JSON and SQLite authorities are isolated.",
            },
        ),
        project_id="project-alpha",
    )


def _skill_update() -> ProjectSkillUpdate:
    structured = json.loads(SKILL_FIXTURE.read_text(encoding="utf-8"))
    return ProjectSkillUpdate(
        project_id=str(structured["project_id"]),
        markdown="# Authority Skill\n\nJSON authority.",
        structured=structured,
        expected_revision=0,
        reason="user confirmed authority fixture",
    )


def test_public_builders_still_compose_json_as_the_only_runtime_authority(tmp_path: Path) -> None:
    documents = build_document_repository(ROOT, runtime_root=tmp_path)
    skills = build_project_skill_repository(ROOT, runtime_root=tmp_path)

    assert type(documents) is ObjectStoreDocumentRepository
    assert type(skills) is ObjectStoreProjectSkillRepository
    document = documents.create(_document_draft())
    skill = skills.save(_skill_update())
    assert documents.read(str(document["id"])) == document
    assert skills.load(str(skill["project_id"])) == skill
    assert not (tmp_path / ".rebuild-data" / "structured-records.sqlite3").exists()


def test_json_and_sqlite_repositories_do_not_provide_implicit_fallback(tmp_path: Path) -> None:
    json_store = JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )
    json_documents = ObjectStoreDocumentRepository(json_store)
    json_skills = ObjectStoreProjectSkillRepository(json_store)
    document = json_documents.create(_document_draft())
    skill = json_skills.save(_skill_update())
    sqlite_records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / "structured-records.sqlite3"
    )
    sqlite_documents = SQLiteDocumentRepository(sqlite_records)
    sqlite_skills = SQLiteProjectSkillRepository(sqlite_records)

    assert sqlite_documents.read(str(document["id"])) is None
    assert sqlite_skills.load(str(skill["project_id"])) is None

    sqlite_document = sqlite_documents.create(
        DocumentDraft(
            title="SQLite-only authority fixture",
            document_type="project_doc",
            markdown="Only SQLite can see this revision.",
            source_refs=(
                {
                    "source_id": "source-authority-002",
                    "locator": "char:0-34",
                    "quote": "Only SQLite can see this revision.",
                },
            ),
        )
    )
    sqlite_skill_structured = json.loads(SKILL_FIXTURE.read_text(encoding="utf-8"))
    sqlite_skill_structured["project_id"] = "project-sqlite-only"
    sqlite_skill_structured["id"] = "skill-project-sqlite-only"
    sqlite_skill = sqlite_skills.save(
        ProjectSkillUpdate(
            project_id="project-sqlite-only",
            markdown="# SQLite-only Skill",
            structured=sqlite_skill_structured,
            expected_revision=0,
            reason="user confirmed SQLite-only fixture",
        )
    )

    assert json_documents.read(str(sqlite_document["id"])) is None
    assert json_skills.load(str(sqlite_skill["project_id"])) is None


def test_backend_routes_use_internal_authority_factories_without_direct_json_constructors() -> None:
    route_source = (ROOT / "src" / "backend" / "api" / "routes" / "rebuild.py").read_text(encoding="utf-8")
    assert route_source.count("_document_repository(") >= 10
    assert route_source.count("_project_skill_repository(") >= 6
    assert "build_document_repository(" not in route_source
    assert "build_project_skill_repository(" not in route_source
    assert "ObjectStoreDocumentRepository(" not in route_source
    assert "ObjectStoreProjectSkillRepository(" not in route_source
    assert "SQLiteDocumentRepository" not in route_source
    assert "SQLiteProjectSkillRepository" not in route_source


def test_migration_ledger_has_no_cutover_authority_state() -> None:
    ledger_source = (
        ROOT / "src" / "core" / "storage_provider" / "migration_ledger.py"
    ).read_text(encoding="utf-8")

    assert "state IN ('dry_run_ready', 'failed')" in ledger_source
    assert "cutover_ready" not in ledger_source
    assert "cutover_active" not in ledger_source
    assert "rollback_complete" not in ledger_source
