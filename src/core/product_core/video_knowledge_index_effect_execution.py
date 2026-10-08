"""Unregistered Effect-v2 Handler and Probe for video knowledge-index writes.

This adapter intentionally contains no LanceDB client and no scheduling loop.
Composition must inject an operation-scoped writer and a Core Effect lease/
cancellation checkpoint.  The Core runtime alone claims, settles and recovers
the Effect; this module stores only immutable domain evidence.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from core.effect_log import EFFECT_V2, Effect, EffectReceipt, EffectState, GateDecision

from .video_knowledge_index_effect_admission import (
    EFFECT_KIND, INTENT_SCHEMA, RECEIPT_KIND, RECEIPT_SCHEMA, REQUEST_TABLE,
)


RESERVATION_TABLE = "video_knowledge_index_effect_reservation"
RECEIPT_TABLE = "video_knowledge_index_effect_receipt"


class VideoKnowledgeIndexEffectExecutionError(ValueError):
    """The domain cannot prove a safe, exact Effect-v2 outcome."""


@dataclass(frozen=True, slots=True)
class VideoKnowledgeIndexQueryOutcome:
    """Read-only evidence from the LanceDB/signature completion query."""

    state: str
    result: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.state not in {"completed", "not_completed", "unknown"}:
            raise ValueError("video knowledge-index query state is invalid")
        if self.state == "completed":
            _validate_result(self.result)
        elif self.result is not None:
            raise ValueError("only completed video knowledge-index queries include result evidence")


@dataclass(frozen=True, slots=True)
class VideoKnowledgeIndexEffectExecutionHandler:
    """Execute an admitted operation behind a mandatory Core checkpoint.

    ``execute`` performs exactly one staged domain mutation and returns bounded
    artifact evidence.  It never receives a legacy execution record, lease, or
    mutable state.
    """

    database: Path | str
    execute: Callable[[str, Mapping[str, str], Callable[[], None]], Mapping[str, object]]
    query_completion: Callable[[str, Mapping[str, str]], VideoKnowledgeIndexQueryOutcome]
    checkpoint_factory: Callable[[Effect], Callable[[], None]]
    authority_validator: Callable[[Effect, Mapping[str, str]], None] | None = None
    after_reservation_write: Callable[[], None] | None = None
    after_domain_write: Callable[[], None] | None = None
    after_receipt_write: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "database", Path(self.database))
        if not callable(self.execute) or not callable(self.query_completion) or not callable(self.checkpoint_factory):
            raise TypeError("video knowledge-index execution requires Core checkpoint, writer and probe")
        if self.authority_validator is not None and not callable(self.authority_validator):
            raise TypeError("video knowledge-index authority validator must be callable")
        _ensure_schema(self.database)

    def __call__(self, effect: Effect) -> EffectReceipt:
        return self.handle(effect)

    def handle(self, effect: Effect) -> EffectReceipt:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = _read_request(connection, effect)
            self._validate_authority(effect, request)
            if _receipt(connection, effect, request) is not None:
                connection.commit()
                return _effect_receipt(effect)
            reserved = _reservation(connection, effect, request) is not None
            if not reserved:
                _write_reservation(connection, effect, request)
            connection.commit()

        if not reserved and self.after_reservation_write is not None:
            self.after_reservation_write()
        checkpoint = self.checkpoint_factory(effect)
        if not callable(checkpoint):
            raise VideoKnowledgeIndexEffectExecutionError("core-checkpoint-invalid")
        # Prove the Core fence both before and after the actual domain write.
        checkpoint()
        self._validate_authority(effect, request)
        if reserved:
            outcome = _query(self.query_completion, effect.operation_id, request)
            if outcome.state == "completed":
                result = _validate_result(outcome.result)
            elif outcome.state == "not_completed":
                result = _validate_result(self.execute(effect.operation_id, request, checkpoint))
            else:
                raise VideoKnowledgeIndexEffectExecutionError("query-unknown")
        else:
            result = _validate_result(self.execute(effect.operation_id, request, checkpoint))
        checkpoint()
        if self.after_domain_write is not None:
            self.after_domain_write()

        # The Core checkpoint owns its own SQLite write transaction.  Check
        # the fence before taking the immutable Receipt transaction so the
        # two authorities never deadlock on the same database.
        checkpoint()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = _read_request(connection, effect)
            self._validate_authority(effect, request)
            if _receipt(connection, effect, request) is None:
                _write_receipt(connection, effect, request, result)
                if self.after_receipt_write is not None:
                    self.after_receipt_write()
            connection.commit()
        return _effect_receipt(effect)

    def _validate_authority(self, effect: Effect, request: Mapping[str, str]) -> None:
        if self.authority_validator is not None:
            self.authority_validator(effect, request)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


@dataclass(frozen=True, slots=True)
class VideoKnowledgeIndexEffectExecutionProbe:
    """Read immutable Effect evidence; never infer completion from artifacts."""

    database: Path | str
    query_completion: Callable[[str, Mapping[str, str]], VideoKnowledgeIndexQueryOutcome]
    authority_validator: Callable[[Effect, Mapping[str, str]], None] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "database", Path(self.database))
        if not callable(self.query_completion):
            raise TypeError("video knowledge-index probe requires a domain completion query")
        _ensure_schema(self.database)

    def __call__(self, effect: Effect) -> tuple[EffectState, str | None]:
        return self.probe(effect)

    def probe(self, effect: Effect) -> tuple[EffectState, str | None]:
        try:
            with sqlite3.connect(self.database, isolation_level=None) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("BEGIN IMMEDIATE")
                request = _read_request(connection, effect)
                if self.authority_validator is not None:
                    self.authority_validator(effect, request)
                receipt = _receipt(connection, effect, request)
                if receipt is not None:
                    connection.commit()
                    return EffectState.SETTLED_OK, str(receipt["receipt_ref"])
                reserved = _reservation(connection, effect, request) is not None
                connection.commit()
            if not reserved:
                return EffectState.PLANNED, f"facts:video-knowledge-index/not-completed/{effect.operation_id}"
            # The LanceDB/signature query can be slow.  Never retain the
            # Effect SQLite write lock while calling it.
            outcome = _query(self.query_completion, effect.operation_id, request)
            if outcome.state == "not_completed":
                return EffectState.PLANNED, f"facts:video-knowledge-index/not-completed/{effect.operation_id}"
            if outcome.state == "unknown":
                return EffectState.UNKNOWN, "error:video-knowledge-index-query-unknown"
            with sqlite3.connect(self.database, isolation_level=None) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("BEGIN IMMEDIATE")
                # A competing Probe may have materialized the same exact
                # receipt while this Probe was querying.  Re-read every input
                # and treat that receipt as the shared immutable winner.
                request = _read_request(connection, effect)
                if self.authority_validator is not None:
                    self.authority_validator(effect, request)
                receipt = _receipt(connection, effect, request)
                if receipt is not None:
                    connection.commit()
                    return EffectState.SETTLED_OK, str(receipt["receipt_ref"])
                if _reservation(connection, effect, request) is None:
                    raise VideoKnowledgeIndexEffectExecutionError("reservation-lost")
                _write_receipt(connection, effect, request, _validate_result(outcome.result))
                connection.commit()
                return EffectState.SETTLED_OK, _receipt_ref(effect.operation_id)
        except (VideoKnowledgeIndexEffectExecutionError, sqlite3.DatabaseError, TypeError, ValueError):
            return EffectState.UNKNOWN, "error:video-knowledge-index-evidence-drift"


def _read_request(connection: sqlite3.Connection, effect: Effect) -> dict[str, str]:
    if (effect.contract_version != EFFECT_V2 or effect.kind != EFFECT_KIND
            or effect.intent_schema_version != INTENT_SCHEMA
            or effect.expected_receipt_kind != RECEIPT_KIND
            or effect.expected_receipt_schema_version != RECEIPT_SCHEMA):
        raise VideoKnowledgeIndexEffectExecutionError("effect-contract-drift")
    intent = connection.execute(
        "SELECT intent_ref,intent_digest,payload_json,schema_version FROM effect_intent_fact WHERE operation_id=?",
        (effect.operation_id,),
    ).fetchone()
    row = connection.execute(
        f"SELECT request_ref,request_json FROM {REQUEST_TABLE} WHERE operation_id=?", (effect.operation_id,),
    ).fetchone()
    if intent is None or row is None or tuple(intent[:2]) != (effect.intent_ref, effect.intent_digest) or intent[3] != INTENT_SCHEMA:
        raise VideoKnowledgeIndexEffectExecutionError("immutable-input-missing")
    try:
        request = json.loads(str(row[1]))
        payload = json.loads(str(intent[2]))
    except json.JSONDecodeError as error:
        raise VideoKnowledgeIndexEffectExecutionError("immutable-input-invalid") from error
    # Reuse admission's exact static validation, then bind each fact to Effect.
    # Rebuilding the immutable request shape is intentionally pure.  It does
    # not plan another Effect or create a second execution authority.
    try:
        request = _request_shape(request)
    except ValueError as error:
        raise VideoKnowledgeIndexEffectExecutionError("request-drift") from error
    request_ref = f"facts:video-knowledge-index/request/{request['id']}"
    expected_payload = {"request_ref": request_ref, "kind": request["operation"], "mode": "admit", "attempt_index": 0}
    if row[0] != request_ref or payload != expected_payload:
        raise VideoKnowledgeIndexEffectExecutionError("intent-request-drift")
    _validate_gate(connection, effect, request, request_ref)
    return request


def _request_shape(value: object) -> dict[str, str]:
    # Keep request validation coupled to the admission contract without making
    # execution depend on a second executable state machine.
    if not isinstance(value, Mapping):
        raise ValueError("request invalid")
    from .video_knowledge_index_effect_admission import VideoKnowledgeIndexEffectAdmissionFactory
    return dict(VideoKnowledgeIndexEffectAdmissionFactory(admitted_at=0).build(request=value).request)


def _validate_gate(connection: sqlite3.Connection, effect: Effect, request: Mapping[str, str], request_ref: str) -> None:
    expected_revisions = {
        "policy": "video-knowledge-index-policy-v2",
        "boundary": "video-knowledge-index-admission-v2",
        "capability": "video-knowledge-index-lancedb-v2",
        "context_manifest": f"video-workspace:{request['workspace_revision']}:source:{request['source_revision']}",
        "provider": request["embedding_profile_revision"], "bundle": request["lancedb_schema_revision"],
        "handler": "video-knowledge-index-handler-v2", "budget": "video-knowledge-index-budget-v2",
        "workflow": f"video-knowledge-index-{request['operation']}-v2",
    }
    if any(effect.rev_set.get(key) != value for key, value in expected_revisions.items()):
        raise VideoKnowledgeIndexEffectExecutionError("revision-drift")
    row = connection.execute(
        "SELECT decision,rule_ref,scope_ref,budget_after,secret_scope,policy_revision FROM effect_gate_fact WHERE decision_id=?",
        (effect.gate_decision_id,),
    ).fetchone()
    expected_budget = _canonical({"request_ref": request_ref, "kind": request["operation"],
        "workspace_revision": request["workspace_revision"], "source_revision": request["source_revision"],
        "index_generation_ref": f"facts:video-knowledge-index/generation/{request['index_generation']}"})
    expected = (GateDecision.ALLOW.value, "rule:video-knowledge-index-admission-v2",
        f"scope:video-knowledge-index/{request['id']}", expected_budget,
        "scope:video-knowledge-index-secret/not-applicable", "video-knowledge-index-policy-v2")
    if row is None or tuple(row) != expected:
        raise VideoKnowledgeIndexEffectExecutionError("gate-drift")


def _query(
    query_completion: Callable[[str, Mapping[str, str]], VideoKnowledgeIndexQueryOutcome],
    operation_id: str, request: Mapping[str, str],
) -> VideoKnowledgeIndexQueryOutcome:
    try:
        outcome = query_completion(operation_id, request)
    except Exception as error:
        raise VideoKnowledgeIndexEffectExecutionError("query-unavailable") from error
    if not isinstance(outcome, VideoKnowledgeIndexQueryOutcome):
        raise VideoKnowledgeIndexEffectExecutionError("query-invalid")
    return outcome


def _reservation(connection: sqlite3.Connection, effect: Effect, request: Mapping[str, str]) -> Mapping[str, object] | None:
    row = connection.execute(f"SELECT reservation_json FROM {RESERVATION_TABLE} WHERE operation_id=?", (effect.operation_id,)).fetchone()
    if row is None:
        return None
    expected = {"operation_id": effect.operation_id, "request_ref": _request_ref(request), "intent_digest": effect.intent_digest}
    try:
        value = json.loads(str(row[0]))
    except json.JSONDecodeError as error:
        raise VideoKnowledgeIndexEffectExecutionError("reservation-drift") from error
    if value != expected:
        raise VideoKnowledgeIndexEffectExecutionError("reservation-drift")
    return value


def _receipt(connection: sqlite3.Connection, effect: Effect, request: Mapping[str, str]) -> Mapping[str, object] | None:
    row = connection.execute(f"SELECT receipt_json FROM {RECEIPT_TABLE} WHERE operation_id=?", (effect.operation_id,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(str(row[0]))
        result = _validate_result(value.get("result"))
    except (json.JSONDecodeError, ValueError) as error:
        raise VideoKnowledgeIndexEffectExecutionError("receipt-drift") from error
    expected = {"operation_id": effect.operation_id, "request_ref": _request_ref(request),
        "receipt_ref": _receipt_ref(effect.operation_id), "receipt_kind": RECEIPT_KIND,
        "receipt_schema_version": RECEIPT_SCHEMA, "intent_schema_version": INTENT_SCHEMA, "result": result}
    if value != expected:
        raise VideoKnowledgeIndexEffectExecutionError("receipt-drift")
    return value


def _write_reservation(connection: sqlite3.Connection, effect: Effect, request: Mapping[str, str]) -> None:
    value = {"operation_id": effect.operation_id, "request_ref": _request_ref(request), "intent_digest": effect.intent_digest}
    connection.execute(f"INSERT INTO {RESERVATION_TABLE}(operation_id,reservation_json,recorded_at) VALUES(?,?,?)", (effect.operation_id, _canonical(value), effect.recorded_at))
    if _reservation(connection, effect, request) != value:
        raise VideoKnowledgeIndexEffectExecutionError("reservation-write-failed")


def _write_receipt(connection: sqlite3.Connection, effect: Effect, request: Mapping[str, str], result: Mapping[str, object]) -> None:
    value = {"operation_id": effect.operation_id, "request_ref": _request_ref(request), "receipt_ref": _receipt_ref(effect.operation_id), "receipt_kind": RECEIPT_KIND, "receipt_schema_version": RECEIPT_SCHEMA, "intent_schema_version": INTENT_SCHEMA, "result": _validate_result(result)}
    connection.execute(f"INSERT INTO {RECEIPT_TABLE}(operation_id,receipt_json,recorded_at) VALUES(?,?,?)", (effect.operation_id, _canonical(value), effect.recorded_at))
    if _receipt(connection, effect, request) != value:
        raise VideoKnowledgeIndexEffectExecutionError("receipt-write-failed")


def _validate_result(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"artifact_ref", "artifact_revision", "entry_count"}:
        raise ValueError("result fields invalid")
    result = dict(value)
    for field in ("artifact_ref", "artifact_revision"):
        token = result[field]
        if not isinstance(token, str) or not token or len(token) > 160 or any(c.isspace() for c in token) or "/" in token or "\\" in token or ":" in token:
            raise ValueError("result reference invalid")
    if not isinstance(result["entry_count"], int) or isinstance(result["entry_count"], bool) or result["entry_count"] < 0:
        raise ValueError("result count invalid")
    return result


def _ensure_schema(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for table, ref in ((RESERVATION_TABLE, "reservation"), (RECEIPT_TABLE, "receipt")):
            connection.execute(f"CREATE TABLE IF NOT EXISTS {table}(operation_id TEXT PRIMARY KEY,{ref}_json TEXT NOT NULL,recorded_at INTEGER NOT NULL,FOREIGN KEY(operation_id) REFERENCES effect(operation_id))")
            connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_deny_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'video knowledge-index {ref} is immutable'); END")
            connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_deny_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'video knowledge-index {ref} is immutable'); END")
        connection.commit()


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _request_ref(request: Mapping[str, str]) -> str: return f"facts:video-knowledge-index/request/{request['id']}"
def _receipt_ref(operation_id: str) -> str: return f"receipt:video-knowledge-index/{operation_id}"
def _effect_receipt(effect: Effect) -> EffectReceipt: return EffectReceipt(_receipt_ref(effect.operation_id), RECEIPT_KIND, RECEIPT_SCHEMA, INTENT_SCHEMA)
