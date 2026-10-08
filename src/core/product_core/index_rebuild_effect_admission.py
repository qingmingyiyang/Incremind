"""Caller-owned Effect-v2 admission evidence for Index rebuilds.

This is intentionally a domain seam only.  It neither imports the legacy
Index Job runtime nor persists or dispatches an Effect; the command boundary
must atomically persist the returned request fact and plan the returned intent.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from core.effect_log import (
    EFFECT_V2, NOT_APPLICABLE, V2_REVISION_KEYS, Effect, EffectClass, EffectIntent,
    EffectLog,
    GateDecision, GateDecisionFact,
)


EFFECT_KIND = "index_rebuild"
INTENT_SCHEMA = "index-rebuild-intent-v2"
REQUEST_SCHEMA = "index-rebuild-request-v2"
RECEIPT_KIND = "index-rebuild.receipt"
RECEIPT_SCHEMA = "index-rebuild-receipt-v2"
_POLICY_REVISION = "index-rebuild-policy-v2"
REQUEST_TABLE = "index_rebuild_effect_request"
RESERVATION_TABLE = "index_rebuild_effect_reservation"
RECEIPT_TABLE = "index_rebuild_effect_receipt"
_MAX_TOKEN_LENGTH = 160
_MAX_SOURCE_REFS = 64
_FORBIDDEN_MARKERS = ("secret", "token", "cookie", "authorization", "header", "password")


@dataclass(frozen=True, slots=True)
class IndexRebuildEffectAdmission:
    """Frozen gate, intent and request fact for a single rebuild operation."""

    gate_decision_id: str
    gate_fact: GateDecisionFact
    intent: EffectIntent
    request: Mapping[str, object]
    admitted_at: int


@dataclass(frozen=True, slots=True)
class SQLiteIndexRebuildEffectAdmission:
    """Atomically plan an Effect-v2 Index operation and persist its evidence.

    The caller owns the transaction.  Schema bootstrap, Core Gate/Intent/Effect
    facts, and the domain request either all commit together or all roll back.
    """

    effect_log: EffectLog

    def admit_in_connection(
        self, connection: sqlite3.Connection, admission: IndexRebuildEffectAdmission, *, now: int,
    ) -> tuple[Effect, bool]:
        if not connection.in_transaction:
            raise RuntimeError("index rebuild admission requires caller-owned transaction")
        if now != admission.admitted_at:
            raise ValueError("index rebuild admission time drifted")
        initialize_index_rebuild_effect_schema_in_connection(connection)
        effect, created = self.effect_log.plan_v2_in_connection(
            connection, admission.intent, gate_decision_id=admission.gate_decision_id,
            gate_fact=admission.gate_fact, now=now,
        )
        persist_request_in_connection(
            connection, effect=effect, request=admission.request, recorded_at=now,
        )
        return effect, created


def initialize_index_rebuild_effect_schema_in_connection(connection: sqlite3.Connection) -> None:
    """Create only domain tables inside the caller's already-open transaction."""
    if not connection.in_transaction:
        raise RuntimeError("index rebuild schema bootstrap requires caller-owned transaction")
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {REQUEST_TABLE}("
        "operation_id TEXT PRIMARY KEY,request_ref TEXT NOT NULL UNIQUE,request_json TEXT NOT NULL,"
        "recorded_at INTEGER NOT NULL,FOREIGN KEY(operation_id) REFERENCES effect(operation_id))"
    )
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {RESERVATION_TABLE}("
        "operation_id TEXT PRIMARY KEY,reservation_json TEXT NOT NULL,recorded_at INTEGER NOT NULL,"
        "FOREIGN KEY(operation_id) REFERENCES effect(operation_id))"
    )
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE}("
        "operation_id TEXT PRIMARY KEY,receipt_json TEXT NOT NULL,recorded_at INTEGER NOT NULL,"
        "FOREIGN KEY(operation_id) REFERENCES effect(operation_id))"
    )
    for table in (REQUEST_TABLE, RESERVATION_TABLE, RECEIPT_TABLE):
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_deny_update "
            f"BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'index rebuild fact is immutable'); END"
        )
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_deny_delete "
            f"BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'index rebuild fact is immutable'); END"
        )


