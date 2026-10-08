"""CAS-safe generic Memory candidate review into SQLite staging."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from core.memory_core import (
    ManualPublicationContractError,
    STAGING_PUBLICATION_CONTEXT_COLLECTION,
    build_manual_publication_context,
    validate_manual_publication_context,
)
from .memory_candidate_review import (
    MemoryCandidateReviewError,
    build_memory_staging_draft,
)


_CANDIDATE_COLLECTION = "memory_candidates"
_STAGING = {
    "atom": "staging_atoms",
    "scenario": "staging_scenarios",
    "series_memory": "staging_series_memory",
}
_CURRENT = {
    "atom": "memory_atoms",
    "scenario": "memory_scenarios",
    "series_memory": "memory_series_memory",
}


class MemoryPublicationReviewCandidateStorePort(Protocol):
    namespace_id: str

    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None: ...

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int,
    ) -> int: ...

    def revision(self, collection: str, object_id: str) -> int: ...


class MemoryPublicationReviewRecordsPort(Protocol):
    def begin(self) -> Any: ...

    def read(self, collection: str, object_id: str) -> Any: ...


class MemoryPublicationReviewOperationStorePort(Protocol):
    records: object

    def operation_id(self, evidence: Mapping[str, object]) -> str: ...

    def prepare(
        self,
        *,
        operation_id: str,
        evidence: Mapping[str, object],
        candidate: Mapping[str, object],
        draft: Mapping[str, object],
        context: Mapping[str, object],
        now: str | None = None,
    ) -> Any: ...

    def get(self, operation_id: str) -> Any: ...

    def mark_sqlite_staging_created(self, operation_id: str, *, expected_revision: int) -> Any: ...

    def mark_candidate_reviewed(self, operation_id: str, *, expected_revision: int) -> Any: ...

    def finalize(self, operation_id: str, *, expected_revision: int) -> Any: ...


class MemoryPublicationReviewStagingServiceError(ValueError):
    """Raised when generic review-to-staging evidence is unsafe to advance."""


class MemoryPublicationReviewStagingServiceConflict(
    MemoryPublicationReviewStagingServiceError
):
    """Raised for CAS, payload, draft/context or current-projection drift."""


@dataclass(slots=True)
class MemoryPublicationReviewStagingSagaService:
    """Coordinate JSON candidate CAS with a single SQLite staging authority.

    This is deliberately not a publication service.  It creates only a
    reviewable SQLite draft plus its immutable manual-review context.  A later
    generic publication adapter owns current projections and Trust Audit.
    """

    candidates: MemoryPublicationReviewCandidateStorePort
    records: MemoryPublicationReviewRecordsPort
    operations: MemoryPublicationReviewOperationStorePort

    def __post_init__(self) -> None:
        if not isinstance(self.candidates.namespace_id, str) or not self.candidates.namespace_id:
            raise MemoryPublicationReviewStagingServiceError("candidate namespace is invalid")
        if self.operations.records is not self.records:
            raise MemoryPublicationReviewStagingServiceError(
                "review staging operations must share the SQLite records authority"
            )

    def prepare_review(
        self,
        candidate_id: str,
        *,
        review_reason: str,
        reviewed_at: str,
        atom_type: str | None = None,
        tags: Sequence[str] = (),
        confidence: float = 0.7,
        series_id: str | None = None,
        scenario_ids: Sequence[str] = (),
        atom_ids: Sequence[str] = (),
    ) -> Any:
        """Persist deterministic restart inputs without advancing side effects."""

        candidate, candidate_revision = self._pending_candidate(candidate_id)
        layer = _layer(candidate.get("target_layer"))
        external_update = candidate.get("external_series_update")
        if external_update is not None:
            if (
                layer != "series_memory"
                or not isinstance(external_update, Mapping)
                or external_update.get("authority_identity") != "sqlite:structured-records-v1"
            ):
                raise MemoryPublicationReviewStagingServiceConflict(
                    "External Series JSON-base candidate cannot enter SQLite staging"
                )
        hierarchy_update = candidate.get("hierarchy_update")
        if hierarchy_update is not None and (
            layer not in {"scenario", "series_memory"}
            or not isinstance(hierarchy_update, Mapping)
            or hierarchy_update.get("authority_identity") != "sqlite:structured-records-v1"
        ):
            raise MemoryPublicationReviewStagingServiceConflict(
                "Hierarchy update candidate cannot enter SQLite staging"
            )
        source_refs = _source_refs(candidate.get("source_refs"))
        if not source_refs:
            raise MemoryPublicationReviewStagingServiceError("candidate source refs are invalid")
        try:
            draft = build_memory_staging_draft(
                candidate,
                target_layer=layer,
                source_refs=source_refs,
                timestamp=reviewed_at,
                atom_type=atom_type,
                tags=tags,
                confidence=confidence,
                series_id=series_id,
                scenario_ids=scenario_ids,
                atom_ids=atom_ids,
            )
            context = build_manual_publication_context(
                namespace_id=self.candidates.namespace_id,
                layer=layer,
                draft_id=_required_string(draft, "id"),
                candidate_id=candidate_id,
                reviewed_at=reviewed_at,
                review_reason=review_reason,
                source_refs=source_refs,
                evidence_refs=_evidence_refs(candidate.get("evidence_refs"), source_refs),
            )
        except (ManualPublicationContractError, MemoryCandidateReviewError) as exc:
            raise MemoryPublicationReviewStagingServiceError(str(exc)) from exc
        evidence = {
            "namespace_id": self.candidates.namespace_id,
            "candidate_id": candidate_id,
            "candidate_revision": candidate_revision,
            "candidate_sha256": _digest(candidate),
            "layer": layer,
            "draft_id": _required_string(draft, "id"),
            "draft_sha256": _digest(draft),
            "context_id": _required_string(context, "id"),
            "context_sha256": _digest(context),
            "candidate_authority": "json:object-store-v1",
            "staging_authority": "sqlite:structured-records-v1",
        }
        return self.operations.prepare(
            operation_id=self.operations.operation_id(evidence),
            evidence=evidence,
            candidate=candidate,
            draft=draft,
            context=context,
            now=reviewed_at,
        )

    def review_to_staging(
        self,
        candidate_id: str,
        *,
        review_reason: str,
        reviewed_at: str,
        atom_type: str | None = None,
        tags: Sequence[str] = (),
        confidence: float = 0.7,
        series_id: str | None = None,
        scenario_ids: Sequence[str] = (),
        atom_ids: Sequence[str] = (),
    ) -> Any:
        candidate = self._candidate(candidate_id)
        if candidate.get("status") == "promoted":
            operation = self._operation_for_promoted_candidate(candidate)
            self._assert_replay_request(operation, review_reason, reviewed_at)
            return self.resume(operation.operation_id)
        operation = self.prepare_review(
            candidate_id,
            review_reason=review_reason,
            reviewed_at=reviewed_at,
            atom_type=atom_type,
            tags=tags,
            confidence=confidence,
            series_id=series_id,
            scenario_ids=scenario_ids,
            atom_ids=atom_ids,
        )
        return self.resume(operation.operation_id)

    def resume(self, operation_id: str) -> Any:
        """Advance at most once through each remaining durable state."""

        operation = self.operations.get(operation_id)
        if operation is None:
            raise MemoryPublicationReviewStagingServiceError("review staging operation does not exist")
        for _ in range(3):
            if operation.state == "prepared":
                self._assert_pending_candidate(operation)
                self._stage(operation)
                operation = self.operations.mark_sqlite_staging_created(
                    operation.operation_id,
                    expected_revision=operation.revision,
                )
                continue
            if operation.state == "sqlite_staging_created":
                if not self._candidate_is_reviewed(operation):
                    self._review_candidate(operation)
                operation = self.operations.mark_candidate_reviewed(
                    operation.operation_id,
                    expected_revision=operation.revision,
                )
                continue
            if operation.state == "candidate_reviewed":
                self._assert_staged(operation)
                self._assert_candidate_reviewed(operation)
                operation = self.operations.finalize(
                    operation.operation_id,
                    expected_revision=operation.revision,
                )
                continue
            if operation.state == "finalized":
                self._assert_staged(operation)
                self._assert_candidate_reviewed(operation)
                return operation
            raise MemoryPublicationReviewStagingServiceError("review staging operation state is invalid")
        if operation.state != "finalized":
            raise MemoryPublicationReviewStagingServiceError("review staging operation did not converge")
        self._assert_staged(operation)
        self._assert_candidate_reviewed(operation)
        return operation

    def _stage(self, operation: Any) -> None:
        self._validated_operation_payloads(operation)
        staging_collection = _STAGING[operation.evidence.layer]
        current_collection = _CURRENT[operation.evidence.layer]
        try:
            with self.records.begin() as uow:
                current = uow.read(current_collection, operation.evidence.draft_id)
                if current is not None and not (
                    _sqlite_external_series_replacement(operation.draft, current)
                    or _sqlite_hierarchy_replacement(operation.draft, current)
                ):
                    raise MemoryPublicationReviewStagingServiceConflict(
                        "current Memory projection already exists"
                    )
                staged = uow.read(staging_collection, operation.evidence.draft_id)
                if staged is None:
                    uow.put(
                        staging_collection,
                        operation.evidence.draft_id,
                        operation.draft,
                        expected_revision=0,
                    )
                elif dict(staged.payload) != dict(operation.draft):
                    raise MemoryPublicationReviewStagingServiceConflict(
                        "SQLite staging draft drifted"
                    )
                context = uow.read(
                    STAGING_PUBLICATION_CONTEXT_COLLECTION,
                    operation.evidence.context_id,
                )
                if context is None:
                    uow.put(
                        STAGING_PUBLICATION_CONTEXT_COLLECTION,
                        operation.evidence.context_id,
                        operation.context,
                        expected_revision=0,
                    )
                elif dict(context.payload) != dict(operation.context):
                    raise MemoryPublicationReviewStagingServiceConflict(
                        "SQLite staging context drifted"
                    )
                uow.commit()
        except MemoryPublicationReviewStagingServiceError:
            raise
        except Exception as exc:
            raise MemoryPublicationReviewStagingServiceConflict(
                "SQLite staging write conflicted"
            ) from exc

    def _review_candidate(self, operation: Any) -> None:
        self._assert_pending_candidate(operation)
        reviewed = _reviewed_candidate(operation)
        try:
            revision = self.candidates.write(
                _CANDIDATE_COLLECTION,
                operation.evidence.candidate_id,
                reviewed,
                expected_revision=operation.evidence.candidate_revision,
            )
        except ValueError as exc:
            raise MemoryPublicationReviewStagingServiceConflict(
                "candidate review CAS conflicted"
            ) from exc
        if revision != operation.evidence.candidate_revision + 1:
            raise MemoryPublicationReviewStagingServiceConflict(
                "candidate review revision drifted"
            )

    def _assert_pending_candidate(
        self, operation: Any
    ) -> dict[str, object]:
        candidate, revision = self._pending_candidate(operation.evidence.candidate_id)
        if (
            revision != operation.evidence.candidate_revision
            or candidate != dict(operation.candidate)
            or _digest(candidate) != operation.evidence.candidate_sha256
        ):
            raise MemoryPublicationReviewStagingServiceConflict(
                "pending candidate evidence drifted"
            )
        return candidate

    def _candidate_is_reviewed(
        self, operation: Any
    ) -> bool:
        candidate = self._candidate(operation.evidence.candidate_id)
        return (
            _candidate_revision(self.candidates, operation.evidence.candidate_id)
            == operation.evidence.candidate_revision + 1
            and candidate == _reviewed_candidate(operation)
        )

    def _assert_candidate_reviewed(
        self, operation: Any
    ) -> None:
        if not self._candidate_is_reviewed(operation):
            raise MemoryPublicationReviewStagingServiceConflict(
                "reviewed candidate evidence drifted"
            )

    def _assert_staged(self, operation: Any) -> None:
        draft, context = self._validated_operation_payloads(operation)
        staged = self.records.read(_STAGING[operation.evidence.layer], operation.evidence.draft_id)
        stored_context = self.records.read(
            STAGING_PUBLICATION_CONTEXT_COLLECTION,
            operation.evidence.context_id,
        )
        if (
            staged is None
            or stored_context is None
            or dict(staged.payload) != draft
            or dict(stored_context.payload) != context
        ):
            raise MemoryPublicationReviewStagingServiceConflict(
                "SQLite staging evidence is missing or drifted"
            )

    def _pending_candidate(self, candidate_id: str) -> tuple[dict[str, object], int]:
        candidate = self._candidate(candidate_id)
        if candidate.get("status") != "pending_review":
            raise MemoryPublicationReviewStagingServiceConflict(
                "candidate must be pending_review"
            )
        _layer(candidate.get("target_layer"))
        review = candidate.get("review")
        if not isinstance(review, Mapping) or review.get("requires_user_confirmation") is not True:
            raise MemoryPublicationReviewStagingServiceConflict(
                "candidate must require user confirmation"
            )
        if review.get("auto_promote_allowed") is not False:
            raise MemoryPublicationReviewStagingServiceConflict(
                "candidate cannot auto-promote"
            )
        return candidate, _candidate_revision(self.candidates, candidate_id)

    def _candidate(self, candidate_id: str) -> dict[str, object]:
        candidate = self.candidates.read(_CANDIDATE_COLLECTION, candidate_id)
        if candidate is None:
            raise MemoryPublicationReviewStagingServiceError("memory candidate does not exist")
        payload = dict(candidate)
        if _required_string(payload, "id") != candidate_id:
            raise MemoryPublicationReviewStagingServiceConflict(
                "candidate identity drifted"
            )
        return payload

    def _operation_for_promoted_candidate(
        self, candidate: Mapping[str, object]
    ) -> Any:
        application = candidate.get("application")
        if not isinstance(application, Mapping):
            raise MemoryPublicationReviewStagingServiceConflict(
                "promoted candidate is not a saga replay"
            )
        operation_id = application.get("operation_id")
        if not isinstance(operation_id, str):
            raise MemoryPublicationReviewStagingServiceConflict(
                "candidate application operation is invalid"
            )
        operation = self.operations.get(operation_id)
        if (
            operation is None
            or operation.evidence.candidate_id != candidate.get("id")
            or application.get("draft_id") != operation.evidence.draft_id
            or application.get("staging_authority") != operation.evidence.staging_authority
        ):
            raise MemoryPublicationReviewStagingServiceConflict(
                "candidate application operation drifted"
            )
        return operation

    @staticmethod
    def _validated_operation_payloads(
        operation: Any,
    ) -> tuple[dict[str, object], dict[str, object]]:
        draft = dict(operation.draft)
        context = dict(operation.context)
        if _digest(draft) != operation.evidence.draft_sha256 or _required_string(draft, "id") != operation.evidence.draft_id:
            raise MemoryPublicationReviewStagingServiceConflict("staging draft evidence drifted")
        try:
            normalized_context = validate_manual_publication_context(
                context,
                namespace_id=operation.evidence.namespace_id,
                layer=operation.evidence.layer,
                draft_id=operation.evidence.draft_id,
            )
        except ManualPublicationContractError as exc:
            raise MemoryPublicationReviewStagingServiceConflict(str(exc)) from exc
        if (
            _digest(normalized_context) != operation.evidence.context_sha256
            or _required_string(normalized_context, "id") != operation.evidence.context_id
        ):
            raise MemoryPublicationReviewStagingServiceConflict("staging context evidence drifted")
        return draft, normalized_context

    @staticmethod
    def _assert_replay_request(
        operation: Any,
        review_reason: str,
        reviewed_at: str,
    ) -> None:
        context = dict(operation.context)
        if (
            context.get("review_reason") != review_reason.strip()
            or context.get("reviewed_at") != reviewed_at
        ):
            raise MemoryPublicationReviewStagingServiceConflict(
                "review replay request drifted"
            )


def _reviewed_candidate(
    operation: Any,
) -> dict[str, object]:
    payload = dict(operation.candidate)
    review = payload.get("review")
    context = dict(operation.context)
    if not isinstance(review, Mapping):
        raise MemoryPublicationReviewStagingServiceConflict("candidate review is invalid")
    payload["status"] = "promoted"
    payload["review"] = {
        **dict(review),
        "requires_user_confirmation": True,
        "auto_promote_allowed": False,
        "reason": _required_string(context, "review_reason"),
        "reviewed_by": "user",
        "reviewed_at": _required_string(context, "reviewed_at"),
    }
    payload["application"] = {
        "operation_id": operation.operation_id,
        "draft_id": operation.evidence.draft_id,
        "staging_authority": operation.evidence.staging_authority,
    }
    payload["updated_at"] = _required_string(context, "reviewed_at")
    return payload


def _layer(value: object) -> str:
    if value not in _STAGING:
        raise MemoryPublicationReviewStagingServiceConflict(
            "candidate target_layer is not generic Memory"
        )
    return str(value)


def _source_refs(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    refs: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        source_id = item.get("source_id")
        locator = item.get("locator")
        if not isinstance(source_id, str) or not source_id or not isinstance(locator, str) or not locator:
            continue
        ref = {"source_id": source_id, "locator": locator}
        if isinstance(item.get("quote"), str):
            ref["quote"] = item["quote"]
        refs.append(ref)
    return refs


def _evidence_refs(value: object, fallback: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return _source_refs(value) or [dict(item) for item in fallback]


def _candidate_revision(candidates: MemoryPublicationReviewCandidateStorePort, candidate_id: str) -> int:
    revision = candidates.revision(_CANDIDATE_COLLECTION, candidate_id)
    if revision < 1:
        raise MemoryPublicationReviewStagingServiceConflict("candidate revision is missing")
    return revision


def _required_string(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise MemoryPublicationReviewStagingServiceConflict(f"{key} is required")
    return value


def _sqlite_external_series_replacement(
    draft: Mapping[str, object], current: Any
) -> bool:
    update = draft.get("external_series_update")
    if not isinstance(update, Mapping):
        return False
    return (
        update.get("authority_identity") == "sqlite:structured-records-v1"
        and update.get("expected_object_revision") == current.revision
        and update.get("base_series_revision") == current.payload.get("revision")
        and draft.get("revision") == current.payload.get("revision", 0) + 1
    )


def _sqlite_hierarchy_replacement(
    draft: Mapping[str, object], current: Any
) -> bool:
    update = draft.get("hierarchy_update")
    if not isinstance(update, Mapping):
        return False
    return (
        update.get("authority_identity") == "sqlite:structured-records-v1"
        and update.get("object_id") == draft.get("id")
        and update.get("expected_object_revision") == current.revision
        and update.get("base_domain_revision") == current.payload.get("revision")
        and draft.get("revision") == current.payload.get("revision", 0) + 1
    )


def _digest(payload: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise MemoryPublicationReviewStagingServiceConflict(
            "review staging payload is invalid"
        ) from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
