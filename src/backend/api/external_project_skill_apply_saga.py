from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from core.project_skill_core import (
    ProjectSkillExpectedRevisionError,
    ProjectSkillRepositoryError,
    ProjectSkillUpdate,
)
from core.storage_provider import (
    ExternalProjectSkillApplyEvidence,
    ExternalProjectSkillApplySagaConflict,
    ExternalProjectSkillApplyOperation,
    ObjectStorePort,
)


class ExternalProjectSkillApplyError(ValueError):
    pass


class ExternalProjectSkillApplyConflict(ExternalProjectSkillApplyError):
    pass


class ProjectSkillRepository(Protocol):
    def load(self, project_id: str) -> Mapping[str, object] | None: ...
    def save(self, update: ProjectSkillUpdate) -> Mapping[str, object]: ...
    def revisions(self, project_id: str) -> tuple[Mapping[str, object], ...]: ...


class OperationStore(Protocol):
    def prepare(self, *, operation_id: str, evidence: ExternalProjectSkillApplyEvidence, now: str | None = None) -> ExternalProjectSkillApplyOperation: ...
    def mark_skill_applied(self, operation_id: str, *, expected_revision: int, applied_skill_revision: int, now: str | None = None) -> ExternalProjectSkillApplyOperation: ...
    def finalize(self, operation_id: str, *, expected_revision: int, now: str | None = None) -> ExternalProjectSkillApplyOperation: ...


@dataclass(frozen=True, slots=True)
class ExternalProjectSkillApplyResult:
    operation_id: str
    operation_revision: int
    project_id: str
    project_skill_id: str
    project_skill_revision: int
    state: str


class ExternalProjectSkillApplySagaService:
    def __init__(self, *, skills: ProjectSkillRepository, drafts: ObjectStorePort, operations: OperationStore,
                 namespace_id: str = "default", project_skill_authority_identity: str, now=None) -> None:
        self._skills, self._drafts, self._operations = skills, drafts, operations
        self._namespace_id = namespace_id
        self._authority = project_skill_authority_identity
        self._now = now or _utc_now

    def prepare(self, draft_id: str, *, expected_revision: int) -> ExternalProjectSkillApplyOperation:
        draft = self._drafts.read("external_agent_review_drafts", draft_id)
        if draft is None:
            raise ExternalProjectSkillApplyError("external agent review draft not found")
        project_id, skill_id, structured, markdown = _draft_contract(draft, expected_revision)
        payload_hash = _payload_hash(draft_id, project_id, skill_id, expected_revision, structured, markdown)
        evidence = ExternalProjectSkillApplyEvidence(
            self._namespace_id, project_id, skill_id, expected_revision, payload_hash, self._authority
        )
        try:
            return self._operations.prepare(operation_id=draft_id, evidence=evidence)
        except ExternalProjectSkillApplySagaConflict as exc:
            raise ExternalProjectSkillApplyConflict(str(exc)) from exc

    def apply(self, draft_id: str, *, expected_revision: int) -> ExternalProjectSkillApplyResult:
        operation = self.prepare(draft_id, expected_revision=expected_revision)
        evidence = operation.evidence
        project_id = evidence.project_id
        skill_id = evidence.project_skill_id
        payload_hash = evidence.payload_sha256
        draft = self._drafts.read("external_agent_review_drafts", draft_id)
        if draft is None:
            raise ExternalProjectSkillApplyError("external agent review draft not found")
        _project_id, _skill_id, structured, markdown = _draft_contract(draft, expected_revision)
        token = f"external_project_skill_apply:{draft_id}:{payload_hash}"
        if operation.state == "prepared":
            applied_revision = _matching_revision(self._skills.revisions(project_id), token, expected_revision)
            if applied_revision is None:
                current = self._skills.load(project_id)
                current_revision = 0 if current is None else current.get("revision")
                if current_revision != expected_revision:
                    raise ExternalProjectSkillApplyConflict("project skill revision advanced without operation evidence")
                try:
                    updated = self._skills.save(ProjectSkillUpdate(
                        project_id,
                        markdown,
                        structured,
                        expected_revision,
                        token,
                        transition_kind="external_proposal_apply",
                        actor="user",
                        confirmation_kind="external_review_confirmation",
                    ))
                except ProjectSkillExpectedRevisionError as exc:
                    raise ExternalProjectSkillApplyConflict(str(exc)) from exc
                except ProjectSkillRepositoryError as exc:
                    raise ExternalProjectSkillApplyError(str(exc)) from exc
                applied_revision = _positive(updated.get("revision"), "applied project skill revision")
            operation = self._operations.mark_skill_applied(
                draft_id, expected_revision=operation.revision, applied_skill_revision=applied_revision
            )
        if operation.state == "skill_applied":
            applied_revision = _positive(operation.applied_skill_revision, "applied project skill revision")
            current_draft = self._drafts.read("external_agent_review_drafts", draft_id)
            if current_draft is None:
                raise ExternalProjectSkillApplyError("external agent review draft disappeared")
            if not _is_finalized(current_draft, draft_id, project_id, skill_id, applied_revision):
                if current_draft.get("status") != "pending_review":
                    raise ExternalProjectSkillApplyError("review draft state drifted before finalization")
                self._drafts.write("external_agent_review_drafts", draft_id, _finalized(
                    current_draft, draft_id, project_id, skill_id, applied_revision, self._now()
                ), expected_revision=None)
            operation = self._operations.finalize(draft_id, expected_revision=operation.revision)
        if operation.state != "finalized" or operation.applied_skill_revision is None:
            raise ExternalProjectSkillApplyError("project skill apply operation did not finalize")
        return ExternalProjectSkillApplyResult(draft_id, operation.revision, project_id, skill_id,
                                               operation.applied_skill_revision, operation.state)


