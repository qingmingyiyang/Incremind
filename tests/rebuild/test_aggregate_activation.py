from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.aggregate_activation import AggregateActivationError, activate_document_skill_fixture
from core.aggregate_cutover_orchestrator import stage_document_skill_cutover_fixture
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
from core.storage_provider import JsonObjectStore, SQLiteAggregateAuthorityStore, SQLiteMigrationLedger, SQLiteStructuredRecordStore


ROOT = Path(__file__).resolve().parents[2]
SKILL_FIXTURE = ROOT / "core-contracts" / "rebuild" / "fixtures" / "project_skill" / "valid-active-skill.json"


def _staged_fixture(tmp_path: Path):
    rebuild_root = tmp_path / ".rebuild-data"
    store = JsonObjectStore(rebuild_root, legacy_root=tmp_path / "library")
    document = ObjectStoreDocumentRepository(store).create(
        DocumentDraft(
            title="Activation document",
            document_type="project_doc",
            markdown="Activation requires local rollback evidence.",
            source_refs=(
                {
                    "source_id": "source-activation-001",
                    "locator": "char:0-44",
                    "quote": "Activation requires local rollback evidence.",
                },
            ),
        )
    )
    structured = json.loads(SKILL_FIXTURE.read_text(encoding="utf-8"))
    skill = ObjectStoreProjectSkillRepository(store).save(
        ProjectSkillUpdate(
            str(structured["project_id"]),
            "# Activation Skill",
            structured,
            0,
            "user confirmed activation fixture",
        )
    )
    document_inventory = scan_document_migration_inventory(rebuild_root, namespace_id="default")
    skill_inventory = scan_project_skill_migration_inventory(rebuild_root, namespace_id="default")
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    document_plan = plan_document_migration_dry_run(
        ledger=ledger,
        migration_id="activation-documents-v1",
        target_schema_version=2,
        inventory=document_inventory,
        rollback_pointer="snapshot:activation-documents",
    )
    skill_plan = plan_project_skill_migration_dry_run(
        ledger=ledger,
        migration_id="activation-project-skills-v1",
        target_schema_version=2,
        inventory=skill_inventory,
        rollback_pointer="snapshot:activation-project-skills",
    )
    stage_document_skill_cutover_fixture(
        runtime_root=tmp_path,
        ledger=ledger,
        document_dry_run=document_plan,
        project_skill_dry_run=skill_plan,
    )
    return store, document, skill


def _authority(tmp_path: Path) -> SQLiteAggregateAuthorityStore:
    return SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)


def _records(tmp_path: Path) -> SQLiteStructuredRecordStore:
    return SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)


def _write_evidence(tmp_path: Path, *, evidence_id: str = "activation-evidence-v1") -> str:
    authority = _authority(tmp_path)
    documents = authority.get("default", "documents")
    skills = authority.get("default", "project_skills")
    payload = {
        "id": evidence_id,
        "namespace_id": "default",
        "backup_pointer": "snapshot:fixture-backup-v1",
        "rollback_manifest_id": "rollback:fixture-manifest-v1",
        "verified_at": "2026-07-11T12:00:00+00:00",
        "verified_by": "local-verifier:fixture",
        "documents_source_fingerprint": documents.evidence.source_fingerprint,
        "documents_target_fingerprint": documents.evidence.target_fingerprint,
        "project_skills_source_fingerprint": skills.evidence.source_fingerprint,
        "project_skills_target_fingerprint": skills.evidence.target_fingerprint,
        "target_identity": documents.evidence.target_identity,
    }
    with _records(tmp_path).begin() as uow:
        uow.put("aggregate_activation_evidence", evidence_id, payload, expected_revision=0)
        uow.commit()
    return evidence_id


def test_activation_atomically_enables_both_factories_after_local_evidence(tmp_path: Path) -> None:
    _store, document, skill = _staged_fixture(tmp_path)
    evidence_id = _write_evidence(tmp_path)

    result = activate_document_skill_fixture(
        runtime_root=tmp_path,
        namespace_id="default",
        evidence_id=evidence_id,
    )

    authority = _authority(tmp_path)
    assert result.document_authority_revision == 3
    assert result.project_skill_authority_revision == 3
    assert authority.get("default", "documents").state == "sqlite_active"
    assert authority.get("default", "project_skills").state == "sqlite_active"
    documents = build_document_repository(ROOT, runtime_root=tmp_path)
    skills = build_project_skill_repository(ROOT, runtime_root=tmp_path)
    assert type(documents) is SQLiteDocumentRepository
    assert type(skills) is SQLiteProjectSkillRepository
    assert documents.read(str(document["id"])) == document
    assert skills.load(str(skill["project_id"])) == skill


def test_missing_or_invalid_local_evidence_leaves_both_authorities_staged(tmp_path: Path) -> None:
    _staged_fixture(tmp_path)
    with pytest.raises(AggregateActivationError, match="evidence is missing"):
        activate_document_skill_fixture(runtime_root=tmp_path, namespace_id="default", evidence_id="missing-evidence")

    authority = _authority(tmp_path)
    assert authority.get("default", "documents").state == "sqlite_staged"
    assert authority.get("default", "project_skills").state == "sqlite_staged"


def test_source_drift_or_target_marker_mismatch_leaves_both_staged(tmp_path: Path) -> None:
    store, document, _skill = _staged_fixture(tmp_path)
    evidence_id = _write_evidence(tmp_path)
    current = store.read("documents", str(document["id"]))
    current["title"] = "drift after staging"
    store.write("documents", str(document["id"]), current, expected_revision=1)
    with pytest.raises(AggregateActivationError, match="source fingerprint changed"):
        activate_document_skill_fixture(runtime_root=tmp_path, namespace_id="default", evidence_id=evidence_id)
    assert _authority(tmp_path).get("default", "documents").state == "sqlite_staged"
    assert _authority(tmp_path).get("default", "project_skills").state == "sqlite_staged"

    fresh = tmp_path / "marker"
    _staged_fixture(fresh)
    evidence_id = _write_evidence(fresh)
    records = _records(fresh)
    marker = records.read("aggregate_authority_targets", "default~documents")
    payload = dict(marker.payload)
    payload["target_fingerprint"] = "f" * 64
    with records.begin() as uow:
        uow.put("aggregate_authority_targets", "default~documents", payload, expected_revision=1)
        uow.commit()
    with pytest.raises(AggregateActivationError, match="target marker mismatch"):
        activate_document_skill_fixture(runtime_root=fresh, namespace_id="default", evidence_id=evidence_id)
    assert _authority(fresh).get("default", "documents").state == "sqlite_staged"
    assert _authority(fresh).get("default", "project_skills").state == "sqlite_staged"
