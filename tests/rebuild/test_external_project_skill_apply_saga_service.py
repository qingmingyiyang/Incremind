from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.api.external_project_skill_apply_saga import (
    ExternalProjectSkillApplyConflict,
    ExternalProjectSkillApplySagaService,
)
from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate, SQLiteProjectSkillRepository
from core.storage_provider import (
    JsonObjectStore,
    SQLiteExternalProjectSkillApplySagaStore,
    SQLiteStructuredRecordStore,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "core-contracts" / "rebuild" / "fixtures" / "project_skill" / "valid-active-skill.json"


class FailDraftFinalizeOnce:
    def __init__(self, store):
        self.store = store
        self.failed = False

    def read(self, collection, object_id):
        return self.store.read(collection, object_id)

    def list(self, collection):
        return self.store.list(collection)

    def delete(self, collection, object_id):
        return self.store.delete(collection, object_id)

    def write(self, collection, object_id, payload, expected_revision):
        if collection == "external_agent_review_drafts" and payload.get("status") == "applied" and not self.failed:
            self.failed = True
            raise RuntimeError("injected draft finalize failure")
        return self.store.write(collection, object_id, payload, expected_revision)


def _fixture():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for key in ("revision", "markdown_revision", "json_revision", "markdown_uri", "json_uri", "created_at", "updated_at"):
        payload.pop(key, None)
    return payload


def _draft(structured):
    updated = dict(structured)
    updated["style_preferences"] = {"voice": "operation recovered", "format_defaults": ["Markdown"]}
    return {
        "id": "draft-skill-saga-001",
        "draft_type": "project_skill_update",
        "status": "pending_review",
        "project_id": "project-alpha",
        "target_id": "skill-project-alpha",
        "suggested_changes": {"structured": updated, "markdown": "# Recovered Skill"},
        "review": {"state": "pending_review"},
        "application": {"state": "blocked"},
    }


@pytest.mark.parametrize("authority", ["json:object-store-v1", "sqlite:structured-records-v1"])
def test_apply_finalizes_once_without_duplicate_skill_revision(tmp_path: Path, authority: str):
    json_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured.sqlite3")
    skills = ObjectStoreProjectSkillRepository(json_store) if authority.startswith("json") else SQLiteProjectSkillRepository(records)
    structured = _fixture()
    skills.save(ProjectSkillUpdate("project-alpha", "# Initial", structured, 0, "initial"))
    draft = _draft(structured)
    json_store.write("external_agent_review_drafts", draft["id"], draft, expected_revision=0)
    operations = SQLiteExternalProjectSkillApplySagaStore(records)
    service = ExternalProjectSkillApplySagaService(
        skills=skills, drafts=json_store, operations=operations,
        project_skill_authority_identity=authority,
    )

    first = service.apply(draft["id"], expected_revision=1)
    repeated = service.apply(draft["id"], expected_revision=1)

    assert first.state == repeated.state == "finalized"
    assert first.project_skill_revision == repeated.project_skill_revision == 2
    assert skills.load("project-alpha")["revision"] == 2
    assert len(skills.revisions("project-alpha")) == 2
    assert skills.revisions("project-alpha")[-1]["transition_kind"] == "external_proposal_apply"
    assert skills.revisions("project-alpha")[-1]["confirmation_kind"] == "external_review_confirmation"
    assert json_store.read("external_agent_review_drafts", draft["id"])["application"]["operation_id"] == draft["id"]


def test_replay_recovers_after_skill_commit_without_creating_revision_three(tmp_path: Path):
    json_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured.sqlite3")
    skills = SQLiteProjectSkillRepository(records)
    structured = _fixture()
    skills.save(ProjectSkillUpdate("project-alpha", "# Initial", structured, 0, "initial"))
    draft = _draft(structured)
    json_store.write("external_agent_review_drafts", draft["id"], draft, expected_revision=0)
    operations = SQLiteExternalProjectSkillApplySagaStore(records)
    failing = ExternalProjectSkillApplySagaService(
        skills=skills, drafts=FailDraftFinalizeOnce(json_store), operations=operations,
        project_skill_authority_identity="sqlite:structured-records-v1",
    )

    with pytest.raises(RuntimeError, match="injected draft finalize failure"):
        failing.apply(draft["id"], expected_revision=1)
    assert skills.load("project-alpha")["revision"] == 2
    assert operations.get(draft["id"]).state == "skill_applied"

    drifted = json_store.read("external_agent_review_drafts", draft["id"])
    drifted["suggested_changes"]["markdown"] = "# Drifted after commit"
    json_store.write("external_agent_review_drafts", draft["id"], drifted, expected_revision=None)
    with pytest.raises(ExternalProjectSkillApplyConflict, match="evidence drifted"):
        ExternalProjectSkillApplySagaService(
            skills=skills, drafts=json_store, operations=operations,
            project_skill_authority_identity="sqlite:structured-records-v1",
        ).apply(draft["id"], expected_revision=1)
    json_store.write("external_agent_review_drafts", draft["id"], draft, expected_revision=None)

    with pytest.raises(ExternalProjectSkillApplyConflict, match="evidence drifted"):
        ExternalProjectSkillApplySagaService(
            skills=skills, drafts=json_store, operations=operations,
            project_skill_authority_identity="json:object-store-v1",
        ).apply(draft["id"], expected_revision=1)

    recovered = ExternalProjectSkillApplySagaService(
        skills=skills, drafts=json_store, operations=operations,
        project_skill_authority_identity="sqlite:structured-records-v1",
    ).apply(draft["id"], expected_revision=1)
    assert recovered.state == "finalized"
    assert skills.load("project-alpha")["revision"] == 2
    assert len(skills.revisions("project-alpha")) == 2
