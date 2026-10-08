"""Durable operation state for generic Memory review-to-staging work."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from .sqlite_uow import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


_COLLECTION = "memory_publication_review_staging_operations"
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_LAYERS = {"atom", "scenario", "series_memory"}
_NEXT = {
    "prepared": "sqlite_staging_created",
    "sqlite_staging_created": "candidate_reviewed",
    "candidate_reviewed": "finalized",
}


class MemoryPublicationReviewStagingSagaError(ValueError):
    pass


class MemoryPublicationReviewStagingSagaConflict(MemoryPublicationReviewStagingSagaError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryPublicationReviewStagingEvidence:
    namespace_id: str
    candidate_id: str
    candidate_revision: int
    candidate_sha256: str
    layer: str
    draft_id: str
    draft_sha256: str
    context_id: str
    context_sha256: str
    candidate_authority: str = "json:object-store-v1"
    staging_authority: str = "sqlite:structured-records-v1"


@dataclass(frozen=True, slots=True)
class MemoryPublicationReviewStagingOperation:
    operation_id: str
    state: str
    revision: int
    evidence: MemoryPublicationReviewStagingEvidence
    candidate: Mapping[str, object]
    draft: Mapping[str, object]
    context: Mapping[str, object]
    created_at: str
    updated_at: str


class SQLiteMemoryPublicationReviewStagingSagaStore:
    """CAS state machine that stores all restart inputs with the operation."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        self._records = records

    @property
    def records(self) -> SQLiteStructuredRecordStore:
        return self._records

    def prepare(
        self,
        *,
        operation_id: str,
        evidence: MemoryPublicationReviewStagingEvidence | Mapping[str, object],
        candidate: Mapping[str, object],
        draft: Mapping[str, object],
        context: Mapping[str, object],
        now: str | None = None,
    ) -> MemoryPublicationReviewStagingOperation:
        evidence = _as_evidence(evidence)
        _segment("operation_id", operation_id)
        _evidence(evidence)
        normalized_candidate = _payload_mapping(candidate, evidence.candidate_sha256, "candidate")
        normalized_draft = _payload_mapping(draft, evidence.draft_sha256, "draft")
        normalized_context = _payload_mapping(context, evidence.context_sha256, "context")
        if normalized_candidate.get("id") != evidence.candidate_id:
            raise MemoryPublicationReviewStagingSagaError("review staging candidate identity is invalid")
        if normalized_draft.get("id") != evidence.draft_id:
            raise MemoryPublicationReviewStagingSagaError("review staging draft identity is invalid")
        if normalized_context.get("id") != evidence.context_id:
            raise MemoryPublicationReviewStagingSagaError("review staging context identity is invalid")
        current = self.get(operation_id)
        if current is not None:
            if (
                current.evidence != evidence
                or current.candidate != normalized_candidate
                or current.draft != normalized_draft
                or current.context != normalized_context
            ):
                raise MemoryPublicationReviewStagingSagaConflict("review staging evidence drifted")
            return current
        timestamp = now or _now()
        try:
            with self._records.begin() as uow:
                record = uow.put(
                    _COLLECTION,
                    operation_id,
                    _operation_payload(
                        operation_id,
                        "prepared",
                        evidence,
                        normalized_candidate,
                        normalized_draft,
                        normalized_context,
                        timestamp,
                        timestamp,
                    ),
                    expected_revision=0,
                )
                uow.commit()
        except SQLiteUnitOfWorkConflict as exc:
            current = self.get(operation_id)
            if current is not None and current.evidence == evidence:
                return current
            raise MemoryPublicationReviewStagingSagaConflict("review staging prepare conflicted") from exc
        return _operation(record.payload, record.revision)

    def get(self, operation_id: str) -> MemoryPublicationReviewStagingOperation | None:
        _segment("operation_id", operation_id)
        record = self._records.read(_COLLECTION, operation_id)
        return _operation(record.payload, record.revision) if record is not None else None

    def mark_sqlite_staging_created(
        self, operation_id: str, *, expected_revision: int, now: str | None = None
    ) -> MemoryPublicationReviewStagingOperation:
        return self._transition(operation_id, expected_revision, "sqlite_staging_created", now)

    def mark_candidate_reviewed(
        self, operation_id: str, *, expected_revision: int, now: str | None = None
    ) -> MemoryPublicationReviewStagingOperation:
        return self._transition(operation_id, expected_revision, "candidate_reviewed", now)

    def finalize(
        self, operation_id: str, *, expected_revision: int, now: str | None = None
    ) -> MemoryPublicationReviewStagingOperation:
        return self._transition(operation_id, expected_revision, "finalized", now)

    def list_recoverable(self) -> tuple[MemoryPublicationReviewStagingOperation, ...]:
        return tuple(
            operation
            for record in self._records.list(_COLLECTION)
            if (operation := _operation(record.payload, record.revision)).state != "finalized"
        )

    def operation_id(
        self, evidence: MemoryPublicationReviewStagingEvidence | Mapping[str, object]
    ) -> str:
        """Return the stable id for a product-core evidence mapping."""

        return memory_publication_review_staging_operation_id(evidence)

    def _transition(
        self, operation_id: str, expected_revision: int, state: str, now: str | None
    ) -> MemoryPublicationReviewStagingOperation:
        current = self.get(operation_id)
        if current is None:
            raise MemoryPublicationReviewStagingSagaError("review staging operation does not exist")
        if current.revision != expected_revision:
            raise MemoryPublicationReviewStagingSagaConflict("review staging revision conflicted")
        if _NEXT.get(current.state) != state:
            raise MemoryPublicationReviewStagingSagaConflict("illegal review staging transition")
        try:
            with self._records.begin() as uow:
                record = uow.put(
                    _COLLECTION,
                    operation_id,
                    _operation_payload(
                        operation_id,
                        state,
                        current.evidence,
                        current.candidate,
                        current.draft,
                        current.context,
                        current.created_at,
                        now or _now(),
                    ),
                    expected_revision=expected_revision,
                )
                uow.commit()
        except SQLiteUnitOfWorkConflict as exc:
            raise MemoryPublicationReviewStagingSagaConflict("review staging transition conflicted") from exc
        return _operation(record.payload, record.revision)


