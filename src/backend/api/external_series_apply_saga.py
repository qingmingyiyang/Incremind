from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from core.storage_provider import (
    ExternalSeriesApplyEvidence,
    ExternalSeriesApplyOperation,
    ExternalSeriesApplySagaConflict,
    ObjectStoreRevisionError,
)


class ExternalSeriesApplyError(ValueError):
    pass


class ExternalSeriesApplyConflict(ExternalSeriesApplyError):
    pass


class SeriesObjectStore(Protocol):
    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None: ...
    def write(self, collection: str, object_id: str, payload: Mapping[str, object], expected_revision: int | None) -> int: ...
    def revision(self, collection: str, object_id: str) -> int: ...


class OperationStore(Protocol):
    def prepare(self, *, operation_id: str, evidence: ExternalSeriesApplyEvidence, now: str | None = None) -> ExternalSeriesApplyOperation: ...
    def mark_series_applied(self, operation_id: str, *, expected_revision: int, applied_series_revision: int, now: str | None = None) -> ExternalSeriesApplyOperation: ...
    def finalize(self, operation_id: str, *, expected_revision: int, now: str | None = None) -> ExternalSeriesApplyOperation: ...


@dataclass(frozen=True, slots=True)
class ExternalSeriesApplyResult:
    operation_id: str
    operation_revision: int
    series_id: str
    series_memory_id: str
    series_revision: int
    state: str


class ExternalSeriesApplySagaService:
    def __init__(self, *, objects: SeriesObjectStore, operations: OperationStore,
                 namespace_id: str = "default", authority_identity: str = "json:object-store-v1", now=None) -> None:
        self._objects, self._operations = objects, operations
        self._namespace_id, self._authority = namespace_id, authority_identity
        self._now = now or _utc_now

    def apply(self, draft_id: str, *, expected_object_revision: int) -> ExternalSeriesApplyResult:
        draft = self._objects.read("external_agent_review_drafts", draft_id)
        if draft is None: raise ExternalSeriesApplyError("external agent review draft not found")
        series_id, memory_id, proposed = _draft_contract(draft)
        proposed_revision = _positive(proposed.get("revision"), "proposed series revision")
        base_series_revision = proposed_revision - 1
        if expected_object_revision < 0: raise ExternalSeriesApplyError("expected object revision is invalid")
        payload_hash = _payload_hash(draft_id, series_id, memory_id, expected_object_revision, base_series_revision, proposed)
        evidence = ExternalSeriesApplyEvidence(self._namespace_id, series_id, memory_id, expected_object_revision,
                                               base_series_revision, payload_hash, self._authority)
        try:
            operation = self._operations.prepare(operation_id=draft_id, evidence=evidence)
        except ExternalSeriesApplySagaConflict as exc:
            raise ExternalSeriesApplyConflict(str(exc)) from exc

        if operation.state == "prepared":
            current = self._objects.read("memory_series_memory", memory_id)
            object_revision = self._objects.revision("memory_series_memory", memory_id)
            if current == proposed:
                if object_revision != expected_object_revision + 1:
                    raise ExternalSeriesApplyConflict("series payload matched but object revision evidence drifted")
            else:
                current_domain = 0 if current is None else _non_negative(current.get("revision"), "current series revision")
                if current_domain != base_series_revision or object_revision != expected_object_revision:
                    raise ExternalSeriesApplyConflict("series revision advanced without operation evidence")
                try:
                    written_revision = self._objects.write("memory_series_memory", memory_id, proposed, expected_object_revision)
                except ObjectStoreRevisionError as exc:
                    raise ExternalSeriesApplyConflict(str(exc)) from exc
                if written_revision != expected_object_revision + 1:
                    raise ExternalSeriesApplyError("series object revision did not advance exactly once")
            operation = self._operations.mark_series_applied(
                draft_id, expected_revision=operation.revision, applied_series_revision=proposed_revision
            )

        if operation.state == "series_applied":
            applied_revision = _positive(operation.applied_series_revision, "applied series revision")
            current_draft = self._objects.read("external_agent_review_drafts", draft_id)
            if current_draft is None: raise ExternalSeriesApplyError("external agent review draft disappeared")
            if not _is_finalized(current_draft, draft_id, series_id, memory_id, applied_revision):
                if current_draft.get("status") != "pending_review":
                    raise ExternalSeriesApplyError("review draft state drifted before finalization")
                self._objects.write("external_agent_review_drafts", draft_id, _finalized(
                    current_draft, draft_id, series_id, memory_id, applied_revision, self._now()
                ), expected_revision=None)
            operation = self._operations.finalize(draft_id, expected_revision=operation.revision)

        if operation.state != "finalized" or operation.applied_series_revision is None:
            raise ExternalSeriesApplyError("series apply operation did not finalize")
        return ExternalSeriesApplyResult(draft_id, operation.revision, series_id, memory_id,
                                         operation.applied_series_revision, operation.state)