def _draft_contract(draft, expected_revision):
    if draft.get("draft_type") != "project_skill_update":
        raise ExternalProjectSkillApplyError("review draft is not a project skill update")
    application = draft.get("application") if isinstance(draft.get("application"), Mapping) else {}
    if draft.get("status") != "pending_review" and not (draft.get("status") == "applied" and application.get("operation_id")):
        raise ExternalProjectSkillApplyError("review draft is not pending or recoverable")
    changes = draft.get("suggested_changes")
    structured = _first_mapping(changes, ("structured", "project_skill", "structured_project_skill"))
    markdown = _first_text(changes, ("markdown", "project_skill_markdown")) or _clean_text(draft.get("proposed_content"))
    if structured is None or markdown is None:
        raise ExternalProjectSkillApplyError("project skill draft payload is invalid")
    project_id = draft.get("project_id") or structured.get("project_id")
    skill_id = structured.get("id")
    if not isinstance(project_id, str) or not project_id or not isinstance(skill_id, str) or not skill_id:
        raise ExternalProjectSkillApplyError("project skill draft identity is invalid")
    if draft.get("target_id") not in {None, skill_id} or structured.get("project_id") != project_id:
        raise ExternalProjectSkillApplyError("project skill draft identity drifted")
    _positive(expected_revision, "expected_revision")
    return project_id, skill_id, dict(structured), markdown


def _first_mapping(value, keys):
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _first_text(value, keys):
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        candidate = _clean_text(value.get(key))
        if candidate is not None:
            return candidate
    return None


def _clean_text(value):
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _payload_hash(draft_id, project_id, skill_id, base_revision, structured, markdown):
    encoded = json.dumps({"draft_id": draft_id, "project_id": project_id, "skill_id": skill_id,
                          "base_revision": base_revision, "structured": structured, "markdown": markdown},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _matching_revision(revisions, token, base_revision):
    matches = [item for item in revisions if item.get("reason") == token and item.get("parent_revision") == base_revision]
    if len(matches) > 1:
        raise ExternalProjectSkillApplyConflict("multiple project skill revisions match one operation")
    return None if not matches else _positive(matches[0].get("revision"), "matched project skill revision")


def _is_finalized(draft, operation_id, project_id, skill_id, revision):
    app = draft.get("application") if isinstance(draft.get("application"), Mapping) else {}
    return draft.get("status") == "applied" and app.get("operation_id") == operation_id and app.get("applied_project_id") == project_id and app.get("applied_project_skill_id") == skill_id and app.get("applied_project_skill_revision") == revision


def _finalized(draft, operation_id, project_id, skill_id, revision, timestamp):
    result = dict(draft); review = dict(draft.get("review") or {}); app = dict(draft.get("application") or {})
    result.update({"status": "applied", "updated_at": timestamp})
    review.update({"state": "applied", "reviewed_by": "user", "reviewed_at": timestamp})
    app.update({"state": "applied", "operation_id": operation_id, "applied_by": "user", "applied_at": timestamp,
                "applied_project_id": project_id, "applied_project_skill_id": skill_id,
                "applied_project_skill_revision": revision, "writes_long_term_memory": False, "writes_staging_memory": False})
    result["review"], result["application"] = review, app
    return result


def _positive(value, label):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ExternalProjectSkillApplyError(f"{label} is invalid")
    return value


def _utc_now():
    return datetime.now(timezone.utc).isoformat()
