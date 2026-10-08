"""Durable, Core-owned Context Graph snapshot revisions.

The structured-record store accepts only short safe record segments.  Graph
identities deliberately are not used as record ids: valid graph ids can be
longer (and contain characters) than that storage boundary permits.  A
transactional sequence allocates opaque short ids, while the immutable payload
keeps the complete scoped identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping

from core.context_graph import (
    ContextPermissionError,
    ContextPermissionGrant,
    ContextGraphSnapshot,
    ContextGraphValidationError,
    snapshot_from_dict,
    validate_snapshot,
)
from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
    SQLiteUnitOfWorkConflict,
    SQLiteUnitOfWorkError,
)


_HEADS = "context_graph_snapshot_heads"
_REVISIONS = "context_graph_snapshot_revisions"
_SEQUENCES = "context_graph_snapshot_sequences"
_SEQUENCE_ID = "sequence"
_SCHEMA_VERSION = "1.0.0"


class ContextGraphSnapshotRepositoryError(ValueError):
    """The persisted Context Graph snapshot authority is invalid or conflicted."""


class ContextGraphSnapshotConflict(ContextGraphSnapshotRepositoryError):
    """A predecessor CAS or immutable revision identity did not match."""


@dataclass(frozen=True, slots=True)
class ContextGraphSnapshotRecord:
    """One validated immutable graph snapshot with its storage metadata."""

    record_id: str
    snapshot: ContextGraphSnapshot
    store_revision: int
    predecessor: str | None
    capability_id: str
    capability_revision: str
    permission_grant: ContextPermissionGrant
    permission_evidence_refs: tuple[str, ...]

    @property
    def project_id(self) -> str:
        return self.snapshot.project_id

    @property
    def graph_id(self) -> str:
        return self.snapshot.graph_id

    @property
    def graph_revision(self) -> str:
        return self.snapshot.graph_revision


# More explicit spelling for consumers that prefer a revision noun.
ContextGraphSnapshotRevision = ContextGraphSnapshotRecord


class ContextGraphSnapshotRepository:
    """Append-only snapshot store with one CAS-protected current head per graph."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise ContextGraphSnapshotRepositoryError("Context Graph snapshot store is invalid")
        self._records = records

    def append(
        self,
        snapshot: ContextGraphSnapshot,
        expected_predecessor: str | None,
        *,
        capability_id: str,
        capability_revision: str,
        permission_grant: ContextPermissionGrant,
        permission_evidence_refs: tuple[str, ...],
    ) -> ContextGraphSnapshotRecord:
        """Append a new immutable graph revision and advance its head atomically."""

        normalized = _normalize_snapshot(snapshot)
        predecessor = _predecessor(expected_predecessor)
        authority = _compilation_authority(
            normalized, capability_id, capability_revision, permission_grant,
            permission_evidence_refs,
        )
        try:
            with self._records.begin() as uow:
                head = _find_head(uow, normalized.project_id, normalized.graph_id)
                if head is None:
                    if predecessor is not None:
                        raise ContextGraphSnapshotConflict("Context Graph first revision requires no predecessor")
                    head_id = _next_id(uow, "head")
                    head_revision = 0
                else:
                    head_id, head_revision, actual_predecessor = head
                    if predecessor != actual_predecessor:
                        raise ContextGraphSnapshotConflict("Context Graph snapshot predecessor conflicted")
                if _find_revision(uow, normalized.project_id, normalized.graph_id, normalized.graph_revision) is not None:
                    raise ContextGraphSnapshotConflict("Context Graph snapshot revision is immutable")
                revision_id = _next_id(uow, "revision")
                revision = uow.put(
                    _REVISIONS,
                    revision_id,
                    _revision_payload(normalized, predecessor, *authority),
                    expected_revision=0,
                )
                uow.put(
                    _HEADS,
                    head_id,
                    _head_payload(normalized, revision_id),
                    expected_revision=head_revision,
                )
                uow.commit()
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise ContextGraphSnapshotConflict("Context Graph snapshot write conflicted") from error
        return ContextGraphSnapshotRecord(
            revision_id, normalized, revision.revision, predecessor, *authority,
        )

    def current(self, project_id: str, graph_id: str) -> ContextGraphSnapshotRecord | None:
        """Resolve the current revision only within its exact project/graph scope."""

        project, graph = _scope(project_id, graph_id)
        head = _find_head_from_records(self._records.list(_HEADS), project, graph)
        if head is None:
            return None
        record = self._records.read(_REVISIONS, head[2])
        if record is None:
            raise ContextGraphSnapshotRepositoryError("Context Graph snapshot head is dangling")
        result = _record_from_storage(record)
        if (result.project_id, result.graph_id, result.graph_revision) != (project, graph, head[3]):
            raise ContextGraphSnapshotRepositoryError("Context Graph snapshot head identity drifted")
        return result

    def revision(
        self, project_id: str, graph_id: str, graph_revision: str,
    ) -> ContextGraphSnapshotRecord | None:
        """Resolve one immutable revision only within its exact identity scope."""

        project, graph = _scope(project_id, graph_id)
        revision = _required_text(graph_revision, "graph revision")
        found = _find_revision_from_records(self._records.list(_REVISIONS), project, graph, revision)
        return _record_from_storage(found) if found is not None else None

    def previous(
        self, project_id: str, graph_id: str, graph_revision: str,
    ) -> ContextGraphSnapshotRecord | None:
        """Resolve a predecessor only when its revision stays in the same graph scope."""

        current = self.revision(project_id, graph_id, graph_revision)
        if current is None or current.predecessor is None:
            return None
        previous = self.revision(project_id, graph_id, current.predecessor)
        if previous is None:
            raise ContextGraphSnapshotRepositoryError("Context Graph snapshot predecessor is dangling")
        if previous.record_id == current.record_id or previous.graph_revision == current.graph_revision:
            raise ContextGraphSnapshotRepositoryError("Context Graph snapshot predecessor chain is invalid")
        return previous


