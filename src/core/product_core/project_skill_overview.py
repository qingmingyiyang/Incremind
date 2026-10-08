from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


class ProjectSkillOverviewReaderPort(Protocol):
    """Read-only Project Skill repository view used by Product Core."""

    def load(self, project_id: str) -> Mapping[str, object] | None:
        """Load the current Project Skill metadata."""

    def revisions(self, project_id: str) -> tuple[Mapping[str, object], ...]:
        """List persisted Project Skill revision metadata."""


class ProjectSkillOverviewError(ValueError):
    """Raised when Project Skill overview input or stored payload is invalid."""


@dataclass(frozen=True, slots=True)
class ProjectSkillOverviewRevision:
    revision: int
    revision_id: str
    markdown_ref: str
    json_ref: str
    reason: str | None
    created_at: str | None


@dataclass(frozen=True, slots=True)
class ProjectSkillOverviewItem:
    skill_id: str
    project_id: str
    name: str
    purpose: str
    status: str
    trust_status: str
    revision: int
    markdown_revision: int
    json_revision: int
    markdown_ref: str
    json_ref: str
    required_context_refs: tuple[str, ...]
    output_rule_refs: tuple[str, ...]
    source_refs: tuple[str, ...]
    evidence_state: str
    user_edit_policy: str | None
    update_strategy: str | None
    conflict_status: str | None
    revisions: tuple[ProjectSkillOverviewRevision, ...]
    blocked_operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectSkillOverview:
    status: str
    project_id: str
    skill: ProjectSkillOverviewItem | None
    blocked_operations: tuple[str, ...]
    next_step_boundary: str


class GetProjectSkillOverview:
    """Builds a read-only Project Skill overview without AI rewrite or mutation."""

    _BLOCKED_OPERATIONS = (
        "ai_rewrite",
        "silent_user_edit_overwrite",
        "project_skill_mutation",
        "project_skill_save",
        "memory_publication",
        "source_content_read",
        "real_library_write",
    )

    def __init__(self, reader: ProjectSkillOverviewReaderPort) -> None:
        self._reader = reader

    def execute(self, project_id: str) -> ProjectSkillOverview:
        normalized_project = _normalize_project_id(project_id)
        skill = self._reader.load(normalized_project)
        if skill is None:
            return ProjectSkillOverview(
                status="missing",
                project_id=normalized_project,
                skill=None,
                blocked_operations=self._BLOCKED_OPERATIONS,
                next_step_boundary="project_skill_overview_missing_create_or_import_required",
            )
        item = _overview_item(skill, revisions=self._reader.revisions(normalized_project))
        return ProjectSkillOverview(
            status="ready" if item.evidence_state == "ready" else "degraded",
            project_id=normalized_project,
            skill=item,
            blocked_operations=self._BLOCKED_OPERATIONS,
            next_step_boundary=(
                "project_skill_overview_ready_for_endpoint_display"
                if item.evidence_state == "ready"
                else "project_skill_overview_requires_source_refs_before_rewrite_or_answer"
            ),
        )


def serialize_project_skill_overview(overview: ProjectSkillOverview) -> dict[str, object]:
    return {
        "status": overview.status,
        "project_id": overview.project_id,
        "skill": _serialize_item(overview.skill),
        "blocked_operations": list(overview.blocked_operations),
        "next_step_boundary": overview.next_step_boundary,
    }


def _serialize_item(item: ProjectSkillOverviewItem | None) -> dict[str, object] | None:
    if item is None:
        return None
    return {
        "skill_id": item.skill_id,
        "project_id": item.project_id,
        "name": item.name,
        "purpose": item.purpose,
        "status": item.status,
        "trust_status": item.trust_status,
        "revision": item.revision,
        "markdown_revision": item.markdown_revision,
        "json_revision": item.json_revision,
        "markdown_ref": item.markdown_ref,
        "json_ref": item.json_ref,
        "required_context_refs": list(item.required_context_refs),
        "output_rule_refs": list(item.output_rule_refs),
        "source_refs": list(item.source_refs),
        "evidence_state": item.evidence_state,
        "user_edit_policy": item.user_edit_policy,
        "update_strategy": item.update_strategy,
        "conflict_status": item.conflict_status,
        "revisions": [
            {
                "revision": revision.revision,
                "revision_id": revision.revision_id,
                "markdown_ref": revision.markdown_ref,
                "json_ref": revision.json_ref,
                "reason": revision.reason,
                "created_at": revision.created_at,
            }
            for revision in item.revisions
        ],
        "blocked_operations": list(item.blocked_operations),
    }