def _draft_contract(draft):
    if draft.get("draft_type") != "series_update": raise ExternalSeriesApplyError("review draft is not a series update")
    app = draft.get("application") if isinstance(draft.get("application"), Mapping) else {}
    if draft.get("status") != "pending_review" and not (draft.get("status") == "applied" and app.get("operation_id")):
        raise ExternalSeriesApplyError("review draft is not pending or recoverable")
    changes = draft.get("suggested_changes")
    if not isinstance(changes, Mapping):
        raise ExternalSeriesApplyError("series_update draft requires suggested_changes")
    proposed = _first_mapping(changes, ("structured", "series_memory", "structured_series_memory"))
    if proposed is None: raise ExternalSeriesApplyError("series_update draft requires structured series memory JSON")
    series_id, memory_id = proposed.get("series_id"), proposed.get("id")
    if not isinstance(series_id, str) or not series_id or not isinstance(memory_id, str) or not memory_id:
        raise ExternalSeriesApplyError("series memory requires id and series_id")
    if draft.get("target_id") not in {None, series_id, memory_id}:
        raise ExternalSeriesApplyError("series memory id does not match draft target_id")
    return series_id, memory_id, dict(proposed)


def _payload_hash(draft_id, series_id, memory_id, object_revision, series_revision, payload):
    encoded = json.dumps({"draft_id": draft_id, "series_id": series_id, "series_memory_id": memory_id,
                          "base_object_revision": object_revision, "base_series_revision": series_revision,
                          "payload": payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _first_mapping(value, keys):
    if not isinstance(value, Mapping): return None
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, Mapping): return candidate
    return None


def _is_finalized(draft, operation_id, series_id, memory_id, revision):
    app = draft.get("application") if isinstance(draft.get("application"), Mapping) else {}
    return draft.get("status") == "applied" and app.get("operation_id") == operation_id and app.get("applied_series_id") == series_id and app.get("applied_series_memory_id") == memory_id and app.get("applied_series_memory_revision") == revision


def _finalized(draft, operation_id, series_id, memory_id, revision, timestamp):
    result = dict(draft); review = dict(draft.get("review") or {}); app = dict(draft.get("application") or {})
    result.update({"status": "applied", "updated_at": timestamp})
    review.update({"state": "applied", "reviewed_by": "user", "reviewed_at": timestamp})
    app.update({"state": "applied", "operation_id": operation_id, "applied_by": "user", "applied_at": timestamp,
                "applied_series_id": series_id, "applied_series_memory_id": memory_id,
                "applied_series_memory_revision": revision, "writes_long_term_memory": True,
                "writes_long_term_memory_reason": "user_confirmed_series_update_apply", "writes_staging_memory": False})
    result["review"], result["application"] = review, app
    return result


def _positive(value, label):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1: raise ExternalSeriesApplyError(f"{label} is invalid")
    return value


def _non_negative(value, label):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0: raise ExternalSeriesApplyError(f"{label} is invalid")
    return value


def _utc_now(): return datetime.now(timezone.utc).isoformat()
