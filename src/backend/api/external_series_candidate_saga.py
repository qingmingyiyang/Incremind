from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from core.storage_provider import SQLiteStructuredRecordStore

from core.memory_core import MemoryCandidateRepositoryError, ObjectStoreMemoryCandidateRepository
from core.memory_core.runtime import memory_candidate_id
from core.storage_provider import (
    ExternalSeriesCandidateEvidence,
    ExternalSeriesCandidateSagaConflict,
)


class ExternalSeriesCandidateError(ValueError):
    pass


class ExternalSeriesCandidateConflict(ExternalSeriesCandidateError):
    pass


class SeriesCandidateObjectStore(Protocol):
    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None: ...
    def write(self, collection: str, object_id: str, payload: Mapping[str, object], expected_revision: int | None) -> int: ...
    def revision(self, collection: str, object_id: str) -> int: ...


class SeriesCurrentProjection(Protocol):
    authority_identity: str

    def read_current(self, object_id: str) -> Mapping[str, object] | None: ...
    def current_revision(self, object_id: str) -> int: ...


@dataclass(frozen=True, slots=True)
class SQLiteSeriesCurrentProjection:
    """Read-only Series current/CAS evidence from the compound SQLite target."""

    records: SQLiteStructuredRecordStore
    authority_identity: str = "sqlite:structured-records-v1"

    def read_current(self, object_id: str) -> Mapping[str, object] | None:
        record = self.records.read("memory_series_memory", object_id)
        return dict(record.payload) if record is not None else None

    def current_revision(self, object_id: str) -> int:
        record = self.records.read("memory_series_memory", object_id)
        return record.revision if record is not None else 0


@dataclass(frozen=True, slots=True)
class ObjectStoreSeriesCurrentProjection:
    objects: SeriesCandidateObjectStore
    authority_identity: str = "json:object-store-v1"

    def read_current(self, object_id: str) -> Mapping[str, object] | None:
        return self.objects.read("memory_series_memory", object_id)

    def current_revision(self, object_id: str) -> int:
        return self.objects.revision("memory_series_memory", object_id)


class CandidateOperationStore(Protocol):
    def prepare(self, *, operation_id: str, evidence: ExternalSeriesCandidateEvidence, now: str | None = None): ...
    def mark_candidate_created(self, operation_id: str, *, expected_revision: int, candidate_revision: int, now: str | None = None): ...
    def finalize(self, operation_id: str, *, expected_revision: int, now: str | None = None): ...


@dataclass(frozen=True, slots=True)
class ExternalSeriesCandidateResult:
    operation_id: str
    operation_revision: int
    candidate_id: str
    candidate_revision: int
    series_id: str
    series_memory_id: str
    proposed_series_revision: int
    state: str


