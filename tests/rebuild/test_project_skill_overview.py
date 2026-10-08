from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.composition import build_project_skill_overview
from core.product_core import (
    GetProjectSkillOverview,
    ProjectSkillOverviewError,
    ServeProjectSkillOverviewEndpoint,
    serialize_project_skill_overview,
)
from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate
from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / "fixtures" / "project_skill" / name).read_text(encoding="utf-8"))


def _parts(tmp_path: Path) -> tuple[GetProjectSkillOverview, ObjectStoreProjectSkillRepository]:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    skills = ObjectStoreProjectSkillRepository(object_store)
    return GetProjectSkillOverview(skills), skills


def _save_skill(
    skills: ObjectStoreProjectSkillRepository,
    structured: dict[str, object],
    *,
    markdown: str = "# Alpha 项目 Skill\n\n沿用旧结构。",
    expected_revision: int = 0,
    reason: str = "test project skill overview",
) -> dict[str, object]:
    return dict(
        skills.save(
            ProjectSkillUpdate(
                project_id=str(structured["project_id"]),
                markdown=markdown,
                structured=structured,
                expected_revision=expected_revision,
                reason=reason,
            )
        )
    )


def test_project_skill_overview_reads_revision_and_source_refs_without_rewrite(tmp_path: Path) -> None:
    use_case, skills = _parts(tmp_path)
    saved = _save_skill(skills, _fixture("valid-active-skill.json"))

    overview = use_case.execute("project-alpha")
    payload = serialize_project_skill_overview(overview)

    assert overview.status == "ready"
    assert overview.skill is not None
    assert overview.skill.skill_id == saved["id"]
    assert overview.skill.revision == 1
    assert overview.skill.markdown_revision == 1
    assert overview.skill.json_revision == 1
    assert overview.skill.markdown_ref == "crp://default/projects/project-alpha/project-skill.md"
    assert overview.skill.json_ref == "crp://default/projects/project-alpha/project-skill.json"
    assert overview.skill.source_refs == ("source-text-001#char:0-80",)
    assert "series_memory:series-memory-alpha" in overview.skill.required_context_refs
    assert "rule-structure:must" in overview.skill.output_rule_refs
    assert overview.skill.evidence_state == "ready"
    assert overview.skill.user_edit_policy == "user_wins"
    assert overview.skill.update_strategy == "patch_existing_first"
    assert overview.skill.conflict_status == "none"
    assert [revision.revision for revision in overview.skill.revisions] == [1]
    assert "ai_rewrite" in overview.blocked_operations
    assert "silent_user_edit_overwrite" in overview.blocked_operations
    assert "project_skill_mutation" in overview.blocked_operations
    assert "memory_publication" in overview.blocked_operations
    assert payload["skill"] is not None
    assert payload["skill"]["source_refs"] == ["source-text-001#char:0-80"]
    assert "markdown" not in payload["skill"]
    assert "structured" not in payload["skill"]
    assert not (tmp_path / "library").exists()


def test_project_skill_overview_marks_missing_source_refs_without_fabricating_refs(tmp_path: Path) -> None:
    use_case, skills = _parts(tmp_path)
    structured = copy.deepcopy(_fixture("valid-active-skill.json"))
    structured["source_refs"] = []
    structured["evidence_refs"] = []
    for rule in structured["output_rules"]:
        if isinstance(rule, dict):
            rule["source_refs"] = []
    _save_skill(skills, structured)

    overview = use_case.execute("project-alpha")

    assert overview.status == "degraded"
    assert overview.skill is not None
    assert overview.skill.source_refs == ()
    assert overview.skill.evidence_state == "missing_source_refs"
    assert overview.next_step_boundary == "project_skill_overview_requires_source_refs_before_rewrite_or_answer"
    assert "source_content_read" in overview.skill.blocked_operations
    assert "ai_rewrite" in overview.skill.blocked_operations


def test_project_skill_overview_missing_skill_is_non_mutating_unavailable_state(tmp_path: Path) -> None:
    use_case, _skills = _parts(tmp_path)

    overview = use_case.execute("project-missing")

    assert overview.status == "missing"
    assert overview.project_id == "project-missing"
    assert overview.skill is None
    assert overview.next_step_boundary == "project_skill_overview_missing_create_or_import_required"
    assert "project_skill_save" in overview.blocked_operations
    assert not (tmp_path / "library").exists()


def test_project_skill_overview_rejects_empty_project_id(tmp_path: Path) -> None:
    use_case, _skills = _parts(tmp_path)

    with pytest.raises(ProjectSkillOverviewError, match="project_id cannot be empty"):
        use_case.execute("  ")


def test_project_skill_overview_composition_reads_shared_temp_store_without_library_write(tmp_path: Path) -> None:
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    skills = ObjectStoreProjectSkillRepository(object_store)
    _save_skill(skills, _fixture("valid-active-skill.json"))
    use_case = build_project_skill_overview(ROOT, runtime_root=tmp_path)

    overview = use_case.execute("project-alpha")

    assert overview.status == "ready"
    assert overview.skill is not None
    assert overview.skill.skill_id == "skill-project-alpha"
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "project_skill_revisions").exists()
    assert not (tmp_path / "library").exists()


