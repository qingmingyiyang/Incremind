"""Unregistered Handler/Probe for the Memory Projection rebuild Effect-v2 seam."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.effect_log import (
    EFFECT_V2,
    Effect,
    EffectReceipt,
    EffectState,
    GateDecision,
    GateDecisionFact,
)
from core.product_core.memory_projection_authority_contract import (
    MemoryProjectionAuthoritySnapshot,
    authority_snapshot_fingerprint,
)
from core.product_core.memory_projection_contract import (
    GENERATOR_POLICY_ID,
    PROJECTION_VERSION,
)
from core.product_core.memory_projection_repository import (
    ObjectStoreMemoryProjectionRepository,
    projection_artifact_id,
)

from .memory_projection_rebuild_effect_admission import (
    EFFECT_KIND, INTENT_SCHEMA, RECEIPT_KIND, RECEIPT_SCHEMA, RECEIPT_TABLE,
    REQUEST_TABLE, RESERVATION_TABLE,
)


class MemoryProjectionRebuildEffectExecutionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryProjectionRebuildEffectExecutionHandler:
    database: Path | str
    authority: object
    projections: ObjectStoreMemoryProjectionRepository

    def __post_init__(self) -> None:
        object.__setattr__(self, "database", Path(self.database))

    def __call__(self, effect: Effect) -> EffectReceipt:
        return self.handle(effect)

    def handle(self, effect: Effect) -> EffectReceipt:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = _request(connection, effect)
            receipt = _receipt(connection, effect, request)
            if receipt is not None:
                connection.commit()
                return _effect_receipt(effect)
            _reserve(connection, effect, request)
            connection.commit()
        snapshot = _load_current(self.authority, request)
        generated_at = datetime.fromtimestamp(effect.occurred_at, timezone.utc).isoformat()
        projection = snapshot.build(generated_at=generated_at)
        artifact_id = self.projections.stage_projection(projection)
        manifest = self.projections.begin_rebuild(
            project_id=request["project_id"], authority_identity=request["authority_identity"],
            authority_fingerprint=request["authority_fingerprint"], job_id=effect.operation_id,
            updated_at=generated_at,
        )
        if manifest.get("status") != "ready":
            manifest = self.projections.activate_staged(
                project_id=request["project_id"], authority_identity=request["authority_identity"],
                authority_fingerprint=request["authority_fingerprint"], job_id=effect.operation_id,
                artifact_id=artifact_id, updated_at=generated_at,
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = _request(connection, effect)
            _load_current(self.authority, request)
            _write_receipt(connection, effect, request, artifact_id, manifest)
            connection.commit()
        return _effect_receipt(effect)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


@dataclass(frozen=True, slots=True)
class MemoryProjectionRebuildEffectExecutionProbe:
    database: Path | str
    authority: object
    projections: ObjectStoreMemoryProjectionRepository

    def __post_init__(self) -> None:
        object.__setattr__(self, "database", Path(self.database))

    def __call__(self, effect: Effect) -> tuple[EffectState, str | None]:
        try:
            with sqlite3.connect(self.database) as connection:
                connection.row_factory = sqlite3.Row
                request = _request(connection, effect)
                _load_current(self.authority, request)
                receipt = _receipt(connection, effect, request)
                if receipt is not None:
                    return EffectState.SETTLED_OK, str(receipt["receipt_ref"])
                ready_manifest = _ready_manifest(self.projections, effect, request)
                if ready_manifest is not None:
                    # The artifact and active manifest are independently
                    # verifiable.  Persist only the closed domain Receipt;
                    # Core Reaper remains the sole Effect-settlement writer.
                    _write_receipt(connection, effect, request,
                                   str(ready_manifest["active_artifact_id"]), ready_manifest)
                    receipt = _receipt(connection, effect, request)
                    assert receipt is not None
                    return EffectState.SETTLED_OK, str(receipt["receipt_ref"])
                reserved = connection.execute(f"SELECT 1 FROM {RESERVATION_TABLE} WHERE operation_id=?", (effect.operation_id,)).fetchone()
                if reserved is not None:
                    return EffectState.UNKNOWN, "error:memory-projection-rebuild-reserved"
                return EffectState.PLANNED, f"facts:memory-projection-rebuild/retry/{effect.operation_id}"
        except MemoryProjectionRebuildEffectExecutionError:
            return EffectState.UNKNOWN, "error:memory-projection-rebuild-evidence-drift"
        except (sqlite3.DatabaseError, TypeError, ValueError):
            return EffectState.SETTLED_ERR, "error:memory-projection-rebuild-invalid"


def _request(connection: sqlite3.Connection, effect: Effect) -> dict[str, str]:
    if effect.contract_version != EFFECT_V2 or effect.kind != EFFECT_KIND or effect.intent_schema_version != INTENT_SCHEMA:
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild Effect contract drifted")
    row = connection.execute(f"SELECT request_json,request_digest FROM {REQUEST_TABLE} WHERE operation_id=?", (effect.operation_id,)).fetchone()
    if row is None:
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild request is unavailable")
    try:
        request = json.loads(str(row[0]))
    except json.JSONDecodeError as error:
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild request is invalid") from error
    expected = {"request_id", "project_id", "authority_identity", "authority_fingerprint", "projection_version", "generator_policy_id", "request_ref"}
    if not isinstance(request, dict) or set(request) != expected:
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild request fields drifted")
    if str(row[1]) != _request_digest(request):
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild request digest drifted")
    request_id = request["request_id"]
    if (effect.root_id != request_id
            or effect.intent_ref != f"intent:memory-projection-rebuild/{request_id}"
            or effect.gate_decision_id != f"gate:memory-projection-rebuild/{request_id}"
            or request["request_ref"] != f"facts:memory-projection-rebuild/request/{request['request_id']}"
            or request["projection_version"] != PROJECTION_VERSION
            or request["generator_policy_id"] != GENERATOR_POLICY_ID
            or effect.rev_set.get("context_manifest") != f"projection-authority:{request['authority_fingerprint']}"):
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild request identity drifted")
    intent = connection.execute(
        "SELECT intent_ref,intent_digest,payload_json,schema_version FROM effect_intent_fact WHERE operation_id=?", (effect.operation_id,),
    ).fetchone()
    if (intent is None or str(intent[0]) != f"intent:memory-projection-rebuild/{request_id}"
            or str(intent[0]) != effect.intent_ref
            or str(intent[1]) != effect.intent_digest or str(intent[3]) != INTENT_SCHEMA):
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild intent fact is unavailable")
    try:
        payload = json.loads(str(intent[2]))
    except json.JSONDecodeError as error:
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild intent fact is invalid") from error
    expected_payload = {
        "request_ref": request["request_ref"],
        "project_ref": f"crp://memory-projections/projects/{request['project_id']}",
        "authority_ref": f"facts:memory-projection-authority/{request['authority_identity']}",
        "projection_version_ref": f"facts:memory-projection-policy/{request['projection_version']}",
        "generator_policy_ref": f"facts:memory-projection-policy/{request['generator_policy_id']}",
        "attempt_index": 0,
        "mode": "admit",
    }
    if payload != expected_payload:
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild intent payload drifted")
    _validate_gate_fact(connection, effect, request)
    return request


def _validate_gate_fact(
    connection: sqlite3.Connection, effect: Effect, request: Mapping[str, str],
) -> None:
    row = connection.execute(
        "SELECT decision_id,decision,rule_ref,scope_ref,budget_after,secret_scope,policy_revision,"
        "mutated_intent_digest,decision_digest FROM effect_gate_fact WHERE decision_id=?",
        (effect.gate_decision_id,),
    ).fetchone()
    expected_id = f"gate:memory-projection-rebuild/{request['request_id']}"
    if row is None or str(row[0]) != expected_id or effect.gate_decision_id != expected_id:
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild Gate fact is unavailable")
    try:
        budget_after = json.loads(str(row[4]))
        decision = GateDecision(str(row[1]))
        fact = GateDecisionFact(
            decision=decision,
            rule_ref=str(row[2]),
            scope_ref=str(row[3]),
            budget_after=budget_after,
            secret_scope=str(row[5]),
            policy_revision=str(row[6]),
            mutated_intent_digest=(str(row[7]) if row[7] is not None else None),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild Gate fact is invalid") from error
    expected_budget = {
        "project_ref": f"crp://memory-projections/projects/{request['project_id']}",
        "authority_ref": f"facts:memory-projection-authority/{request['authority_identity']}",
        "request_ref": request["request_ref"],
        "projection_version_ref": f"facts:memory-projection-policy/{PROJECTION_VERSION}",
        "generator_policy_ref": f"facts:memory-projection-policy/{GENERATOR_POLICY_ID}",
        "rebuild_count": 1,
    }
    if (fact.decision is not GateDecision.ALLOW
            or fact.rule_ref != "rule:memory-projection-derived-only-v2"
            or fact.scope_ref != f"scope:memory-projection/{request['project_id']}"
            or fact.secret_scope != "scope:memory-projection-secret/not-applicable"
            or fact.policy_revision != "memory-projection-derived-only-policy-v2"
            or fact.policy_revision != effect.rev_set.get("policy")
            or fact.budget_after != expected_budget
            or fact.mutated_intent_digest is not None
            or fact.decision_digest != str(row[8])):
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild Gate fact drifted")


def _load_current(authority: object, request: Mapping[str, str]) -> MemoryProjectionAuthoritySnapshot:
    load = getattr(authority, "load", None)
    if not callable(load):
        raise MemoryProjectionRebuildEffectExecutionError("projection authority loader is unavailable")
    snapshot = load(request["project_id"])
    if (not isinstance(snapshot, MemoryProjectionAuthoritySnapshot)
            or snapshot.project_id != request["project_id"]
            or snapshot.authority_identity != request["authority_identity"]
            or authority_snapshot_fingerprint(snapshot) != request["authority_fingerprint"]):
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild authority drifted")
    return snapshot


def _reserve(connection: sqlite3.Connection, effect: Effect, request: Mapping[str, str]) -> None:
    digest = _request_digest(request)
    connection.execute(f"INSERT OR IGNORE INTO {RESERVATION_TABLE}(operation_id,request_digest,reserved_at) VALUES(?,?,?)", (effect.operation_id, digest, effect.recorded_at))
    row = connection.execute(f"SELECT request_digest FROM {RESERVATION_TABLE} WHERE operation_id=?", (effect.operation_id,)).fetchone()
    if row is None or str(row[0]) != digest:
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild reservation drifted")


def _receipt(connection: sqlite3.Connection, effect: Effect, request: Mapping[str, str]) -> dict[str, str] | None:
    row = connection.execute(f"SELECT receipt_json,receipt_ref FROM {RECEIPT_TABLE} WHERE operation_id=?", (effect.operation_id,)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row[0]))
    except json.JSONDecodeError as error:
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild receipt is invalid") from error
    expected_keys = {"operation_id", "request_id", "request_ref", "artifact_id", "manifest_job_id", "authority_identity", "authority_fingerprint", "projection_version", "generator_policy_id", "receipt_ref", "receipt_kind", "receipt_schema_version", "intent_schema_version"}
    if (not isinstance(payload, dict) or set(payload) != expected_keys
            or payload.get("operation_id") != effect.operation_id
            or payload.get("request_id") != request["request_id"]
            or payload.get("request_ref") != request["request_ref"]
            or payload.get("artifact_id") != projection_artifact_id(
                request["project_id"], request["authority_identity"], request["authority_fingerprint"],
            )
            or payload.get("receipt_ref") != row[1]
            or payload.get("manifest_job_id") != effect.operation_id
            or payload.get("authority_identity") != request["authority_identity"]
            or payload.get("authority_fingerprint") != request["authority_fingerprint"]
            or payload.get("projection_version") != PROJECTION_VERSION
            or payload.get("generator_policy_id") != GENERATOR_POLICY_ID
            or payload.get("receipt_kind") != RECEIPT_KIND
            or payload.get("receipt_schema_version") != RECEIPT_SCHEMA
            or payload.get("intent_schema_version") != INTENT_SCHEMA):
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild receipt drifted")
    return payload


def _write_receipt(connection: sqlite3.Connection, effect: Effect, request: Mapping[str, str], artifact_id: str, manifest: Mapping[str, object]) -> None:
    if (manifest.get("active_artifact_id") != artifact_id or manifest.get("status") != "ready"
            or manifest.get("job_id") != effect.operation_id
            or manifest.get("authority_identity") != request["authority_identity"]
            or manifest.get("requested_authority_fingerprint") != request["authority_fingerprint"]
            or manifest.get("active_authority_fingerprint") != request["authority_fingerprint"]
            or manifest.get("projection_version") != PROJECTION_VERSION
            or manifest.get("generator_policy_id") != GENERATOR_POLICY_ID):
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild activation is not durable")
    receipt_ref = f"receipt:memory-projection-rebuild/{effect.operation_id}"
    payload = {"operation_id": effect.operation_id, "request_id": request["request_id"], "request_ref": request["request_ref"], "artifact_id": artifact_id, "manifest_job_id": effect.operation_id, "authority_identity": request["authority_identity"], "authority_fingerprint": request["authority_fingerprint"], "projection_version": PROJECTION_VERSION, "generator_policy_id": GENERATOR_POLICY_ID, "receipt_ref": receipt_ref, "receipt_kind": RECEIPT_KIND, "receipt_schema_version": RECEIPT_SCHEMA, "intent_schema_version": INTENT_SCHEMA}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.execute(f"INSERT OR IGNORE INTO {RECEIPT_TABLE}(operation_id,receipt_ref,receipt_json,recorded_at) VALUES(?,?,?,?)", (effect.operation_id, receipt_ref, encoded, effect.recorded_at))
    existing = _receipt(connection, effect, request)
    if existing is None:
        raise MemoryProjectionRebuildEffectExecutionError("projection rebuild receipt write failed")


def _effect_receipt(effect: Effect) -> EffectReceipt:
    return EffectReceipt(f"receipt:memory-projection-rebuild/{effect.operation_id}", RECEIPT_KIND, RECEIPT_SCHEMA, INTENT_SCHEMA)


def _request_digest(request: Mapping[str, str]) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(dict(request), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _ready_manifest(
    projections: ObjectStoreMemoryProjectionRepository, effect: Effect, request: Mapping[str, str],
) -> Mapping[str, object] | None:
    """Return only an exactly frozen, readable active projection artifact."""
    result = projections.load_current(
        project_id=request["project_id"],
        authority_identity=request["authority_identity"],
        authority_fingerprint=request["authority_fingerprint"],
    )
    manifest = result.manifest
    if result.status != "fresh" or manifest is None:
        return None
    if manifest.get("job_id") != effect.operation_id:
        raise MemoryProjectionRebuildEffectExecutionError("projection manifest execution identity drifted")
    return manifest
