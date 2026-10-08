from __future__ import annotations

import pytest

from core.product_core.project_skill_authoring_intent import (
    MAX_HOME_QUESTION_CHARS,
    PROJECT_SKILL_EVIDENCE_SCOPE,
    ProjectSkillAuthoringIntentError,
    classify_project_skill_authoring_intent,
)


@pytest.mark.parametrize(
    ("question", "operation"),
    [
        ("请根据这个项目过往资料创建项目技能，先给我草稿。", "create"),
        ("把当前项目规则修改得更适合每周复盘。", "update"),
        ("请给项目工作规则补充证据核验步骤。", "supplement"),
        ("重构这个 Project Skill，让输出先给结论再给依据。", "refactor"),
        ("Please create a Project Skill for evidence-based reviews.", "create"),
        ("Update the project skill to include source verification.", "update"),
    ],
)
def test_classifies_explicit_home_authoring_operations(question: str, operation: str) -> None:
    result = classify_project_skill_authoring_intent(project_id="project-alpha", question=question)

    assert result.kind == "project_skill_authoring"
    assert result.operation == operation
    assert result.project_id == "project-alpha"
    assert result.goal == question
    assert result.evidence_scope == PROJECT_SKILL_EVIDENCE_SCOPE
    assert result.requires_user_confirmation is True
    assert result.provider_call_allowed is False
    assert result.authority_write_allowed is False


@pytest.mark.parametrize(
    ("question", "reason"),
    [
        ("这个项目接下来应该先做什么？", "question_has_no_project_skill_target"),
        ("项目技能是什么，它会影响回答吗？", "project_skill_target_has_no_authoring_action"),
        ("不要修改项目技能，只解释当前规则。", "project_skill_action_is_negated"),
        ("Do not update the project skill; explain it.", "project_skill_action_is_negated"),
        ("帮我创建一份会议纪要。", "question_has_no_project_skill_target"),
    ],
)
def test_keeps_non_authoring_questions_on_normal_question_path(question: str, reason: str) -> None:
    result = classify_project_skill_authoring_intent(project_id="project-alpha", question=question)

    assert result.kind == "normal_question"
    assert result.operation is None
    assert result.goal == ""
    assert result.evidence_scope == ()
    assert result.requires_user_confirmation is False
    assert result.reason == reason


def test_first_explicit_action_controls_multi_action_request() -> None:
    result = classify_project_skill_authoring_intent(
        project_id="project-alpha",
        question="先补充项目技能的来源要求，再重构章节结构。",
    )

    assert result.operation == "supplement"


def test_payload_is_read_only_and_contains_no_write_authority() -> None:
    result = classify_project_skill_authoring_intent(
        project_id="project-alpha",
        question="请创建项目规则。",
    )
    payload = result.to_payload()

    assert payload["authority_write_allowed"] is False
    assert payload["provider_call_allowed"] is False
    with pytest.raises(TypeError):
        payload["kind"] = "normal_question"  # type: ignore[index]


@pytest.mark.parametrize(
    ("project_id", "question", "message"),
    [
        ("", "请创建项目规则。", "project_id is required"),
        ("project-alpha", "", "question is required"),
        ("project-alpha", "请创建项目规则。\x00", "question contains control characters"),
        ("x" * 161, "请创建项目规则。", "project_id exceeds 160 characters"),
        ("project-alpha", "请创建项目规则。" + "x" * MAX_HOME_QUESTION_CHARS, "question exceeds 6000 characters"),
    ],
)
def test_rejects_invalid_scope_or_question(project_id: str, question: str, message: str) -> None:
    with pytest.raises(ProjectSkillAuthoringIntentError, match=message):
        classify_project_skill_authoring_intent(project_id=project_id, question=question)
