from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.document_engine import DocumentDraft, DocumentRepositoryPort
from core.project_skill_core import ProjectSkillRepositoryPort


class SkillDocumentHandoffError(ValueError):
    """Raised when a Project Skill cannot safely drive Document creation."""


@dataclass(frozen=True, slots=True)
class SkillDocumentHandoffResult:
    project_id: str
    skill_id: str
    skill_revision: int
    document_id: str
    document_revision: int


class CreateDocumentFromProjectSkill:
    """Creates a source-backed Document draft from the current Project Skill."""

    def __init__(
        self,
        *,
        skills: ProjectSkillRepositoryPort,
        documents: DocumentRepositoryPort,
    ) -> None:
        self._skills = skills
        self._documents = documents

    def execute(
        self,
        project_id: str,
        *,
        title: str | None = None,
    ) -> SkillDocumentHandoffResult:
        skill = self._current_skill(project_id)
        skill_id = _required_str(skill, "id")
        revision = _required_int(skill, "revision")
        source_refs = _skill_source_refs(skill)
        if not source_refs:
            raise SkillDocumentHandoffError("Project Skill must carry source refs for document handoff")
        document = self._documents.create(
            DocumentDraft(
                title=title or f"{_required_str(skill, 'name')} 文档草稿",
                document_type="project_doc",
                markdown=_markdown_from_skill(skill),
                source_refs=tuple(source_refs),
                project_id=project_id,
            )
        )
        return SkillDocumentHandoffResult(
            project_id=project_id,
            skill_id=skill_id,
            skill_revision=revision,
            document_id=_required_str(document, "id"),
            document_revision=_required_int(document, "revision"),
        )

    def _current_skill(self, project_id: str) -> Mapping[str, object]:
        skill = self._skills.load(project_id)
        if skill is None:
            raise SkillDocumentHandoffError(f"Project Skill not found: {project_id}")
        if skill.get("status") != "active":
            raise SkillDocumentHandoffError("Project Skill must be active for document handoff")
        if skill.get("trust_status") not in {"user_confirmed", "trusted", "system_generated"}:
            raise SkillDocumentHandoffError("Project Skill trust status is not eligible for handoff")
        conflict = skill.get("conflict")
        if not isinstance(conflict, Mapping) or conflict.get("status") != "none":
            raise SkillDocumentHandoffError("Project Skill cannot hand off while conflict is unresolved")
        for context in _required_list(skill, "required_context"):
            if isinstance(context, Mapping) and context.get("stale") is True:
                raise SkillDocumentHandoffError("Project Skill cannot hand off stale required context")
        update_rules = skill.get("update_rules")
        if not isinstance(update_rules, Mapping) or update_rules.get("user_edit_policy") != "user_wins":
            raise SkillDocumentHandoffError("Project Skill handoff requires user_edit_policy=user_wins")
        return skill


def _markdown_from_skill(skill: Mapping[str, object]) -> str:
    lines = [
        f"# {_required_str(skill, 'name')}",
        "",
        "## 项目目标",
        _required_str(skill, "purpose"),
        "",
        "## 输出结构规则",
    ]
    for rule in _required_list(skill, "output_rules"):
        if not isinstance(rule, Mapping):
            continue
        priority = rule.get("priority", "should")
        text = rule.get("rule")
        if isinstance(text, str) and text:
            lines.append(f"- [{priority}] {text}")
    style = skill.get("style_preferences")
    if isinstance(style, Mapping):
        voice = style.get("voice")
        if isinstance(voice, str) and voice:
            lines.extend(["", "## 表达风格", voice])
        defaults = style.get("format_defaults")
        if isinstance(defaults, list) and defaults:
            lines.extend(["", "## 默认格式"])
            for item in defaults:
                if isinstance(item, str) and item:
                    lines.append(f"- {item}")
    lines.extend(["", "## 必读上下文"])
    for context in _required_list(skill, "required_context"):
        if not isinstance(context, Mapping):
            continue
        reason = context.get("reason")
        uri = context.get("uri")
        if isinstance(reason, str) and isinstance(uri, str):
            lines.append(f"- {reason}：{uri}")
    return "\n".join(lines).strip() + "\n"


def _skill_source_refs(skill: Mapping[str, object]) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for candidate in (
        *_required_list(skill, "source_refs"),
        *_required_list(skill, "evidence_refs"),
    ):
        _append_source_ref(refs, seen, candidate)
    for rule in _required_list(skill, "output_rules"):
        if not isinstance(rule, Mapping):
            continue
        for candidate in _required_list(rule, "source_refs"):
            _append_source_ref(refs, seen, candidate)
    return refs


def _append_source_ref(
    refs: list[dict[str, object]],
    seen: set[tuple[str, str, str | None]],
    candidate: object,
) -> None:
    if not isinstance(candidate, Mapping):
        return
    source_id = candidate.get("source_id")
    locator = candidate.get("locator")
    quote = candidate.get("quote")
    if not isinstance(source_id, str) or not source_id:
        return
    if not isinstance(locator, str) or not locator:
        return
    key = (source_id, locator, quote if isinstance(quote, str) else None)
    if key in seen:
        return
    seen.add(key)
    ref: dict[str, object] = {"source_id": source_id, "locator": locator}
    if isinstance(quote, str):
        ref["quote"] = quote
    refs.append(ref)


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise SkillDocumentHandoffError(f"{key} is required")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise SkillDocumentHandoffError(f"{key} must be an integer")
    return value


def _required_list(mapping: Mapping[str, object], key: str) -> list[object]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise SkillDocumentHandoffError(f"{key} must be a list")
    return list(value)
