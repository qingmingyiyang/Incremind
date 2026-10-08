from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from core.project_skill_core import ProjectSkillRepositoryPort


class ProjectDocumentModelRequestRepositoryPort(Protocol):
    def save_request(self, request: Mapping[str, object]) -> Mapping[str, object]: ...


class DocumentApplicationSkillRuntimePort(Protocol):
    def resolve_context(
        self,
        *,
        project_id: str,
        consumer: str,
        task_kind: str,
        task_text: str,
        invocation_id: str,
        project_summary: str = "",
    ) -> Mapping[str, object]: ...


class ProjectDocumentModelRequestError(ValueError):
    """Raised when project evidence cannot safely produce a document request."""


@dataclass(frozen=True, slots=True)
class ProjectDocumentModelRequestResult:
    project_id: str
    project_skill_id: str
    project_skill_revision: int
    model_request_id: str
    source_ref_count: int
    application_skill_resolution_id: str | None


_SKILL_RESOLUTION_ID = re.compile(r"^skill-resolution-[0-9a-f]{32}$")


class CreateProjectDocumentModelRequest:
    """Creates a local document-draft request without invoking a provider."""

    def __init__(
        self,
        *,
        project_skills: ProjectSkillRepositoryPort,
        model_requests: ProjectDocumentModelRequestRepositoryPort,
        application_skills: DocumentApplicationSkillRuntimePort | None = None,
        namespace_id: str = "default",
    ) -> None:
        self._project_skills = project_skills
        self._model_requests = model_requests
        self._application_skills = application_skills
        self._namespace_id = namespace_id

    def execute(
        self,
        project_id: str,
        *,
        brief: str,
        title: str | None = None,
        created_at: str | None = None,
    ) -> ProjectDocumentModelRequestResult:
        clean_project_id = _required_text(project_id, "project_id", maximum=128)
        clean_brief = _required_text(brief, "brief", maximum=16_000)
        clean_title = _optional_text(title, "title", maximum=500)
        project_skill = self._eligible_project_skill(clean_project_id)
        source_refs = _project_skill_source_refs(project_skill)
        if not source_refs:
            raise ProjectDocumentModelRequestError(
                "Project Skill must carry source refs before document generation"
            )
        request_id = _model_request_id(
            project_id=clean_project_id,
            project_skill=project_skill,
            brief=clean_brief,
            title=clean_title,
            source_refs=source_refs,
        )
        application_skill = self._resolve_application_skill(
            project_id=clean_project_id,
            project_skill=project_skill,
            brief=clean_brief,
            request_id=request_id,
        )
        request = _document_model_request(
            namespace_id=self._namespace_id,
            project_skill=project_skill,
            brief=clean_brief,
            title=clean_title,
            source_refs=source_refs,
            request_id=request_id,
            application_skill=application_skill,
            created_at=created_at or _utc_now(),
        )
        saved = self._model_requests.save_request(request)
        return ProjectDocumentModelRequestResult(
            project_id=clean_project_id,
            project_skill_id=_required_str(project_skill, "id"),
            project_skill_revision=_required_int(project_skill, "revision"),
            model_request_id=_required_str(saved, "id"),
            source_ref_count=len(source_refs),
            application_skill_resolution_id=(
                _required_str(application_skill, "resolution_id")
                if application_skill is not None
                else None
            ),
        )

    def _eligible_project_skill(self, project_id: str) -> Mapping[str, object]:
        skill = self._project_skills.load(project_id)
        if skill is None:
            raise ProjectDocumentModelRequestError(f"Project Skill not found: {project_id}")
        if skill.get("project_id") != project_id:
            raise ProjectDocumentModelRequestError("Project Skill project_id drifted")
        if skill.get("status") != "active":
            raise ProjectDocumentModelRequestError("Project Skill must be active")
        if skill.get("trust_status") not in {"user_confirmed", "trusted", "system_generated"}:
            raise ProjectDocumentModelRequestError("Project Skill trust status is not eligible")
        conflict = skill.get("conflict")
        if not isinstance(conflict, Mapping) or conflict.get("status") != "none":
            raise ProjectDocumentModelRequestError("Project Skill conflict must be resolved")
        for context in _required_list(skill, "required_context"):
            if isinstance(context, Mapping) and context.get("stale") is True:
                raise ProjectDocumentModelRequestError("Project Skill required context is stale")
        update_rules = skill.get("update_rules")
        if not isinstance(update_rules, Mapping) or update_rules.get("user_edit_policy") != "user_wins":
            raise ProjectDocumentModelRequestError(
                "Project Skill document generation requires user_edit_policy=user_wins"
            )
        _required_str(skill, "id")
        _required_int(skill, "revision")
        _required_str(skill, "json_uri")
        _required_str(skill, "name")
        _required_str(skill, "purpose")
        return skill

    def _resolve_application_skill(
        self,
        *,
        project_id: str,
        project_skill: Mapping[str, object],
        brief: str,
        request_id: str,
    ) -> Mapping[str, object] | None:
        if self._application_skills is None:
            return None
        try:
            value = self._application_skills.resolve_context(
                project_id=project_id,
                consumer="document.generate",
                task_kind="project-document",
                task_text=brief,
                invocation_id=request_id,
                project_summary=_project_summary(project_skill),
            )
        except Exception as error:
            raise ProjectDocumentModelRequestError(
                "Application Skill resolution failed"
            ) from error
        return _application_skill_context(value, project_id=project_id)


