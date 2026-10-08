from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.project_skill_core import (
    ProjectSkillPublicationDraftError,
    ProjectSkillPublicationTarget,
    build_project_skill_publication_draft,
)
from tools.validate_rebuild_contracts import load_contract_registry, validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _draft(*, target: ProjectSkillPublicationTarget | None = None) -> dict[str, object]:
    selected_target = target or ProjectSkillPublicationTarget.for_create("project-alpha")
    current_payload = None
    if selected_target.expected_revision:
        current_payload = {
            **_draft()["structured_payload"],
            "id": selected_target.skill_id,
            "project_id": selected_target.project_id,
            "revision": selected_target.expected_revision,
            "markdown_revision": selected_target.expected_revision,
            "json_revision": selected_target.expected_revision,
            "status": "active",
            "trust_status": "user_confirmed",
        }
    return build_project_skill_publication_draft(
        target=selected_target,
        source_candidate_id="candidate-alpha-001",
        proposed_content="Alpha 项目的输出应优先 patch 既有结构，并保留可追溯来源。",
        source_refs=({"source_id": "source-alpha-001", "locator": "char:0-64"},),
        evidence_refs=({"source_id": "source-alpha-001", "locator": "char:0-64"},),
        reviewed_by="user",
        reviewed_at="2026-07-12T10:00:00+08:00",
        review_reason="用户确认该项目需要连续维护既有输出结构。",
        current_payload=current_payload,
    )


def test_create_draft_has_canonical_identity_complete_payload_and_markdown() -> None:
    draft = _draft()
    structured = draft["structured_payload"]

    assert draft["target_layer"] == "project_skill"
    assert draft["skill_id"] == "skill-project-alpha"
    assert draft["expected_project_skill_revision"] == 0
    assert draft["reviewed_by"] == "user"
    assert draft["markdown"].startswith("# Alpha 项目的输出应优先")
    assert isinstance(structured, dict)
    assert structured["id"] == draft["skill_id"]
    assert structured["project_id"] == "project-alpha"
    assert structured["revision"] == 1
    assert structured["status"] == "draft"
    assert structured["source_refs"] == draft["source_refs"]
    assert structured["evidence_refs"] == draft["evidence_refs"]
    assert validate_contract_instance(
        "project_skill.schema.json",
        _schema("project_skill.schema.json"),
        structured,
    ) == []


def test_update_draft_binds_existing_identity_and_next_revision() -> None:
    draft = _draft(
        target=ProjectSkillPublicationTarget.for_update(
            project_id="project-alpha",
            skill_id="skill-existing-alpha",
            expected_revision=4,
        )
    )

    assert draft["skill_id"] == "skill-existing-alpha"
    assert draft["expected_project_skill_revision"] == 4
    assert draft["structured_payload"]["id"] == "skill-existing-alpha"
    assert draft["structured_payload"]["revision"] == 5
    assert draft["structured_payload"]["markdown_revision"] == 5
    assert draft["structured_payload"]["json_revision"] == 5


def test_draft_digest_is_deterministic_and_changes_for_material_drift() -> None:
    first = _draft()
    same = _draft()
    content_changed = build_project_skill_publication_draft(
        target=ProjectSkillPublicationTarget.for_create("project-alpha"),
        source_candidate_id="candidate-alpha-001",
        proposed_content="Alpha 项目的输出必须保留证据，并补充一个明确的变更点。",
        source_refs=({"source_id": "source-alpha-001", "locator": "char:0-64"},),
        evidence_refs=({"source_id": "source-alpha-001", "locator": "char:0-64"},),
        reviewed_by="user",
        reviewed_at="2026-07-12T10:00:00+08:00",
        review_reason="用户确认该项目需要连续维护既有输出结构。",
    )

    revision_changed = _draft(
        target=ProjectSkillPublicationTarget.for_update(
            project_id="project-alpha",
            skill_id="skill-project-alpha",
            expected_revision=1,
        )
    )
    evidence_changed = build_project_skill_publication_draft(
        target=ProjectSkillPublicationTarget.for_create("project-alpha"),
        source_candidate_id="candidate-alpha-001",
        proposed_content="Alpha 项目的输出应优先 patch 既有结构，并保留可追溯来源。",
        source_refs=({"source_id": "source-alpha-001", "locator": "char:0-64"},),
        evidence_refs=({"source_id": "source-alpha-002", "locator": "char:65-128"},),
        reviewed_by="user",
        reviewed_at="2026-07-12T10:00:00+08:00",
        review_reason="用户确认该项目需要连续维护既有输出结构。",
    )

    assert same == first
    assert revision_changed["draft_digest"] != first["draft_digest"]
    assert evidence_changed["draft_digest"] != first["draft_digest"]
    assert content_changed["draft_digest"] != first["draft_digest"]


