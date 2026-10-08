"""CAS-safe review of Project Skill candidates into SQLite staging drafts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.storage_provider import (
    JsonObjectStore,
    ObjectStoreRevisionError,
    ProjectSkillReviewStagingEvidence,
    ProjectSkillReviewStagingOperation,
    SQLiteProjectSkillReviewStagingSagaStore,
    SQLiteStructuredRecordStore,
)

from .publication_draft import (
    ProjectSkillPublicationDraftError,
    ProjectSkillPublicationTarget,
    build_project_skill_publication_draft,
    validate_project_skill_publication_draft,
)


_CANDIDATE_COLLECTION = "memory_candidates"
_STAGING_COLLECTION = "staging_project_skills"
_INDEX_COLLECTION = "project_skill_index"
_SKILL_COLLECTION = "project_skills"
_CANDIDATE_AUTHORITY = "json:object-store-v1"
_STAGING_AUTHORITY = "sqlite:structured-records-v1"


class ProjectSkillReviewStagingServiceError(ValueError):
    """Raised when review-to-staging evidence is unsafe to advance."""


class ProjectSkillReviewStagingServiceConflict(ProjectSkillReviewStagingServiceError):
    """Raised for CAS, identity, draft, or candidate drift."""


@dataclass(slots=True)
class ProjectSkillReviewStagingSagaService:
    """Coordinates a bounded JSON candidate → SQLite staging saga.

    This service deliberately does not publish a Project Skill aggregate. The
    staged publication draft remains subject to a separate explicit publish
    confirmation and composite Unit of Work.
    """

    candidates: JsonObjectStore
    records: SQLiteStructuredRecordStore
    operations: SQLiteProjectSkillReviewStagingSagaStore

    def __post_init__(self) -> None:
        if self.candidates.namespace_id is None or not isinstance(self.candidates.namespace_id, str):
            raise ProjectSkillReviewStagingServiceError("candidate namespace is invalid")
        if self.operations.records is not self.records:
            raise ProjectSkillReviewStagingServiceError(
                "review staging operations must share the SQLite records authority"
            )

    def prepare_review(
        self,
        candidate_id: str,
        *,
        review_reason: str,
        reviewed_at: str,
    ) -> ProjectSkillReviewStagingOperation:
        """Persist a deterministic operation without advancing either side effect."""

        candidate, candidate_revision = self._pending_candidate(candidate_id)
        target, current_payload = self._current_context(
            _required_string(candidate, "project_id")
        )
        generated_revision = candidate.get("expected_project_skill_revision")
        if generated_revision is not None and generated_revision != target.expected_revision:
            raise ProjectSkillReviewStagingServiceConflict(
                "Project Skill candidate generation baseline is stale"
            )
        source_refs = _source_refs(candidate.get("source_refs"))
        if not source_refs:
            raise ProjectSkillReviewStagingServiceError("candidate source refs are invalid")
        try:
            draft = build_project_skill_publication_draft(
                target=target,
                source_candidate_id=candidate_id,
                proposed_content=_required_string(candidate, "proposed_content"),
                source_refs=source_refs,
                evidence_refs=source_refs,
                reviewed_by="user",
                reviewed_at=reviewed_at,
                review_reason=review_reason,
                namespace_id=self.candidates.namespace_id,
                current_payload=current_payload,
                proposed_payload=(
                    candidate.get("project_skill_draft")
                    if isinstance(candidate.get("project_skill_draft"), Mapping)
                    else None
                ),
            )
        except ProjectSkillPublicationDraftError as exc:
            raise ProjectSkillReviewStagingServiceError(str(exc)) from exc
        evidence = ProjectSkillReviewStagingEvidence(
            namespace_id=self.candidates.namespace_id,
            candidate_id=candidate_id,
            candidate_revision=candidate_revision,
            candidate_sha256=_digest(candidate),
            project_id=target.project_id,
            skill_id=target.skill_id,
            expected_skill_revision=target.expected_revision,
            draft_id=_required_string(draft, "id"),
            draft_sha256=_required_string(draft, "draft_digest"),
            candidate_authority=_CANDIDATE_AUTHORITY,
            staging_authority=_STAGING_AUTHORITY,
        )
        return self.operations.prepare(
            operation_id=_operation_id(evidence),
            evidence=evidence,
            draft=draft,
            now=reviewed_at,
        )

    def review_to_staging(
        self,
        candidate_id: str,
        *,
        review_reason: str,
        reviewed_at: str,
    ) -> ProjectSkillReviewStagingOperation:
        """Prepare and complete the bounded saga, or safely replay a prior review."""

        candidate = self._candidate(candidate_id)
        if candidate.get("status") == "promoted":
            operation = self._operation_for_promoted_candidate(candidate)
            self._assert_replay_request(operation, review_reason, reviewed_at)
            return self.resume(operation.operation_id)
        operation = self.prepare_review(
            candidate_id,
            review_reason=review_reason,
            reviewed_at=reviewed_at,
        )
        return self.resume(operation.operation_id)

    def resume(self, operation_id: str) -> ProjectSkillReviewStagingOperation:
        """Advance one operation at most once per remaining state transition."""

        operation = self.operations.get(operation_id)
        if operation is None:
            raise ProjectSkillReviewStagingServiceError("review staging operation does not exist")
        for _ in range(3):
            if operation.state == "prepared":
                self._assert_pending_candidate(operation)
                self._stage_draft(operation)
                operation = self.operations.mark_sqlite_draft_staged(
                    operation.operation_id,
                    expected_revision=operation.revision,
                )
                continue
            if operation.state == "sqlite_draft_staged":
                self._assert_staged_draft(operation)
                if not self._candidate_is_reviewed(operation):
                    self._review_candidate(operation)
                operation = self.operations.mark_candidate_reviewed(
                    operation.operation_id,
                    expected_revision=operation.revision,
                )
                continue
            if operation.state == "candidate_reviewed":
                self._assert_staged_draft(operation)
                self._assert_candidate_reviewed(operation)
                operation = self.operations.finalize(
                    operation.operation_id,
                    expected_revision=operation.revision,
                )
                continue
            if operation.state == "finalized":
                self._assert_staged_draft(operation)
                self._assert_candidate_reviewed(operation)
                return operation
            raise ProjectSkillReviewStagingServiceError("review staging operation state is invalid")
        if operation.state != "finalized":
            raise ProjectSkillReviewStagingServiceError("review staging operation did not converge")
        self._assert_staged_draft(operation)
        self._assert_candidate_reviewed(operation)
        return operation

    def compensate(
        self,
        operation_id: str,
        *,
        compensation_code: str,
    ) -> ProjectSkillReviewStagingOperation:
        """Remove this operation's exact orphan draft and record a terminal outcome.

        Candidate content is never rewritten by compensation. Once a candidate
        was reviewed, publication evidence must be preserved and this method
        fails closed instead of attempting a reverse write across authorities.
        """

        operation = self.operations.get(operation_id)
        if operation is None:
            raise ProjectSkillReviewStagingServiceError("review staging operation does not exist")
        if operation.state == "compensated":
            return operation
        if operation.state not in {"prepared", "sqlite_draft_staged"}:
            raise ProjectSkillReviewStagingServiceConflict(
                "review staging operation already crossed the compensation boundary"
            )
        candidate = self.candidates.read(_CANDIDATE_COLLECTION, operation.evidence.candidate_id)
        if isinstance(candidate, Mapping):
            application = candidate.get("application")
            if candidate.get("status") == "promoted" or (
                isinstance(application, Mapping)
                and application.get("operation_id") == operation.operation_id
            ):
                raise ProjectSkillReviewStagingServiceConflict(
                    "reviewed candidate cannot be compensated"
                )
        draft = self._validated_draft(operation)
        try:
            with self.records.begin() as uow:
                staged = uow.read(_STAGING_COLLECTION, operation.evidence.draft_id)
                if staged is not None:
                    if dict(staged.payload) != draft:
                        raise ProjectSkillReviewStagingServiceConflict(
                            "staging draft compensation evidence drifted"
                        )
                    uow.delete(
                        _STAGING_COLLECTION,
                        operation.evidence.draft_id,
                        expected_revision=staged.revision,
                    )
                uow.commit()
        except ProjectSkillReviewStagingServiceError:
            raise
        except Exception as exc:
            raise ProjectSkillReviewStagingServiceConflict(
                "SQLite staging compensation conflicted"
            ) from exc
        return self.operations.mark_compensated(
            operation.operation_id,
            expected_revision=operation.revision,
            compensation_code=compensation_code,
        )

    def _stage_draft(self, operation: ProjectSkillReviewStagingOperation) -> None:
        draft = self._validated_draft(operation)
        try:
            with self.records.begin() as uow:
                target = _current_target_in_uow(uow, operation.evidence.project_id)
                _assert_target(operation, target)
                staged = uow.read(_STAGING_COLLECTION, operation.evidence.draft_id)
                if staged is None:
                    uow.put(
                        _STAGING_COLLECTION,
                        operation.evidence.draft_id,
                        draft,
                        expected_revision=0,
                    )
                elif dict(staged.payload) != draft:
                    raise ProjectSkillReviewStagingServiceConflict("staging draft evidence drifted")
                uow.commit()
        except ProjectSkillReviewStagingServiceError:
            raise
        except Exception as exc:
            raise ProjectSkillReviewStagingServiceConflict("SQLite staging write conflicted") from exc

    def _review_candidate(self, operation: ProjectSkillReviewStagingOperation) -> None:
        self._assert_current_target(operation)
        candidate = self._assert_pending_candidate(operation)
        draft = self._validated_draft(operation)
        reviewed = _reviewed_candidate(candidate, operation, draft)
        try:
            next_revision = self.candidates.write(
                _CANDIDATE_COLLECTION,
                operation.evidence.candidate_id,
                reviewed,
                expected_revision=operation.evidence.candidate_revision,
            )
        except ObjectStoreRevisionError as exc:
            raise ProjectSkillReviewStagingServiceConflict("candidate review CAS conflicted") from exc
        if next_revision != operation.evidence.candidate_revision + 1:
            raise ProjectSkillReviewStagingServiceConflict("candidate review revision drifted")

    def _candidate_is_reviewed(self, operation: ProjectSkillReviewStagingOperation) -> bool:
        candidate = self._candidate(operation.evidence.candidate_id)
        return candidate.get("status") == "promoted" and _candidate_revision(
            self.candidates, operation.evidence.candidate_id
        ) == operation.evidence.candidate_revision + 1

    def _assert_candidate_reviewed(self, operation: ProjectSkillReviewStagingOperation) -> None:
        candidate = self._candidate(operation.evidence.candidate_id)
        candidate_revision = _candidate_revision(self.candidates, operation.evidence.candidate_id)
        if candidate_revision != operation.evidence.candidate_revision + 1:
            raise ProjectSkillReviewStagingServiceConflict("reviewed candidate revision drifted")
        draft = self._validated_draft(operation)
        if candidate != _reviewed_candidate_base(candidate, operation, draft):
            raise ProjectSkillReviewStagingServiceConflict("reviewed candidate evidence drifted")

    def _assert_staged_draft(self, operation: ProjectSkillReviewStagingOperation) -> None:
        staged = self.records.read(_STAGING_COLLECTION, operation.evidence.draft_id)
        if staged is None or dict(staged.payload) != self._validated_draft(operation):
            raise ProjectSkillReviewStagingServiceConflict("staging draft is missing or drifted")

    def _assert_current_target(self, operation: ProjectSkillReviewStagingOperation) -> None:
        _assert_target(operation, self._current_target(operation.evidence.project_id))

    def _current_target(self, project_id: str) -> ProjectSkillPublicationTarget:
        target, _current = self._current_context(project_id)
        return target

    def _current_context(
        self, project_id: str
    ) -> tuple[ProjectSkillPublicationTarget, dict[str, object] | None]:
        with self.records.begin() as uow:
            target = _current_target_in_uow(uow, project_id)
            current = uow.read(_SKILL_COLLECTION, target.skill_id)
            current_payload = dict(current.payload) if current is not None else None
        return target, current_payload

    def _pending_candidate(self, candidate_id: str) -> tuple[dict[str, object], int]:
        candidate = self._candidate(candidate_id)
        if candidate.get("status") != "pending_review":
            raise ProjectSkillReviewStagingServiceConflict("candidate must be pending_review")
        if candidate.get("target_layer") != "project_skill":
            raise ProjectSkillReviewStagingServiceConflict("candidate target_layer must be project_skill")
        review = candidate.get("review")
        if not isinstance(review, Mapping) or review.get("requires_user_confirmation") is not True:
            raise ProjectSkillReviewStagingServiceConflict("candidate must require user confirmation")
        if review.get("auto_promote_allowed") is not False:
            raise ProjectSkillReviewStagingServiceConflict("candidate cannot auto-promote")
        return candidate, _candidate_revision(self.candidates, candidate_id)

    def _assert_pending_candidate(
        self, operation: ProjectSkillReviewStagingOperation
    ) -> dict[str, object]:
        candidate, candidate_revision = self._pending_candidate(operation.evidence.candidate_id)
        _assert_pending_evidence(operation, candidate, candidate_revision)
        return candidate

    def _candidate(self, candidate_id: str) -> dict[str, object]:
        candidate = self.candidates.read(_CANDIDATE_COLLECTION, candidate_id)
        if candidate is None:
            raise ProjectSkillReviewStagingServiceError("memory candidate does not exist")
        payload = _validated_candidate(candidate)
        if _required_string(payload, "id") != candidate_id:
            raise ProjectSkillReviewStagingServiceConflict("candidate identity drifted")
        return payload

    def _operation_for_promoted_candidate(
        self, candidate: Mapping[str, object]
    ) -> ProjectSkillReviewStagingOperation:
        application = candidate.get("application")
        if not isinstance(application, Mapping):
            raise ProjectSkillReviewStagingServiceConflict("promoted candidate is not a saga replay")
        operation_id = application.get("operation_id")
        if not isinstance(operation_id, str):
            raise ProjectSkillReviewStagingServiceConflict("candidate application operation is invalid")
        operation = self.operations.get(operation_id)
        if operation is None or operation.evidence.candidate_id != candidate.get("id"):
            raise ProjectSkillReviewStagingServiceConflict("candidate application operation drifted")
        return operation

    def _assert_replay_request(
        self,
        operation: ProjectSkillReviewStagingOperation,
        review_reason: str,
        reviewed_at: str,
    ) -> None:
        draft = self._validated_draft(operation)
        if draft.get("review_reason") != review_reason.strip() or draft.get("reviewed_at") != reviewed_at:
            raise ProjectSkillReviewStagingServiceConflict("review replay request drifted")

    @staticmethod
    def _validated_draft(operation: ProjectSkillReviewStagingOperation) -> dict[str, object]:
        try:
            draft = validate_project_skill_publication_draft(operation.draft)
        except ProjectSkillPublicationDraftError as exc:
            raise ProjectSkillReviewStagingServiceConflict(str(exc)) from exc
        if draft.get("id") != operation.evidence.draft_id or draft.get("draft_digest") != operation.evidence.draft_sha256:
            raise ProjectSkillReviewStagingServiceConflict("publication draft evidence drifted")
        return draft


def _current_target_in_uow(uow, project_id: str) -> ProjectSkillPublicationTarget:
    index = uow.read(_INDEX_COLLECTION, project_id)
    canonical_skill_id = f"skill-{project_id}"
    if index is None:
        if uow.read(_SKILL_COLLECTION, canonical_skill_id) is not None:
            raise ProjectSkillReviewStagingServiceConflict("current Project Skill index drifted")
        for record in uow.list(_SKILL_COLLECTION):
            if record.payload.get("project_id") == project_id:
                raise ProjectSkillReviewStagingServiceConflict("current Project Skill identity drifted")
        try:
            return ProjectSkillPublicationTarget.for_create(project_id)
        except ProjectSkillPublicationDraftError as exc:
            raise ProjectSkillReviewStagingServiceError(str(exc)) from exc
    index_payload = dict(index.payload)
    skill_id = _required_string(index_payload, "skill_id")
    skill = uow.read(_SKILL_COLLECTION, skill_id)
    if skill is None:
        raise ProjectSkillReviewStagingServiceConflict("current Project Skill is missing")
    current = dict(skill.payload)
    if current.get("id") != skill_id or current.get("project_id") != project_id:
        raise ProjectSkillReviewStagingServiceConflict("current Project Skill identity drifted")
    revision = current.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ProjectSkillReviewStagingServiceConflict("current Project Skill revision is invalid")
    try:
        return ProjectSkillPublicationTarget.for_update(
            project_id=project_id,
            skill_id=skill_id,
            expected_revision=revision,
        )
    except ProjectSkillPublicationDraftError as exc:
        raise ProjectSkillReviewStagingServiceError(str(exc)) from exc


def _assert_target(
    operation: ProjectSkillReviewStagingOperation, target: ProjectSkillPublicationTarget
) -> None:
    evidence = operation.evidence
    if (
        target.project_id != evidence.project_id
        or target.skill_id != evidence.skill_id
        or target.expected_revision != evidence.expected_skill_revision
    ):
        raise ProjectSkillReviewStagingServiceConflict("current Project Skill target drifted")


def _assert_pending_evidence(
    operation: ProjectSkillReviewStagingOperation,
    candidate: Mapping[str, object],
    candidate_revision: int,
) -> None:
    evidence = operation.evidence
    if candidate_revision != evidence.candidate_revision or _digest(candidate) != evidence.candidate_sha256:
        raise ProjectSkillReviewStagingServiceConflict("pending candidate evidence drifted")


def _reviewed_candidate(
    candidate: Mapping[str, object],
    operation: ProjectSkillReviewStagingOperation,
    draft: Mapping[str, object],
) -> dict[str, object]:
    payload = dict(candidate)
    review = payload.get("review")
    if not isinstance(review, Mapping):
        raise ProjectSkillReviewStagingServiceConflict("candidate review is invalid")
    payload["status"] = "promoted"
    payload["review"] = {
        **dict(review),
        "requires_user_confirmation": True,
        "auto_promote_allowed": False,
        "reason": _required_string(draft, "review_reason"),
        "reviewed_by": "user",
        "reviewed_at": _required_string(draft, "reviewed_at"),
    }
    payload["application"] = {
        "operation_id": operation.operation_id,
        "draft_id": operation.evidence.draft_id,
        "staging_authority": operation.evidence.staging_authority,
    }
    payload["updated_at"] = _required_string(draft, "reviewed_at")
    return _validated_candidate(payload)


def _reviewed_candidate_base(
    candidate: Mapping[str, object],
    operation: ProjectSkillReviewStagingOperation,
    draft: Mapping[str, object],
) -> dict[str, object]:
    application = candidate.get("application")
    review = candidate.get("review")
    expected_application = {
        "operation_id": operation.operation_id,
        "draft_id": operation.evidence.draft_id,
        "staging_authority": operation.evidence.staging_authority,
    }
    if not isinstance(application, Mapping) or dict(application) != expected_application:
        raise ProjectSkillReviewStagingServiceConflict("candidate application evidence drifted")
    if not isinstance(review, Mapping):
        raise ProjectSkillReviewStagingServiceConflict("candidate review evidence drifted")
    if (
        candidate.get("status") != "promoted"
        or review.get("reviewed_by") != "user"
        or review.get("reviewed_at") != draft.get("reviewed_at")
        or review.get("reason") != draft.get("review_reason")
        or candidate.get("updated_at") != draft.get("reviewed_at")
    ):
        raise ProjectSkillReviewStagingServiceConflict("candidate review evidence drifted")
    return dict(candidate)


def _candidate_revision(candidates: JsonObjectStore, candidate_id: str) -> int:
    revision = candidates.revision(_CANDIDATE_COLLECTION, candidate_id)
    if revision < 1:
        raise ProjectSkillReviewStagingServiceConflict("candidate revision is missing")
    return revision


def _operation_id(evidence: ProjectSkillReviewStagingEvidence) -> str:
    material = "\n".join(
        (evidence.namespace_id, evidence.candidate_id, str(evidence.candidate_revision))
    )
    return f"review-stage-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _digest(payload: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ProjectSkillReviewStagingServiceError("candidate payload is not JSON serializable") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _source_refs(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    refs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return []
        source_id, locator = item.get("source_id"), item.get("locator")
        if not isinstance(source_id, str) or not source_id or not isinstance(locator, str) or not locator:
            return []
        refs.append({"source_id": source_id, "locator": locator})
    return refs


def _validated_candidate(value: Mapping[str, object]) -> dict[str, object]:
    """Validate the candidate evidence this boundary consumes and rewrites.

    Project Skill core cannot import ``memory_core``. The shared schema remains
    the wire contract; this narrow validation fails closed on every candidate
    field the saga relies on, including the application evidence it writes.
    """

    payload = dict(value)
    if payload.get("schema_version") != "1.0.0":
        raise ProjectSkillReviewStagingServiceError("candidate schema_version is invalid")
    _required_string(payload, "id")
    _required_string(payload, "project_id")
    if payload.get("target_layer") not in {
        "atom",
        "scenario",
        "persona",
        "series_memory",
        "project_skill",
    }:
        raise ProjectSkillReviewStagingServiceError("candidate target_layer is invalid")
    if payload.get("candidate_type") not in {
        "answer_fact",
        "answer_decision",
        "answer_action",
        "answer_summary",
        "document_takeaway",
        "other",
    }:
        raise ProjectSkillReviewStagingServiceError("candidate type is invalid")
    if payload.get("status") not in {"pending_review", "rejected", "promoted", "withdrawn"}:
        raise ProjectSkillReviewStagingServiceError("candidate status is invalid")
    _required_string(payload, "proposed_content")
    if not _source_refs(payload.get("source_refs")):
        raise ProjectSkillReviewStagingServiceError("candidate source refs are invalid")
    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ProjectSkillReviewStagingServiceError("candidate provenance is invalid")
    input_refs = provenance.get("input_refs")
    if not isinstance(input_refs, Sequence) or isinstance(input_refs, (str, bytes)) or not input_refs:
        raise ProjectSkillReviewStagingServiceError("candidate input refs are invalid")
    review = payload.get("review")
    if not isinstance(review, Mapping):
        raise ProjectSkillReviewStagingServiceError("candidate review is invalid")
    if payload["status"] == "pending_review":
        if review.get("requires_user_confirmation") is not True or review.get("auto_promote_allowed") is not False:
            raise ProjectSkillReviewStagingServiceError("pending candidate review policy is invalid")
        if review.get("reviewed_by") is not None or review.get("reviewed_at") is not None:
            raise ProjectSkillReviewStagingServiceError("pending candidate cannot be reviewed")
    if payload["status"] in {"rejected", "promoted", "withdrawn"}:
        if review.get("reviewed_by") not in {"user", "system"} or not isinstance(
            review.get("reviewed_at"), str
        ):
            raise ProjectSkillReviewStagingServiceError("reviewed candidate evidence is invalid")
    if "application" in payload:
        if payload["status"] != "promoted":
            raise ProjectSkillReviewStagingServiceError(
                "candidate application requires promoted status"
            )
        application = payload["application"]
        if not isinstance(application, Mapping) or set(application) != {
            "operation_id",
            "draft_id",
            "staging_authority",
        }:
            raise ProjectSkillReviewStagingServiceError("candidate application is invalid")
        if application.get("staging_authority") != _STAGING_AUTHORITY:
            raise ProjectSkillReviewStagingServiceError("candidate application authority is invalid")
        for key in ("operation_id", "draft_id"):
            if not isinstance(application.get(key), str) or not application[key].strip():
                raise ProjectSkillReviewStagingServiceError("candidate application is invalid")
    _required_string(payload, "created_at")
    _required_string(payload, "updated_at")
    return payload


def _required_string(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProjectSkillReviewStagingServiceError(f"{key} is required")
    return value
