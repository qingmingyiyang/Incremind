from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.composition import build_skill_document_handoff
from core.document_engine import ObjectStoreDocumentRepository
from core.product_core import CreateDocumentFromProjectSkill, SkillDocumentHandoffError
from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _fixture(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / "fixtures" / "project_skill" / name).read_text(encoding="utf-8"))


def _parts(tmp_path: Path) -> tuple[
    CreateDocumentFromProjectSkill,
    ObjectStoreProjectSkillRepository,
    ObjectStoreDocumentRepository,
]:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    skills = ObjectStoreProjectSkillRepository(object_store)
    documents = ObjectStoreDocumentRepository(object_store)
    return CreateDocumentFromProjectSkill(skills=skills, documents=documents), skills, documents


def _save_skill(
    skills: ObjectStoreProjectSkillRepository,
    structured: dict[str, object],
) -> dict[str, object]:
    return dict(
        skills.save(
            ProjectSkillUpdate(
                project_id=str(structured["project_id"]),
                markdown="# Alpha 项目 Skill\n\n沿用旧结构。",
                structured=structured,
                expected_revision=0,
                reason="test handoff skill",
            )
        )
    )


class _SkillStub:
    def __init__(self, skill: dict[str, object]) -> None:
        self._skill = skill

    def load(self, project_id: str) -> dict[str, object] | None:
        if self._skill.get("project_id") != project_id:
            return None
        return dict(self._skill)

    def save(self, update: ProjectSkillUpdate) -> dict[str, object]:
        raise AssertionError("handoff does not save Project Skill")


def test_skill_document_handoff_creates_source_backed_project_document(
    tmp_path: Path,
) -> None:
    handoff, skills, documents = _parts(tmp_path)
    skill = _save_skill(skills, _fixture("valid-active-skill.json"))

    result = handoff.execute(str(skill["project_id"]), title="Alpha 连续文档草稿")
    document = documents.read(result.document_id)
    revision = documents.revision(result.document_id, result.document_revision)
    markdown = documents.markdown(result.document_id)

    assert result.skill_id == skill["id"]
    assert result.skill_revision == 1
    assert result.document_revision == 1
    assert document is not None
    assert revision is not None
    assert document["type"] == "project_doc"
    assert document["project_id"] == "project-alpha"
    assert "后续更新优先 patch 既有结构" in (markdown or "")
    assert "生成文档必须保留可追溯 source refs" in (markdown or "")
    assert "直接、具体、可执行" in (markdown or "")
    assert document["source_snapshot"]["source_refs"] == [{"source_id": "source-text-001", "locator": "char:0-80"}]
    assert revision["source_snapshot"] == document["source_snapshot"]
    assert validate_contract_instance("document.schema.json", _schema("document.schema.json"), document) == []
    assert (
        validate_contract_instance(
            "document_revision.schema.json",
            _schema("document_revision.schema.json"),
            revision,
        )
        == []
    )
    assert not (tmp_path / "library").exists()


def test_project_skill_incremental_update_stably_drives_next_document_output(
    tmp_path: Path,
) -> None:
    handoff, skills, documents = _parts(tmp_path)
    first = _save_skill(skills, _fixture("valid-active-skill.json"))
    updated = copy.deepcopy(first)
    updated["output_rules"].append(
        {
            "rule_id": "rule-new-project-context",
            "priority": "must",
            "rule": "后续文档必须新增本轮项目资料里的验收清单。",
            "source_refs": [
                {
                    "source_id": "source-new-project-content",
                    "locator": "char:20-90",
                    "quote": "新增本轮项目资料里的验收清单",
                }
            ],
        }
    )
    updated["source_refs"].append(
        {
            "source_id": "source-new-project-content",
            "locator": "char:20-90",
            "quote": "新增本轮项目资料里的验收清单",
        }
    )

    second = skills.save(
        ProjectSkillUpdate(
            project_id=str(first["project_id"]),
            markdown="# Alpha 项目 Skill\n\n沿用旧结构，并新增本轮验收清单。",
            structured=updated,
            expected_revision=1,
            reason="new project content incrementally updates skill",
        )
    )
    result = handoff.execute(str(first["project_id"]), title="增量项目文档草稿")
    document = documents.read(result.document_id)
    markdown = documents.markdown(result.document_id)

    assert second["revision"] == 2
    assert result.skill_revision == 2
    assert "后续文档必须新增本轮项目资料里的验收清单" in (markdown or "")
    assert document is not None
    assert {"source_id": "source-new-project-content", "locator": "char:20-90", "quote": "新增本轮项目资料里的验收清单"} in document["source_snapshot"]["source_refs"]
    assert skills.markdown(str(first["project_id"]), revision=1) == "# Alpha 项目 Skill\n\n沿用旧结构。"
    assert not (tmp_path / "library").exists()


def test_skill_document_handoff_rejects_unsafe_current_skill(tmp_path: Path) -> None:
    handoff, skills, _documents = _parts(tmp_path)
    unsafe = _fixture("valid-active-skill.json")
    unsafe["status"] = "conflicted"
    unsafe["conflict"] = {"status": "detected", "conflict_refs": ["rule-structure"], "resolution": None}
    _save_skill(skills, unsafe)

    with pytest.raises(SkillDocumentHandoffError, match="active"):
        handoff.execute("project-alpha")


def test_skill_document_handoff_rejects_stale_context_after_save(tmp_path: Path) -> None:
    _handoff, _skills, documents = _parts(tmp_path)
    active_stale = _fixture("valid-active-skill.json")
    active_stale["required_context"][0]["stale"] = True
    handoff = CreateDocumentFromProjectSkill(
        skills=_SkillStub(active_stale),
        documents=documents,
    )

    with pytest.raises(SkillDocumentHandoffError, match="stale required context"):
        handoff.execute("project-alpha")


def test_skill_document_handoff_composition_uses_shared_temp_storage(tmp_path: Path) -> None:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    skills = ObjectStoreProjectSkillRepository(object_store)
    skill = _save_skill(skills, _fixture("valid-active-skill.json"))
    handoff = build_skill_document_handoff(ROOT, runtime_root=tmp_path)

    result = handoff.execute(str(skill["project_id"]), title="Composed Skill Document")
    documents = ObjectStoreDocumentRepository(object_store)
    assert documents.read(result.document_id) is not None
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "documents").exists()
    assert not (tmp_path / "library").exists()