def test_update_draft_preserves_user_outline_and_appends_evidence() -> None:
    current = _draft()["structured_payload"]
    current["revision"] = 4
    current["markdown_revision"] = 4
    current["json_revision"] = 4
    current["status"] = "active"
    current["trust_status"] = "user_confirmed"
    current["outline"] = [
        {
            "section_id": "user-evidence",
            "title": "用户证据结构",
            "kind": "sources",
            "required": True,
        }
    ]
    draft = build_project_skill_publication_draft(
        target=ProjectSkillPublicationTarget.for_update(
            project_id="project-alpha",
            skill_id="skill-project-alpha",
            expected_revision=4,
        ),
        source_candidate_id="candidate-alpha-002",
        proposed_content="新增资料要求输出包含风险和下一步。",
        source_refs=({"source_id": "source-alpha-002", "locator": "char:0-32"},),
        evidence_refs=({"source_id": "source-alpha-002", "locator": "char:0-32"},),
        reviewed_by="user",
        reviewed_at="2026-07-12T11:00:00+08:00",
        review_reason="用户确认连续更新。",
        current_payload=current,
    )
    structured = draft["structured_payload"]

    assert structured["outline"] == current["outline"]
    assert [item["source_id"] for item in structured["source_refs"]] == [
        "source-alpha-001",
        "source-alpha-002",
    ]
    assert len(structured["decision_log"]) == 2


def test_update_draft_requires_exact_current_payload() -> None:
    target = ProjectSkillPublicationTarget.for_update(
        project_id="project-alpha",
        skill_id="skill-project-alpha",
        expected_revision=4,
    )

    with pytest.raises(ProjectSkillPublicationDraftError, match="current_payload"):
        build_project_skill_publication_draft(
            target=target,
            source_candidate_id="candidate-alpha-002",
            proposed_content="新增资料。",
            source_refs=({"source_id": "source-alpha-002", "locator": "char:0-8"},),
            evidence_refs=({"source_id": "source-alpha-002", "locator": "char:0-8"},),
            reviewed_by="user",
            reviewed_at="2026-07-12T11:00:00+08:00",
            review_reason="用户确认连续更新。",
        )


def test_ai_proposed_payload_survives_review_staging_with_normalized_rules() -> None:
    proposed = {
        "name": "AI 项目规则",
        "purpose": "先给结论和证据，再列风险。",
        "output_rules": [{
            "rule_id": "rule-ai-summary",
            "origin": "ai",
            "rule": "先给结论与证据。",
            "priority": "must",
            "source_refs": [],
            "locked_by_user": False,
        }],
        "style_preferences": {"voice": "直接", "format_defaults": ["Markdown"]},
        "update_rules": {"patch_strategy": "patch_existing_first", "user_edit_policy": "user_wins", "allowed_auto_updates": []},
        "outline": [{"section_id": "summary", "title": "结论", "kind": "summary", "required": True}],
    }
    draft = build_project_skill_publication_draft(
        target=ProjectSkillPublicationTarget.for_create("project-alpha"),
        source_candidate_id="candidate-ai-001",
        proposed_content="先给结论和证据，再列风险。",
        source_refs=({"source_id": "ai-draft-001", "locator": "ai-draft:generated-output"},),
        evidence_refs=({"source_id": "ai-draft-001", "locator": "ai-draft:generated-output"},),
        reviewed_by="user",
        reviewed_at="2026-07-19T04:00:00+08:00",
        review_reason="用户确认 AI 草稿。",
        proposed_payload=proposed,
    )
    structured = draft["structured_payload"]
    assert structured["name"] == "AI 项目规则"
    assert structured["outline"] == proposed["outline"]
    assert structured["output_rules"][0]["rule"] == "先给结论与证据。"
    assert structured["output_rules"][0]["source_refs"] == draft["source_refs"]
    assert validate_contract_instance("project_skill.schema.json", _schema("project_skill.schema.json"), structured) == []