def _normalize_snapshot(snapshot: ContextGraphSnapshot) -> ContextGraphSnapshot:
    try:
        validate_snapshot(snapshot)
        # Round-trip through the public payload parser before persistence. This
        # rejects unsupported/future fields and makes stored JSON canonical.
        return snapshot_from_dict(_snapshot_payload(snapshot))
    except (ContextGraphValidationError, TypeError, ValueError) as error:
        raise ContextGraphSnapshotRepositoryError("Context Graph snapshot is invalid") from error


def _scope(project_id: str, graph_id: str) -> tuple[str, str]:
    return _required_text(project_id, "project id"), _required_text(graph_id, "graph id")


def _predecessor(value: str | None) -> str | None:
    if value is None:
        return None
    return _required_text(value, "expected predecessor")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextGraphSnapshotRepositoryError(f"Context Graph {label} is invalid")
    return value


def _next_id(uow: SQLiteStructuredRecordUnitOfWork, prefix: str) -> str:
    sequence = uow.read(_SEQUENCES, _SEQUENCE_ID)
    if sequence is None:
        number, expected = 1, 0
    else:
        payload = sequence.payload
        if set(payload) != {"schema_version", "next_id"} or payload.get("schema_version") != _SCHEMA_VERSION:
            raise ContextGraphSnapshotRepositoryError("Context Graph snapshot sequence is invalid")
        next_id = payload.get("next_id")
        if type(next_id) is not int or next_id < 1:
            raise ContextGraphSnapshotRepositoryError("Context Graph snapshot sequence is invalid")
        number, expected = next_id, sequence.revision
    uow.put(_SEQUENCES, _SEQUENCE_ID, {"schema_version": _SCHEMA_VERSION, "next_id": number + 1}, expected_revision=expected)
    return f"{prefix}-{number}"


def _head_payload(snapshot: ContextGraphSnapshot, revision_id: str) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "project_id": snapshot.project_id,
        "graph_id": snapshot.graph_id,
        "graph_revision": snapshot.graph_revision,
        "revision_id": revision_id,
    }