def memory_publication_review_staging_operation_id(
    evidence: MemoryPublicationReviewStagingEvidence | Mapping[str, object],
) -> str:
    evidence = _as_evidence(evidence)
    _evidence(evidence)
    material = "\n".join(
        (evidence.namespace_id, evidence.candidate_id, str(evidence.candidate_revision), evidence.layer)
    )
    return f"memory-review-stage-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _operation_payload(operation_id, state, evidence, candidate, draft, context, created, updated):
    return {
        "operation_id": operation_id,
        "state": state,
        "namespace_id": evidence.namespace_id,
        "candidate_id": evidence.candidate_id,
        "candidate_revision": evidence.candidate_revision,
        "candidate_sha256": evidence.candidate_sha256,
        "layer": evidence.layer,
        "draft_id": evidence.draft_id,
        "draft_sha256": evidence.draft_sha256,
        "context_id": evidence.context_id,
        "context_sha256": evidence.context_sha256,
        "candidate": _payload_mapping(candidate, evidence.candidate_sha256, "candidate"),
        "draft": _payload_mapping(draft, evidence.draft_sha256, "draft"),
        "context": _payload_mapping(context, evidence.context_sha256, "context"),
        "candidate_authority": evidence.candidate_authority,
        "staging_authority": evidence.staging_authority,
        "created_at": created,
        "updated_at": updated,
    }


