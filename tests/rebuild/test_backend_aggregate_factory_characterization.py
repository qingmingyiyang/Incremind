from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from backend.api.routes.product import (
    document_templates as product_document_templates,
    source_content as product_source_content,
)
import pytest
from backend.api.app import create_app
from backend.api.external_apply_saga import ExternalDocumentApplySagaService
from backend.api.external_project_skill_apply_saga import ExternalProjectSkillApplySagaService
from fastapi.testclient import TestClient

from core.aggregate_repository_factory import AUTHORITY_DATABASE_NAME, STRUCTURED_DATABASE_NAME, TARGET_IDENTITY
from core.document_engine import DocumentDraft, ObjectStoreDocumentRepository, SQLiteDocumentRepository
from core.effect_log import EFFECT_V2, EffectState
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate, SQLiteProjectSkillRepository
from core.storage_provider import (
    AggregateAuthorityEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteExternalApplySagaStore,
    SQLiteExternalProjectSkillApplySagaStore,
    SQLiteStructuredRecordStore,
    RebuildStorageSettings,
)


ROOT = Path(__file__).resolve().parents[2]
SKILL_FIXTURE = ROOT / "core-contracts" / "rebuild" / "fixtures" / "project_skill" / "valid-active-skill.json"


def _v2_effect_for_intent(runtime, *, kind: str, intent_ref: str):
    """Resolve the Core-owned v2 Effect, rather than treating the Saga id as one."""

    with runtime.log._connect() as connection:
        rows = connection.execute(
            "SELECT operation_id, gate_decision_id FROM effect "
            "WHERE kind=? AND contract_version=? AND intent_ref=?",
            (kind, EFFECT_V2, intent_ref),
        ).fetchall()
        assert len(rows) == 1
        effect_id, gate_decision_id = map(str, rows[0])
        assert connection.execute(
            "SELECT COUNT(*) FROM effect_gate_fact WHERE decision_id=?",
            (gate_decision_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM effect_intent_fact WHERE operation_id=?",
            (effect_id,),
        ).fetchone()[0] == 1
    effect = runtime.log.get(effect_id)
    assert effect.operation_id.startswith("eff2_")
    return effect


class _FailProjectSkillDraftFinalize:
    def __init__(self, delegate):
        self.delegate = delegate

    def read(self, collection, object_id): return self.delegate.read(collection, object_id)
    def list(self, collection): return self.delegate.list(collection)
    def delete(self, collection, object_id): return self.delegate.delete(collection, object_id)
    def write(self, collection, object_id, payload, expected_revision):
        if collection == "external_agent_review_drafts" and payload.get("status") == "applied":
            raise OSError("injected draft finalize failure")
        return self.delegate.write(collection, object_id, payload, expected_revision)


def _activate(records, authority, aggregate: str, migration_id: str) -> None:
    evidence = AggregateAuthorityEvidence(
        migration_id=migration_id,
        source_fingerprint="a" * 64,
        target_fingerprint="b" * 64,
        target_identity=TARGET_IDENTITY,
    )
    marker_id = f"default~{aggregate}"
    with records.begin() as uow:
        uow.put(
            "aggregate_authority_targets",
            marker_id,
            {
                "namespace_id": "default",
                "aggregate": aggregate,
                "migration_id": migration_id,
                "source_fingerprint": "a" * 64,
                "target_fingerprint": "b" * 64,
                "target_identity": TARGET_IDENTITY,
            },
            expected_revision=0,
        )
        uow.commit()
    initial = authority.create_json_active(namespace_id="default", aggregate=aggregate, reason="initial")
    staged = authority.transition(
        namespace_id="default",
        aggregate=aggregate,
        expected_revision=initial.revision,
        to_state="sqlite_staged",
        evidence=evidence,
        reason="staged",
    )
    authority.transition(
        namespace_id="default",
        aggregate=aggregate,
        expected_revision=staged.revision,
        to_state="sqlite_active",
        evidence=evidence,
        reason="active",
    )


def test_backend_read_endpoints_use_active_sqlite_authority(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    document = SQLiteDocumentRepository(records).create(
        DocumentDraft(
            title="Backend SQLite document",
            document_type="project_doc",
            markdown="Backend read endpoint sees SQLite authority.",
            source_refs=(
                {
                    "source_id": "source-backend-factory-001",
                    "locator": "char:0-44",
                    "quote": "Backend read endpoint sees SQLite authority.",
                },
            ),
        )
    )
    structured = json.loads(SKILL_FIXTURE.read_text(encoding="utf-8"))
    skill = SQLiteProjectSkillRepository(records).save(
        ProjectSkillUpdate(
            project_id=str(structured["project_id"]),
            markdown="# Backend SQLite Skill",
            structured=structured,
            expected_revision=0,
            reason="user confirmed backend factory fixture",
        )
    )
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    _activate(records, authority, "documents", "backend-documents-v1")
    _activate(records, authority, "project_skills", "backend-project-skills-v1")

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        skill_response = client.get(f"/api/rebuild/projects/{skill['project_id']}/skill")
        revisions_response = client.get(f"/api/rebuild/documents/{document['id']}/revisions")
        html_response = client.get(f"/api/rebuild/documents/{document['id']}/html")

    assert skill_response.status_code == 200
    assert skill_response.json()["id"] == skill["id"]
    assert revisions_response.status_code == 200
    assert revisions_response.json()["document_id"] == document["id"]
    assert html_response.status_code == 200
    assert "Backend read endpoint sees SQLite authority." in html_response.text


def test_project_skill_outline_update_uses_active_sqlite_authority_without_json_split_write(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    sqlite_skills = SQLiteProjectSkillRepository(records)
    structured = json.loads(SKILL_FIXTURE.read_text(encoding="utf-8"))
    saved = sqlite_skills.save(
        ProjectSkillUpdate(
            project_id=str(structured["project_id"]),
            markdown="# Backend SQLite Skill",
            structured=structured,
            expected_revision=0,
            reason="user confirmed outline authority fixture",
        )
    )
    project_id = str(saved["project_id"])
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    _activate(records, authority, "project_skills", "backend-project-skill-outline-v1")
    json_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        updated = client.put(
            f"/api/rebuild/projects/{project_id}/skill/outline",
            json={
                "expected_revision": 1,
                "outline": [
                    {"section_id": "summary", "title": "结论", "kind": "summary", "required": True},
                ],
                "reason": "用户确认SQLite项目章节结构",
            },
        )
        conflict = client.put(
            f"/api/rebuild/projects/{project_id}/skill/outline",
            json={"expected_revision": 1, "outline": [], "reason": "旧revision冲突"},
        )
        rolled_back = client.post(
            f"/api/rebuild/projects/{project_id}/skill/rollback",
            json={
                "confirm": True,
                "target_revision": 1,
                "expected_revision": 2,
                "reason": "用户恢复SQLite项目章节结构",
            },
        )

    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["outline"][0]["section_id"] == "summary"
    assert conflict.status_code == 409
    assert rolled_back.status_code == 200
    assert rolled_back.json()["revision"] == 3
    assert rolled_back.json()["outline"] == []
    assert sqlite_skills.load(project_id)["revision"] == 3
    assert sqlite_skills.structured(project_id, revision=2)["outline"][0]["section_id"] == "summary"
    assert sqlite_skills.revisions(project_id)[-1]["transition_kind"] == "user_rollback"
    assert ObjectStoreProjectSkillRepository(json_store).load(project_id) is None


def test_template_outline_override_reads_active_sqlite_project_skill_without_json_split_read(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    sqlite_skills = SQLiteProjectSkillRepository(records)
    structured = json.loads(SKILL_FIXTURE.read_text(encoding="utf-8"))
    saved = sqlite_skills.save(
        ProjectSkillUpdate(
            project_id=str(structured["project_id"]),
            markdown="# SQLite outline override",
            structured=structured,
            expected_revision=0,
            reason="template authority fixture",
        )
    )
    project_id = str(saved["project_id"])
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    _activate(records, authority, "project_skills", "backend-project-skill-template-outline-v1")
    json_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    settings = RebuildStorageSettings.from_toml(ROOT / "config" / "rebuild.toml.example", repository_root=ROOT)

    outline = product_document_templates._outline_override_for_project(tmp_path, json_store, settings, project_id)
    missing = product_document_templates._outline_override_for_project(tmp_path, json_store, settings, "project-missing")

    assert outline == structured.get("outline")
    assert missing is None
    assert ObjectStoreProjectSkillRepository(json_store).load(project_id) is None


def test_external_project_skill_apply_uses_active_sqlite_authority_and_http_replay_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    sqlite_skills = SQLiteProjectSkillRepository(records)
    structured = json.loads(SKILL_FIXTURE.read_text(encoding="utf-8"))
    for key in ("revision", "markdown_revision", "json_revision", "markdown_uri", "json_uri", "created_at", "updated_at"):
        structured.pop(key, None)
    saved = sqlite_skills.save(ProjectSkillUpdate(
        project_id=str(structured["project_id"]), markdown="# Initial SQLite Skill",
        structured=structured, expected_revision=0, reason="initial",
    ))
    project_id, skill_id = str(saved["project_id"]), str(saved["id"])
    proposed = dict(structured)
    proposed["style_preferences"] = {"voice": "saga route", "format_defaults": ["Markdown"]}
    draft_id = "draft-skill-route-sqlite"
    json_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    json_store.write("external_agent_review_drafts", draft_id, {
        "id": draft_id, "draft_type": "project_skill_update", "status": "pending_review",
        "project_id": project_id, "target_id": skill_id,
        "suggested_changes": {"structured": proposed, "markdown": "# Applied via saga route"},
        "review": {"state": "pending_review"}, "application": {"state": "blocked"},
    }, expected_revision=0)
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    _activate(records, authority, "project_skills", "backend-project-skill-route-v1")
    observed_handler_states = []
    runtime_holder = {}
    original_apply = ExternalProjectSkillApplySagaService.apply

    def apply_after_effect_claim(self, operation_id, *, expected_revision):
        observed_handler_states.append(
            _v2_effect_for_intent(
                runtime_holder["runtime"],
                kind="external_project_skill_apply",
                intent_ref=(
                    "crp://default/external-project-skill-apply-intents/"
                    f"{operation_id}"
                ),
            ).state
        )
        return original_apply(self, operation_id, expected_revision=expected_revision)

    monkeypatch.setattr(ExternalProjectSkillApplySagaService, "apply", apply_after_effect_claim)

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        runtime_holder["runtime"] = client.app.state.effect_runtime
        first = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )
        effect = _v2_effect_for_intent(
            client.app.state.effect_runtime,
            kind="external_project_skill_apply",
            intent_ref=(
                "crp://default/external-project-skill-apply-intents/"
                f"{draft_id}"
            ),
        )
        replay = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )

    assert first.status_code == 200
    assert effect.kind == "external_project_skill_apply"
    assert effect.state is EffectState.SETTLED_OK
    assert effect.attempt == 1
    assert effect.result_ref is not None
    assert observed_handler_states == [EffectState.INFLIGHT]
    assert replay.status_code == 409
    assert first.json()["project_skill_revision"] == 2
    assert sqlite_skills.load(project_id)["revision"] == 2
    assert len(sqlite_skills.revisions(project_id)) == 2
    assert ObjectStoreProjectSkillRepository(json_store).load(project_id) is None
    assert json_store.read("external_agent_review_drafts", draft_id)["status"] == "applied"
    assert SQLiteExternalProjectSkillApplySagaStore(records).get(draft_id).state == "finalized"


def test_external_project_skill_http_replay_finishes_skill_applied_half_commit(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    skills = SQLiteProjectSkillRepository(records)
    structured = json.loads(SKILL_FIXTURE.read_text(encoding="utf-8"))
    for key in ("revision", "markdown_revision", "json_revision", "markdown_uri", "json_uri", "created_at", "updated_at"):
        structured.pop(key, None)
    saved = skills.save(ProjectSkillUpdate(str(structured["project_id"]), "# Initial", structured, 0, "initial"))
    draft_id = "draft-skill-http-half-commit"
    drafts = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    drafts.write("external_agent_review_drafts", draft_id, {
        "id": draft_id, "draft_type": "project_skill_update", "status": "pending_review",
        "project_id": saved["project_id"], "target_id": saved["id"],
        "suggested_changes": {"structured": structured, "markdown": "# Recovered over HTTP"},
        "review": {"state": "pending_review"}, "application": {"state": "blocked"},
    }, expected_revision=0)
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    _activate(records, authority, "project_skills", "backend-project-skill-http-replay-v1")
    operations = SQLiteExternalProjectSkillApplySagaStore(records)

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        with pytest.raises(OSError, match="draft finalize failure"):
            ExternalProjectSkillApplySagaService(
                skills=skills, drafts=_FailProjectSkillDraftFinalize(drafts), operations=operations,
                project_skill_authority_identity="sqlite:structured-records-v1",
            ).apply(draft_id, expected_revision=1)
        replay = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )

    assert replay.status_code == 200
    assert replay.json()["project_skill_revision"] == 2
    assert operations.get(draft_id).state == "finalized"
    assert skills.load(str(saved["project_id"]))["revision"] == 2
    assert len(skills.revisions(str(saved["project_id"]))) == 2


def test_external_project_skill_preview_reads_active_sqlite_without_writes(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    skills = SQLiteProjectSkillRepository(records)
    structured = json.loads(SKILL_FIXTURE.read_text(encoding="utf-8"))
    for key in ("revision", "markdown_revision", "json_revision", "markdown_uri", "json_uri", "created_at", "updated_at"):
        structured.pop(key, None)
    saved = skills.save(ProjectSkillUpdate(
        str(structured["project_id"]), "# SQLite authority preview", structured, 0, "initial",
    ))
    proposed = dict(saved)
    proposed["revision"] = 2
    proposed["style_preferences"] = {"voice": "preview changed", "format_defaults": ["Markdown"]}
    draft_id = "draft-skill-preview-sqlite"
    json_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    json_store.write("external_agent_review_drafts", draft_id, {
        "id": draft_id, "draft_type": "project_skill_update", "status": "pending_review",
        "project_id": saved["project_id"], "target_id": saved["id"],
        "suggested_changes": {"structured": proposed, "markdown": "# Proposed preview"},
        "review": {"state": "pending_review"}, "application": {"state": "blocked"},
    }, expected_revision=0)
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    _activate(records, authority, "project_skills", "backend-project-skill-preview-v1")

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        response = client.get(f"/api/rebuild/external-agent/review-drafts/{draft_id}/preview")

    assert response.status_code == 200
    preview = response.json()["preview"]
    assert preview["target_exists"] is True
    assert preview["current_revision"] == 1
    assert preview["proposed_revision"] == 2
    assert "style_preferences" in preview["changed_fields"]
    assert "SQLite authority preview" in preview["current_summary"]
    assert skills.load(str(saved["project_id"]))["revision"] == 1
    assert len(skills.revisions(str(saved["project_id"]))) == 1
    assert ObjectStoreProjectSkillRepository(json_store).load(str(saved["project_id"])) is None


def test_backend_document_mutations_use_active_sqlite_authority_without_json_split_write(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    sqlite_documents = SQLiteDocumentRepository(records)
    document = sqlite_documents.create(
        DocumentDraft(
            title="Backend SQLite mutation",
            document_type="project_doc",
            markdown="Initial SQLite document.",
            source_refs=(
                {
                    "source_id": "source-backend-mutation-001",
                    "locator": "char:0-24",
                    "quote": "Initial SQLite document.",
                },
            ),
        )
    )
    document_id = str(document["id"])
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    _activate(records, authority, "documents", "backend-document-mutations-v1")

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        put_response = client.put(
            f"/api/rebuild/documents/{document_id}",
            json={
                "expected_revision": 1,
                "title": "User-edited SQLite document",
                "markdown": "User edit remains authoritative.",
            },
        )
        assert put_response.status_code == 200
        assert put_response.json()["revision"] == 2
        protected_block = put_response.json()["blocks"][0]

        patch_response = client.patch(
            f"/api/rebuild/documents/{document_id}",
            json={
                "expected_revision": 2,
                "reason": "verify SQLite AI patch conflict",
                "blocks": [
                    {
                        "id": protected_block["id"],
                        "block_type": protected_block["block_type"],
                        "origin": "ai",
                        "content": "AI must not overwrite the user edit.",
                        "source_refs": [],
                        "edited_by_user": False,
                        "lock_policy": "source_required",
                    }
                ],
            },
        )

    assert patch_response.status_code == 200
    assert patch_response.json()["status"] == "ai_patch_conflict"
    assert patch_response.json()["revision"] == 3
    current = sqlite_documents.read(document_id)
    assert current is not None
    assert current["revision"] == 3
    assert "User edit remains authoritative." in (sqlite_documents.markdown(document_id) or "")
    json_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    assert ObjectStoreDocumentRepository(json_store).read(document_id) is None


def test_backend_source_template_creates_document_in_active_sqlite_authority(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        product_source_content,
        "_fetch_url_text",
        lambda url: (
            "<html><body><h1>个人 AI 记忆工作台</h1>"
            "<p>结构化整理需要形成摘要、关键点和统一输出模板。</p>"
            "<p>项目总结需要保留待确认事项和来源引用。</p></body></html>"
        ),
    )
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        intake = client.post(
            "/api/rebuild/workbench/link-source-intake",
            json={"title": "SQLite authority source", "url": "https://example.com/sqlite-authority"},
        )
        assert intake.status_code == 201
        source_id = intake.json()["source_id"]
        assert client.post(f"/api/rebuild/sources/{source_id}/web-content", json={}).status_code == 200
        assert client.post(f"/api/rebuild/sources/{source_id}/structure-content", json={}).status_code == 200
        assert client.post(
            f"/api/rebuild/sources/{source_id}/series-assignment",
            json={"confirm": True},
        ).status_code == 200
        _activate(records, authority, "documents", "backend-document-creation-v1")

        response = client.post(
            f"/api/rebuild/sources/{source_id}/template-document",
            json={"template_type": "review"},
        )
        assert response.status_code == 200
        detail_response = client.get(f"/api/rebuild/documents/{response.json()['document_id']}")

    document_id = response.json()["document_id"]
    sqlite_document = SQLiteDocumentRepository(records).read(document_id)
    assert sqlite_document is not None
    assert sqlite_document["revision"] == 1
    assert detail_response.status_code == 200
    assert detail_response.json()["document_id"] == document_id
    json_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    assert ObjectStoreDocumentRepository(json_store).read(document_id) is None


def test_external_agent_document_preview_reads_active_sqlite_authority_without_writes(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    sqlite_documents = SQLiteDocumentRepository(records)
    document = sqlite_documents.create(
        DocumentDraft(
            title="External preview target",
            document_type="notes",
            markdown="Current SQLite authority content.",
            source_refs=(
                {
                    "source_id": "source-external-preview-001",
                    "locator": "char:0-33",
                    "quote": "Current SQLite authority content.",
                },
            ),
        )
    )
    document_id = str(document["id"])
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    _activate(records, authority, "documents", "backend-external-preview-v1")
    json_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    draft_id = "external-preview-sqlite-document"
    json_store.write(
        "external_agent_review_drafts",
        draft_id,
        {
            "schema_version": "1.0.0",
            "id": draft_id,
            "proposal_id": "proposal-external-preview-sqlite-document",
            "proposal_type": "document_revision_proposal",
            "draft_type": "document_revision",
            "status": "pending_review",
            "project_id": "default",
            "target_id": document_id,
            "summary": "Update the active SQLite document",
            "proposed_content": "Proposed external agent content.",
            "suggested_changes": [],
            "source_refs": [],
            "evidence_refs": [],
            "review": {"state": "pending_review", "requires_user_confirmation": True},
            "application": {"state": "not_applied", "blocked_operations": []},
        },
        expected_revision=None,
    )
    document_before = sqlite_documents.read(document_id)
    draft_before = json_store.read("external_agent_review_drafts", draft_id)

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        response = client.get(f"/api/rebuild/external-agent/review-drafts/{draft_id}/preview")

    assert response.status_code == 200
    preview = response.json()["preview"]
    assert preview["target_exists"] is True
    assert preview["current_revision"] == 1
    assert preview["proposed_revision"] == 2
    assert preview["changed_fields"] == ["markdown"]
    assert preview["current_summary"] == "Current SQLite authority content."
    assert sqlite_documents.read(document_id) == document_before
    assert json_store.read("external_agent_review_drafts", draft_id) == draft_before
    assert ObjectStoreDocumentRepository(json_store).read(document_id) is None


def test_external_agent_document_apply_uses_active_sqlite_authority_and_finalizes_saga(tmp_path: Path, monkeypatch) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    sqlite_documents = SQLiteDocumentRepository(records)
    document = sqlite_documents.create(
        DocumentDraft(
            title="External apply SQLite target",
            document_type="notes",
            markdown="Original SQLite apply content.",
            source_refs=(
                {
                    "source_id": "source-external-apply-001",
                    "locator": "char:0-30",
                    "quote": "Original SQLite apply content.",
                },
            ),
        )
    )
    document_id = str(document["id"])
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    _activate(records, authority, "documents", "backend-external-apply-v1")
    json_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    draft_id = "external-apply-sqlite-document"
    json_store.write(
        "external_agent_review_drafts",
        draft_id,
        {
            "schema_version": "1.0.0",
            "id": draft_id,
            "draft_type": "document_revision",
            "status": "pending_review",
            "project_id": "default",
            "target_id": document_id,
            "proposed_content": "User-confirmed external content.",
            "source_refs": [{"source_id": "source-external-apply-001", "locator": "char:0-30"}],
            "review": {"state": "pending_review", "requires_user_confirmation": True},
            "application": {"state": "not_applied"},
        },
        expected_revision=0,
    )
    observed_handler_states = []
    runtime_holder = {}
    original_apply = ExternalDocumentApplySagaService.apply

    def apply_after_effect_claim(self, operation_id, *, expected_revision):
        observed_handler_states.append(
            _v2_effect_for_intent(
                runtime_holder["runtime"],
                kind="external_document_apply",
                intent_ref=(
                    "crp://default/external-document-apply-intents/"
                    f"{operation_id}"
                ),
            ).state
        )
        return original_apply(self, operation_id, expected_revision=expected_revision)

    monkeypatch.setattr(ExternalDocumentApplySagaService, "apply", apply_after_effect_claim)

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        runtime_holder["runtime"] = client.app.state.effect_runtime
        applied = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )
        effect = _v2_effect_for_intent(
            client.app.state.effect_runtime,
            kind="external_document_apply",
            intent_ref=(
                "crp://default/external-document-apply-intents/"
                f"{draft_id}"
            ),
        )
        stale = client.post(
            f"/api/rebuild/external-agent/review-drafts/{draft_id}/apply",
            json={"confirm": True, "expected_revision": 1},
        )

    assert applied.status_code == 200
    assert effect.kind == "external_document_apply"
    assert effect.state is EffectState.SETTLED_OK
    assert effect.attempt == 1
    assert effect.result_ref is not None
    assert observed_handler_states == [EffectState.INFLIGHT]
    assert applied.json()["document_revision"] == 2
    assert stale.status_code == 409
    assert sqlite_documents.read(document_id)["revision"] == 2
    assert len(sqlite_documents.revisions(document_id)) == 2
    assert sqlite_documents.markdown(document_id) == "User-confirmed external content."
    assert json_store.read("external_agent_review_drafts", draft_id)["status"] == "applied"
    assert SQLiteExternalApplySagaStore(records).get(draft_id).state == "finalized"
    assert ObjectStoreDocumentRepository(json_store).read(document_id) is None


def test_template_memory_candidate_reads_active_sqlite_document_without_document_split_write(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    sqlite_documents = SQLiteDocumentRepository(records)
    document = sqlite_documents.create(
        DocumentDraft(
            title="SQLite review candidate source",
            document_type="review",
            markdown="# Review\n\nOnly a pending candidate may be created.",
            project_id="default",
            source_refs=(
                {
                    "source_id": "source-template-candidate-001",
                    "locator": "char:0-48",
                    "quote": "Only a pending candidate may be created.",
                },
            ),
        )
    )
    document_id = str(document["id"])
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    _activate(records, authority, "documents", "backend-template-candidate-v1")
    json_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        created = client.post(
            f"/api/rebuild/documents/{document_id}/template-memory-candidate",
            json={"document_revision": 1},
        )
        duplicate = client.post(
            f"/api/rebuild/documents/{document_id}/template-memory-candidate",
            json={"document_revision": 1},
        )

    assert created.status_code == 200
    body = created.json()
    assert body["candidate_status"] == "pending_review"
    assert body["memory_publication_state"] == "candidate_created_not_published"
    assert "auto_promote_memory" in body["blocked_operations"]
    assert duplicate.status_code == 400
    candidate = ObjectStoreMemoryCandidateRepository(json_store).get(body["candidate_id"])
    assert candidate is not None
    assert candidate["status"] == "pending_review"
    assert candidate["review"]["auto_promote_allowed"] is False
    assert candidate["provenance"]["document_id"] == document_id
    assert candidate["provenance"]["document_revision"] == 1
    assert len(json_store.list("memory_candidates")) == 1
    assert sqlite_documents.read(document_id)["revision"] == 1
    assert ObjectStoreDocumentRepository(json_store).read(document_id) is None


def test_backend_factory_scope_limits_remaining_explicit_json_mutation_routes() -> None:
    source = (ROOT / "src" / "backend" / "api" / "routes" / "rebuild.py").read_text(encoding="utf-8")

    assert "skills = _project_skill_repository(container.root_dir, store, settings)" in source
    # Detail, revisions, patch, lifecycle and Library projection all resolve
    # the same aggregate authority instead of reading JSON directly. The
    # Library overview resolves through the aggregate factory directly since
    # the memory pulse authority alignment.
    assert source.count("documents = _document_repository(container.root_dir, store,") == 8
    assert source.count("document_resolution = _document_repository_resolution(") == 1
    assert source.count("documents=_document_repository(container.root_dir, store,") == 6
    assert source.count("skills = ObjectStoreProjectSkillRepository(store)") == 0
    assert "ObjectStoreProjectSkillRepository(object_store=store)" not in source
    assert source.count("documents = ObjectStoreDocumentRepository(store)") == 0
    assert source.count("ObjectStoreDocumentRepository(store)") == 0