def _revision_payload(
    snapshot: ContextGraphSnapshot,
    predecessor: str | None,
    capability_id: str,
    capability_revision: str,
    permission_grant: ContextPermissionGrant,
    permission_evidence_refs: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "project_id": snapshot.project_id,
        "graph_id": snapshot.graph_id,
        "graph_revision": snapshot.graph_revision,
        "predecessor": predecessor,
        "snapshot": _snapshot_payload(snapshot),
        "capability_id": capability_id,
        "capability_revision": capability_revision,
        "permission_grant": _permission_grant_payload(permission_grant),
        "permission_evidence_refs": list(permission_evidence_refs),
    }


def _snapshot_payload(snapshot: ContextGraphSnapshot) -> dict[str, object]:
    """Convert frozen dataclass tuples to the public JSON payload shape."""

    payload = json.loads(json.dumps(snapshot.to_dict(), ensure_ascii=False))
    if not isinstance(payload, dict):  # defensive boundary for future model changes
        raise ContextGraphSnapshotRepositoryError("Context Graph snapshot payload is invalid")
    return payload


def _compilation_authority(
    snapshot: ContextGraphSnapshot,
    capability_id: object,
    capability_revision: object,
    permission_grant: object,
    permission_evidence_refs: object,
) -> tuple[str, str, ContextPermissionGrant, tuple[str, ...]]:
    capability = _required_text(capability_id, "capability id")
    capability_version = _required_text(capability_revision, "capability revision")
    if not isinstance(permission_grant, ContextPermissionGrant):
        raise ContextGraphSnapshotRepositoryError("Context Graph permission grant is invalid")
    if permission_grant.project_id != snapshot.project_id:
        raise ContextGraphSnapshotRepositoryError("Context Graph permission project scope drifted")
    try:
        permission_grant.validate(snapshot, tuple(node.node_id for node in snapshot.nodes))
    except (ContextPermissionError, KeyError, ValueError) as error:
        raise ContextGraphSnapshotRepositoryError("Context Graph content permission is incomplete") from error
    if not isinstance(permission_evidence_refs, tuple) or not permission_evidence_refs:
        raise ContextGraphSnapshotRepositoryError("Context Graph permission evidence is required")
    evidence = tuple(_required_text(item, "permission evidence ref") for item in permission_evidence_refs)
    if len(evidence) != len(set(evidence)):
        raise ContextGraphSnapshotRepositoryError("Context Graph permission evidence is duplicated")
    return capability, capability_version, permission_grant, evidence


def _permission_grant_payload(value: ContextPermissionGrant) -> dict[str, object]:
    return {
        "project_id": value.project_id,
        "permission_revision": value.permission_revision,
        "allowed_content_refs": sorted(value.allowed_content_refs),
    }


def _find_head(
    uow: SQLiteStructuredRecordUnitOfWork, project_id: str, graph_id: str,
) -> tuple[str, int, str] | None:
    found = _find_head_from_records(uow.list(_HEADS), project_id, graph_id)
    return (found[0], found[1], found[3]) if found is not None else None


def _find_head_from_records(
    records: tuple[SQLiteStructuredRecord, ...], project_id: str, graph_id: str,
) -> tuple[str, int, str, str] | None:
    matches: list[tuple[str, int, str, str]] = []
    for record in records:
        payload = _head_from_payload(record.payload)
        if payload[0] == project_id and payload[1] == graph_id:
            matches.append((record.object_id, record.revision, payload[3], payload[2]))
    if len(matches) > 1:
        raise ContextGraphSnapshotRepositoryError("Context Graph snapshot has multiple heads")
    return matches[0] if matches else None


def _find_revision(
    uow: SQLiteStructuredRecordUnitOfWork, project_id: str, graph_id: str, graph_revision: str,
) -> SQLiteStructuredRecord | None:
    return _find_revision_from_records(uow.list(_REVISIONS), project_id, graph_id, graph_revision)


def _find_revision_from_records(
    records: tuple[SQLiteStructuredRecord, ...], project_id: str, graph_id: str, graph_revision: str,
) -> SQLiteStructuredRecord | None:
    matches = []
    for record in records:
        result = _record_from_storage(record)
        if (result.project_id, result.graph_id, result.graph_revision) == (project_id, graph_id, graph_revision):
            matches.append(record)
    if len(matches) > 1:
        raise ContextGraphSnapshotRepositoryError("Context Graph snapshot immutable identity is duplicated")
    return matches[0] if matches else None