class ExternalSeriesCandidateSagaService:
    """Converts external Series update drafts into pending local Memory Candidates only."""

    def __init__(
        self,
        *,
        objects: SeriesCandidateObjectStore,
        operations: CandidateOperationStore,
        namespace_id: str = "default",
        authority_identity: str = "json:object-store-v1",
        current: SeriesCurrentProjection | None = None,
        now=None,
    ) -> None:
        self._objects = objects
        self._candidates = ObjectStoreMemoryCandidateRepository(objects)
        self._operations = operations
        self._namespace_id = namespace_id
        self._current = current or ObjectStoreSeriesCurrentProjection(objects, authority_identity)
        self._authority = self._current.authority_identity
        if authority_identity != self._authority:
            raise ExternalSeriesCandidateError("series current authority identity conflicts")
        self._now = now or _utc_now

    def apply(self, draft_id: str, *, expected_object_revision: int) -> ExternalSeriesCandidateResult:
        draft = self._objects.read("external_agent_review_drafts", draft_id)
        if draft is None:
            raise ExternalSeriesCandidateError("external agent review draft not found")
        project_id, series_id, memory_id, proposed = _draft_contract(draft)
        proposed_revision = _positive(proposed.get("revision"), "proposed series revision")
        base_series_revision = proposed_revision - 1
        if expected_object_revision < 0:
            raise ExternalSeriesCandidateError("expected object revision is invalid")
        payload_hash = _payload_hash(draft_id, project_id, series_id, memory_id, expected_object_revision, base_series_revision, proposed)
        candidate_id = memory_candidate_id("external-series-update", draft_id, payload_hash)
        evidence = ExternalSeriesCandidateEvidence(
            self._namespace_id,
            series_id,
            memory_id,
            candidate_id,
            expected_object_revision,
            base_series_revision,
            payload_hash,
            self._authority,
        )
        try:
            operation = self._operations.prepare(operation_id=draft_id, evidence=evidence)
        except ExternalSeriesCandidateSagaConflict as error:
            raise ExternalSeriesCandidateConflict(str(error)) from error

        if operation.state == "prepared":
            self._assert_current_target(
                memory_id=memory_id,
                expected_object_revision=expected_object_revision,
                base_series_revision=base_series_revision,
            )
            candidate = _candidate(
                draft=draft,
                draft_id=draft_id,
                project_id=project_id,
                series_id=series_id,
                memory_id=memory_id,
                proposed=proposed,
                evidence=evidence,
                created_at=operation.created_at,
                namespace_id=self._namespace_id,
            )
            existing = self._candidates.get(candidate_id)
            if existing is None:
                try:
                    self._candidates.save(candidate)
                except MemoryCandidateRepositoryError as error:
                    raise ExternalSeriesCandidateError(str(error)) from error
            elif dict(existing) != candidate:
                raise ExternalSeriesCandidateConflict("external series candidate evidence drifted")
            candidate_revision = self._objects.revision("memory_candidates", candidate_id)
            if candidate_revision < 1:
                raise ExternalSeriesCandidateError("external series candidate revision is unavailable")
            operation = self._operations.mark_candidate_created(
                draft_id,
                expected_revision=operation.revision,
                candidate_revision=candidate_revision,
            )

        if operation.state == "candidate_created":
            candidate_revision = _positive(operation.candidate_revision, "candidate revision")
            candidate = self._candidates.get(candidate_id)
            if candidate is None or self._objects.revision("memory_candidates", candidate_id) != candidate_revision:
                raise ExternalSeriesCandidateConflict("external series candidate evidence drifted")
            current_draft = self._objects.read("external_agent_review_drafts", draft_id)
            if current_draft is None:
                raise ExternalSeriesCandidateError("external agent review draft disappeared")
            if not _is_finalized(current_draft, draft_id, candidate_id, candidate_revision):
                if current_draft.get("status") != "pending_review":
                    raise ExternalSeriesCandidateError("review draft state drifted before candidate finalization")
                self._objects.write(
                    "external_agent_review_drafts",
                    draft_id,
                    _finalized(current_draft, draft_id, candidate_id, candidate_revision, self._now()),
                    expected_revision=None,
                )
            operation = self._operations.finalize(draft_id, expected_revision=operation.revision)

        if operation.state != "finalized" or operation.candidate_revision is None:
            raise ExternalSeriesCandidateError("series candidate operation did not finalize")
        return ExternalSeriesCandidateResult(
            draft_id,
            operation.revision,
            candidate_id,
            operation.candidate_revision,
            series_id,
            memory_id,
            proposed_revision,
            operation.state,
        )

    def _assert_current_target(
        self,
        *,
        memory_id: str,
        expected_object_revision: int,
        base_series_revision: int,
    ) -> None:
        current = self._current.read_current(memory_id)
        actual_revision = self._current.current_revision(memory_id)
        current_domain = 0 if current is None else _non_negative(current.get("revision"), "current series revision")
        if current is None or actual_revision != expected_object_revision or current_domain != base_series_revision:
            raise ExternalSeriesCandidateConflict("series revision advanced without candidate evidence")


def _draft_contract(draft: Mapping[str, object]) -> tuple[str, str, str, dict[str, object]]:
    if draft.get("draft_type") != "series_update":
        raise ExternalSeriesCandidateError("review draft is not a series update")
    application = draft.get("application") if isinstance(draft.get("application"), Mapping) else {}
    recoverable = draft.get("status") == "candidate_created" and application.get("operation_id") == draft.get("id")
    if draft.get("status") != "pending_review" and not recoverable:
        raise ExternalSeriesCandidateError("review draft is not pending or recoverable")
    project_id = _required_str(draft, "project_id")
    changes = draft.get("suggested_changes")
    proposed = _first_mapping(changes, ("structured", "series_memory", "structured_series_memory"))
    if proposed is None:
        raise ExternalSeriesCandidateError("series_update draft requires structured series memory JSON")
    series_id = _required_str(proposed, "series_id")
    memory_id = _required_str(proposed, "id")
    if draft.get("target_id") not in {None, series_id, memory_id}:
        raise ExternalSeriesCandidateError("series memory id does not match draft target_id")
    return project_id, series_id, memory_id, proposed


