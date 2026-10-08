from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from core.project_skill_core import ProjectSkillRepositoryPort


class RecallRepositoryPort(Protocol):
    """Persists recall requests and evidence results."""

    def create_project_default_request(
        self,
        *,
        project_id: str,
        query: str,
        project_skill_id: str,
        layers: Sequence[str] | None = None,
        created_at: str | None = None,
    ) -> Mapping[str, object]:
        """Create the project-scoped request that starts from Project Skill."""

    def save_result(self, result: Mapping[str, object]) -> Mapping[str, object]:
        """Persist a Recall Result evidence package."""


class ProjectSkillFirstRecallError(ValueError):
    """Raised when a project recall cannot safely start from current Project Skill."""


@dataclass(frozen=True, slots=True)
class ProjectSkillFirstRecallResult:
    project_id: str
    skill_id: str
    skill_revision: int
    request_id: str
    result_id: str


class CreateProjectSkillFirstRecall:
    """Creates the minimum Project Skill-first evidence package for project Q&A."""

    def __init__(
        self,
        *,
        skills: ProjectSkillRepositoryPort,
        recalls: RecallRepositoryPort,
    ) -> None:
        self._skills = skills
        self._recalls = recalls

    def execute(
        self,
        project_id: str,
        *,
        query: str,
        created_at: str | None = None,
    ) -> ProjectSkillFirstRecallResult:
        skill = self._current_skill(project_id)
        skill_id = _required_str(skill, "id")
        skill_revision = _required_int(skill, "revision")
        request = self._recalls.create_project_default_request(
            project_id=project_id,
            query=query,
            project_skill_id=skill_id,
            created_at=created_at,
        )
        result = self._recalls.save_result(
            _skill_first_result(
                request=request,
                skill=skill,
                created_at=created_at or _required_str(skill, "updated_at"),
            )
        )
        return ProjectSkillFirstRecallResult(
            project_id=project_id,
            skill_id=skill_id,
            skill_revision=skill_revision,
            request_id=_required_str(request, "id"),
            result_id=_required_str(result, "id"),
        )

    def _current_skill(self, project_id: str) -> Mapping[str, object]:
        skill = self._skills.load(project_id)
        if skill is None:
            raise ProjectSkillFirstRecallError(f"Project Skill not found: {project_id}")
        if skill.get("status") != "active":
            raise ProjectSkillFirstRecallError("Project Skill must be active for recall")
        if skill.get("trust_status") not in {"user_confirmed", "trusted", "system_generated"}:
            raise ProjectSkillFirstRecallError("Project Skill trust status is not eligible for recall")
        conflict = skill.get("conflict")
        if not isinstance(conflict, Mapping) or conflict.get("status") != "none":
            raise ProjectSkillFirstRecallError("Project Skill cannot drive recall while conflict is unresolved")
        for context in _required_list(skill, "required_context"):
            if isinstance(context, Mapping) and context.get("stale") is True:
                raise ProjectSkillFirstRecallError("Project Skill cannot drive recall with stale required context")
        source_refs = _skill_source_refs(skill)
        if not source_refs:
            raise ProjectSkillFirstRecallError("Project Skill recall requires source refs")
        return skill


def _skill_first_result(
    *,
    request: Mapping[str, object],
    skill: Mapping[str, object],
    created_at: str,
) -> dict[str, object]:
    request_id = _required_str(request, "id")
    project_id = _required_str(request, "project_id")
    skill_id = _required_str(skill, "id")
    layers = _required_string_list(request, "layers")
    source_refs = _skill_source_refs(skill)
    token_estimate = _token_estimate(skill)
    hit = {
        "hit_id": _stable_id("hit-project-skill", request_id, skill_id),
        "layer": "l3_project_skill",
        "object_id": skill_id,
        "project_id": project_id,
        "source_project_label": None,
        "trust_status": _required_str(skill, "trust_status"),
        "score": 1.0,
        "token_estimate": token_estimate,
        "source_refs": source_refs,
        "snippet": _skill_snippet(skill),
        "explanation": "当前项目 Skill 是项目问答的首个证据层。",
    }
    missing_layers = [layer for layer in layers if layer != "l3_project_skill"]
    return {
        "schema_version": "1.0.0",
        "id": _stable_id("recall-result", request_id, skill_id),
        "request_id": request_id,
        "project_id": project_id,
        "status": "partial",
        "hits": [hit],
        "coverage": {
            "status": "partial",
            "requested_layers": layers,
            "covered_layers": ["l3_project_skill"],
            "missing_layers": missing_layers,
            "low_trust": False,
            "source_ref_count": len(source_refs),
        },
        "truncation": {
            "applied": False,
            "reason": "none",
            "dropped_hit_ids": [],
            "final_hit_count": 1,
            "final_token_estimate": token_estimate,
        },
        "explanation": {
            "summary": "已先读取当前 Project Skill，后续层级证据尚未接入。",
            "layer_order": layers,
            "warnings": ["only_project_skill_evidence"],
        },
        "cross_project": {
            "used": False,
            "grant_id": None,
            "project_ids": [],
        },
        "errors": [],
        "created_at": created_at,
    }


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


def _skill_snippet(skill: Mapping[str, object]) -> str:
    lines = [_required_str(skill, "purpose")]
    for rule in _required_list(skill, "output_rules"):
        if not isinstance(rule, Mapping):
            continue
        value = rule.get("rule")
        if isinstance(value, str) and value:
            lines.append(value)
            break
    return " ".join(lines)


def _token_estimate(skill: Mapping[str, object]) -> int:
    text = f"{_required_str(skill, 'purpose')} {_skill_snippet(skill)}"
    return max(1, min(12000, len(text)))


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ProjectSkillFirstRecallError(f"{key} is required")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProjectSkillFirstRecallError(f"{key} must be an integer")
    return value


def _required_list(mapping: Mapping[str, object], key: str) -> list[object]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise ProjectSkillFirstRecallError(f"{key} must be a list")
    return list(value)


def _required_string_list(mapping: Mapping[str, object], key: str) -> list[str]:
    values = _required_list(mapping, key)
    if not all(isinstance(value, str) and value for value in values):
        raise ProjectSkillFirstRecallError(f"{key} must contain strings")
    return list(values)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"