def _document_model_request(
    *,
    namespace_id: str,
    project_skill: Mapping[str, object],
    brief: str,
    title: str | None,
    source_refs: list[dict[str, object]],
    request_id: str,
    application_skill: Mapping[str, object] | None,
    created_at: str,
) -> dict[str, object]:
    project_id = _required_str(project_skill, "project_id")
    project_skill_id = _required_str(project_skill, "id")
    input_refs: list[dict[str, object]] = [
        {
            "kind": "project_skill",
            "object_id": project_skill_id,
            "uri": _required_str(project_skill, "json_uri"),
        }
    ]
    if application_skill is not None:
        resolution_id = _required_str(application_skill, "resolution_id")
        input_refs.append(
            {
                "kind": "application_skill_resolution",
                "object_id": resolution_id,
                "uri": (
                    f"crp://{namespace_id}/application-skill-resolutions/"
                    f"{resolution_id}.json"
                ),
            }
        )
    return {
        "schema_version": "1.0.0",
        "id": request_id,
        "project_id": project_id,
        "capability": "text_generation",
        "provider_preference": {
            "mode": "local_only",
            "provider": None,
            "model": None,
            "allow_remote": False,
            "config_version": 1,
        },
        "payload": {
            "kind": "document_draft",
            "content": _document_prompt(
                project_skill=project_skill,
                brief=brief,
                title=title,
                application_skill_context=(
                    str(application_skill.get("context_markdown") or "")
                    if application_skill is not None
                    else ""
                ),
            ),
            "input_refs": input_refs,
            "source_refs": source_refs,
            "recall_result_id": None,
        },
        "privacy": {
            "scope": "local_only",
            "pii": "possible",
            "allow_remote": False,
            "redaction": {"applied": False, "strategy": "none"},
            "retention": "none",
        },
        "timeout": {
            "request_timeout_ms": 60000,
            "idle_timeout_ms": 10000,
            "deadline_at": None,
        },
        "cancel": {
            "cancellable": True,
            "cancel_token": _stable_id("cancel-token", request_id),
            "requested": False,
        },
        "budget": {
            "max_input_tokens": 12000,
            "max_output_tokens": 4096,
            "max_total_tokens": 16096,
            "max_cost_usd": 0,
        },
        "response_schema": {
            "type": "text",
            "json_schema_uri": None,
            "strict": False,
        },
        "created_at": created_at,
    }


def _document_prompt(
    *,
    project_skill: Mapping[str, object],
    brief: str,
    title: str | None,
    application_skill_context: str,
) -> str:
    fixed_policy = (
        "请只根据项目工作规则、给定任务和可追溯来源生成 Markdown 文档草稿。"
        "不得补充未提供的项目事实，不得发布记忆，也不得覆盖用户已有文档。\n"
        "项目工作规则是项目事实、输出结构和表达偏好的唯一 authority。"
        "Application Skill 仅提供通用方法；发生冲突时必须遵守项目工作规则。\n\n"
    )
    method = f"{application_skill_context.rstrip()}\n\n" if application_skill_context else ""
    title_line = f"目标标题：{title}\n" if title else ""
    return (
        fixed_policy
        + method
        + "## 项目工作规则\n\n"
        + _project_skill_markdown(project_skill)
        + "\n\n## 当前文档任务\n\n"
        + title_line
        + brief
    )


