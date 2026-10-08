"""Effect-v2 handler and probe for Index rebuild domain facts.

The handler writes immutable domain evidence and returns an ``EffectReceipt``.
Core ``EffectRunner`` remains solely responsible for binding and settling it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from core.effect_log import EFFECT_V2, Effect, EffectReceipt, EffectState

from .index_rebuild_effect_admission import (
    EFFECT_KIND, INTENT_SCHEMA, RECEIPT_KIND, RECEIPT_SCHEMA, REQUEST_SCHEMA,
    RECEIPT_TABLE, REQUEST_TABLE, RESERVATION_TABLE, canonical,
    validate_domain_request,
)


class IndexRebuildEffectExecutionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IndexRebuildQueryOutcome:
    """Read-only proof supplied by the domain's external completion query.

    ``completed`` contains the exact bounded result that would have been
    received from ``execute``.  The probe does not write a Receipt; it returns
    recoverable evidence so Core can preserve uncertainty until the Receipt is
    explicitly materialized by a permitted recovery command.
    """

    state: str
    result: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.state not in {"completed", "not_completed", "unknown"}:
            raise ValueError("index rebuild query state is invalid")
        if self.state == "completed":
            _validate_result(self.result)
        elif self.result is not None:
            raise ValueError("only completed index queries may include a result")


@dataclass(frozen=True, slots=True)
class IndexRebuildEffectExecutionHandler:
    database: Path | str
    load_manifest: Callable[[str], Mapping[str, object] | None]
    load_ledger: Callable[[str], Mapping[str, object] | None]
    execute: Callable[[str, Mapping[str, object], Mapping[str, object], Mapping[str, object]], Mapping[str, object]]
    query_completion: Callable[[Mapping[str, object]], IndexRebuildQueryOutcome]
    after_reservation_write: Callable[[], None] | None = None
    after_domain_write: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "database", Path(self.database))

    def __call__(self, effect: Effect) -> EffectReceipt:
        return self.handle(effect)

    def handle(self, effect: Effect) -> EffectReceipt:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            domain = _read_domain_request(connection, effect)
            _assert_live_authorities(domain, self.load_manifest, self.load_ledger)
            if _receipt(connection, effect, domain) is not None:
                connection.commit()
                return _effect_receipt(effect)
            reserved = _reservation(connection, effect, domain) is not None
            if not reserved:
                _write_reservation(connection, effect, domain)
            connection.commit()
        if self.after_reservation_write is not None:
            self.after_reservation_write()
        manifest, ledger = _assert_live_authorities(domain, self.load_manifest, self.load_ledger)
        if reserved:
            query = _query(self.query_completion, domain, effect.operation_id)
            if query.state == "completed":
                result = dict(query.result or {})
            elif query.state == "not_completed":
                result = self.execute(effect.operation_id, domain["request"], manifest, ledger)
            else:
                raise IndexRebuildEffectExecutionError("query-unknown")
        else:
            result = self.execute(effect.operation_id, domain["request"], manifest, ledger)
        if self.after_domain_write is not None:
            self.after_domain_write()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            domain = _read_domain_request(connection, effect)
            _assert_live_authorities(domain, self.load_manifest, self.load_ledger)
            if _receipt(connection, effect, domain) is None:
                _write_receipt(connection, effect, domain, result)
            connection.commit()
        return _effect_receipt(effect)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection


@dataclass(frozen=True, slots=True)
class IndexRebuildEffectExecutionProbe:
    database: Path | str
    load_manifest: Callable[[str], Mapping[str, object] | None]
    load_ledger: Callable[[str], Mapping[str, object] | None]
    query_completion: Callable[[Mapping[str, object]], IndexRebuildQueryOutcome]

    def __post_init__(self) -> None:
        object.__setattr__(self, "database", Path(self.database))

    def __call__(self, effect: Effect) -> tuple[EffectState, str | None]:
        return self.probe(effect)

    def probe(self, effect: Effect) -> tuple[EffectState, str | None]:
        try:
            with sqlite3.connect(self.database, isolation_level=None) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("BEGIN IMMEDIATE")
                domain = _read_domain_request(connection, effect)
                _assert_live_authorities(domain, self.load_manifest, self.load_ledger)
                if _receipt(connection, effect, domain) is not None:
                    connection.commit()
                    return EffectState.SETTLED_OK, _receipt_ref(effect.operation_id)
                query = _query(self.query_completion, domain, effect.operation_id)
                if query.state == "completed":
                    connection.commit()
                    # A probe is a verifier, not a second receipt writer.  In
                    # particular it can be called by the Reaper after a lease
                    # expiry, when the former handler fence is no longer
                    # authoritative.  A completed external artifact without
                    # an immutable domain receipt therefore remains UNKNOWN
                    # until a Core-owned recovery command materializes it.
                    return EffectState.UNKNOWN, f"error:index-rebuild-receipt-missing/{effect.operation_id}"
                if query.state == "not_completed":
                    connection.commit()
                    return EffectState.PLANNED, f"facts:index-rebuild-not-completed/{effect.operation_id}"
                connection.commit()
                return EffectState.UNKNOWN, f"error:index-rebuild-query-unknown/{effect.operation_id}"
        except IndexRebuildEffectExecutionError as error:
            return EffectState.UNKNOWN, f"error:index-rebuild-{_reason(error)}"
        except (sqlite3.DatabaseError, TypeError, ValueError):
            return EffectState.UNKNOWN, "error:index-rebuild-invalid"


def _read_domain_request(connection: sqlite3.Connection, effect: Effect) -> Mapping[str, object]:
    _validate_effect(effect)
    intent_row = connection.execute(
        "SELECT intent_ref,intent_digest,payload_json,schema_version FROM effect_intent_fact WHERE operation_id=?",
        (effect.operation_id,),
    ).fetchone()
    if intent_row is None or tuple(intent_row[:2]) != (effect.intent_ref, effect.intent_digest) or intent_row[3] != INTENT_SCHEMA:
        raise IndexRebuildEffectExecutionError("input-drift")
    try:
        payload = json.loads(str(intent_row[2]))
    except json.JSONDecodeError as error:
        raise IndexRebuildEffectExecutionError("input-drift") from error
    row = connection.execute(f"SELECT request_json FROM {REQUEST_TABLE} WHERE operation_id=?", (effect.operation_id,)).fetchone()
    if row is None:
        raise IndexRebuildEffectExecutionError("input-drift")
    try:
        domain = validate_domain_request(effect, json.loads(str(row[0])))
    except (json.JSONDecodeError, ValueError) as error:
        raise IndexRebuildEffectExecutionError("input-drift") from error
    expected_payload = {"request_ref": domain["request_ref"], "manifest_ref": domain["manifest_ref"],
                        "ledger_ref": domain["ledger_ref"], "admission_ref": domain["request_ref"],
                        "mode": "admit", "attempt_index": 0}
    if payload != expected_payload:
        raise IndexRebuildEffectExecutionError("input-drift")
    _assert_effect_gate_and_revisions(connection, effect, domain)
    return domain


def _assert_effect_gate_and_revisions(connection: sqlite3.Connection, effect: Effect, domain: Mapping[str, object]) -> None:
    """Bind the domain request to the exact Core Gate and v2 revision set.

    The Handler and Probe read the same durable facts.  This prevents a
    caller from swapping a request-shaped payload under a valid operation id
    while retaining an unrelated manifest/budget or Gate decision.
    """
    expected_revisions = {
        "policy": "index-rebuild-policy-v2",
        "boundary": "index-rebuild-boundary-v2",
        "capability": "index-rebuild-capability-v2",
        "context_manifest": domain["manifest_revision"],
        "handler": "index-rebuild-handler-v2",
        "budget": domain["ledger_revision"],
        "workflow": "index-rebuild-workflow-v2",
    }
    if any(effect.rev_set.get(key) != value for key, value in expected_revisions.items()):
        raise IndexRebuildEffectExecutionError("input-drift")
    row = connection.execute(
        "SELECT decision,rule_ref,scope_ref,budget_after,secret_scope,policy_revision "
        "FROM effect_gate_fact WHERE decision_id=?", (effect.gate_decision_id,),
    ).fetchone()
    expected_budget = canonical({
        "request_ref": domain["request_ref"], "manifest_ref": domain["manifest_ref"],
        "ledger_ref": domain["ledger_ref"], "rebuild_budget": 1,
    })
    expected = (
        "allow", "rule:index-rebuild-verified-ledger-v2",
        f"scope:index-rebuild/{domain['request']['id']}", expected_budget,
        "scope:index-rebuild-secret/not-applicable", "index-rebuild-policy-v2",
    )
    if row is None or tuple(row) != expected:
        raise IndexRebuildEffectExecutionError("input-drift")


def _validate_effect(effect: Effect) -> None:
    if (effect.contract_version != EFFECT_V2 or effect.kind != EFFECT_KIND
            or effect.intent_schema_version != INTENT_SCHEMA):
        raise IndexRebuildEffectExecutionError("input-drift")


def _assert_live_authorities(domain, load_manifest, load_ledger):
    manifest = load_manifest(str(domain["manifest_ref"]))
    if not isinstance(manifest, Mapping) or _revision("index-rebuild-manifest", manifest) != domain["manifest_revision"]:
        raise IndexRebuildEffectExecutionError("manifest-drift")
    ledger = load_ledger(str(domain["ledger_ref"]))
    if not isinstance(ledger, Mapping) or _revision("index-rebuild-ledger", ledger) != domain["ledger_revision"]:
        raise IndexRebuildEffectExecutionError("ledger-drift")
    return manifest, ledger


def _reservation(connection, effect, domain):
    row = connection.execute(f"SELECT reservation_json FROM {RESERVATION_TABLE} WHERE operation_id=?", (effect.operation_id,)).fetchone()
    if row is None:
        return None
    expected = {"operation_id": effect.operation_id, "request_ref": domain["request_ref"], "intent_digest": effect.intent_digest}
    try:
        value = json.loads(str(row[0]))
    except json.JSONDecodeError as error:
        raise IndexRebuildEffectExecutionError("input-drift") from error
    if value != expected:
        raise IndexRebuildEffectExecutionError("input-drift")
    return value


def _receipt(connection, effect, domain):
    row = connection.execute(f"SELECT receipt_json FROM {RECEIPT_TABLE} WHERE operation_id=?", (effect.operation_id,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(str(row[0]))
    except json.JSONDecodeError as error:
        raise IndexRebuildEffectExecutionError("input-drift") from error
    try:
        result = _validate_result(value.get("result"))
    except ValueError as error:
        raise IndexRebuildEffectExecutionError("input-drift") from error
    expected = {"operation_id": effect.operation_id, "request_ref": domain["request_ref"], "receipt_ref": _receipt_ref(effect.operation_id),
                "receipt_kind": RECEIPT_KIND, "receipt_schema_version": RECEIPT_SCHEMA, "intent_schema_version": INTENT_SCHEMA,
                "result": result}
    if value != expected:
        raise IndexRebuildEffectExecutionError("input-drift")
    return value


def _write_reservation(connection, effect, domain):
    value = {"operation_id": effect.operation_id, "request_ref": domain["request_ref"], "intent_digest": effect.intent_digest}
    connection.execute(
        f"INSERT OR IGNORE INTO {RESERVATION_TABLE}(operation_id,reservation_json,recorded_at) VALUES(?,?,?)",
        (effect.operation_id, canonical(value), effect.recorded_at),
    )
    if _reservation(connection, effect, domain) != value:
        raise IndexRebuildEffectExecutionError("input-drift")


def _write_receipt(connection, effect, domain, result):
    try:
        result = _validate_result(result)
    except ValueError as error:
        raise IndexRebuildEffectExecutionError("input-drift") from error
    value = {"operation_id": effect.operation_id, "request_ref": domain["request_ref"], "receipt_ref": _receipt_ref(effect.operation_id),
             "receipt_kind": RECEIPT_KIND, "receipt_schema_version": RECEIPT_SCHEMA, "intent_schema_version": INTENT_SCHEMA,
             "result": result}
    connection.execute(
        f"INSERT OR IGNORE INTO {RECEIPT_TABLE}(operation_id,receipt_json,recorded_at) VALUES(?,?,?)",
        (effect.operation_id, canonical(value), effect.recorded_at),
    )
    existing = _receipt(connection, effect, domain)
    if existing != value:
        raise IndexRebuildEffectExecutionError("input-drift")


def _revision(prefix, value):
    return f"{prefix}:sha256:{hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()}"


def _query(query_completion, domain, operation_id: str) -> IndexRebuildQueryOutcome:
    try:
        query_domain = dict(domain)
        query_domain["operation_id"] = operation_id
        outcome = query_completion(query_domain)
    except Exception as error:
        raise IndexRebuildEffectExecutionError("query-unavailable") from error
    if not isinstance(outcome, IndexRebuildQueryOutcome):
        raise IndexRebuildEffectExecutionError("query-invalid")
    return outcome


def _validate_result(value: object) -> dict[str, object]:
    """Result facts remain portable evidence, never paths or provider payloads."""
    if not isinstance(value, Mapping) or set(value) != {"status", "artifact_ref", "artifact_revision", "entry_count"}:
        raise ValueError("index rebuild result fields are not exact")
    result = dict(value)
    if result["status"] != "ready":
        raise ValueError("index rebuild result status is invalid")
    for key in ("artifact_ref", "artifact_revision"):
        item = result[key]
        if (not isinstance(item, str) or not item or len(item) > 160 or item != item.strip()
                or any(character.isspace() or ord(character) < 32 for character in item)
                or item.startswith(("/", "\\")) or ":\\" in item or "/" in item or "\\" in item
                or any(marker in item.lower() for marker in ("secret", "token", "cookie", "authorization", "header", "password"))):
            raise ValueError("index rebuild result reference is invalid")
    if (not isinstance(result["entry_count"], int) or isinstance(result["entry_count"], bool)
            or result["entry_count"] < 0 or result["entry_count"] > 10_000_000):
        raise ValueError("index rebuild result entry_count is invalid")
    return result


def _receipt_ref(operation_id): return f"receipt:index-rebuild/{operation_id}"
def _effect_receipt(effect): return EffectReceipt(_receipt_ref(effect.operation_id), RECEIPT_KIND, RECEIPT_SCHEMA, INTENT_SCHEMA)
def _reason(error): return str(error) if str(error) in {"input-drift", "manifest-drift", "ledger-drift"} else "invalid"
