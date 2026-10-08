from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.composition import build_project_skill_first_recall
from core.product_core import CreateProjectSkillFirstRecall, ProjectSkillFirstRecallError
from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate
from core.search_and_recall import ObjectStoreRecallRepository
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _fixture(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / "fixtures" / "project_skill" / name).read_text(encoding="utf-8"))


def _parts(tmp_path: Path) -> tuple[
    CreateProjectSkillFirstRecall,
    ObjectStoreProjectSkillRepository,
    ObjectStoreRecallRepository,
]:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    skills = ObjectStoreProjectSkillRepository(object_store)
    recalls = ObjectStoreRecallRepository(object_store)
    return CreateProjectSkillFirstRecall(skills=skills, recalls=recalls), skills, recalls


def _save_skill(skills: ObjectStoreProjectSkillRepository, structured: dict[str, object]) -> dict[str, object]:
    return dict(
        skills.save(
            ProjectSkillUpdate(
                project_id=str(structured["project_id"]),
                markdown="# Alpha 项目 Skill\n\n沿用旧结构。",
                structured=structured,
                expected_revision=0,
                reason="test project skill first recall",
            )
        )
    )


def test_project_skill_first_recall_reads_current_skill_before_lower_layers(
    tmp_path: Path,
) -> None:
    use_case, skills, recalls = _parts(tmp_path)
    skill = _save_skill(skills, _fixture("valid-active-skill.json"))

    result = use_case.execute(
        "project-alpha",
        query="这个项目下一版应该先改哪里？",
        created_at="2026-06-29T15:30:00+08:00",
    )
    request = recalls.get_request(result.request_id)
    recall_result = recalls.get_result(result.result_id)

    assert result.skill_id == skill["id"]
    assert result.skill_revision == skill["revision"]
    assert request is not None
    assert recall_result is not None
    assert validate_contract_instance("recall_request.schema.json", _schema("recall_request.schema.json"), request) == []
    assert validate_contract_instance("recall_result.schema.json", _schema("recall_result.schema.json"), recall_result) == []
    assert request["required_context_refs"][0]["kind"] == "project_skill"
    assert request["required_context_refs"][0]["object_id"] == skill["id"]
    assert request["layers"][:2] == ["l4_persona", "l3_project_skill"]
    first_hit = recall_result["hits"][0]
    assert first_hit["layer"] == "l3_project_skill"
    assert first_hit["object_id"] == skill["id"]
    assert first_hit["project_id"] == "project-alpha"
    assert first_hit["source_project_label"] is None
    assert "后续更新优先 patch 既有结构" in first_hit["snippet"]
    assert recall_result["coverage"]["covered_layers"] == ["l3_project_skill"]
    assert recall_result["cross_project"] == {"used": False, "grant_id": None, "project_ids": []}
    assert "answer" not in recall_result
    assert not (tmp_path / "library").exists()


def test_project_skill_first_recall_rejects_missing_or_unsafe_skill(tmp_path: Path) -> None:
    use_case, skills, _recalls = _parts(tmp_path)
    with pytest.raises(ProjectSkillFirstRecallError, match="not found"):
        use_case.execute("project-alpha", query="这个项目下一版应该先改哪里？")

    draft = _fixture("valid-active-skill.json")
    draft["status"] = "draft"
    _save_skill(skills, draft)
    with pytest.raises(ProjectSkillFirstRecallError, match="active"):
        use_case.execute("project-alpha", query="这个项目下一版应该先改哪里？")


def test_project_skill_first_recall_composition_uses_shared_temp_storage(tmp_path: Path) -> None:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    skills = ObjectStoreProjectSkillRepository(object_store)
    skill = _save_skill(skills, _fixture("valid-active-skill.json"))
    use_case = build_project_skill_first_recall(ROOT, runtime_root=tmp_path)

    result = use_case.execute(
        str(skill["project_id"]),
        query="这个项目下一版应该先改哪里？",
        created_at="2026-06-29T15:30:00+08:00",
    )
    recalls = ObjectStoreRecallRepository(object_store)

    assert recalls.get_request(result.request_id) is not None
    assert recalls.get_result(result.result_id) is not None
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "recall_results").exists()
    assert not (tmp_path / "library").exists()