class IndexRebuildEffectAdmissionFactory:
    """Freeze request, manifest and ledger evidence before any Effect exists."""

    def __init__(self, *, admitted_at: int) -> None:
        if not isinstance(admitted_at, int) or isinstance(admitted_at, bool) or admitted_at < 0:
            raise ValueError("admitted_at must be a non-negative Unix timestamp")
        self._admitted_at = admitted_at

    def build(
        self, *, request: Mapping[str, object], manifest: Mapping[str, object], ledger: Mapping[str, object],
    ) -> IndexRebuildEffectAdmission:
        request_copy = _exact_request(request)
        manifest_copy = _exact_manifest(manifest)
        ledger_copy = _exact_ledger(ledger)
        request_id = _token(request_copy, "id")
        manifest_id = _token(manifest_copy, "id")
        ledger_id = _token(ledger_copy, "id")
        if request_copy["backend_kind"] != "sqlite_fts5":
            raise ValueError("index rebuild requires sqlite_fts5 backend")
        request_ref = f"facts:index-rebuild/request/{request_id}"
        manifest_ref = f"facts:index-rebuild/manifest/{manifest_id}"
        ledger_ref = f"facts:index-rebuild/ledger/{ledger_id}"
        manifest_revision = _revision("index-rebuild-manifest", manifest_copy)
        ledger_revision = _revision("index-rebuild-ledger", ledger_copy)
        request_revision = _revision("index-rebuild-request", request_copy)
        root_id = f"index-rebuild-{request_id}"
        gate_id = f"gate:index-rebuild/{request_id}"
        intent_ref = f"intent:index-rebuild/{request_id}"
        revisions = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
        revisions.update({
            "policy": _POLICY_REVISION,
            "boundary": "index-rebuild-boundary-v2",
            "capability": "index-rebuild-capability-v2",
            "context_manifest": manifest_revision,
            "handler": "index-rebuild-handler-v2",
            "budget": ledger_revision,
            "workflow": "index-rebuild-workflow-v2",
        })
        gate = GateDecisionFact(
            GateDecision.ALLOW, "rule:index-rebuild-verified-ledger-v2", f"scope:index-rebuild/{request_id}",
            {"request_ref": request_ref, "manifest_ref": manifest_ref, "ledger_ref": ledger_ref,
             "rebuild_budget": 1}, "scope:index-rebuild-secret/not-applicable", _POLICY_REVISION,
        )
        domain_request = MappingProxyType({
            "schema_version": REQUEST_SCHEMA,
            "request_ref": request_ref,
            "request_revision": request_revision,
            "request": request_copy,
            "manifest_ref": manifest_ref,
            "manifest_revision": manifest_revision,
            "manifest": manifest_copy,
            "ledger_ref": ledger_ref,
            "ledger_revision": ledger_revision,
            "ledger": ledger_copy,
        })
        intent = EffectIntent(
            session_id="index-rebuild", root_id=root_id, step_key="execute", kind=EFFECT_KIND,
            effect_class=EffectClass.QUERYABLE, intent_ref=intent_ref, gate_decision_id=gate_id,
            rev_set=revisions,
            payload={"request_ref": request_ref, "manifest_ref": manifest_ref, "ledger_ref": ledger_ref,
                     "admission_ref": request_ref, "mode": "admit", "attempt_index": 0},
            contract_version=EFFECT_V2, intent_schema_version=INTENT_SCHEMA,
            expected_receipt_kind=RECEIPT_KIND, expected_receipt_schema_version=RECEIPT_SCHEMA,
        )
        return IndexRebuildEffectAdmission(gate_id, gate, intent, domain_request, self._admitted_at)