def _operation(payload: Mapping[str, object], revision: int) -> MemoryPublicationReviewStagingOperation:
    if not isinstance(payload, Mapping):
        raise MemoryPublicationReviewStagingSagaError("review staging operation is invalid")
    evidence = MemoryPublicationReviewStagingEvidence(
        *[
            payload.get(key)
            for key in (
                "namespace_id",
                "candidate_id",
                "candidate_revision",
                "candidate_sha256",
                "layer",
                "draft_id",
                "draft_sha256",
                "context_id",
                "context_sha256",
                "candidate_authority",
                "staging_authority",
            )
        ]
    )
    operation_id = payload.get("operation_id")
    _segment("operation_id", operation_id)
    _evidence(evidence)
    state = payload.get("state")
    if state not in {*_NEXT, "finalized"}:
        raise MemoryPublicationReviewStagingSagaError("review staging state is invalid")
    candidate = _payload_mapping(payload.get("candidate"), evidence.candidate_sha256, "candidate")
    draft = _payload_mapping(payload.get("draft"), evidence.draft_sha256, "draft")
    context = _payload_mapping(payload.get("context"), evidence.context_sha256, "context")
    if (
        candidate.get("id") != evidence.candidate_id
        or draft.get("id") != evidence.draft_id
        or context.get("id") != evidence.context_id
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(payload.get("created_at"), str)
        or not isinstance(payload.get("updated_at"), str)
    ):
        raise MemoryPublicationReviewStagingSagaError("review staging operation is invalid")
    return MemoryPublicationReviewStagingOperation(
        operation_id,
        state,
        revision,
        evidence,
        candidate,
        draft,
        context,
        payload["created_at"],
        payload["updated_at"],
    )


def _evidence(evidence: MemoryPublicationReviewStagingEvidence) -> None:
    if not isinstance(evidence, MemoryPublicationReviewStagingEvidence):
        raise MemoryPublicationReviewStagingSagaError("review staging evidence is invalid")
    for value in (
        evidence.namespace_id,
        evidence.candidate_id,
        evidence.draft_id,
        evidence.context_id,
    ):
        _segment("evidence", value)
    if evidence.layer not in _LAYERS:
        raise MemoryPublicationReviewStagingSagaError("review staging layer is invalid")
    if not isinstance(evidence.candidate_revision, int) or isinstance(evidence.candidate_revision, bool) or evidence.candidate_revision < 1:
        raise MemoryPublicationReviewStagingSagaError("review staging revision is invalid")
    for digest in (evidence.candidate_sha256, evidence.draft_sha256, evidence.context_sha256):
        if not isinstance(digest, str) or not _SHA.fullmatch(digest):
            raise MemoryPublicationReviewStagingSagaError("review staging digest is invalid")
    if (
        evidence.candidate_authority != "json:object-store-v1"
        or evidence.staging_authority != "sqlite:structured-records-v1"
    ):
        raise MemoryPublicationReviewStagingSagaError("review staging authority is invalid")


def _as_evidence(
    value: MemoryPublicationReviewStagingEvidence | Mapping[str, object],
) -> MemoryPublicationReviewStagingEvidence:
    if isinstance(value, MemoryPublicationReviewStagingEvidence):
        return value
    if not isinstance(value, Mapping):
        raise MemoryPublicationReviewStagingSagaError("review staging evidence is invalid")
    return MemoryPublicationReviewStagingEvidence(
        *[
            value.get(key)
            for key in (
                "namespace_id",
                "candidate_id",
                "candidate_revision",
                "candidate_sha256",
                "layer",
                "draft_id",
                "draft_sha256",
                "context_id",
                "context_sha256",
                "candidate_authority",
                "staging_authority",
            )
        ]
    )


def _payload_mapping(value: object, expected_digest: str, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise MemoryPublicationReviewStagingSagaError(f"review staging {label} is invalid")
    try:
        normalized = json.loads(json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise MemoryPublicationReviewStagingSagaError(f"review staging {label} is invalid") from exc
    if not isinstance(normalized, dict) or _digest(normalized) != expected_digest:
        raise MemoryPublicationReviewStagingSagaError(f"review staging {label} evidence drifted")
    return normalized


def _digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _segment(label: str, value: object) -> None:
    if not isinstance(value, str) or not _SAFE.fullmatch(value):
        raise MemoryPublicationReviewStagingSagaError(f"{label} is invalid")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
