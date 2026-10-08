from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping


AuthoringOperation = Literal["create", "update", "supplement", "refactor"]
IntentKind = Literal["project_skill_authoring", "normal_question"]

MAX_HOME_QUESTION_CHARS = 6000
PROJECT_SKILL_EVIDENCE_SCOPE = (
    "project_sources",
    "project_documents",
    "published_project_memory",
    "current_project_skill",
    "confirmed_success_cases",
)


class ProjectSkillAuthoringIntentError(ValueError):
    """Raised when homepage authoring intent input violates the local contract."""


@dataclass(frozen=True, slots=True)
class ProjectSkillAuthoringIntent:
    kind: IntentKind
    project_id: str
    operation: AuthoringOperation | None
    goal: str
    evidence_scope: tuple[str, ...]
    requires_user_confirmation: bool
    provider_call_allowed: bool
    authority_write_allowed: bool
    reason: str

    def to_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "kind": self.kind,
                "project_id": self.project_id,
                "operation": self.operation,
                "goal": self.goal,
                "evidence_scope": self.evidence_scope,
                "requires_user_confirmation": self.requires_user_confirmation,
                "provider_call_allowed": self.provider_call_allowed,
                "authority_write_allowed": self.authority_write_allowed,
                "reason": self.reason,
            }
        )


_SKILL_TARGET = re.compile(
    r"(?:项目\s*(?:skill|技能|规则|工作规则)|project\s*skill)",
    re.IGNORECASE,
)
_NEGATED_ACTION = re.compile(
    r"(?:不要|无需|不用|别|禁止|不允许|不需要|do\s+not|don't|dont|without)"
    r"[^。！？!?\n]{0,24}"
    r"(?:创建|新建|生成|设计|修改|编辑|更新|补充|完善|扩展|重构|重写|create|design|update|edit|supplement|extend|refactor|rewrite)",
    re.IGNORECASE,
)
_OPERATIONS: tuple[tuple[AuthoringOperation, re.Pattern[str]], ...] = (
    (
        "refactor",
        re.compile(r"(?:重构|重写|重新设计|整体改写|refactor|rewrite|redesign)", re.IGNORECASE),
    ),
    (
        "supplement",
        re.compile(r"(?:补充|完善|扩展|增加|添加|补上|supplement|extend|add\s+to)", re.IGNORECASE),
    ),
    (
        "update",
        re.compile(r"(?:修改|编辑|更新|调整|改进|优化|update|edit|modify|revise|improve)", re.IGNORECASE),
    ),
    (
        "create",
        re.compile(r"(?:创建|新建|生成|设计|起草|总结成|沉淀成|create|design|draft|generate|build)", re.IGNORECASE),
    ),
)


def classify_project_skill_authoring_intent(
    *,
    project_id: str,
    question: str,
) -> ProjectSkillAuthoringIntent:
    clean_project_id = _required_text(project_id, "project_id", maximum=160)
    clean_question = _required_text(question, "question", maximum=MAX_HOME_QUESTION_CHARS)

    if not _SKILL_TARGET.search(clean_question):
        return _normal_question(clean_project_id, "question_has_no_project_skill_target")
    if _NEGATED_ACTION.search(clean_question):
        return _normal_question(clean_project_id, "project_skill_action_is_negated")

    operation = _detect_operation(clean_question)
    if operation is None:
        return _normal_question(clean_project_id, "project_skill_target_has_no_authoring_action")

    return ProjectSkillAuthoringIntent(
        kind="project_skill_authoring",
        project_id=clean_project_id,
        operation=operation,
        goal=clean_question,
        evidence_scope=PROJECT_SKILL_EVIDENCE_SCOPE,
        requires_user_confirmation=True,
        provider_call_allowed=False,
        authority_write_allowed=False,
        reason="explicit_project_skill_authoring_request",
    )


def _detect_operation(question: str) -> AuthoringOperation | None:
    matches: list[tuple[int, int, AuthoringOperation]] = []
    for priority, (operation, pattern) in enumerate(_OPERATIONS):
        match = pattern.search(question)
        if match is not None:
            matches.append((match.start(), priority, operation))
    if not matches:
        return None
    # The earliest explicit action controls. Priority makes specific rewrite and
    # supplement operations win when two verbs start at the same position.
    return min(matches)[2]


def _normal_question(project_id: str, reason: str) -> ProjectSkillAuthoringIntent:
    return ProjectSkillAuthoringIntent(
        kind="normal_question",
        project_id=project_id,
        operation=None,
        goal="",
        evidence_scope=(),
        requires_user_confirmation=False,
        provider_call_allowed=False,
        authority_write_allowed=False,
        reason=reason,
    )


def _required_text(value: str, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ProjectSkillAuthoringIntentError(f"{label} must be text")
    clean = value.strip()
    if not clean:
        raise ProjectSkillAuthoringIntentError(f"{label} is required")
    if len(clean) > maximum:
        raise ProjectSkillAuthoringIntentError(f"{label} exceeds {maximum} characters")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in clean):
        raise ProjectSkillAuthoringIntentError(f"{label} contains control characters")
    return clean