def _overview_item(
    skill: Mapping[str, object],
    *,
    revisions: Sequence[Mapping[str, object]],
) -> ProjectSkillOverviewItem:
    skill_id = _required_str(skill, "id")
    project_id = _required_str(skill, "project_id")
    source_refs = _skill_source_refs(skill)
    update_rules = skill.get("update_rules")
    conflict = skill.get("conflict")
    return ProjectSkillOverviewItem(
        skill_id=skill_id,
        project_id=project_id,
        name=_required_str(skill, "name"),
        purpose=_required_str(skill, "purpose"),
        status=_required_str(skill, "status"),
        trust_status=_required_str(skill, "trust_status"),
        revision=_required_int(skill, "revision"),
        markdown_revision=_required_int(skill, "markdown_revision"),
        json_revision=_required_int(skill, "json_revision"),
        markdown_ref=_required_str(skill, "markdown_uri"),
        json_ref=_required_str(skill, "json_uri"),
        required_context_refs=_context_refs(skill.get("required_context")),
        output_rule_refs=_output_rule_refs(skill.get("output_rules")),
        source_refs=tuple(source_refs),
        evidence_state="ready" if source_refs else "missing_source_refs",
        user_edit_policy=(
            _optional_str(update_rules.get("user_edit_policy")) if isinstance(update_rules, Mapping) else None
        ),
        update_strategy=(
            _optional_str(update_rules.get("patch_strategy")) if isinstance(update_rules, Mapping) else None
        ),
        conflict_status=_optional_str(conflict.get("status")) if isinstance(conflict, Mapping) else None,
        revisions=tuple(_overview_revision(revision) for revision in revisions),
        blocked_operations=GetProjectSkillOverview._BLOCKED_OPERATIONS,
    )


def _overview_revision(revision: Mapping[str, object]) -> ProjectSkillOverviewRevision:
    return ProjectSkillOverviewRevision(
        revision=_required_int(revision, "revision"),
        revision_id=_required_str(revision, "id"),
        markdown_ref=_required_str(revision, "markdown_uri"),
        json_ref=_required_str(revision, "json_uri"),
        reason=_optional_str(revision.get("reason")),
        created_at=_optional_str(revision.get("created_at")),
    )


def _normalize_project_id(project_id: str) -> str:
    normalized = project_id.strip()
    if not normalized:
        raise ProjectSkillOverviewError("project_id cannot be empty")
    return normalized


def _skill_source_refs(skill: Mapping[str, object]) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for candidate in (
        *_list_or_empty(skill.get("source_refs")),
        *_list_or_empty(skill.get("evidence_refs")),
    ):
        _append_source_ref(refs, seen, candidate)
    for rule in _list_or_empty(skill.get("output_rules")):
        if isinstance(rule, Mapping):
            for candidate in _list_or_empty(rule.get("source_refs")):
                _append_source_ref(refs, seen, candidate)
    return refs


def _append_source_ref(refs: list[str], seen: set[str], candidate: object) -> None:
    if not isinstance(candidate, Mapping):
        return
    source_id = candidate.get("source_id")
    locator = candidate.get("locator")
    if not isinstance(source_id, str) or not source_id:
        return
    if not isinstance(locator, str) or not locator:
        return
    ref = f"{source_id}#{locator}"
    if ref in seen:
        return
    seen.add(ref)
    refs.append(ref)


def _context_refs(value: object) -> tuple[str, ...]:
    refs: list[str] = []
    for context in _list_or_empty(value):
        if not isinstance(context, Mapping):
            continue
        kind = _optional_str(context.get("kind")) or "context"
        object_id = _optional_str(context.get("object_id"))
        uri = _optional_str(context.get("uri"))
        if object_id:
            refs.append(f"{kind}:{object_id}")
        elif uri:
            refs.append(f"{kind}:{uri}")
    return tuple(refs)


def _output_rule_refs(value: object) -> tuple[str, ...]:
    refs: list[str] = []
    for rule in _list_or_empty(value):
        if not isinstance(rule, Mapping):
            continue
        rule_id = _optional_str(rule.get("rule_id"))
        priority = _optional_str(rule.get("priority")) or "unspecified"
        if rule_id:
            refs.append(f"{rule_id}:{priority}")
    return tuple(refs)


def _list_or_empty(value: object) -> list[object]:
    if not isinstance(value, list):
        return []
    return list(value)


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ProjectSkillOverviewError(f"{key} is required")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProjectSkillOverviewError(f"{key} must be an integer")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