def _head_from_payload(payload: Mapping[str, object]) -> tuple[str, str, str, str]:
    fields = {"schema_version", "project_id", "graph_id", "graph_revision", "revision_id"}
    if not isinstance(payload, Mapping) or set(payload) != fields or payload.get("schema_version") != _SCHEMA_VERSION:
        raise ContextGraphSnapshotRepositoryError("Context Graph snapshot head is invalid")
    project, graph = _scope(payload.get("project_id"), payload.get("graph_id"))
    revision = _required_text(payload.get("graph_revision"), "head graph revision")
    revision_id = _required_text(payload.get("revision_id"), "head revision id")
    return project, graph, revision, revision_id


def _record_from_storage(record: SQLiteStructuredRecord) -> ContextGraphSnapshotRecord:
    payload = record.payload
    fields = {
        "schema_version", "project_id", "graph_id", "graph_revision", "predecessor", "snapshot",
        "capability_id", "capability_revision", "permission_grant", "permission_evidence_refs",
    }
    if not isinstance(payload, Mapping) or set(payload) != fields or payload.get("schema_version") != _SCHEMA_VERSION:
        raise ContextGraphSnapshotRepositoryError("Context Graph snapshot revision is invalid")
    project, graph = _scope(payload.get("project_id"), payload.get("graph_id"))
    graph_revision = _required_text(payload.get("graph_revision"), "graph revision")
    predecessor_raw = payload.get("predecessor")
    if predecessor_raw is not None and not isinstance(predecessor_raw, str):
        raise ContextGraphSnapshotRepositoryError("Context Graph snapshot predecessor is invalid")
    predecessor = _predecessor(predecessor_raw)
    snapshot_raw = payload.get("snapshot")
    try:
        snapshot = snapshot_from_dict(snapshot_raw)  # type: ignore[arg-type]
        validate_snapshot(snapshot)
    except (ContextGraphValidationError, TypeError, ValueError) as error:
        raise ContextGraphSnapshotRepositoryError("Context Graph stored snapshot is invalid") from error
    if (snapshot.project_id, snapshot.graph_id, snapshot.graph_revision) != (project, graph, graph_revision):
        raise ContextGraphSnapshotRepositoryError("Context Graph snapshot identity drifted")
    authority = _authority_from_payload(snapshot, payload)
    return ContextGraphSnapshotRecord(record.object_id, snapshot, record.revision, predecessor, *authority)


def _authority_from_payload(
    snapshot: ContextGraphSnapshot, payload: Mapping[str, object],
) -> tuple[str, str, ContextPermissionGrant, tuple[str, ...]]:
    grant_raw = payload.get("permission_grant")
    grant_fields = {"project_id", "permission_revision", "allowed_content_refs"}
    if not isinstance(grant_raw, Mapping) or set(grant_raw) != grant_fields:
        raise ContextGraphSnapshotRepositoryError("Context Graph stored permission grant is invalid")
    allowed = grant_raw.get("allowed_content_refs")
    if (
        not isinstance(allowed, list)
        or any(not isinstance(item, str) or not item.strip() for item in allowed)
        or len(allowed) != len(set(allowed))
    ):
        raise ContextGraphSnapshotRepositoryError("Context Graph stored permission grant is invalid")
    try:
        grant = ContextPermissionGrant(
            project_id=grant_raw.get("project_id"),
            permission_revision=grant_raw.get("permission_revision"),
            allowed_content_refs=frozenset(allowed),
        )
    except (TypeError, ValueError) as error:
        raise ContextGraphSnapshotRepositoryError("Context Graph stored permission grant is invalid") from error
    evidence_raw = payload.get("permission_evidence_refs")
    if not isinstance(evidence_raw, list):
        raise ContextGraphSnapshotRepositoryError("Context Graph stored permission evidence is invalid")
    return _compilation_authority(
        snapshot, payload.get("capability_id"), payload.get("capability_revision"), grant, tuple(evidence_raw),
    )