def _project_skill_markdown(skill: Mapping[str, object]) -> str:
    lines = [
        f"项目：{_required_str(skill, 'name')}",
        f"Project Skill revision：{_required_int(skill, 'revision')}",
        f"项目目标：{_required_str(skill, 'purpose')}",
        "输出规则：",
    ]
    for item in _required_list(skill, "output_rules"):
        if not isinstance(item, Mapping):
            continue
        rule = item.get("rule")
        if isinstance(rule, str) and rule.strip():
            lines.append(f"- [{item.get('priority', 'should')}] {rule.strip()}")
    style = skill.get("style_preferences")
    if isinstance(style, Mapping):
        voice = style.get("voice")
        if isinstance(voice, str) and voice.strip():
            lines.append(f"表达风格：{voice.strip()}")
        defaults = style.get("format_defaults")
        if isinstance(defaults, list):
            clean_defaults = [item.strip() for item in defaults if isinstance(item, str) and item.strip()]
            if clean_defaults:
                lines.append("默认格式：" + "；".join(clean_defaults))
    contexts = []
    for item in _required_list(skill, "required_context"):
        if not isinstance(item, Mapping):
            continue
        reason = item.get("reason")
        uri = item.get("uri")
        if isinstance(reason, str) and isinstance(uri, str):
            contexts.append(f"- {reason}：{uri}")
    if contexts:
        lines.extend(("必读上下文：", *contexts))
    return "\n".join(lines)


def _project_skill_source_refs(skill: Mapping[str, object]) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    seen: set[tuple[str, str, str | None]] = set()
    candidates: list[object] = [
        *_required_list(skill, "source_refs"),
        *_required_list(skill, "evidence_refs"),
    ]
    for rule in _required_list(skill, "output_rules"):
        if isinstance(rule, Mapping):
            candidates.extend(_required_list(rule, "source_refs"))
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        source_id = candidate.get("source_id")
        locator = candidate.get("locator")
        quote = candidate.get("quote")
        if not isinstance(source_id, str) or not source_id:
            continue
        if not isinstance(locator, str) or not locator:
            continue
        key = (source_id, locator, quote if isinstance(quote, str) else None)
        if key in seen:
            continue
        seen.add(key)
        ref: dict[str, object] = {"source_id": source_id, "locator": locator}
        if isinstance(quote, str):
            ref["quote"] = quote
        refs.append(ref)
    return refs


def _project_summary(skill: Mapping[str, object]) -> str:
    return _project_skill_markdown(skill)[:8000]


def _application_skill_context(value: object, *, project_id: str) -> dict[str, object]:
    expected = {
        "resolution_id",
        "project_id",
        "consumer",
        "context_markdown",
        "selected",
        "fallback",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ProjectDocumentModelRequestError("Application Skill context schema drifted")
    if value.get("project_id") != project_id or value.get("consumer") != "document.generate":
        raise ProjectDocumentModelRequestError(
            "Application Skill context project or consumer drifted"
        )
    resolution_id = _required_str(value, "resolution_id")
    if not _SKILL_RESOLUTION_ID.fullmatch(resolution_id):
        raise ProjectDocumentModelRequestError("Application Skill resolution id is invalid")
    context = value.get("context_markdown")
    if not isinstance(context, str) or len(context.encode("utf-8")) > 32 * 1024:
        raise ProjectDocumentModelRequestError("Application Skill context is invalid or unbounded")
    selected = value.get("selected")
    if not isinstance(selected, Sequence) or isinstance(selected, (str, bytes)) or len(selected) > 3:
        raise ProjectDocumentModelRequestError("Application Skill selection is invalid or unbounded")
    clean_selected = []
    for item in selected:
        if not isinstance(item, Mapping):
            raise ProjectDocumentModelRequestError("Application Skill selection item is invalid")
        clean_selected.append(dict(item))
    fallback = value.get("fallback")
    if fallback not in {"none", "default_consumer_flow"}:
        raise ProjectDocumentModelRequestError("Application Skill fallback is invalid")
    if (bool(clean_selected) and (not context or fallback != "none")) or (
        not clean_selected and (context or fallback != "default_consumer_flow")
    ):
        raise ProjectDocumentModelRequestError("Application Skill context selection drifted")
    return {
        "resolution_id": resolution_id,
        "project_id": project_id,
        "consumer": "document.generate",
        "context_markdown": context,
        "selected": clean_selected,
        "fallback": fallback,
    }


def _model_request_id(
    *,
    project_id: str,
    project_skill: Mapping[str, object],
    brief: str,
    title: str | None,
    source_refs: Sequence[Mapping[str, object]],
) -> str:
    return _stable_id(
        "model-request-document",
        project_id,
        _required_str(project_skill, "id"),
        str(_required_int(project_skill, "revision")),
        title or "",
        brief,
        json.dumps(list(source_refs), ensure_ascii=False, sort_keys=True),
    )


def _required_text(value: object, label: str, *, maximum: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > maximum:
        raise ProjectDocumentModelRequestError(f"{label} is required and must be at most {maximum} characters")
    return text


def _optional_text(value: object, label: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, label, maximum=maximum)


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ProjectDocumentModelRequestError(f"{key} is required")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProjectDocumentModelRequestError(f"{key} must be an integer")
    return value


def _required_list(mapping: Mapping[str, object], key: str) -> list[object]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise ProjectDocumentModelRequestError(f"{key} must be a list")
    return list(value)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
