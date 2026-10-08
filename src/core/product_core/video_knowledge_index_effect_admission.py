"""Durable Effect-v2 admission for video knowledge-index mutations.

The request fact is intentionally limited to opaque identifiers and frozen
authority revisions.  It contains neither video content nor storage locations;
execution remains a separate Core Effect Runtime concern.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from core.effect_log import (
    EFFECT_V2,
    NOT_APPLICABLE,
    V2_REVISION_KEYS,
    Effect,
    EffectClass,
    EffectIntent,
    EffectLog,
    GateDecision,
    GateDecisionFact,
)


EFFECT_KIND = "video_knowledge_index"
INTENT_SCHEMA = "video-knowledge-index-intent-v2"
REQUEST_SCHEMA = "video-knowledge-index-request-v2"
RECEIPT_KIND = "video-knowledge-index.receipt"
RECEIPT_SCHEMA = "video-knowledge-index-receipt-v2"
REQUEST_TABLE = "video_knowledge_index_effect_request"
IDENTITY_TABLE = "video_knowledge_index_effect_identity"
_POLICY_REVISION = "video-knowledge-index-policy-v2"
_OPERATIONS = frozenset({"full_rebuild", "upsert_video", "delete_video", "delete_series"})
_BASE_FIELDS = {
    "id", "operation", "source_revision", "workspace_revision",
    "embedding_profile_revision", "lancedb_schema_revision", "index_generation",
}
_OPTIONAL_FIELDS = {"series_id", "video_id"}
_FORBIDDEN_MARKERS = ("secret", "token", "cookie", "authorization", "password", "header")
_MAX_TOKEN_LENGTH = 160


@dataclass(frozen=True, slots=True)
class VideoKnowledgeIndexEffectAdmission:
    """Frozen Gate, intent and bounded domain request for one index mutation."""

    gate_decision_id: str
    gate_fact: GateDecisionFact
    intent: EffectIntent
    request: Mapping[str, str]
    admitted_at: int


class VideoKnowledgeIndexEffectAdmissionFactory:
    """Build a stable command identity while fencing it with frozen revisions."""

    def __init__(self, *, admitted_at: int) -> None:
        if not isinstance(admitted_at, int) or isinstance(admitted_at, bool) or admitted_at < 0:
            raise ValueError("admitted_at must be a non-negative Unix timestamp")
        self._admitted_at = admitted_at

    def build(self, *, request: Mapping[str, object]) -> VideoKnowledgeIndexEffectAdmission:
        frozen_request = _exact_request(request)
        request_id = frozen_request["id"]
        operation = frozen_request["operation"]
        request_ref = f"facts:video-knowledge-index/request/{request_id}"
        gate_id = f"gate:video-knowledge-index/{request_id}"
        revisions = _revisions(frozen_request)
        gate = GateDecisionFact(
            decision=GateDecision.ALLOW,
            rule_ref="rule:video-knowledge-index-admission-v2",
            scope_ref=f"scope:video-knowledge-index/{request_id}",
            budget_after={
                "request_ref": request_ref,
                "kind": operation,
                "workspace_revision": frozen_request["workspace_revision"],
                "source_revision": frozen_request["source_revision"],
                "index_generation_ref": f"facts:video-knowledge-index/generation/{frozen_request['index_generation']}",
            },
            secret_scope="scope:video-knowledge-index-secret/not-applicable",
            policy_revision=_POLICY_REVISION,
        )
        intent = EffectIntent(
            session_id="video-knowledge-index",
            root_id=f"video-knowledge-index-{request_id}",
            step_key=operation,
            kind=EFFECT_KIND,
            effect_class=EffectClass.QUERYABLE,
            intent_ref=f"intent:video-knowledge-index/{request_id}",
            gate_decision_id=gate_id,
            rev_set=revisions,
            payload={"request_ref": request_ref, "kind": operation, "mode": "admit", "attempt_index": 0},
            contract_version=EFFECT_V2,
            intent_schema_version=INTENT_SCHEMA,
            expected_receipt_kind=RECEIPT_KIND,
            expected_receipt_schema_version=RECEIPT_SCHEMA,
        )
        return VideoKnowledgeIndexEffectAdmission(
            gate_decision_id=gate_id,
            gate_fact=gate,
            intent=intent,
            request=MappingProxyType(frozen_request),
            admitted_at=self._admitted_at,
        )


@dataclass(frozen=True, slots=True)
class SQLiteVideoKnowledgeIndexEffectAdmission:
    """Atomically plan Core Effect-v2 and its immutable bounded request fact."""

    effect_log: EffectLog

    def admit_in_connection(
        self, connection: sqlite3.Connection, admission: VideoKnowledgeIndexEffectAdmission, *, now: int,
    ) -> tuple[Effect, bool]:
        if not connection.in_transaction:
            raise RuntimeError("video knowledge-index admission requires caller-owned transaction")
        if now != admission.admitted_at:
            raise ValueError("video knowledge-index admission time drifted")
        request = validate_domain_request(admission)
        initialize_video_knowledge_index_effect_schema_in_connection(connection)
        effect, created = self.effect_log.plan_v2_in_connection(
            connection, admission.intent, gate_decision_id=admission.gate_decision_id,
            gate_fact=admission.gate_fact, now=now,
        )
        _persist_request_in_connection(connection, effect=effect, request=request, recorded_at=now)
        return effect, created


def initialize_video_knowledge_index_effect_schema_in_connection(connection: sqlite3.Connection) -> None:
    """Bootstrap domain facts inside the already-open caller transaction."""
    if not connection.in_transaction:
        raise RuntimeError("video knowledge-index schema bootstrap requires caller-owned transaction")
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {IDENTITY_TABLE}("
        "identity_id INTEGER PRIMARY KEY AUTOINCREMENT,identity_json TEXT NOT NULL UNIQUE,recorded_at INTEGER NOT NULL)"
    )
    connection.execute(
        f"CREATE TRIGGER IF NOT EXISTS {IDENTITY_TABLE}_deny_update "
        f"BEFORE UPDATE ON {IDENTITY_TABLE} BEGIN "
        "SELECT RAISE(ABORT,'video knowledge-index identity is immutable'); END"
    )
    connection.execute(
        f"CREATE TRIGGER IF NOT EXISTS {IDENTITY_TABLE}_deny_delete "
        f"BEFORE DELETE ON {IDENTITY_TABLE} BEGIN "
        "SELECT RAISE(ABORT,'video knowledge-index identity is immutable'); END"
    )
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {REQUEST_TABLE}("
        "operation_id TEXT PRIMARY KEY,request_ref TEXT NOT NULL UNIQUE,request_json TEXT NOT NULL,"
        "recorded_at INTEGER NOT NULL,FOREIGN KEY(operation_id) REFERENCES effect(operation_id))"
    )
    connection.execute(
        f"CREATE TRIGGER IF NOT EXISTS {REQUEST_TABLE}_deny_update "
        f"BEFORE UPDATE ON {REQUEST_TABLE} BEGIN "
        "SELECT RAISE(ABORT,'video knowledge-index request is immutable'); END"
    )
    connection.execute(
        f"CREATE TRIGGER IF NOT EXISTS {REQUEST_TABLE}_deny_delete "
        f"BEFORE DELETE ON {REQUEST_TABLE} BEGIN "
        "SELECT RAISE(ABORT,'video knowledge-index request is immutable'); END"
    )


def allocate_identity_in_connection(connection: sqlite3.Connection, identity: Mapping[str, str], *, now: int) -> int:
    """Allocate a stable, append-only short identity in the caller transaction."""
    if not connection.in_transaction:
        raise RuntimeError("video knowledge-index identity requires caller-owned transaction")
    initialize_video_knowledge_index_effect_schema_in_connection(connection)
    expected = {"operation", "series_id", "video_id", "source_revision", "workspace_revision", "embedding_profile_revision", "lancedb_schema_revision"}
    if set(identity) != expected or any(not isinstance(value, str) or not value for value in identity.values()):
        raise ValueError("video knowledge-index identity fields are invalid")
    encoded = _canonical(identity)
    connection.execute(f"INSERT OR IGNORE INTO {IDENTITY_TABLE}(identity_json,recorded_at) VALUES(?,?)", (encoded, now))
    row = connection.execute(f"SELECT identity_id FROM {IDENTITY_TABLE} WHERE identity_json=?", (encoded,)).fetchone()
    if row is None or not isinstance(row[0], int) or row[0] < 1:
        raise RuntimeError("video knowledge-index identity allocation failed")
    return row[0]


def validate_domain_request(admission: VideoKnowledgeIndexEffectAdmission) -> dict[str, str]:
    """Bind an unmodified bounded request to its stable Gate and v2 intent."""
    request = _exact_request(admission.request)
    request_id = request["id"]
    operation = request["operation"]
    expected_revisions = _revisions(request)
    expected_payload = {
        "request_ref": f"facts:video-knowledge-index/request/{request_id}",
        "kind": operation,
        "mode": "admit",
        "attempt_index": 0,
    }
    intent = admission.intent
    if (intent.contract_version != EFFECT_V2 or intent.kind != EFFECT_KIND
            or intent.effect_class is not EffectClass.QUERYABLE
            or intent.intent_schema_version != INTENT_SCHEMA
            or intent.expected_receipt_kind != RECEIPT_KIND
            or intent.expected_receipt_schema_version != RECEIPT_SCHEMA):
        raise ValueError("video knowledge-index Effect contract drifted")
    if (admission.gate_decision_id != f"gate:video-knowledge-index/{request_id}"
            or intent.gate_decision_id != admission.gate_decision_id
            or intent.intent_ref != f"intent:video-knowledge-index/{request_id}"
            or intent.root_id != f"video-knowledge-index-{request_id}"
            or intent.step_key != operation):
        raise ValueError("video knowledge-index admission identity drifted")
    if dict(intent.payload) != expected_payload or dict(intent.rev_set) != expected_revisions:
        raise ValueError("video knowledge-index admission authority drifted")
    expected_gate = GateDecisionFact(
        decision=GateDecision.ALLOW,
        rule_ref="rule:video-knowledge-index-admission-v2",
        scope_ref=f"scope:video-knowledge-index/{request_id}",
        budget_after={
            "request_ref": expected_payload["request_ref"],
            "kind": operation,
            "workspace_revision": request["workspace_revision"],
            "source_revision": request["source_revision"],
            "index_generation_ref": (
                f"facts:video-knowledge-index/generation/{request['index_generation']}"
            ),
        },
        secret_scope="scope:video-knowledge-index-secret/not-applicable",
        policy_revision=_POLICY_REVISION,
    )
    if admission.gate_fact != expected_gate:
        raise ValueError("video knowledge-index Gate authority drifted")
    return request


def _persist_request_in_connection(
    connection: sqlite3.Connection, *, effect: Effect, request: Mapping[str, str], recorded_at: int,
) -> None:
    request_ref = f"facts:video-knowledge-index/request/{request['id']}"
    encoded = _canonical(request)
    existing = connection.execute(
        f"SELECT request_ref,request_json FROM {REQUEST_TABLE} WHERE operation_id=?", (effect.operation_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != (request_ref, encoded):
            raise ValueError("video knowledge-index admission replay drifted")
        return
    connection.execute(
        f"INSERT INTO {REQUEST_TABLE}(operation_id,request_ref,request_json,recorded_at) VALUES(?,?,?,?)",
        (effect.operation_id, request_ref, encoded, recorded_at),
    )


def _exact_request(value: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(value, Mapping) or not set(value).issubset(_BASE_FIELDS | _OPTIONAL_FIELDS) or not _BASE_FIELDS <= set(value):
        raise ValueError("video knowledge-index request fields are not exact")
    result = {key: _opaque(value[key], key) for key in value}
    operation = result["operation"]
    if operation not in _OPERATIONS:
        raise ValueError("video knowledge-index operation is unsupported")
    has_series, has_video = "series_id" in result, "video_id" in result
    if operation == "full_rebuild" and (has_series or has_video):
        raise ValueError("full_rebuild cannot target a series or video")
    if operation == "delete_series" and (not has_series or has_video):
        raise ValueError("delete_series requires only series_id")
    if operation in {"upsert_video", "delete_video"} and not (has_series and has_video):
        raise ValueError("video operations require series_id and video_id")
    return result


def _revisions(request: Mapping[str, str]) -> dict[str, str]:
    revisions = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
    revisions.update({
        "policy": _POLICY_REVISION,
        "boundary": "video-knowledge-index-admission-v2",
        "capability": "video-knowledge-index-lancedb-v2",
        "context_manifest": (
            f"video-workspace:{request['workspace_revision']}:source:{request['source_revision']}"
        ),
        "provider": request["embedding_profile_revision"],
        "bundle": request["lancedb_schema_revision"],
        "handler": "video-knowledge-index-handler-v2",
        "budget": "video-knowledge-index-budget-v2",
        "workflow": f"video-knowledge-index-{request['operation']}-v2",
    })
    return revisions


def _opaque(value: object, field: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip() or len(value) > _MAX_TOKEN_LENGTH
            or any(character.isspace() or ord(character) < 32 for character in value)
            or value.startswith(("/", "\\")) or ":\\" in value or "/" in value or "\\" in value
            or any(marker in value.casefold() for marker in _FORBIDDEN_MARKERS)):
        raise ValueError(f"video knowledge-index {field} must be an opaque non-secret token")
    return value


def _canonical(value: Mapping[str, str]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