def test_project_skill_overview_does_not_create_revision_or_overwrite_user_skill(tmp_path: Path) -> None:
    use_case, skills = _parts(tmp_path)
    first = _save_skill(skills, _fixture("valid-active-skill.json"))
    updated = copy.deepcopy(first)
    updated["purpose"] = "用户确认后的更新目标。"
    second = _save_skill(
        skills,
        updated,
        markdown="# Alpha 项目 Skill\n\n用户确认后的更新目标。",
        expected_revision=1,
        reason="user edited project purpose",
    )
    revisions_before = skills.revisions("project-alpha")

    overview = use_case.execute("project-alpha")
    revisions_after = skills.revisions("project-alpha")

    assert overview.skill is not None
    assert overview.skill.revision == second["revision"]
    assert overview.skill.purpose == "用户确认后的更新目标。"
    assert revisions_after == revisions_before
    assert skills.markdown("project-alpha") == "# Alpha 项目 Skill\n\n用户确认后的更新目标。"
    assert "silent_user_edit_overwrite" in overview.skill.blocked_operations


def test_project_skill_overview_endpoint_returns_display_ready_payload(tmp_path: Path) -> None:
    use_case, skills = _parts(tmp_path)
    _save_skill(skills, _fixture("valid-active-skill.json"))

    response = ServeProjectSkillOverviewEndpoint().execute(
        method="GET",
        path="/api/rebuild/project-skill/overview?project_id=project-alpha",
        get_project_skill_overview=use_case.execute,
    )

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.body["status"] == "ready"
    assert response.body["project_id"] == "project-alpha"
    assert response.body["endpoint_boundary"] == {
        "read_only": True,
        "display_ready": True,
        "allows_ai_rewrite": False,
        "allows_mutation": False,
        "allows_memory_publication": False,
    }
    skill = response.body["skill"]
    assert isinstance(skill, dict)
    assert skill["skill_id"] == "skill-project-alpha"
    assert skill["source_refs"] == ["source-text-001#char:0-80"]
    assert "ai_rewrite" in skill["blocked_operations"]
    assert "silent_user_edit_overwrite" in skill["blocked_operations"]
    assert "markdown" not in skill
    assert "structured" not in skill
    assert not (tmp_path / "library").exists()


def test_project_skill_overview_endpoint_returns_missing_and_degraded_states(tmp_path: Path) -> None:
    use_case, skills = _parts(tmp_path)
    missing = ServeProjectSkillOverviewEndpoint().execute(
        method="GET",
        path="/api/rebuild/project-skill/overview?project_id=project-missing",
        get_project_skill_overview=use_case.execute,
    )
    structured = copy.deepcopy(_fixture("valid-active-skill.json"))
    structured["source_refs"] = []
    structured["evidence_refs"] = []
    for rule in structured["output_rules"]:
        if isinstance(rule, dict):
            rule["source_refs"] = []
    _save_skill(skills, structured)
    degraded = ServeProjectSkillOverviewEndpoint().execute(
        method="GET",
        path="/api/rebuild/project-skill/overview?project_id=project-alpha",
        get_project_skill_overview=use_case.execute,
    )

    assert missing.status_code == 200
    assert missing.body["status"] == "missing"
    assert missing.body["skill"] is None
    assert "project_skill_save" in missing.body["blocked_operations"]
    assert degraded.status_code == 200
    assert degraded.body["status"] == "degraded"
    assert isinstance(degraded.body["skill"], dict)
    assert degraded.body["skill"]["source_refs"] == []
    assert degraded.body["skill"]["evidence_state"] == "missing_source_refs"


def test_project_skill_overview_endpoint_rejects_wrong_method_path_and_query(tmp_path: Path) -> None:
    use_case, _skills = _parts(tmp_path)
    endpoint = ServeProjectSkillOverviewEndpoint()

    wrong_method = endpoint.execute(
        method="POST",
        path="/api/rebuild/project-skill/overview?project_id=project-alpha",
        get_project_skill_overview=use_case.execute,
    )
    wrong_path = endpoint.execute(
        method="GET",
        path="/api/rebuild/project-skill/items?project_id=project-alpha",
        get_project_skill_overview=use_case.execute,
    )
    missing_project = endpoint.execute(
        method="GET",
        path="/api/rebuild/project-skill/overview",
        get_project_skill_overview=use_case.execute,
    )
    empty_project = endpoint.execute(
        method="GET",
        path="/api/rebuild/project-skill/overview?project_id=",
        get_project_skill_overview=use_case.execute,
    )
    duplicate_project = endpoint.execute(
        method="GET",
        path="/api/rebuild/project-skill/overview?project_id=project-alpha&project_id=project-beta",
        get_project_skill_overview=use_case.execute,
    )
    unsupported_query = endpoint.execute(
        method="GET",
        path="/api/rebuild/project-skill/overview?project_id=project-alpha&rewrite=true",
        get_project_skill_overview=use_case.execute,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "GET"
    assert wrong_method.body["detail"] == "project skill overview endpoint only supports GET"
    assert wrong_path.status_code == 404
    assert missing_project.status_code == 400
    assert missing_project.body["detail"] == "project_id is required"
    assert empty_project.status_code == 400
    assert empty_project.body["detail"] == "project_id cannot be empty"
    assert duplicate_project.status_code == 400
    assert duplicate_project.body["detail"] == "project_id must be provided exactly once"
    assert unsupported_query.status_code == 400
    assert unsupported_query.body["detail"] == "unsupported query parameter: rewrite"
