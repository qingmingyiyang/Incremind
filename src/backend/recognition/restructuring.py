"""Frozen, human-reviewed recognition restructuring proposals.

This module deliberately contains no model client.  A model may produce the
``outputs`` passed to :meth:`save`, but it cannot publish them: the captured
versions are checked again inside the review transaction before the existing
recognition lifecycle authority is invoked.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any
from uuid import uuid4

from core.storage_provider import SQLiteStructuredRecord, SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict

from .service import (
    _EXPERIENCES,
    _RECOGNITIONS,
    _as_scope,
    normalize_conditions,
    _content,
    _id,
    _ids,
    _now,
    _revision,
    _scope,
    _segment,
    RecognitionConflict,
    RecognitionError,
    RecognitionService,
    WorkScope,
)


_PROPOSALS = "recognition_restructure_proposals"
_TASKS = "recognition_tasks"
_OPERATIONS = frozenset({"revise", "split", "merge", "supersede", "revoke", "noop"})
_TERMINAL = frozenset({"approved", "rejected"})
_MAX_METADATA_BYTES = 32_000
_SENSITIVE_METADATA = frozenset({"api_key", "apikey", "authorization", "credential", "password", "secret", "token"})


class RestructureProposalService:
    """Proposal authority layered on a :class:`RecognitionService` store."""

    def __init__(self, recognitions: RecognitionService | SQLiteStructuredRecordStore) -> None:
        # Routes normally share the already-created RecognitionService.  The
        # store form keeps this authority convenient for focused callers while
        # still using the exact same lifecycle implementation.
        self._recognitions = (
            recognitions if isinstance(recognitions, RecognitionService) else RecognitionService(recognitions)
        )
        self._records: SQLiteStructuredRecordStore = self._recognitions.records

    def capture(
        self,
        *,
        scope: WorkScope,
        recognition_ids: Sequence[str],
        expected_revisions: Mapping[str, int],
        pending_output: bool = False,
    ) -> dict[str, object]:
        """Freeze active parent payloads and their recursive evidence chain."""
        ids = _ids(recognition_ids, "recognition_ids")
        revisions = _expected_revisions(ids, expected_revisions)
        with self._records.begin() as uow:
            recognitions: dict[str, SQLiteStructuredRecord] = {}
            experiences: dict[str, SQLiteStructuredRecord] = {}
            candidates: dict[str, SQLiteStructuredRecord] = {}

            def visit(recognition_id: str, expected: int | None = None) -> None:
                if recognition_id in recognitions:
                    if expected is not None and recognitions[recognition_id].revision != expected:
                        raise RecognitionConflict("recognition revision conflicted")
                    return
                record = self._recognitions._require_scope(uow, _RECOGNITIONS, recognition_id, scope)
                if record.payload.get("state") != "active":
                    raise RecognitionConflict("recognition is no longer active")
                if expected is not None and record.revision != expected:
                    raise RecognitionConflict("record revision conflicted")
                recognitions[recognition_id] = record
                experience_ids = _ids(record.payload.get("source_experience_ids"), "source_experience_ids")
                source_ids = _ids(record.payload.get("source_recognition_ids"), "source_recognition_ids")
                self._recognitions._assert_sources_active(
                    uow,
                    scope,
                    experience_ids,
                    source_ids,
                    experience_revisions={item: int(record.payload["source_experience_revisions"][item]) for item in experience_ids},
                    recognition_revisions={item: int(record.payload["source_recognition_revisions"][item]) for item in source_ids},
                    excluded_recognition_id=recognition_id,
                )
                for experience_id in experience_ids:
                    experience = self._recognitions._require_scope(uow, _EXPERIENCES, experience_id, scope)
                    if experience.payload.get("state") != "active":
                        raise RecognitionConflict("a source experience has been revoked")
                    experiences[experience_id] = experience
                for source_id in source_ids:
                    visit(source_id, int(record.payload["source_recognition_revisions"][source_id]))

            for item_id in ids:
                candidate = uow.read("recognition_candidates", item_id) if pending_output else None
                if candidate is None:
                    visit(item_id, revisions[item_id])
                    continue
                if _as_scope(candidate.payload) != scope or candidate.payload.get("state") != "pending" or candidate.revision != revisions[item_id]:
                    raise RecognitionConflict("pending restructure input changed")
                candidates[item_id] = candidate
                experience_ids = _ids(candidate.payload.get("source_experience_ids"), "source_experience_ids")
                source_ids = _ids(candidate.payload.get("source_recognition_ids"), "source_recognition_ids")
                self._recognitions._assert_sources_active(uow, scope, experience_ids, source_ids,
                    experience_revisions=candidate.payload["source_experience_revisions"],
                    recognition_revisions=candidate.payload["source_recognition_revisions"])
                for source_id in source_ids:
                    visit(source_id, candidate.payload["source_recognition_revisions"][source_id])
                for experience_id in experience_ids:
                    experiences[experience_id] = self._recognitions._require_scope(uow, _EXPERIENCES, experience_id, scope)
            states = _capture_memory_states(uow, scope, {"recognitions": [_snapshot_record(r) for r in recognitions.values()],
                "experiences": [_snapshot_record(r) for r in experiences.values()]}) if pending_output else []
            uow.commit()
        return {
            **({"pending_output": True, "memory_states": states, "candidates": [_snapshot_record(r) for r in candidates.values()]} if pending_output else {}),
            "schema_version": 2 if pending_output else 1,
            "scope": _scope(scope),
            "project_id": scope.project_id,
            "captured_at": _now(),
            "target_recognition_ids": list(ids),
            "target_revisions": revisions,
            "recognitions": [_snapshot_record(record) for record in recognitions.values()],
            "experiences": [_snapshot_record(record) for record in experiences.values()],
        }

    def save(
        self,
        *,
        scope: WorkScope,
        proposal_id: str,
        snapshot: Mapping[str, object],
        operation: str,
        outputs: Sequence[Mapping[str, object]],
        reason: str,
        step_metadata: Mapping[str, object] | None = None,
        origin_task_id: str | None = None,
    ) -> dict[str, object]:
        """Save with a standalone transaction; Turn commits may share theirs."""
        with self._records.begin() as uow:
            result = self.save_in_uow(uow, scope=scope, proposal_id=proposal_id,
                snapshot=snapshot, operation=operation, outputs=outputs, reason=reason,
                step_metadata=step_metadata, origin_task_id=origin_task_id)
            uow.commit()
        return result

    def save_in_uow(
        self,
        uow,
        *,
        scope: WorkScope,
        proposal_id: str,
        snapshot: Mapping[str, object],
        operation: str,
        outputs: Sequence[Mapping[str, object]],
        reason: str,
        step_metadata: Mapping[str, object] | None = None,
        origin_task_id: str | None = None,
    ) -> dict[str, object]:
        """Persist a pending proposal after verifying its frozen input remains current."""
        _segment("proposal_id", proposal_id)
        validated = _validate_snapshot(scope, snapshot)
        operation = _operation(operation)
        output_rows = _validate_outputs(operation, outputs, validated, proposal_id=proposal_id)
        reason = _reason(reason)
        metadata = _public_metadata(step_metadata)
        origin_task_id = _origin_task_id(origin_task_id)
        if origin_task_id is not None:
            _require_origin_task(uow, scope, origin_task_id, proposal_id, require_completed=False)
        existing = uow.read(_PROPOSALS, proposal_id)
        if existing is not None:
            if _as_scope(existing.payload) != scope:
                raise RecognitionConflict("restructure proposal id already exists")
            comparable = {
                "snapshot": validated, "operation": operation, "outputs": output_rows,
                "reason": reason, "step_metadata": metadata, "origin_task_id": origin_task_id,
            }
            stored = {key: existing.payload.get(key) for key in comparable}
            if stored == comparable:
                return _public(existing)
            raise RecognitionConflict("restructure proposal id already exists with different content")
        _assert_snapshot_current(self._recognitions, uow, scope, validated)
        payload = {
            "id": proposal_id,
            "scope": _scope(scope),
            "project_id": scope.project_id,
            "state": "pending",
            "operation": operation,
            "snapshot": validated,
            "outputs": output_rows,
            "reason": reason,
            "diff": _diff(validated, operation, output_rows),
            "step_metadata": metadata,
            # Manual proposals deliberately retain ``None``.  Older manual
            # rows did not have this field and are read as the same value.
            "origin_task_id": origin_task_id,
            "target_recognition_ids": list(validated["target_recognition_ids"]),
            # Erasure needs every recursively captured dependency, not only targets.
            "input_recognition_ids": [item["id"] for item in validated["recognitions"]],
            "input_experience_ids": [item["id"] for item in validated["experiences"]],
            "output_recognition_ids": [item["recognition_id"] for item in output_rows if "recognition_id" in item],
            "created_at": _now(),
            "reviewed_at": None,
            "reviewed_by": None,
            "result_recognition_ids": [],
        }
        try:
            record = uow.put(_PROPOSALS, proposal_id, payload, expected_revision=0)
        except SQLiteUnitOfWorkConflict as exc:
            raise RecognitionConflict("restructure proposal id already exists") from exc
        return _public(record)

    def get(self, *, scope: WorkScope, proposal_id: str) -> dict[str, object]:
        _segment("proposal_id", proposal_id)
        with self._records.begin() as uow:
            record = uow.read(_PROPOSALS, proposal_id)
            if record is None or _as_scope(record.payload) != scope:
                raise RecognitionConflict("restructure proposal is unavailable in this work scope")
            _require_visible_origin_task(uow, scope, record)
            uow.commit()
        return _public(record)

    def list(self, *, scope: WorkScope, include_terminal: bool = True) -> tuple[dict[str, object], ...]:
        with self._records.begin() as uow:
            visible = []
            for item in uow.list(_PROPOSALS):
                if _as_scope(item.payload) != scope or (not include_terminal and item.payload.get("state") != "pending"):
                    continue
                try:
                    _require_visible_origin_task(uow, scope, item)
                except RecognitionConflict:
                    continue
                visible.append(_public(item))
            uow.commit()
        return tuple(visible)

    def review(
        self,
        *,
        scope: WorkScope,
        proposal_id: str,
        expected_revision: int,
        decision: str,
        reviewer: str,
    ) -> dict[str, object]:
        """Reject or atomically apply a frozen proposal.

        Repeating a successful decision is idempotent.  Its original storage
        revision is accepted so clients can safely retry after a lost response.
        """
        _segment("proposal_id", proposal_id)
        _segment("reviewer", reviewer)
        if decision == "approved":
            decision = "approve"
        elif decision == "rejected":
            decision = "reject"
        if decision not in {"approve", "reject"}:
            raise RecognitionError("restructure review decision is invalid")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            raise RecognitionError("proposal revision is invalid")
        with self._records.begin() as uow:
            proposal = self._recognitions._require_scope(uow, _PROPOSALS, proposal_id, scope)
            _require_visible_origin_task(uow, scope, proposal)
            if proposal.payload.get("state") in _TERMINAL:
                prior = "approve" if proposal.payload.get("state") == "approved" else "reject"
                if prior == decision and expected_revision in {proposal.revision, proposal.revision - 1}:
                    uow.commit()
                    return _public(proposal)
                raise RecognitionConflict("restructure proposal was already reviewed")
            self._recognitions._expect(proposal, expected_revision)
            snapshot = _validate_snapshot(scope, proposal.payload.get("snapshot"))
            if decision == "reject":
                record = uow.put(
                    _PROPOSALS,
                    proposal_id,
                    {**dict(proposal.payload), "state": "rejected", "reviewed_at": _now(), "reviewed_by": reviewer},
                    expected_revision=proposal.revision,
                )
                uow.commit()
                return _public(record)
            _assert_snapshot_current(self._recognitions, uow, scope, snapshot)
            operation = _operation(proposal.payload.get("operation"))
            outputs = _validate_outputs(operation, proposal.payload.get("outputs"), snapshot)
            if snapshot.get("pending_output"):
                if operation != "merge" or len(snapshot["target_recognition_ids"]) < 2:
                    raise RecognitionError("pending output only supports merging")
                output = outputs[0]
                # A consolidation merge retains the entire frozen evidence union.
                wanted = {r["id"] for r in snapshot["experiences"]}
                recognitions = {r["id"] for r in snapshot["recognitions"]} - set(snapshot["target_recognition_ids"])
                if set(output["source_experience_ids"]) != wanted or set(output["source_recognition_ids"]) != recognitions:
                    raise RecognitionError("pending merge must retain all sources")
                candidate = self._recognitions.propose_in_uow(uow, scope=scope, content=output["content"],
                    conditions=output["conditions"], source_experience_ids=output["source_experience_ids"],
                    source_recognition_ids=output["source_recognition_ids"], candidate_id=output["recognition_id"])
                for parent in snapshot["candidates"]:
                    previous = uow.read("v2_candidate_merges", parent["id"])
                    uow.put("v2_candidate_merges", parent["id"], {"candidate_id": candidate.id,
                        "proposal_id": proposal_id, "project_id": scope.project_id},
                        expected_revision=previous.revision if previous else 0)
                record = uow.put(_PROPOSALS, proposal_id,
                    {**dict(proposal.payload), "state": "approved", "reviewed_at": _now(), "reviewed_by": reviewer,
                        "result_candidate_ids": [candidate.id]}, expected_revision=proposal.revision)
                uow.commit()
                return _public(record)
            result = self._recognitions._apply_restructure_in_uow(
                uow,
                scope=scope,
                operation=operation,
                target_ids=snapshot["target_recognition_ids"],
                expected_revisions=snapshot["target_revisions"],
                outputs=outputs,
                reason=str(proposal.payload["reason"]),
            )
            record = uow.put(
                _PROPOSALS,
                proposal_id,
                {**dict(proposal.payload), "state": "approved", "reviewed_at": _now(), "reviewed_by": reviewer,
                 "result_recognition_ids": [item.id for item in result]},
                expected_revision=proposal.revision,
            )
            uow.commit()
        return _public(record)


def _expected_revisions(ids: Sequence[str], values: Mapping[str, int]) -> dict[str, int]:
    if not isinstance(values, Mapping) or set(values) != set(ids):
        raise RecognitionError("recognition ids and expected revisions are invalid")
    return {item_id: _revision(values[item_id]) for item_id in ids}


def _origin_task_id(value: object) -> str | None:
    if value is None:
        return None
    _segment("origin_task_id", value)
    return str(value)


def _require_origin_task(
    uow: Any,
    scope: WorkScope,
    task_id: str,
    proposal_id: str,
    *,
    require_completed: bool,
) -> SQLiteStructuredRecord:
    """Return the model task only when it owns this proposal in this scope.

    A proposal created by a Turn is not public merely because its proposal row
    exists.  The Turn remains the authoritative execution boundary: saving may
    occur while it is running (inside the Turn's transaction), whereas reading
    or reviewing requires the completed receipt projection.
    """
    task = uow.read(_TASKS, task_id)
    if task is None or not _task_matches_scope(task.payload, scope):
        raise RecognitionConflict("model restructure task is unavailable")
    if task.payload.get("kind") != "restructure" or task.payload.get("proposal_id") != proposal_id:
        raise RecognitionConflict("model restructure task is unavailable")
    state = task.payload.get("state")
    allowed = {"completed"} if require_completed else {"running", "result_ready", "completed"}
    if state not in allowed:
        raise RecognitionConflict("model restructure task is unavailable")
    return task


def _task_matches_scope(payload: Mapping[str, object], scope: WorkScope) -> bool:
    if payload.get("project_id") != scope.project_id:
        return False
    try:
        return _as_scope(payload) == scope
    except RecognitionError:
        return False


def _require_visible_origin_task(uow: Any, scope: WorkScope, proposal: SQLiteStructuredRecord) -> None:
    """Keep manual proposals public and gate task-produced proposals by receipt."""
    origin_task_id = proposal.payload.get("origin_task_id")
    if origin_task_id is None:
        return
    try:
        task_id = _origin_task_id(origin_task_id)
    except RecognitionError as exc:
        raise RecognitionConflict("model restructure task is unavailable") from exc
    _require_origin_task(uow, scope, task_id, proposal.object_id, require_completed=True)


def _snapshot_record(record: SQLiteStructuredRecord) -> dict[str, object]:
    return {"id": record.object_id, "revision": record.revision, "payload": deepcopy(dict(record.payload))}


def _memory_state_keys(scope, snapshot):
    keys = {("v2_private_scopes", scope.project_id)}
    for row in snapshot["recognitions"]:
        keys.add(("recognition_recall_preferences", row["id"]))
        keys.add(("source_egress_recognition_policies", row["id"]))
    for row in snapshot["experiences"]:
        keys.add(("source_egress_experience_policies", row["id"]))
        for ref in row["payload"].get("provenance", {}).get("source_refs", []):
            if ref.get("type") == "document":
                keys.add(("v2_document_recall", ref["id"]))
    return keys


def _capture_memory_states(reader, scope, snapshot):
    states = []
    for collection, identity in sorted(_memory_state_keys(scope, snapshot)):
        row = reader.read(collection, identity)
        if row and ((collection == "v2_private_scopes" and row.payload.get("private"))
                or (collection.startswith("source_egress_") and row.payload.get("allowed_purposes") == [])
                or (row.payload.get("state") == "forgotten" and row.payload.get("by", "user") == "user")):
            raise RecognitionConflict("pending restructure evidence is unavailable")
        states.append({"collection": collection, "id": identity, "revision": row.revision if row else 0})
    return states


def _validate_snapshot(scope: WorkScope, raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("schema_version") not in {1, 2} or _as_scope(raw) != scope:
        raise RecognitionError("restructure snapshot is invalid")
    pending = raw.get("schema_version") == 2
    if pending and raw.get("pending_output") is not True:
        raise RecognitionError("pending restructure mode is invalid")
    targets = _ids(raw.get("target_recognition_ids"), "target_recognition_ids")
    if len(targets) > 12:
        raise RecognitionError("restructure snapshot has too many targets")
    captured_at = raw.get("captured_at")
    if not isinstance(captured_at, str) or not captured_at or len(captured_at) > 128:
        raise RecognitionError("restructure snapshot captured_at is invalid")
    target_revisions = _expected_revisions(targets, raw.get("target_revisions"))
    raw_recognitions = raw.get("recognitions")
    raw_experiences = raw.get("experiences")
    if not isinstance(raw_recognitions, Sequence) or isinstance(raw_recognitions, (str, bytes)) or (not raw_recognitions and not pending):
        raise RecognitionError("restructure snapshot recognitions are invalid")
    if not isinstance(raw_experiences, Sequence) or isinstance(raw_experiences, (str, bytes)):
        raise RecognitionError("restructure snapshot experiences are invalid")
    if len(raw_recognitions) > 256 or len(raw_experiences) > 512:
        raise RecognitionError("restructure snapshot is too large")
    recognitions = [_valid_snapshot_row(item, scope, "recognition") for item in raw_recognitions]
    experiences = [_valid_snapshot_row(item, scope, "experience") for item in raw_experiences]
    if len({item["id"] for item in recognitions}) != len(recognitions) or len({item["id"] for item in experiences}) != len(experiences):
        raise RecognitionError("restructure snapshot has duplicate evidence")
    raw_candidates = raw.get("candidates", []) if pending else []
    if not isinstance(raw_candidates, list) or len(raw_candidates) > 12:
        raise RecognitionError("restructure snapshot candidates are invalid")
    candidates = [_valid_snapshot_row(item, scope, "candidate") for item in raw_candidates]
    if len({r["id"] for r in candidates}) != len(candidates) or any(r["payload"].get("state") != "pending" for r in candidates):
        raise RecognitionError("restructure candidate snapshot is invalid")
    recognition_ids = {item["id"] for item in recognitions}
    candidate_ids = {item["id"] for item in candidates}
    if not candidate_ids.issubset(targets) or candidate_ids & recognition_ids:
        raise RecognitionError("restructure candidate targets are invalid")
    by_id = {item["id"]: item for item in [*recognitions, *candidates]}
    if not set(targets).issubset(by_id) or any(by_id[item]["revision"] != target_revisions[item] for item in targets):
        raise RecognitionError("restructure snapshot targets are invalid")
    # The complete captured graph must be internally closed, which prevents a
    # forged partial snapshot from smuggling references in later.
    exp_ids = {item["id"] for item in experiences}
    for item in [*recognitions, *candidates]:
        payload = item["payload"]
        if payload.get("state") != ("pending" if item in candidates else "active"):
            raise RecognitionError("restructure snapshot recognition is inactive")
        if not set(_ids(payload.get("source_experience_ids"), "source_experience_ids")).issubset(exp_ids):
            raise RecognitionError("restructure snapshot experience chain is incomplete")
        if not set(_ids(payload.get("source_recognition_ids"), "source_recognition_ids")).issubset(recognition_ids):
            raise RecognitionError("restructure snapshot recognition chain is incomplete")
    states = raw.get("memory_states", []) if pending else []
    if pending:
        expected_keys = _memory_state_keys(scope, {"recognitions": recognitions, "experiences": experiences})
        if (not isinstance(states, list) or len(states) != len(expected_keys)
                or {(r.get("collection"), r.get("id")) for r in states} != expected_keys
                or any(type(r.get("revision")) is not int or r["revision"] < 0 for r in states)):
            raise RecognitionError("pending restructure memory states are invalid")
    return {**({"pending_output": True, "memory_states": states, "candidates": candidates} if pending else {}),
            "schema_version": 2 if pending else 1, "scope": _scope(scope), "project_id": scope.project_id,
            "captured_at": captured_at, "target_recognition_ids": list(targets),
            "target_revisions": target_revisions, "recognitions": recognitions, "experiences": experiences}


def _valid_snapshot_row(raw: object, scope: WorkScope, kind: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("payload"), Mapping):
        raise RecognitionError(f"restructure snapshot {kind} is invalid")
    object_id = _id(raw.get("id"), f"snapshot {kind} id")
    revision = _revision(raw.get("revision"))
    payload = dict(raw["payload"])
    if _as_scope(payload) != scope or payload.get("id") != object_id:
        raise RecognitionError(f"restructure snapshot {kind} scope is invalid")
    _content(payload.get("content"))
    return {"id": object_id, "revision": revision, "payload": deepcopy(payload)}


def _assert_snapshot_current(service: RecognitionService, uow: Any, scope: WorkScope, snapshot: Mapping[str, Any]) -> None:
    if snapshot.get("pending_output"):
        for item in snapshot["memory_states"]:
            row = uow.read(item["collection"], item["id"])
            if (row.revision if row else 0) != item["revision"]:
                raise RecognitionConflict("pending restructure memory state changed")
        _capture_memory_states(uow, scope, snapshot)
    for item in snapshot.get("candidates", []):
        if uow.read("v2_candidate_merges", item["id"]):
            raise RecognitionConflict("pending restructure input already merged")
        record = service._require_scope(uow, "recognition_candidates", item["id"], scope)
        if record.revision != item["revision"] or dict(record.payload) != item["payload"] or record.payload.get("state") != "pending":
            raise RecognitionConflict("restructure candidate changed; capture again")
    for item in snapshot["recognitions"]:
        record = service._require_scope(uow, _RECOGNITIONS, item["id"], scope)
        if (record.revision != item["revision"] or record.payload.get("state") != "active"
                or dict(record.payload) != item["payload"]):
            raise RecognitionConflict("restructure input recognition changed; capture again")
    for item in snapshot["experiences"]:
        record = service._require_scope(uow, _EXPERIENCES, item["id"], scope)
        if (record.revision != item["revision"] or record.payload.get("state") != "active"
                or dict(record.payload) != item["payload"]):
            raise RecognitionConflict("restructure input experience changed; capture again")
    service._assert_sources_active(
        uow, scope, [item["id"] for item in snapshot["experiences"]],
        [item["id"] for item in snapshot["recognitions"]],
        experience_revisions={item["id"]: item["revision"] for item in snapshot["experiences"]},
        recognition_revisions={item["id"]: item["revision"] for item in snapshot["recognitions"]},
    )


def _operation(value: object) -> str:
    if not isinstance(value, str) or value not in _OPERATIONS:
        raise RecognitionError("restructure operation is invalid")
    return value


def _validate_outputs(
    operation: str, raw: object, snapshot: Mapping[str, Any], *, proposal_id: str | None = None,
) -> list[dict[str, object]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise RecognitionError("restructure outputs are invalid")
    expected = {"revise": (1, 1), "split": (2, 12), "merge": (1, 1), "supersede": (1, 1), "revoke": (0, 0), "noop": (0, 0)}[operation]
    if not expected[0] <= len(raw) <= expected[1]:
        raise RecognitionError("restructure output count is invalid")
    targets = set(snapshot["target_recognition_ids"])
    allowed_experiences = {item["id"] for item in snapshot["experiences"]}
    allowed_recognitions = {item["id"] for item in snapshot["recognitions"]} - targets
    rows: list[dict[str, object]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise RecognitionError("restructure output is invalid")
        allowed_keys = {"content", "conditions", "source_experience_ids", "source_recognition_ids", "recognition_id"}
        if set(item) - allowed_keys or not {"content", "conditions", "source_experience_ids", "source_recognition_ids"}.issubset(item):
            raise RecognitionError("restructure output shape is invalid")
        experiences = _ids(item["source_experience_ids"], "source_experience_ids")
        recognitions = _ids(item["source_recognition_ids"], "source_recognition_ids")
        if not experiences and not recognitions:
            raise RecognitionError("restructure output requires at least one frozen evidence source")
        if not set(experiences).issubset(allowed_experiences) or not set(recognitions).issubset(allowed_recognitions):
            raise RecognitionError("restructure output cites evidence outside the frozen snapshot")
        row: dict[str, object] = {"content": _content(item["content"]), "conditions": list(normalize_conditions(item["conditions"])),
                                  "source_experience_ids": list(experiences), "source_recognition_ids": list(recognitions)}
        if operation in {"split", "merge", "supersede"}:
            candidate_id = item.get("recognition_id") or (
                f"recognition-{proposal_id}-{index}" if proposal_id is not None else f"recognition-{uuid4().hex}"
            )
            row["recognition_id"] = _id(candidate_id, "recognition_id")
        elif "recognition_id" in item:
            raise RecognitionError("restructure output id is invalid")
        rows.append(row)
    child_ids = [row.get("recognition_id") for row in rows]
    if len(child_ids) != len(set(child_ids)):
        raise RecognitionError("restructure output ids are invalid")
    return rows


def _reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 10_000:
        raise RecognitionError("restructure reason is invalid")
    return value.strip()


def _public_metadata(value: Mapping[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RecognitionError("restructure step metadata is invalid")
    def visit(item: object, path: str = "") -> object:
        if isinstance(item, Mapping):
            result: dict[str, object] = {}
            for key, nested in item.items():
                if not isinstance(key, str) or key.lower().replace("-", "_") in _SENSITIVE_METADATA:
                    raise RecognitionError("restructure step metadata must not contain credentials")
                result[key] = visit(nested, f"{path}.{key}")
            return result
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            return [visit(nested, path) for nested in item]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        raise RecognitionError("restructure step metadata is invalid")
    result = visit(value)
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise RecognitionError("restructure step metadata is too large")
    return result  # type: ignore[return-value]


def _diff(snapshot: Mapping[str, Any], operation: str, outputs: Sequence[Mapping[str, object]]) -> dict[str, object]:
    parents = [item["payload"] for item in [*snapshot["recognitions"], *snapshot.get("candidates", [])] if item["id"] in set(snapshot["target_recognition_ids"])]
    before = {"contents": [item["content"] for item in parents],
              "conditions": sorted({condition for item in parents for condition in normalize_conditions(item.get("conditions", ()))})}
    after = (before if operation == "noop" else {
        "contents": [str(item["content"]) for item in outputs],
        "conditions": sorted({condition for item in outputs for condition in normalize_conditions(item["conditions"])}),
    })
    return {"operation": operation, "retained_contents": sorted(set(before["contents"]) & set(after["contents"])),
            "removed_contents": sorted(set(before["contents"]) - set(after["contents"])),
            "added_contents": sorted(set(after["contents"]) - set(before["contents"])),
            "retained_conditions": sorted(set(before["conditions"]) & set(after["conditions"])),
            "removed_conditions": sorted(set(before["conditions"]) - set(after["conditions"])),
            "added_conditions": sorted(set(after["conditions"]) - set(before["conditions"]))}


def _public(record: SQLiteStructuredRecord) -> dict[str, object]:
    result = deepcopy(dict(record.payload))
    # Legacy manual proposals predate task-origin binding.  Expose the same
    # explicit shape as new manual proposals without rewriting their history.
    result.setdefault("origin_task_id", None)
    result["revision"] = record.revision
    return result
