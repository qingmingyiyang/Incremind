"""Durable cross-authority review-to-staging operation state."""

from __future__ import annotations

import re
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from .sqlite_uow import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict

_COLLECTION = "project_skill_review_staging_operations"
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_NEXT = {
    "prepared": "sqlite_draft_staged",
    "sqlite_draft_staged": "candidate_reviewed",
    "candidate_reviewed": "finalized",
}


class ProjectSkillReviewStagingSagaError(ValueError):
    pass


class ProjectSkillReviewStagingSagaConflict(ProjectSkillReviewStagingSagaError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectSkillReviewStagingEvidence:
    namespace_id: str
    candidate_id: str
    candidate_revision: int
    candidate_sha256: str
    project_id: str
    skill_id: str
    expected_skill_revision: int
    draft_id: str
    draft_sha256: str
    candidate_authority: str = "json:object-store-v1"
    staging_authority: str = "sqlite:structured-records-v1"


@dataclass(frozen=True, slots=True)
class ProjectSkillReviewStagingOperation:
    operation_id: str
    state: str
    revision: int
    evidence: ProjectSkillReviewStagingEvidence
    draft: Mapping[str, object]
    created_at: str
    updated_at: str
    compensation_code: str | None = None


class SQLiteProjectSkillReviewStagingSagaStore:
    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        self._records = records

    @property
    def records(self) -> SQLiteStructuredRecordStore:
        """Return the enlisted records authority for composition-time checks."""

        return self._records

    def prepare(
        self,
        *,
        operation_id: str,
        evidence: ProjectSkillReviewStagingEvidence,
        draft: Mapping[str, object],
        now: str | None = None,
    ) -> ProjectSkillReviewStagingOperation:
        _segment("operation_id", operation_id)
        _evidence(evidence)
        normalized_draft = _draft(evidence, draft)
        current = self.get(operation_id)
        if current:
            if current.evidence != evidence or current.draft != normalized_draft:
                raise ProjectSkillReviewStagingSagaConflict("review staging evidence drifted")
            return current
        timestamp = now or _now()
        try:
            with self._records.begin() as uow:
                record = uow.put(
                    _COLLECTION,
                    operation_id,
                    _payload(operation_id, "prepared", evidence, normalized_draft, timestamp, timestamp),
                    expected_revision=0,
                )
                uow.commit()
        except SQLiteUnitOfWorkConflict as exc:
            current = self.get(operation_id)
            if current and current.evidence == evidence and current.draft == normalized_draft:
                return current
            raise ProjectSkillReviewStagingSagaConflict("review staging prepare conflicted") from exc
        return _operation(record.payload, record.revision)

    def get(self, operation_id: str) -> ProjectSkillReviewStagingOperation | None:
        _segment("operation_id", operation_id)
        record = self._records.read(_COLLECTION, operation_id)
        return _operation(record.payload, record.revision) if record else None

    def mark_sqlite_draft_staged(self, operation_id: str, *, expected_revision: int, now: str | None = None):
        return self._transition(operation_id, expected_revision, "sqlite_draft_staged", now)

    def mark_candidate_reviewed(self, operation_id: str, *, expected_revision: int, now: str | None = None):
        return self._transition(operation_id, expected_revision, "candidate_reviewed", now)

    def finalize(self, operation_id: str, *, expected_revision: int, now: str | None = None):
        return self._transition(operation_id, expected_revision, "finalized", now)

    def mark_compensated(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        compensation_code: str,
        now: str | None = None,
    ) -> ProjectSkillReviewStagingOperation:
        _segment("compensation_code", compensation_code)
        current = self.get(operation_id)
        if current is None:
            raise ProjectSkillReviewStagingSagaError("review staging operation does not exist")
        if current.revision != expected_revision:
            raise ProjectSkillReviewStagingSagaConflict("review staging revision conflicted")
        if current.state not in {"prepared", "sqlite_draft_staged"}:
            raise ProjectSkillReviewStagingSagaConflict("review staging operation cannot be compensated")
        try:
            with self._records.begin() as uow:
                record = uow.put(
                    _COLLECTION,
                    operation_id,
                    _payload(
                        operation_id,
                        "compensated",
                        current.evidence,
                        current.draft,
                        current.created_at,
                        now or _now(),
                        compensation_code=compensation_code,
                    ),
                    expected_revision=expected_revision,
                )
                uow.commit()
        except SQLiteUnitOfWorkConflict as exc:
            raise ProjectSkillReviewStagingSagaConflict("review staging compensation conflicted") from exc
        return _operation(record.payload, record.revision)

    def list_recoverable(self):
        return tuple(
            op
            for record in self._records.list(_COLLECTION)
            if (op := _operation(record.payload, record.revision)).state not in {"finalized", "compensated"}
        )

    def _transition(self, operation_id: str, expected_revision: int, state: str, now: str | None):
        current = self.get(operation_id)
        if current is None:
            raise ProjectSkillReviewStagingSagaError("review staging operation does not exist")
        if current.revision != expected_revision:
            raise ProjectSkillReviewStagingSagaConflict("review staging revision conflicted")
        if _NEXT.get(current.state) != state:
            raise ProjectSkillReviewStagingSagaConflict("illegal review staging transition")
        try:
            with self._records.begin() as uow:
                record = uow.put(
                    _COLLECTION,
                    operation_id,
                    _payload(
                        operation_id,
                        state,
                        current.evidence,
                        current.draft,
                        current.created_at,
                        now or _now(),
                        compensation_code=current.compensation_code,
                    ),
                    expected_revision=expected_revision,
                )
                uow.commit()
        except SQLiteUnitOfWorkConflict as exc:
            raise ProjectSkillReviewStagingSagaConflict("review staging transition conflicted") from exc
        return _operation(record.payload, record.revision)


def _payload(operation_id, state, evidence, draft, created, updated, *, compensation_code=None):
    return {
        "operation_id": operation_id,
        "state": state,
        "namespace_id": evidence.namespace_id,
        "candidate_id": evidence.candidate_id,
        "candidate_revision": evidence.candidate_revision,
        "candidate_sha256": evidence.candidate_sha256,
        "project_id": evidence.project_id,
        "skill_id": evidence.skill_id,
        "expected_skill_revision": evidence.expected_skill_revision,
        "draft_id": evidence.draft_id,
        "draft_sha256": evidence.draft_sha256,
        "draft": _draft(evidence, draft),
        "candidate_authority": evidence.candidate_authority,
        "staging_authority": evidence.staging_authority,
        "created_at": created,
        "updated_at": updated,
        "compensation_code": compensation_code,
    }


def _operation(p, r):
    e = ProjectSkillReviewStagingEvidence(
        *[
            p.get(k)
            for k in (
                "namespace_id",
                "candidate_id",
                "candidate_revision",
                "candidate_sha256",
                "project_id",
                "skill_id",
                "expected_skill_revision",
                "draft_id",
                "draft_sha256",
                "candidate_authority",
                "staging_authority",
            )
        ]
    )
    _segment("operation_id", p.get("operation_id"))
    _evidence(e)
    draft = _draft(e, p.get("draft"))
    if p.get("state") not in {*_NEXT, "finalized", "compensated"}:
        raise ProjectSkillReviewStagingSagaError("review staging state is invalid")
    compensation_code = p.get("compensation_code")
    if p.get("state") == "compensated":
        _segment("compensation_code", compensation_code)
    elif compensation_code is not None:
        raise ProjectSkillReviewStagingSagaError("non-terminal review staging operation cannot have compensation")
    if (
        not isinstance(r, int)
        or r < 1
        or not isinstance(p.get("created_at"), str)
        or not isinstance(p.get("updated_at"), str)
    ):
        raise ProjectSkillReviewStagingSagaError("review staging operation is invalid")
    return ProjectSkillReviewStagingOperation(
        p["operation_id"], p["state"], r, e, draft, p["created_at"], p["updated_at"], compensation_code
    )


def _draft(evidence: ProjectSkillReviewStagingEvidence, value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ProjectSkillReviewStagingSagaError("review staging draft is invalid")
    try:
        normalized = json.loads(json.dumps(dict(value), ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ProjectSkillReviewStagingSagaError("review staging draft is not JSON serializable") from exc
    if not isinstance(normalized, dict):
        raise ProjectSkillReviewStagingSagaError("review staging draft is invalid")
    if normalized.get("id") != evidence.draft_id or normalized.get("draft_digest") != evidence.draft_sha256:
        raise ProjectSkillReviewStagingSagaError("review staging draft evidence drifted")
    return normalized


def _evidence(e):
    if not isinstance(e, ProjectSkillReviewStagingEvidence):
        raise ProjectSkillReviewStagingSagaError("review staging evidence is invalid")
    for v in (e.namespace_id, e.candidate_id, e.project_id, e.skill_id, e.draft_id):
        _segment("evidence", v)
    for v in (e.candidate_sha256, e.draft_sha256):
        if not isinstance(v, str) or not _SHA.fullmatch(v):
            raise ProjectSkillReviewStagingSagaError("review staging digest is invalid")
    if not all(
        isinstance(v, int) and not isinstance(v, bool) and v >= 0
        for v in (e.candidate_revision, e.expected_skill_revision)
    ):
        raise ProjectSkillReviewStagingSagaError("review staging revision is invalid")
    if e.candidate_authority != "json:object-store-v1" or e.staging_authority != "sqlite:structured-records-v1":
        raise ProjectSkillReviewStagingSagaError("review staging authority is invalid")


def _segment(label, v):
    if not isinstance(v, str) or not _SAFE.fullmatch(v):
        raise ProjectSkillReviewStagingSagaError(f"{label} is invalid")


def _now():
    return datetime.now(timezone.utc).isoformat()
