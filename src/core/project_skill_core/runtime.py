from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from core.storage_provider import ObjectStorePort, ObjectStoreRevisionError

from .ports import ProjectSkillUpdate


class ProjectSkillRepositoryError(ValueError):
    """Raised when a Project Skill operation violates the runtime contract."""


class ProjectSkillExpectedRevisionError(ProjectSkillRepositoryError):
    """Raised when the caller saves against a stale Project Skill revision."""


_TRANSITION_CONTRACTS = {
    "revision_saved": ("system", "unspecified"),
    "user_edit": ("user", "direct_user_save"),
    "user_rollback": ("user", "direct_user_rollback"),
    "ai_publication": ("user", "review_and_second_confirmation"),
    "ai_publication_rollback": ("user", "publication_rollback_confirmation"),
    "external_proposal_apply": ("user", "external_review_confirmation"),
}


@dataclass(slots=True)
class ObjectStoreProjectSkillRepository:
    """ObjectStore-backed Project Skill repository for Phase 4 runtime smoke."""

    object_store: ObjectStorePort
    namespace_id: str = "default"
    now: str = "2026-06-29T19:00:00+08:00"

    def load(self, project_id: str) -> Mapping[str, object] | None:
        skill_id = self._index(project_id)
        if skill_id is None:
            return None
        skill = self.object_store.read("project_skills", skill_id)
        return dict(skill) if skill is not None else None

    def get(self, skill_id: str) -> Mapping[str, object] | None:
        skill = self.object_store.read("project_skills", skill_id)
        return dict(skill) if skill is not None else None

    def list_by_project(self, project_id: str) -> tuple[Mapping[str, object], ...]:
        return tuple(skill for skill in self.list_all() if skill.get("project_id") == project_id)

    def list_all(self) -> tuple[Mapping[str, object], ...]:
        return tuple(sorted((dict(skill) for skill in self.object_store.list("project_skills")), key=lambda skill: str(skill.get("id", ""))))

    def save(self, update: ProjectSkillUpdate) -> Mapping[str, object]:
        _validate_transition(update)
        current = self.load(update.project_id)
        current_revision = 0 if current is None else _required_int(current, "revision")
        if current_revision != update.expected_revision:
            raise ProjectSkillExpectedRevisionError(
                f"expected revision {update.expected_revision}, found {current_revision}"
            )
        revision = current_revision + 1
        skill = self._skill_from_update(update, revision=revision, current=current)
        self._validate_publish_gate(skill)
        skill_id = _required_str(skill, "id")
        try:
            self.object_store.write(
                "project_skills",
                skill_id,
                skill,
                expected_revision=current_revision,
            )
        except ObjectStoreRevisionError as exc:
            raise ProjectSkillExpectedRevisionError(str(exc)) from exc
        self.object_store.write(
            "project_skill_markdown",
            _revision_object_id(skill_id, revision),
            {
                "id": _revision_object_id(skill_id, revision),
                "skill_id": skill_id,
                "project_id": update.project_id,
                "revision": revision,
                "markdown": update.markdown,
                "created_at": self.now,
            },
            expected_revision=0,
        )
        self.object_store.write(
            "project_skill_json",
            _revision_object_id(skill_id, revision),
            {
                "id": _revision_object_id(skill_id, revision),
                "skill_id": skill_id,
                "project_id": update.project_id,
                "revision": revision,
                "structured": skill,
                "created_at": self.now,
            },
            expected_revision=0,
        )
        self.object_store.write(
            "project_skill_revisions",
            _revision_object_id(skill_id, revision),
            {
                "id": _revision_object_id(skill_id, revision),
                "skill_id": skill_id,
                "project_id": update.project_id,
                "revision": revision,
                "parent_revision": None if current_revision == 0 else current_revision,
                "reason": update.reason.strip(),
                "transition_kind": update.transition_kind,
                "actor": update.actor,
                "confirmation_kind": update.confirmation_kind,
                "source_revision": update.source_revision,
                "markdown_uri": skill["markdown_uri"],
                "json_uri": skill["json_uri"],
                "created_at": self.now,
            },
            expected_revision=0,
        )
        self.object_store.write(
            "project_skill_index",
            update.project_id,
            {"project_id": update.project_id, "skill_id": skill_id, "updated_at": self.now},
            expected_revision=current_revision,
        )
        return dict(skill)

    def rollback(
        self,
        project_id: str,
        *,
        target_revision: int,
        expected_revision: int,
        reason: str,
    ) -> Mapping[str, object]:
        current = self.load(project_id)
        if current is None:
            raise ProjectSkillRepositoryError("Project Skill not found")
        current_revision = _required_int(current, "revision")
        if current_revision != expected_revision:
            raise ProjectSkillExpectedRevisionError(
                f"expected revision {expected_revision}, found {current_revision}"
            )
        if not isinstance(target_revision, int) or isinstance(target_revision, bool) or target_revision < 1:
            raise ProjectSkillRepositoryError("target revision must be a positive integer")
        if target_revision >= current_revision:
            raise ProjectSkillRepositoryError("target revision must precede current revision")
        target = self.structured(project_id, revision=target_revision)
        markdown = self.markdown(project_id, revision=target_revision)
        if target is None or markdown is None:
            raise ProjectSkillRepositoryError("target Project Skill revision is incomplete")
        if current.get("status") != "active" or target.get("status") != "active":
            raise ProjectSkillRepositoryError("direct rollback requires active Project Skill revisions")
        return self.save(
            ProjectSkillUpdate(
                project_id=project_id,
                markdown=markdown,
                structured=target,
                expected_revision=expected_revision,
                reason=reason,
                transition_kind="user_rollback",
                actor="user",
                confirmation_kind="direct_user_rollback",
                source_revision=target_revision,
            )
        )

    def markdown(self, project_id: str, *, revision: int | None = None) -> str | None:
        skill = self.load(project_id)
        if skill is None:
            return None
        skill_id = _required_str(skill, "id")
        target_revision = revision if revision is not None else _required_int(skill, "markdown_revision")
        payload = self.object_store.read("project_skill_markdown", _revision_object_id(skill_id, target_revision))
        if payload is None:
            return None
        markdown = payload.get("markdown")
        if not isinstance(markdown, str):
            raise ProjectSkillRepositoryError("stored skill markdown must contain markdown")
        return markdown

    def structured(self, project_id: str, *, revision: int | None = None) -> Mapping[str, object] | None:
        skill = self.load(project_id)
        if skill is None:
            return None
        skill_id = _required_str(skill, "id")
        target_revision = revision if revision is not None else _required_int(skill, "json_revision")
        payload = self.object_store.read("project_skill_json", _revision_object_id(skill_id, target_revision))
        if payload is None:
            return None
        structured = payload.get("structured")
        if not isinstance(structured, Mapping):
            raise ProjectSkillRepositoryError("stored skill json must contain structured object")
        return dict(structured)

    def revisions(self, project_id: str) -> tuple[Mapping[str, object], ...]:
        skill = self.load(project_id)
        if skill is None:
            return ()
        skill_id = _required_str(skill, "id")
        revisions = [
            dict(item)
            for item in self.object_store.list("project_skill_revisions")
            if item.get("skill_id") == skill_id
        ]
        return tuple(sorted(revisions, key=lambda item: int(item["revision"])))

    def _index(self, project_id: str) -> str | None:
        index = self.object_store.read("project_skill_index", project_id)
        if index is None:
            return None
        skill_id = index.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id:
            raise ProjectSkillRepositoryError("project skill index requires skill_id")
        return skill_id

    def _skill_from_update(
        self,
        update: ProjectSkillUpdate,
        *,
        revision: int,
        current: Mapping[str, object] | None,
    ) -> dict[str, object]:
        structured = dict(update.structured)
        skill_id = _required_str(structured, "id") if "id" in structured else f"skill-{update.project_id}"
        if current is not None and skill_id != current.get("id"):
            raise ProjectSkillRepositoryError("Project Skill id cannot change across revisions")
        project_id = structured.get("project_id", update.project_id)
        if project_id != update.project_id:
            raise ProjectSkillRepositoryError("Project Skill project_id must match update project_id")
        structured["id"] = skill_id
        structured["project_id"] = update.project_id
        structured["markdown_uri"] = f"crp://{self.namespace_id}/projects/{update.project_id}/project-skill.md"
        structured["json_uri"] = f"crp://{self.namespace_id}/projects/{update.project_id}/project-skill.json"
        structured["markdown_revision"] = revision
        structured["json_revision"] = revision
        structured["revision"] = revision
        structured.setdefault("schema_version", "1.0.0")
        structured.setdefault("created_at", self.now if current is None else current.get("created_at", self.now))
        structured["updated_at"] = self.now
        if current is None:
            existing_decisions = structured.get("decision_log")
        else:
            existing_decisions = current.get("decision_log")
        decisions = list(existing_decisions) if isinstance(existing_decisions, list) else []
        if current is not None or not decisions:
            decisions.append(
                {
                    "decision_id": f"decision-{skill_id}-{update.transition_kind}-r{revision}",
                    "reason": update.reason.strip(),
                    "actor": update.actor,
                    "created_at": self.now,
                }
            )
        structured["decision_log"] = decisions
        _assert_json_object(structured)
        return structured

    def _validate_publish_gate(self, skill: Mapping[str, object]) -> None:
        revision = _required_int(skill, "revision")
        if _required_int(skill, "markdown_revision") != revision:
            raise ProjectSkillRepositoryError("markdown_revision must match revision")
        if _required_int(skill, "json_revision") != revision:
            raise ProjectSkillRepositoryError("json_revision must match revision")
        status = _required_str(skill, "status")
        if status != "active":
            return
        conflict = skill.get("conflict")
        if not isinstance(conflict, Mapping) or conflict.get("status") != "none":
            raise ProjectSkillRepositoryError("active Project Skill cannot have unresolved conflict")
        if skill.get("trust_status") == "imported_unverified":
            raise ProjectSkillRepositoryError("imported_unverified Project Skill cannot become active")
        for context in _required_list(skill, "required_context"):
            if isinstance(context, Mapping) and context.get("stale") is True:
                raise ProjectSkillRepositoryError("active Project Skill cannot use stale required context")
        update_rules = skill.get("update_rules")
        if not isinstance(update_rules, Mapping):
            raise ProjectSkillRepositoryError("Project Skill requires update_rules")
        if update_rules.get("user_edit_policy") != "user_wins":
            raise ProjectSkillRepositoryError("active Project Skill must keep user_edit_policy=user_wins")


