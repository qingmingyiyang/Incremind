"""Auditable recovery of missing external-Agent publication outbox entries.

The normal publication UoWs append the outbox in the same SQLite transaction.
This module is deliberately narrower: it repairs only *already current* formal
publication authorities created before that invariant existed.  It never
constructs a publication, current projection, timestamp, or project binding.
Those facts must be present and mutually consistent in the existing records.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re

from .external_agent_publication_change import (
    ExternalAgentPublicationChangeError,
    ExternalAgentPublicationChangeOutbox,
    publication_outbox_collection,
)
from .sqlite_uow import SQLiteStructuredRecord, SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


DEFAULT_EXTERNAL_AGENT_PUBLICATION_BACKFILL_LIMIT = 100
_AUDIT_PREFIX = "external_agent_publication_backfill_audit."
_AUDIT_SCHEMA = "external-agent-publication-backfill-audit-v1"
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_MEMORY_COLLECTIONS = {
    "atom": "memory_atoms",
    "scenario": "memory_scenarios",
    "series_memory": "memory_series_memory",
}


@dataclass(frozen=True, slots=True)
class PublicationOutboxBackfillReceipt:
    publication_identity: str
    project_id: str
    disposition: str


@dataclass(frozen=True, slots=True)
class PublicationOutboxBackfillIssue:
    publication_identity: str
    code: str


@dataclass(frozen=True, slots=True)
class PublicationOutboxBackfillResult:
    scanned: int
    receipts: tuple[PublicationOutboxBackfillReceipt, ...]
    issues: tuple[PublicationOutboxBackfillIssue, ...]


class PublicationOutboxBackfillError(ValueError):
    """A historical publication cannot safely establish an outbox event."""


def publication_backfill_audit_collection(project_id: str) -> str:
    if not isinstance(project_id, str) or not _SAFE.fullmatch(project_id):
        raise PublicationOutboxBackfillError("project identity is invalid")
    collection = f"{_AUDIT_PREFIX}{project_id}"
    if len(collection) > 128:
        raise PublicationOutboxBackfillError("project audit collection is invalid")
    return collection


def backfill_external_agent_publication_outbox(
    records: SQLiteStructuredRecordStore,
    *,
    limit: int = DEFAULT_EXTERNAL_AGENT_PUBLICATION_BACKFILL_LIMIT,
) -> PublicationOutboxBackfillResult:
    """Enqueue at most ``limit`` valid current publication authorities.

    The formal publication record is re-read inside each short UoW before the
    outbox and audit receipt are appended.  That makes retries and restarts
    idempotent while a concurrently rolled-back or superseded authority is
    skipped rather than resurrected.  Invalid historical records are reported
    as safe codes and never prevent later candidates in this bounded pass.
    """
    _validate_limit(limit)
    candidates = records.list("memory_publications")[:limit]
    receipts: list[PublicationOutboxBackfillReceipt] = []
    issues: list[PublicationOutboxBackfillIssue] = []
    for candidate in candidates:
        try:
            receipts.extend(_backfill_one(records, candidate))
        except PublicationOutboxBackfillError as error:
            issues.append(PublicationOutboxBackfillIssue(
                publication_identity=_safe_identity(candidate.object_id),
                code=_safe_issue_code(error),
            ))
        except (SQLiteUnitOfWorkConflict, ExternalAgentPublicationChangeError):
            # A concurrent publication lifecycle may have advanced the source
            # after this pass selected it.  A future bounded restart pass will
            # observe its then-current formal state.
            issues.append(PublicationOutboxBackfillIssue(
                publication_identity=_safe_identity(candidate.object_id),
                code="authority_changed",
            ))
    return PublicationOutboxBackfillResult(
        scanned=len(candidates), receipts=tuple(receipts), issues=tuple(issues),
    )


def _backfill_one(
    records: SQLiteStructuredRecordStore,
    candidate: SQLiteStructuredRecord,
) -> tuple[PublicationOutboxBackfillReceipt, ...]:
    with records.begin() as uow:
        publication = uow.read("memory_publications", candidate.object_id)
        if publication is None:
            return ()
        events = _current_publication_events(uow, publication)
        receipts: list[PublicationOutboxBackfillReceipt] = []
        for event in events:
            existing = ExternalAgentPublicationChangeOutbox.enqueue(uow, **event)
            audit_collection = publication_backfill_audit_collection(event["project_id"])
            audit = _audit_payload(event, publication.revision)
            existing_audit = uow.read(audit_collection, event["publication_identity"])
            if existing_audit is None:
                uow.put(audit_collection, event["publication_identity"], audit, expected_revision=0)
                disposition = "enqueued" if existing.revision == 1 else "verified_existing"
            elif dict(existing_audit.payload) == audit:
                disposition = "replayed"
            else:
                raise PublicationOutboxBackfillError("backfill audit conflicts")
            receipts.append(PublicationOutboxBackfillReceipt(
                publication_identity=event["publication_identity"],
                project_id=event["project_id"],
                disposition=disposition,
            ))
        if events:
            uow.commit()
        return tuple(receipts)


def _current_publication_events(uow, publication: SQLiteStructuredRecord) -> tuple[dict[str, str], ...]:
    payload = dict(publication.payload)
    if payload.get("schema_version") != "1.0.0" or payload.get("status") != "published":
        return ()
    identity = _required_safe(payload, "id")
    if identity != publication.object_id or payload.get("publication_id", identity) != identity:
        raise PublicationOutboxBackfillError("publication identity is invalid")
    kind = payload.get("object_type")
    layer = payload.get("layer")
    if kind in _MEMORY_COLLECTIONS and layer == kind:
        return _memory_events(uow, publication, payload)
    if kind == "project_skill" and layer == "project_skill":
        return _project_skill_events(uow, publication, payload)
    raise PublicationOutboxBackfillError("publication kind is unsupported")


def _memory_events(uow, publication: SQLiteStructuredRecord, payload: Mapping[str, object]) -> tuple[dict[str, str], ...]:
    layer = _required_safe(payload, "layer")
    object_id = _required_safe(payload, "published_object_id")
    revision = _required_positive_int(payload, "published_revision")
    occurred_at = _frozen_transition_time(uow, payload, "published_at")
    current = uow.read(_MEMORY_COLLECTIONS[layer], object_id)
    if (
        current is None
        or current.payload.get("id") != object_id
        or current.payload.get("revision") != revision
        or current.payload.get("trust_status") != "user_confirmed"
    ):
        raise PublicationOutboxBackfillError("memory current projection conflicts")
    if layer == "atom":
        return ()
    if layer == "scenario":
        project_ids = (_required_safe(current.payload, "project_id"),)
    else:
        project_ids = _project_ids(current.payload.get("project_ids"))
    return tuple({
        "publication_identity": publication.object_id,
        "project_id": project_id,
        "change_type": "memory.published",
        "object_ref": f"crp://memory/{project_id}/{object_id}",
        "object_revision": f"r{revision}",
        "occurred_at": occurred_at,
    } for project_id in project_ids)


def _project_skill_events(uow, publication: SQLiteStructuredRecord, payload: Mapping[str, object]) -> tuple[dict[str, str], ...]:
    project_id = _required_safe(payload, "project_id")
    skill_id = _required_safe(payload, "published_object_id")
    revision = _required_positive_int(payload, "published_revision")
    occurred_at = _frozen_transition_time(uow, payload, "created_at")
    current = uow.read("project_skills", skill_id)
    if (
        current is None
        or current.payload.get("id") != skill_id
        or current.payload.get("project_id") != project_id
        or current.payload.get("revision") != revision
        or current.payload.get("status") != "active"
        or current.payload.get("trust_status") != "user_confirmed"
    ):
        raise PublicationOutboxBackfillError("project skill current projection conflicts")
    return ({
        "publication_identity": publication.object_id,
        "project_id": project_id,
        "change_type": "project_skill.published",
        "object_ref": f"crp://skills/{project_id}/{skill_id}",
        "object_revision": f"project-skill-r{revision}",
        "occurred_at": occurred_at,
    },)


def _frozen_transition_time(uow, payload: Mapping[str, object], timestamp_field: str) -> str:
    occurred_at = _required_string(payload, timestamp_field)
    transition_ref = _required_string(payload, "transition_ref")
    transition_id = transition_ref.rsplit("/", 1)[-1].removesuffix(".json")
    if not _SAFE.fullmatch(transition_id):
        raise PublicationOutboxBackfillError("publication transition reference is invalid")
    transition = uow.read("memory_transitions", transition_id)
    if (
        transition is None
        or transition.payload.get("id") != transition_id
        or transition.payload.get("created_at") != occurred_at
        or transition.payload.get("object_type") != payload.get("object_type")
        or transition.payload.get("object_id") != payload.get("published_object_id")
    ):
        raise PublicationOutboxBackfillError("publication transition conflicts")
    return occurred_at


def _audit_payload(event: Mapping[str, str], publication_revision: int) -> dict[str, object]:
    return {
        "schema": _AUDIT_SCHEMA,
        "state": "verified",
        "publication_record_revision": publication_revision,
        "event": dict(event),
    }


def _project_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise PublicationOutboxBackfillError("series project bindings are invalid")
    projects = tuple(_required_safe({"project_id": item}, "project_id") for item in value)
    if len(set(projects)) != len(projects):
        raise PublicationOutboxBackfillError("series project bindings are duplicated")
    return projects


def _validate_limit(limit: int) -> None:
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= DEFAULT_EXTERNAL_AGENT_PUBLICATION_BACKFILL_LIMIT
    ):
        raise ValueError("limit must be an integer from 1 through 100")


def _required_safe(payload: Mapping[str, object], key: str) -> str:
    value = _required_string(payload, key)
    if not _SAFE.fullmatch(value):
        raise PublicationOutboxBackfillError(f"publication {key} is invalid")
    return value


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise PublicationOutboxBackfillError(f"publication {key} is invalid")
    return value


def _required_positive_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PublicationOutboxBackfillError(f"publication {key} is invalid")
    return value


def _safe_identity(value: str) -> str:
    return value if _SAFE.fullmatch(value) else "invalid-publication-id"


def _safe_issue_code(error: PublicationOutboxBackfillError) -> str:
    # Exception strings are intentionally a finite local vocabulary.  Do not
    # expose malformed authority values through startup state or logs.
    return str(error).replace(" ", "_")[:96]
