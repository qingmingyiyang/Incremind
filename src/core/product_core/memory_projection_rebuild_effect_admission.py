"""Caller-owned Effect-v2 admission for non-authoritative Memory Projection rebuilds.

This is intentionally an unregistered domain seam.  It does not import the
legacy Job worker or mutate a Job projection; its only execution authority is
the Core Effect and the immutable request fact persisted in the same caller
transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from core.effect_log import (
    EFFECT_V2,
    NOT_APPLICABLE,
    V2_REVISION_KEYS,
    EffectClass,
    EffectIntent,
    EffectLog,
    GateDecision,
    GateDecisionFact,
)
from core.product_core.memory_projection_contract import (
    GENERATOR_POLICY_ID,
    PROJECTION_VERSION,
)
from core.product_core.memory_projection_authority_contract import (
    MemoryProjectionAuthoritySnapshot,
    authority_snapshot_fingerprint,
)


EFFECT_KIND = "memory_projection_rebuild"
INTENT_SCHEMA = "memory-projection-rebuild-effect-intent-v2"
RECEIPT_KIND = "memory-projection-rebuild.receipt"
RECEIPT_SCHEMA = "memory-projection-rebuild-effect-receipt-v2"
REQUEST_TABLE = "memory_projection_rebuild_effect_request"
RESERVATION_TABLE = "memory_projection_rebuild_effect_reservation"
RECEIPT_TABLE = "memory_projection_rebuild_effect_receipt"


class MemoryProjectionRebuildEffectAdmissionError(ValueError):
    """The caller did not supply coherent immutable rebuild evidence."""


@dataclass(frozen=True, slots=True)
class MemoryProjectionRebuildEffectAdmission:
    authorization: GateDecisionFact
    gate_decision_id: str
    intent: EffectIntent
    request: Mapping[str, str]
    admitted_at: int


class MemoryProjectionRebuildEffectAdmissionFactory:
    """Freeze one authority snapshot before caller-owned v2 planning."""

    def __init__(self, *, admitted_at: int) -> None:
        if not isinstance(admitted_at, int) or isinstance(admitted_at, bool) or admitted_at < 0:
            raise MemoryProjectionRebuildEffectAdmissionError("admitted_at must be a non-negative Unix timestamp")
        self._admitted_at = admitted_at

    def build(self, snapshot: MemoryProjectionAuthoritySnapshot) -> MemoryProjectionRebuildEffectAdmission:
        if not isinstance(snapshot, MemoryProjectionAuthoritySnapshot):
            raise TypeError("snapshot must be a MemoryProjectionAuthoritySnapshot")
        project_id = _required(snapshot.project_id, "project_id")
        authority_identity = _required(snapshot.authority_identity, "authority_identity")
        fingerprint = authority_snapshot_fingerprint(snapshot)
        request_id = _stable_id(project_id, authority_identity, fingerprint)
        request_ref = f"facts:memory-projection-rebuild/request/{request_id}"
        gate_id = f"gate:memory-projection-rebuild/{request_id}"
        intent_ref = f"intent:memory-projection-rebuild/{request_id}"
        revisions = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
        revisions.update({
            "policy": "memory-projection-derived-only-policy-v2",
            "boundary": "memory-projection-rebuild-admission-v2",
            "capability": "memory-projection-rebuild-v2",
            "context_manifest": f"projection-authority:{fingerprint}",
            "bundle": "memory-projection-rebuild-bundle-v2",
            "handler": "memory-projection-rebuild-handler-v2",
            "budget": "memory-projection-rebuild-single-artifact-v2",
            "workflow": "memory-projection-rebuild-workflow-v2",
        })
        gate = GateDecisionFact(
            decision=GateDecision.ALLOW,
            rule_ref="rule:memory-projection-derived-only-v2",
            scope_ref=f"scope:memory-projection/{project_id}",
            budget_after={
                "project_ref": f"crp://memory-projections/projects/{project_id}",
                "authority_ref": f"facts:memory-projection-authority/{authority_identity}",
                "request_ref": request_ref,
                "projection_version_ref": f"facts:memory-projection-policy/{PROJECTION_VERSION}",
                "generator_policy_ref": f"facts:memory-projection-policy/{GENERATOR_POLICY_ID}",
                "rebuild_count": 1,
            },
            secret_scope="scope:memory-projection-secret/not-applicable",
            policy_revision=str(revisions["policy"]),
        )
        request = MappingProxyType({
            "request_id": request_id,
            "project_id": project_id,
            "authority_identity": authority_identity,
            "authority_fingerprint": fingerprint,
            "projection_version": PROJECTION_VERSION,
            "generator_policy_id": GENERATOR_POLICY_ID,
            "request_ref": request_ref,
        })
        intent = EffectIntent(
            session_id=f"memory-projection:{project_id}",
            root_id=request_id,
            parent_id=None,
            step_key="rebuild",
            kind=EFFECT_KIND,
            effect_class=EffectClass.QUERYABLE,
            intent_ref=intent_ref,
            gate_decision_id=gate_id,
            rev_set=revisions,
            payload={
                "request_ref": request_ref,
                "project_ref": f"crp://memory-projections/projects/{project_id}",
                "authority_ref": f"facts:memory-projection-authority/{authority_identity}",
                "projection_version_ref": f"facts:memory-projection-policy/{PROJECTION_VERSION}",
                "generator_policy_ref": f"facts:memory-projection-policy/{GENERATOR_POLICY_ID}",
                "attempt_index": 0,
                "mode": "admit",
            },
            contract_version=EFFECT_V2,
            intent_schema_version=INTENT_SCHEMA,
            expected_receipt_kind=RECEIPT_KIND,
            expected_receipt_schema_version=RECEIPT_SCHEMA,
        )
        return MemoryProjectionRebuildEffectAdmission(
            authorization=gate, gate_decision_id=gate_id, intent=intent,
            request=request, admitted_at=self._admitted_at,
        )


class SQLiteMemoryProjectionRebuildEffectAdmission:
    """Atomically persist the request fact and Core v2 Effect in caller transaction."""

    def __init__(self, effect_log: EffectLog) -> None:
        self._effects = effect_log

    def admit_in_connection(
        self, connection: sqlite3.Connection, admission: MemoryProjectionRebuildEffectAdmission,
    ):
        if not connection.in_transaction:
            raise RuntimeError("memory projection Effect admission requires a caller-owned transaction")
        _initialize_schema(connection)
        intent = admission.intent
        _validate_admission(admission)
        if intent.contract_version != EFFECT_V2 or intent.kind != EFFECT_KIND:
            raise MemoryProjectionRebuildEffectAdmissionError("admission requires the frozen memory projection v2 intent")
        effect, created = self._effects.plan_v2_in_connection(
            connection, intent, gate_decision_id=admission.gate_decision_id,
            gate_fact=admission.authorization, now=admission.admitted_at,
        )
        if effect.operation_id != intent.operation_id:
            raise MemoryProjectionRebuildEffectAdmissionError("planned operation identity drifted")
        _write_request(connection, operation_id=effect.operation_id, request=admission.request)
        return effect, created


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {REQUEST_TABLE} (operation_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, request_digest TEXT NOT NULL UNIQUE, FOREIGN KEY(operation_id) REFERENCES effect(operation_id))"
    )
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {RESERVATION_TABLE} (operation_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, reserved_at INTEGER NOT NULL, FOREIGN KEY(operation_id) REFERENCES effect(operation_id))"
    )
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE} (operation_id TEXT PRIMARY KEY, receipt_ref TEXT NOT NULL UNIQUE, receipt_json TEXT NOT NULL, recorded_at INTEGER NOT NULL, FOREIGN KEY(operation_id) REFERENCES effect(operation_id))"
    )
    for table in (REQUEST_TABLE, RESERVATION_TABLE, RECEIPT_TABLE):
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'memory projection Effect facts are insert-or-verify'); END"
        )
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'memory projection Effect facts are insert-or-verify'); END"
        )


def _write_request(connection: sqlite3.Connection, *, operation_id: str, request: Mapping[str, str]) -> None:
    expected = {"request_id", "project_id", "authority_identity", "authority_fingerprint", "projection_version", "generator_policy_id", "request_ref"}
    if set(request) != expected:
        raise MemoryProjectionRebuildEffectAdmissionError("rebuild request fields are not exact")
    if not isinstance(operation_id, str) or not operation_id.startswith("eff2_"):
        raise MemoryProjectionRebuildEffectAdmissionError("rebuild operation must be an Effect-v2 identity")
    encoded = json.dumps(dict(request), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    connection.execute(
        f"INSERT OR IGNORE INTO {REQUEST_TABLE}(operation_id,request_json,request_digest) VALUES(?,?,?)",
        (operation_id, encoded, digest),
    )
    row = connection.execute(f"SELECT request_json,request_digest FROM {REQUEST_TABLE} WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None or tuple(row) != (encoded, digest):
        raise MemoryProjectionRebuildEffectAdmissionError("immutable rebuild request drifted")


def _validate_admission(admission: MemoryProjectionRebuildEffectAdmission) -> None:
    """Bind the domain request to the exact frozen v2 intent before planning."""
    request = admission.request
    intent = admission.intent
    expected = {"request_id", "project_id", "authority_identity", "authority_fingerprint", "projection_version", "generator_policy_id", "request_ref"}
    if set(request) != expected:
        raise MemoryProjectionRebuildEffectAdmissionError("rebuild request fields are not exact")
    if request["request_id"] != intent.root_id:
        raise MemoryProjectionRebuildEffectAdmissionError("request_id must equal the frozen Effect root")
    if request["request_ref"] != f"facts:memory-projection-rebuild/request/{request['request_id']}":
        raise MemoryProjectionRebuildEffectAdmissionError("request reference drifted")
    if request["projection_version"] != PROJECTION_VERSION or request["generator_policy_id"] != GENERATOR_POLICY_ID:
        raise MemoryProjectionRebuildEffectAdmissionError("projection policy drifted")
    expected_payload = {
        "request_ref": request["request_ref"],
        "project_ref": f"crp://memory-projections/projects/{request['project_id']}",
        "authority_ref": f"facts:memory-projection-authority/{request['authority_identity']}",
        "projection_version_ref": f"facts:memory-projection-policy/{request['projection_version']}",
        "generator_policy_ref": f"facts:memory-projection-policy/{request['generator_policy_id']}",
        "attempt_index": 0,
        "mode": "admit",
    }
    if dict(intent.payload) != expected_payload:
        raise MemoryProjectionRebuildEffectAdmissionError("request does not match frozen intent payload")
    if intent.rev_set.get("context_manifest") != f"projection-authority:{request['authority_fingerprint']}":
        raise MemoryProjectionRebuildEffectAdmissionError("request does not match frozen authority revision")
    if intent.rev_set.get("policy") != admission.authorization.policy_revision:
        raise MemoryProjectionRebuildEffectAdmissionError("Gate policy does not match frozen intent")


def _stable_id(*parts: str) -> str:
    return "mpr_" + hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:48]


def _required(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MemoryProjectionRebuildEffectAdmissionError(f"{field} is required")
    return value