def _revision_object_id(skill_id: str, revision: int) -> str:
    return f"{skill_id}~r{revision}"


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ProjectSkillRepositoryError(f"{key} is required")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProjectSkillRepositoryError(f"{key} must be an integer")
    return value


def _required_list(mapping: Mapping[str, object], key: str) -> list[object]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise ProjectSkillRepositoryError(f"{key} must be a list")
    return list(value)


def _assert_json_object(value: Mapping[str, object]) -> None:
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError as exc:
        raise ProjectSkillRepositoryError("Project Skill structured payload must be JSON serializable") from exc


def _validate_transition(update: ProjectSkillUpdate) -> None:
    reason = update.reason.strip() if isinstance(update.reason, str) else ""
    if not reason or len(reason) > 500:
        raise ProjectSkillRepositoryError("Project Skill update reason is required and must be at most 500 characters")
    expected = _TRANSITION_CONTRACTS.get(update.transition_kind)
    if expected is None:
        raise ProjectSkillRepositoryError("unsupported Project Skill transition")
    if (update.actor, update.confirmation_kind) != expected:
        raise ProjectSkillRepositoryError("Project Skill transition evidence is inconsistent")
    if update.transition_kind == "user_rollback":
        if not isinstance(update.source_revision, int) or isinstance(update.source_revision, bool) or update.source_revision < 1:
            raise ProjectSkillRepositoryError("user rollback requires source_revision")
    elif update.source_revision is not None:
        raise ProjectSkillRepositoryError("source_revision is only valid for user rollback")