def _exact_request(value: Mapping[str, object]) -> dict[str, object]:
    expected = {"id", "backend_kind", "reason", "source_refs"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("index rebuild request fields are not exact")
    copied = dict(value)
    for key in ("id", "backend_kind", "reason"):
        _token(copied, key)
    refs = copied["source_refs"]
    if not isinstance(refs, (list, tuple)) or not refs or len(refs) > _MAX_SOURCE_REFS:
        raise ValueError("index rebuild source_refs require bounded opaque revisions")
    copied["source_refs"] = [_opaque_reference(item, "source_ref") for item in refs]
    return copied


def _exact_manifest(value: Mapping[str, object]) -> dict[str, object]:
    copied = _exact_scalar_evidence(value, {"id", "backend_kind", "source_fingerprint"}, "manifest")
    if copied["backend_kind"] != "sqlite_fts5":
        raise ValueError("index rebuild manifest backend is unsupported")
    return copied


def _exact_ledger(value: Mapping[str, object]) -> dict[str, object]:
    copied = _exact_scalar_evidence(value, {"id", "source_fingerprint", "entry_count"}, "ledger")
    count = copied["entry_count"]
    if not isinstance(count, int) or isinstance(count, bool) or count < 0 or count > 10_000_000:
        raise ValueError("index rebuild ledger entry_count is invalid")
    return copied


def _exact_scalar_evidence(value: Mapping[str, object], expected: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"index rebuild {label} fields are not exact")
    copied = dict(value)
    for key, item in copied.items():
        if key != "entry_count":
            _opaque_reference(item, f"{label}.{key}")
    return copied


def validate_domain_request(effect: Effect, value: Mapping[str, object]) -> dict[str, object]:
    """Verify the persisted request is exactly the Effect's frozen evidence."""
    fields = {
        "schema_version", "request_ref", "request_revision", "request",
        "manifest_ref", "manifest_revision", "manifest", "ledger_ref",
        "ledger_revision", "ledger",
    }
    if not isinstance(value, Mapping) or set(value) != fields or value.get("schema_version") != REQUEST_SCHEMA:
        raise ValueError("index rebuild request fields are not exact")
    if effect.contract_version != EFFECT_V2 or effect.kind != EFFECT_KIND or effect.intent_schema_version != INTENT_SCHEMA:
        raise ValueError("index rebuild Effect contract drifted")
    result = dict(value)
    request = _exact_request(_require_mapping(result, "request"))
    manifest = _exact_manifest(_require_mapping(result, "manifest"))
    ledger = _exact_ledger(_require_mapping(result, "ledger"))
    request_id = _token(request, "id")
    expected_refs = {
        "request_ref": f"facts:index-rebuild/request/{request_id}",
        "manifest_ref": f"facts:index-rebuild/manifest/{_token(manifest, 'id')}",
        "ledger_ref": f"facts:index-rebuild/ledger/{_token(ledger, 'id')}",
    }
    if any(result[key] != expected for key, expected in expected_refs.items()):
        raise ValueError("index rebuild request reference drifted")
    if effect.intent_ref != f"intent:index-rebuild/{request_id}":
        raise ValueError("index rebuild request identity drifted")
    expected_revisions = {
        "request_revision": _revision("index-rebuild-request", request),
        "manifest_revision": _revision("index-rebuild-manifest", manifest),
        "ledger_revision": _revision("index-rebuild-ledger", ledger),
    }
    if any(result[key] != expected for key, expected in expected_revisions.items()):
        raise ValueError("index rebuild request revision drifted")
    result.update({"request": request, "manifest": manifest, "ledger": ledger})
    return result


def persist_request_in_connection(
    connection: sqlite3.Connection, *, effect: Effect, request: Mapping[str, object], recorded_at: int,
) -> None:
    if not connection.in_transaction:
        raise RuntimeError("index rebuild request requires caller-owned transaction")
    if not isinstance(recorded_at, int) or isinstance(recorded_at, bool) or recorded_at < 0:
        raise ValueError("index rebuild recorded_at is invalid")
    domain = validate_domain_request(effect, request)
    encoded = canonical(domain)
    existing = connection.execute(
        f"SELECT request_ref,request_json FROM {REQUEST_TABLE} WHERE operation_id=?", (effect.operation_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != (domain["request_ref"], encoded):
            raise ValueError("index rebuild admission replay drifted")
        return
    connection.execute(
        f"INSERT INTO {REQUEST_TABLE}(operation_id,request_ref,request_json,recorded_at) VALUES(?,?,?,?)",
        (effect.operation_id, domain["request_ref"], encoded, recorded_at),
    )


def _require_mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise ValueError(f"index rebuild {key} is invalid")
    return item


def _token(value: Mapping[str, object], key: str) -> str:
    return _opaque_reference(value.get(key), key)


def _opaque_reference(item: object, field: str) -> str:
    """Accept identifiers and revisions, never paths, credentials or payloads."""
    if (not isinstance(item, str) or not item or item != item.strip() or len(item) > _MAX_TOKEN_LENGTH
            or any(char.isspace() or ord(char) < 32 for char in item)
            or item.startswith(("/", "\\")) or ":\\" in item or "/" in item or "\\" in item
            or any(marker in item.lower() for marker in _FORBIDDEN_MARKERS)):
        raise ValueError(f"index rebuild {field} must be an opaque non-secret reference")
    return item


def _revision(prefix: str, value: Mapping[str, object]) -> str:
    encoded = canonical(value).encode("utf-8")
    return f"{prefix}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
