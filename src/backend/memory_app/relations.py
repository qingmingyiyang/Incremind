"""Manual, reviewable semantic relations between current recognitions.

Relation proposals are intentionally separate from recognition evidence and
retrieval context.  An approved proposal remains an auditable record; graph
projection decides when it is appropriate to expose an approved, current edge.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from uuid import uuid4

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
    SQLiteUnitOfWorkConflict,
)


_PROPOSALS = "recognition_relation_proposals"
_RECOGNITIONS = "recognitions"
_RELATIONS = frozenset({"supports", "refutes", "supplements", "supersedes", "derived_from"})
_DECISIONS = frozenset({"approved", "rejected"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")


class RelationProposalService:
    """Write boundary for human-proposed recognition relationships."""

    def __init__(self, records: SQLiteStructuredRecordStore, *, allow_persona: bool = False, evidence_guard=None) -> None:
        self._records = records
        self._allow_persona = allow_persona
        self._evidence_guard = evidence_guard

    def propose(
        self,
        scope: WorkScope,
        from_id: str,
        to_id: str,
        relation: str,
        evidence: str,
        *, source: str = "manual",
    ) -> dict[str, object]:
        """Save a pending manual proposal without making it active evidence."""
        if source not in {"manual", "model"}:
            raise RecognitionError("proposal source is invalid")
        from_id = _id(from_id, "from_id")
        to_id = _id(to_id, "to_id")
        if from_id == to_id:
            raise RecognitionError("a relation requires two recognitions")
        if not isinstance(relation, str) or relation not in _RELATIONS:
            raise RecognitionError("relation is invalid")
        origin = source
        proposal_id = f"relation-proposal-{uuid4().hex}"
        with self._records.begin() as uow:
            source = _active_recognition(uow, scope, from_id, allow_persona=self._allow_persona)
            target = _active_recognition(uow, scope, to_id, allow_persona=self._allow_persona)
            payload = {
                "id": proposal_id,
                "scope": _scope(scope),
                "project_id": scope.project_id,
                "from_id": from_id,
                "to_id": to_id,
                "relation": relation,
                "evidence": _text(evidence, "evidence"),
                "source": origin,
                "from_revision": source.revision,
                "to_revision": target.revision,
                "state": "pending",
                "created_at": _now(),
                "reviewed_at": None,
            }
            try:
                record = uow.put(_PROPOSALS, proposal_id, payload, expected_revision=0)
            except SQLiteUnitOfWorkConflict as exc:  # pragma: no cover - UUID collision
                raise RecognitionConflict("relation proposal id already exists") from exc
            uow.commit()
        return _proposal(record)

    def propose_evidence(self, scope, target_id, experience_ids, documents, evidence, *, snapshots, conditions=(), proposal_id=None):
        """Suggest a document evidence group supporting one existing insight."""
        from backend.recognition import normalize_conditions
        target = _active_recognition(self._records, scope, target_id, allow_persona=self._allow_persona)
        target_scope = _as_scope(target.payload)
        identity = proposal_id or "evidence-support-" + uuid4().hex
        payload = {"scope": _scope(scope), "project_id": scope.project_id, "target_id": target_id,
            "target_revision": target.revision, "target_project_id": target_scope.project_id,
            "experience_ids": list(experience_ids), "documents": list(documents), "snapshots": snapshots,
            "evidence": _text(evidence, "evidence"), "conditions": list(normalize_conditions(conditions)),
            "state": "pending", "created_at": _now(), "reviewed_at": None}
        with self._records.begin() as tx:
            self._validate_evidence(tx, scope, payload)
            row = tx.put("v2_insight_evidence_support", identity, payload, expected_revision=0)
            tx.commit()
        return {"id": row.object_id, "revision": row.revision, **row.payload}

    def _validate_evidence(self, reader, scope, payload):
        if self._evidence_guard is None:
            raise RecognitionConflict("evidence integrity guard is required")
        private = reader.read("v2_private_scopes", scope.project_id)
        if private and private.payload.get("private"):
            raise RecognitionConflict("support project is private")
        target = _active_recognition(reader, scope, payload["target_id"], allow_persona=self._allow_persona)
        preference = reader.read("recognition_recall_preferences", target.object_id)
        if preference and preference.payload.get("state") == "forgotten":
            raise RecognitionConflict("support target is forgotten")
        if target.revision != payload["target_revision"]:
            raise RecognitionConflict("support target changed")
        for document in payload["documents"]:
            pref = reader.read("v2_document_recall", document["id"])
            stored = reader.read("documents", document["id"])
            if (not stored or stored.payload.get("status") == "archived"
                    or (pref and pref.payload.get("state") == "forgotten" and pref.payload.get("by", "user") == "user")):
                raise RecognitionConflict("support source document is forgotten")
        expected = {(scope.project_id, "experience", identity) for identity in payload["experience_ids"]}
        expected.add((payload["target_project_id"], "recognition", payload["target_id"]))
        actual = {(item["project_id"], root["type"], root["id"]) for item in payload["snapshots"] for root in item["snapshot"]["roots"]}
        if actual != expected:
            raise RecognitionConflict("support source snapshot is incomplete")
        for item in payload["snapshots"]:
            self._evidence_guard(reader, WorkScope(scope.user_id, item["project_id"]), item["snapshot"])

    def list_evidence(self, scope, target_id=None):
        result = []
        for row in self._records.list("v2_insight_evidence_support"):
            actual_scope = _as_scope(row.payload)
            cross_persona = (scope.project_id == "me" and actual_scope.user_id == scope.user_id
                and row.payload.get("target_project_id") == "me" and target_id == row.payload["target_id"])
            if (actual_scope != scope and not cross_persona) or (target_id and row.payload["target_id"] != target_id):
                continue
            try:
                with self._records.begin() as tx:
                    self._validate_evidence(tx, actual_scope, row.payload)
                current = True
            except RecognitionError:
                current = False
            result.append({"id": row.object_id, "revision": row.revision, **row.payload, "current": current})
        return result

    def review_evidence(self, scope, identity, revision, accept):
        with self._records.begin() as tx:
            row = _require_scope(tx, "v2_insight_evidence_support", identity, scope)
            state = "approved" if accept else "rejected"
            if row.payload["state"] == state and revision in {row.revision, row.revision - 1}:
                return {"id": row.object_id, "revision": row.revision, **row.payload}
            if row.revision != revision or row.payload["state"] != "pending":
                raise RecognitionConflict("support review changed")
            if accept:
                self._validate_evidence(tx, scope, row.payload)
            saved = tx.put("v2_insight_evidence_support", identity,
                {**row.payload, "state": state, "reviewed_at": _now()}, expected_revision=row.revision)
            tx.commit()
        return {"id": saved.object_id, "revision": saved.revision, **saved.payload}

    def review(
        self,
        scope: WorkScope,
        proposal_id: str,
        expected_revision: int,
        decision: str,
    ) -> dict[str, object]:
        """Record a human decision after revalidating both proposal endpoints."""
        proposal_id = _id(proposal_id, "proposal_id")
        if not isinstance(decision, str) or decision not in _DECISIONS:
            raise RecognitionError("review decision is invalid")
        _expected_revision(expected_revision)
        with self._records.begin() as uow:
            proposal = _require_scope(uow, _PROPOSALS, proposal_id, scope)
            if proposal.revision != expected_revision:
                raise RecognitionConflict("relation proposal revision conflicted")
            if proposal.payload.get("state") != "pending":
                raise RecognitionConflict("only a pending relation proposal can be reviewed")
            if decision == "approved":
                _validate_endpoints(uow, scope, proposal.payload, allow_persona=self._allow_persona)
            updated = {
                **dict(proposal.payload),
                "state": decision,
                "reviewed_at": _now(),
            }
            record = uow.put(_PROPOSALS, proposal_id, updated, expected_revision=proposal.revision)
            uow.commit()
        return _proposal(record)

    def list(self, scope: WorkScope) -> tuple[dict[str, object], ...]:
        """Return all review states for this project; callers filter active edges."""
        return tuple(
            _proposal(record)
            for record in self._records.list(_PROPOSALS)
            if _as_scope(record.payload) == scope
        )


def _validate_endpoints(
    uow: SQLiteStructuredRecordUnitOfWork,
    scope: WorkScope,
    payload: Mapping[str, object],
    *, allow_persona: bool = False,
) -> None:
    source = _active_recognition(uow, scope, _id(payload.get("from_id"), "from_id"), allow_persona=allow_persona)
    target = _active_recognition(uow, scope, _id(payload.get("to_id"), "to_id"), allow_persona=allow_persona)
    if source.revision != _expected_revision(payload.get("from_revision")):
        raise RecognitionConflict("source recognition revision changed")
    if target.revision != _expected_revision(payload.get("to_revision")):
        raise RecognitionConflict("target recognition revision changed")


def _active_recognition(
    uow: SQLiteStructuredRecordUnitOfWork, scope: WorkScope, recognition_id: str, *, allow_persona: bool = False
) -> SQLiteStructuredRecord:
    record = uow.read(_RECOGNITIONS, recognition_id)
    actual = _as_scope(record.payload) if record is not None else None
    if actual != scope and not (allow_persona and actual == WorkScope(scope.user_id, "me")):
        raise RecognitionConflict("record is unavailable in this work scope")
    if record.payload.get("state") != "active":
        raise RecognitionConflict("relation endpoint is no longer active")
    return record


def _require_scope(
    uow: SQLiteStructuredRecordUnitOfWork, collection: str, object_id: str, scope: WorkScope
) -> SQLiteStructuredRecord:
    record = uow.read(collection, object_id)
    if record is None or _as_scope(record.payload) != scope:
        raise RecognitionConflict("record is unavailable in this work scope")
    return record


def _proposal(record: SQLiteStructuredRecord) -> dict[str, object]:
    payload = record.payload
    return {
        "id": record.object_id,
        "revision": record.revision,
        "project_id": _as_scope(payload).project_id,
        "from_id": _id(payload.get("from_id"), "from_id"),
        "to_id": _id(payload.get("to_id"), "to_id"),
        "relation": _relation(payload.get("relation")),
        "evidence": _text(payload.get("evidence"), "evidence"),
        "source": payload.get("source", "manual"),
        "from_revision": _expected_revision(payload.get("from_revision")),
        "to_revision": _expected_revision(payload.get("to_revision")),
        "state": _state(payload.get("state")),
        "created_at": _text(payload.get("created_at"), "created_at"),
        "reviewed_at": payload.get("reviewed_at"),
    }


def _scope(scope: WorkScope) -> dict[str, str | None]:
    return {"user_id": scope.user_id, "project_id": scope.project_id}


def _as_scope(payload: Mapping[str, object]) -> WorkScope:
    value = payload.get("scope")
    if not isinstance(value, Mapping):
        raise RecognitionError("stored relation proposal scope is invalid")
    scope = WorkScope(value.get("user_id"), value.get("project_id"))
    if payload.get("project_id") != scope.project_id:
        raise RecognitionError("stored relation proposal project scope is invalid")
    return scope


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise RecognitionError(f"{label} is invalid")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 100_000:
        raise RecognitionError(f"{label} is invalid")
    return value.strip()


def _expected_revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RecognitionError("revision is invalid")
    return value


def _relation(value: object) -> str:
    if not isinstance(value, str) or value not in _RELATIONS:
        raise RecognitionError("stored relation is invalid")
    return str(value)


def _state(value: object) -> str:
    if not isinstance(value, str) or value not in {"pending", *_DECISIONS}:
        raise RecognitionError("stored relation proposal state is invalid")
    return str(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