def test_user_edited_ai_rule_survives_staging_only_when_locked_by_user() -> None:
    proposed = {
        "name": "用户编辑后的 AI 规则",
        "purpose": "用户已核对规则正文。",
        "output_rules": [{
            "rule_id": "rule-ai-001",
            "origin": "user",
            "rule": "先核验证据，再给结论。",
            "priority": "must",
            "source_refs": [{"source_id": "source-alpha", "locator": "source:summary"}],
            "locked_by_user": True,
        }],
        "style_preferences": {"voice": "直接", "format_defaults": ["Markdown"]},
        "update_rules": {"patch_strategy": "patch_existing_first", "user_edit_policy": "user_wins", "allowed_auto_updates": []},
        "outline": [{"section_id": "summary", "title": "结论", "kind": "summary", "required": True}],
    }
    draft = build_project_skill_publication_draft(
        target=ProjectSkillPublicationTarget.for_create("project-alpha"),
        source_candidate_id="candidate-ai-001",
        proposed_content="用户已核对规则正文。",
        source_refs=({"source_id": "source-alpha", "locator": "source:summary"},),
        evidence_refs=({"source_id": "source-alpha", "locator": "source:summary"},),
        reviewed_by="user",
        reviewed_at="2026-07-20T01:00:00+08:00",
        review_reason="用户确认编辑后的 AI 草稿。",
        proposed_payload=proposed,
    )

    rule = draft["structured_payload"]["output_rules"][0]
    assert rule["origin"] == "user"
    assert rule["locked_by_user"] is True
    assert validate_contract_instance("project_skill.schema.json", _schema("project_skill.schema.json"), draft["structured_payload"]) == []


def test_user_origin_without_user_lock_is_rejected() -> None:
    proposed = {
        "name": "非法规则", "purpose": "非法规则。",
        "output_rules": [{
            "rule_id": "rule-ai-001", "origin": "user", "rule": "规则。", "priority": "must",
            "source_refs": [], "locked_by_user": False,
        }],
        "style_preferences": {}, "update_rules": {}, "outline": [],
    }
    with pytest.raises(ProjectSkillPublicationDraftError, match="must match user origin"):
        build_project_skill_publication_draft(
            target=ProjectSkillPublicationTarget.for_create("project-alpha"),
            source_candidate_id="candidate-ai-001", proposed_content="非法规则。",
            source_refs=({"source_id": "source-alpha", "locator": "source:summary"},),
            evidence_refs=({"source_id": "source-alpha", "locator": "source:summary"},),
            reviewed_by="user", reviewed_at="2026-07-20T01:00:00+08:00", review_reason="test",
            proposed_payload=proposed,
        )


@pytest.mark.parametrize(
    ("target", "kwargs", "message"),
    [
        (
            ProjectSkillPublicationTarget.for_create("project-alpha"),
            {"source_candidate_id": ""},
            "source_candidate_id",
        ),
        (
            ProjectSkillPublicationTarget.for_create("project-alpha"),
            {"reviewed_by": "system"},
            "reviewed_by",
        ),
        (
            ProjectSkillPublicationTarget.for_create("project-alpha"),
            {"source_refs": ()},
            "source_refs",
        ),
        (
            ProjectSkillPublicationTarget.for_create("project-alpha"),
            {"proposed_content": {"not": "json text"}},
            "proposed_content",
        ),
    ],
)
def test_draft_builder_fails_closed_for_unreviewable_input(
    target: ProjectSkillPublicationTarget,
    kwargs: dict[str, object],
    message: str,
) -> None:
    defaults: dict[str, object] = {
        "target": target,
        "source_candidate_id": "candidate-alpha-001",
        "proposed_content": "Alpha 项目的输出应优先 patch 既有结构，并保留可追溯来源。",
        "source_refs": ({"source_id": "source-alpha-001", "locator": "char:0-64"},),
        "evidence_refs": ({"source_id": "source-alpha-001", "locator": "char:0-64"},),
        "reviewed_by": "user",
        "reviewed_at": "2026-07-12T10:00:00+08:00",
        "review_reason": "用户确认该项目需要连续维护既有输出结构。",
    }
    defaults.update(kwargs)

    with pytest.raises(ProjectSkillPublicationDraftError, match=message):
        build_project_skill_publication_draft(**defaults)


def test_draft_contract_and_explicit_rolled_back_status_are_valid() -> None:
    draft = _draft()
    draft_schema = _schema("project_skill_publication_draft.schema.json")
    skill_schema = _schema("project_skill.schema.json")
    rolled_back = copy.deepcopy(draft["structured_payload"])
    rolled_back["status"] = "rolled_back"

    assert validate_contract_instance(
        "project_skill_publication_draft.schema.json",
        draft_schema,
        draft,
        registry=load_contract_registry(CONTRACT_ROOT),
    ) == []
    assert validate_contract_instance("project_skill.schema.json", skill_schema, rolled_back) == []
    assert rolled_back["status"] != "archived"


def test_draft_contract_rejects_cas_and_digest_drift() -> None:
    draft = _draft()
    tampered = copy.deepcopy(draft)
    tampered["structured_payload"]["revision"] = 4

    errors = validate_contract_instance(
        "project_skill_publication_draft.schema.json",
        _schema("project_skill_publication_draft.schema.json"),
        tampered,
        registry=load_contract_registry(CONTRACT_ROOT),
    )

    assert any("structured_payload.revision" in error for error in errors)
    assert "draft_digest: must cover the complete draft material" in errors