def _candidate(
    *,
    draft: Mapping[str, object],
    draft_id: str,
    project_id: str,
    series_id: str,
    memory_id: str,
    proposed: Mapping[str, object],
    evidence: ExternalSeriesCandidateEvidence,
    created_at: str,
    namespace_id: str,
) -> dict[str, object]:
    source_refs = _refs(draft.get("source_refs"), fallback_id=f"external-agent:{draft_id}")
    evidence_refs = _refs(draft.get("evidence_refs"), fallback_id=f"external-agent:{draft_id}")
    if not source_refs or not evidence_refs:
        raise ExternalSeriesCandidateError("external series draft requires traceable source and evidence refs")
    candidate_id = evidence.candidate_id
    return {
        "schema_version": "1.0.0",
        "id": candidate_id,
        "project_id": project_id,
        "target_layer": "series_memory",
        "candidate_type": "other",
        "status": "pending_review",
        "proposed_content": _required_str(proposed, "overview"),
        "source_refs": source_refs,
        "evidence_refs": evidence_refs,
        "provenance": {
            "model_result_id": None,
            "model_request_id": None,
            "recall_result_id": None,
            "document_id": None,
            "document_revision": None,
            "source_content_read_id": f"external-agent-draft:{draft_id}",
            "media_processing_output_id": None,
            "media_processing_job_id": None,
            "input_refs": [
                {
                    "kind": "source",
                    "object_id": source_refs[0]["source_id"],
                    "uri": f"crp://{namespace_id}/external-agent-review-drafts/{draft_id}.json#source",
                },
                {
                    "kind": "source_content_read",
                    "object_id": f"external-agent-draft:{draft_id}",
                    "uri": f"crp://{namespace_id}/external-agent-review-drafts/{draft_id}.json",
                },
                {
                    "kind": "external_agent_review_draft",
                    "object_id": draft_id,
                    "uri": f"crp://{namespace_id}/external-agent-review-drafts/{draft_id}.json",
                },
            ],
        },
        "external_series_update": {
            "draft_id": draft_id,
            "series_id": series_id,
            "series_memory_id": memory_id,
            "expected_object_revision": evidence.base_object_revision,
            "base_series_revision": evidence.base_series_revision,
            "payload_sha256": evidence.payload_sha256,
            "authority_identity": evidence.authority_identity,
            "proposed": dict(proposed),
        },
        "review": {
            "requires_user_confirmation": True,
            "auto_promote_allowed": False,
            "reason": "External Series update must become a local pending Memory Candidate before staging or publication.",
            "reviewed_by": None,
            "reviewed_at": None,
        },
        "created_at": created_at,
        "updated_at": created_at,
    }


def _refs(value: object, *, fallback_id: str) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    refs: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        source_id = item.get("source_id")
        locator = item.get("locator") or item.get("ref") or source_id
        if not isinstance(locator, str) or not locator:
            continue
        ref: dict[str, object] = {
            "source_id": source_id if isinstance(source_id, str) and source_id else fallback_id,
            "locator": locator,
        }
        quote = item.get("quote")
        if isinstance(quote, str) and quote:
            ref["quote"] = quote
        refs.append(ref)
    return refs


def _payload_hash(
    draft_id: str,
    project_id: str,
    series_id: str,
    memory_id: str,
    object_revision: int,
    series_revision: int,
    payload: Mapping[str, object],
) -> str:
    encoded = json.dumps(
        {
            "draft_id": draft_id,
            "project_id": project_id,
            "series_id": series_id,
            "series_memory_id": memory_id,
            "base_object_revision": object_revision,
            "base_series_revision": series_revision,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _is_finalized(draft: Mapping[str, object], operation_id: str, candidate_id: str, candidate_revision: int) -> bool:
    application = draft.get("application") if isinstance(draft.get("application"), Mapping) else {}
    return (
        draft.get("status") == "candidate_created"
        and application.get("operation_id") == operation_id
        and application.get("memory_candidate_id") == candidate_id
        and application.get("memory_candidate_revision") == candidate_revision
        and application.get("writes_long_term_memory") is False
        and application.get("writes_staging_memory") is False
    )


def _finalized(
    draft: Mapping[str, object],
    operation_id: str,
    candidate_id: str,
    candidate_revision: int,
    timestamp: str,
) -> dict[str, object]:
    result = dict(draft)
    review = dict(draft.get("review") or {})
    application = dict(draft.get("application") or {})
    result.update({"status": "candidate_created", "updated_at": timestamp})
    review.update({"state": "candidate_created", "reviewed_by": "user", "reviewed_at": timestamp})
    application.update(
        {
            "state": "candidate_created",
            "operation_id": operation_id,
            "applied_by": "user",
            "applied_at": timestamp,
            "memory_candidate_id": candidate_id,
            "memory_candidate_revision": candidate_revision,
            "writes_long_term_memory": False,
            "writes_long_term_memory_reason": "external_series_update_requires_candidate_review",
            "writes_staging_memory": False,
        }
    )
    result["review"] = review
    result["application"] = application
    return result


def _first_mapping(value: object, keys: Sequence[str]) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, Mapping):
            return dict(candidate)
    return None


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ExternalSeriesCandidateError(f"{key} is required")
    return value


def _positive(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ExternalSeriesCandidateError(f"{label} is invalid")
    return value


def _non_negative(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ExternalSeriesCandidateError(f"{label} is invalid")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
