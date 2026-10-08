from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.composition import build_project_skill_repository
from core.project_skill_core import (
    ObjectStoreProjectSkillRepository,
    ProjectSkillExpectedRevisionError,
    ProjectSkillRepositoryError,
    ProjectSkillUpdate,
)
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _fixture(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / "fixtures" / "project_skill" / name).read_text(encoding="utf-8"))


def _repository(tmp_path: Path) -> ObjectStoreProjectSkillRepository:
    return ObjectStoreProjectSkillRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )


def _update(
    *,
    structured: dict[str, object],
    markdown: str = "# Alpha 项目 Skill\n\n沿用旧结构。",
    expected_revision: int = 0,
    reason: str = "user confirmed project skill",
) -> ProjectSkillUpdate:
    return ProjectSkillUpdate(
        project_id=str(structured["project_id"]),
        markdown=markdown,
        structured=structured,
        expected_revision=expected_revision,
        reason=reason,
    )


def test_project_skill_repository_persists_markdown_json_and_synced_revision(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    structured = _fixture("valid-active-skill.json")

    saved = repository.save(_update(structured=structured))
    project_id = str(saved["project_id"])
    reloaded = _repository(tmp_path)

    loaded = reloaded.load(project_id)
    assert loaded == saved
    assert loaded is not None
    assert loaded["revision"] == 1
    assert loaded["markdown_revision"] == 1
    assert loaded["json_revision"] == 1
    assert reloaded.markdown(project_id) == "# Alpha 项目 Skill\n\n沿用旧结构。"
    assert reloaded.structured(project_id) == saved
    assert [item["revision"] for item in reloaded.revisions(project_id)] == [1]
    assert validate_contract_instance("project_skill.schema.json", _schema("project_skill.schema.json"), loaded) == []
    assert not (tmp_path / "library").exists()


def test_project_skill_repository_reads_current_skills_by_identity_and_project(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = repository.save(_update(structured=_fixture("valid-active-skill.json")))
    other = _fixture("valid-active-skill.json")
    other["id"] = "skill-other-project"
    other["project_id"] = "project-other"
    repository.save(_update(structured=other))

    assert repository.get(str(first["id"])) == first
    assert repository.get("missing-skill") is None
    assert [skill["id"] for skill in repository.list_by_project(str(first["project_id"]))] == [first["id"]]


def test_project_skill_repository_updates_append_only_and_rejects_stale_revision(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    structured = _fixture("valid-active-skill.json")
    first = repository.save(_update(structured=structured))
    updated_structured = copy.deepcopy(first)
    updated_structured["style_preferences"]["voice"] = "直接、具体、保留来源"

    second = repository.save(
        _update(
            structured=updated_structured,
            markdown="# Alpha 项目 Skill\n\n沿用旧结构，并保留来源。",
            expected_revision=1,
            reason="update style preference",
        )
    )

    assert second["revision"] == 2
    assert second["markdown_revision"] == 2
    assert second["json_revision"] == 2
    assert repository.markdown(str(second["project_id"]), revision=1) == "# Alpha 项目 Skill\n\n沿用旧结构。"
    assert repository.markdown(str(second["project_id"])) == "# Alpha 项目 Skill\n\n沿用旧结构，并保留来源。"
    assert [item["revision"] for item in repository.revisions(str(second["project_id"]))] == [1, 2]
    with pytest.raises(ProjectSkillExpectedRevisionError):
        repository.save(
            _update(
                structured=updated_structured,
                markdown="stale edit",
                expected_revision=1,
                reason="stale update",
            )
        )


def test_project_skill_direct_edit_and_rollback_keep_transition_and_decision_history(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = repository.save(_update(structured=_fixture("valid-active-skill.json")))
    edited = copy.deepcopy(first)
    edited["style_preferences"]["voice"] = "用户直接确认的新表达"
    second = repository.save(ProjectSkillUpdate(
        project_id=str(first["project_id"]),
        markdown="# Alpha 项目 Skill\n\n用户直接编辑。",
        structured=edited,
        expected_revision=1,
        reason="用户直接修改表达偏好",
        transition_kind="user_edit",
        actor="user",
        confirmation_kind="direct_user_save",
    ))
    restored = repository.rollback(
        str(first["project_id"]),
        target_revision=1,
        expected_revision=2,
        reason="用户恢复第一版",
    )

    assert second["decision_log"][-1]["reason"] == "用户直接修改表达偏好"
    assert restored["revision"] == 3
    assert restored["style_preferences"] == first["style_preferences"]
    assert restored["decision_log"][-1]["reason"] == "用户恢复第一版"
    transitions = repository.revisions(str(first["project_id"]))
    assert [item["transition_kind"] for item in transitions] == ["revision_saved", "user_edit", "user_rollback"]
    assert transitions[-1]["source_revision"] == 1
    assert repository.markdown(str(first["project_id"])) == "# Alpha 项目 Skill\n\n沿用旧结构。"


def test_project_skill_publish_gate_rejects_stale_context_conflict_and_policy_drift(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    stale = _fixture("valid-active-skill.json")
    stale["required_context"][0]["stale"] = True
    with pytest.raises(ProjectSkillRepositoryError, match="stale required context"):
        repository.save(_update(structured=stale))

    conflict = _fixture("valid-active-skill.json")
    conflict["conflict"] = {"status": "detected", "conflict_refs": ["rule-structure"], "resolution": None}
    with pytest.raises(ProjectSkillRepositoryError, match="unresolved conflict"):
        repository.save(_update(structured=conflict))

    policy_drift = _fixture("valid-active-skill.json")
    policy_drift["update_rules"]["user_edit_policy"] = "conflict_required"
    with pytest.raises(ProjectSkillRepositoryError, match="user_edit_policy=user_wins"):
        repository.save(_update(structured=policy_drift))


def test_project_skill_repository_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    repository = build_project_skill_repository(ROOT, runtime_root=tmp_path)
    saved = repository.save(_update(structured=_fixture("valid-active-skill.json")))

    assert repository.load(str(saved["project_id"])) == saved
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "project_skills").exists()
    assert not (tmp_path / "library").exists()
