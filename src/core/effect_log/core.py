from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from core.storage_provider.observability import observe_connection
from core.storage_provider.connection_scope import reusable_connection
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping

from blake3 import blake3


EFFECT_SCHEMA_VERSION = 3
LEGACY_V1 = "legacy-v1"
EFFECT_V2 = "effect-v2"
LEGACY_IDENTITY_ALGORITHM = "blake2b-160"
V2_IDENTITY_ALGORITHM = "blake3-256"
LEGACY_REVISION_SCHEMA_VERSION = "legacy-v1"
V2_REVISION_SCHEMA_VERSION = "effect-authority-v2"
V2_REVISION_KEYS = (
    "policy", "boundary", "capability", "context_manifest", "provider",
    "model_route", "bundle", "handler", "secret", "budget", "workflow",
)
NOT_APPLICABLE = "not_applicable"


class EffectState(StrEnum):
    PLANNED = "PLANNED"
    INFLIGHT = "INFLIGHT"
    SETTLED_OK = "SETTLED_OK"
    SETTLED_ERR = "SETTLED_ERR"
    UNKNOWN = "UNKNOWN"
    COMPENSATED = "COMPENSATED"
    ABANDONED = "ABANDONED"


class EffectClass(StrEnum):
    PURE = "PURE"
    IDEMPOTENT = "IDEMPOTENT"
    QUERYABLE = "QUERYABLE"
    AT_MOST_ONCE = "AT_MOST_ONCE"
    NEEDS_REAUTH = "NEEDS_REAUTH"


class EffectPurpose(StrEnum):
    PRIMARY = "primary"
    AUX = "aux"
    PROBE = "probe"


class GateDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    MUTATE = "mutate"
    ASK = "ask"


class InvalidEffectTransition(RuntimeError):
    pass


class EffectHandlerAbandoned(RuntimeError):
    """Handler completed compensation and asks Core to abandon the Effect."""

    def __init__(self, error_ref: str):
        if not isinstance(error_ref, str) or not error_ref.strip():
            raise ValueError("abandoned Effect requires an error reference")
        super().__init__(error_ref)
        self.error_ref = error_ref


class EffectHandlerDeferred(RuntimeError):
    """A Handler is temporarily unavailable before any external operation."""

    def __init__(self, probe_ref: str):
        super().__init__(probe_ref)
        self.probe_ref = probe_ref


_TRANSITIONS = {
    EffectState.PLANNED: {EffectState.INFLIGHT, EffectState.ABANDONED},
    EffectState.INFLIGHT: {
        EffectState.PLANNED,
        EffectState.SETTLED_OK,
        EffectState.SETTLED_ERR,
        EffectState.UNKNOWN,
        EffectState.ABANDONED,
    },
    EffectState.UNKNOWN: {
        EffectState.PLANNED,
        EffectState.SETTLED_OK,
        EffectState.SETTLED_ERR,
        EffectState.ABANDONED,
    },
    EffectState.SETTLED_OK: {EffectState.COMPENSATED},
    EffectState.SETTLED_ERR: set(),
    EffectState.COMPENSATED: set(),
    EffectState.ABANDONED: {EffectState.COMPENSATED},
}


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def derive_operation_id(
    *, session_id: str, root_id: str, step_key: str, intent: Mapping[str, object]
) -> str:
    material = _canonical(
        {
            "session_id": session_id,
            "root_id": root_id,
            "step_key": step_key,
            "intent": dict(intent),
        }
    ).encode("utf-8")
    return "eff_" + hashlib.blake2b(material, digest_size=20).hexdigest()


def derive_v2_operation_id(
    *, session_id: str, root_id: str, step_key: str, intent_digest: str,
) -> str:
    """Stable v2 identity; authority revisions fence planning, never identity."""
    material = _canonical({
        "session_id": session_id, "root_id": root_id, "step_key": step_key,
        "intent_digest": intent_digest,
    }).encode("utf-8")
    return "eff2_" + blake3(material).hexdigest()


def _assert_internal_opaque_ref(value: str, *, field: str) -> None:
    """Only permit internal fact references, never external URLs or headers."""
    if (not isinstance(value, str) or value != value.strip() or not value
            or any(char.isspace() or ord(char) < 32 for char in value)
            or "?" in value or "&" in value or "=" in value
            or re.match(r"^(?:bearer|basic):?", value, flags=re.I)
            or "-----begin" in value.casefold()
            or not re.fullmatch(
                r"(?:crp|facts|lease|intent|gate|receipt|error|decision|rule|scope|policy|budget|boundary|capability|provider|model|bundle|handler):[A-Za-z0-9._:/@+\-]{1,160}",
                value,
            )):
        raise ValueError(f"{field} must be an internal opaque reference")


def _assert_user_decision_ref(value: str, *, field: str) -> None:
    _assert_internal_opaque_ref(value, field=field)
    if not value.startswith("decision:"):
        raise ValueError(f"{field} must be a durable decision: reference")


def _assert_opaque_token(value: str, *, field: str) -> None:
    """Validate persisted non-reference facts without admitting URL/query content."""
    if (not isinstance(value, str) or value != value.strip() or not value
            or len(value) > 160 or any(char.isspace() or ord(char) < 32 for char in value)
            or any(part in value for part in ("?", "&", "="))
            or re.match(r"^(?:https?|wss?|ftp|file|data|bearer|basic):", value, flags=re.I)
            or "-----begin" in value.casefold()
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/+\-]*", value)):
        raise ValueError(f"{field} must be an opaque token")


def _v2_error_ref(error: Exception) -> str:
    return "error:" + blake3(f"{type(error).__name__}:{error}".encode("utf-8")).hexdigest()


def _assert_no_secret_payload(value: object, *, path: str = "payload", leaf_kind: str | None = None) -> None:
    """Enforce the v2 reference-only payload envelope.

    v2 stores executable parameters in the immutable fact addressed by
    ``intent_ref``.  Its inline payload is limited to references, revisions,
    identifiers, scopes, digests, scalar budgets and a small safe metadata set.
    Thus arbitrary ``data`` maps and credential-shaped keys cannot smuggle a
    plaintext string through a blacklist gap.  This is an Effect-log boundary,
    not the Phase 7 egress/canary guarantee.
    """
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("v2 payload keys must be strings")
            folded = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
            if folded.endswith(("_ref", "_refs")):
                nested_kind = "ref"
            elif folded.endswith(("_revision", "_revisions")):
                nested_kind = "revision"
            elif folded.endswith(("_id", "_ids")):
                nested_kind = "id"
            elif folded.endswith(("_scope", "_scopes")):
                nested_kind = "scope"
            elif folded.endswith(("_digest", "_digests")):
                nested_kind = "digest"
            elif folded in {"kind", "purpose", "mode", "method", "format", "content_type", "locale"}:
                nested_kind = "metadata"
            elif (folded.endswith(("_budget", "_count", "_duration", "_seconds", "_milliseconds", "_micros", "_bytes", "_tokens", "_cost", "_index", "_ordinal"))
                  or folded in {"max_tokens", "min_tokens", "enabled"}):
                nested_kind = "scalar"
            else:
                nested_kind = None
            _assert_no_secret_payload(nested, path=f"{path}.{key}", leaf_kind=nested_kind)
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _assert_no_secret_payload(nested, path=f"{path}[{index}]", leaf_kind=leaf_kind)
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate or candidate != value or any(char.isspace() or ord(char) < 32 for char in candidate):
            raise ValueError(f"v2 payload string must be an opaque {leaf_kind or 'reference'}: {path}")
        if leaf_kind == "ref":
            _assert_internal_opaque_ref(candidate, field=path)
        elif leaf_kind in {"revision", "id", "scope", "digest"}:
            _assert_opaque_token(candidate, field=path)
        elif leaf_kind == "metadata":
            if not re.fullmatch(r"[A-Za-z0-9._/-]{1,64}", candidate):
                raise ValueError(f"v2 payload metadata is unsafe: {path}")
        else:
            raise ValueError(f"v2 payload permits strings only in reference-only envelope: {path}")
    elif value is None:
        raise ValueError(f"v2 payload does not permit null values: {path}")
    elif isinstance(value, bool):
        if leaf_kind != "scalar" or not path.endswith(".enabled"):
            raise ValueError(f"v2 payload boolean lacks explicit scalar semantics: {path}")
    elif isinstance(value, (int, float)):
        if leaf_kind != "scalar" or isinstance(value, bool):
            raise ValueError(f"v2 payload number lacks explicit scalar semantics: {path}")
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"v2 payload scalar must be finite and non-negative: {path}")
    else:
        raise ValueError(f"v2 payload value is not reference-only: {path}")


@dataclass(frozen=True)
class GateDecisionFact:
    decision: GateDecision
    rule_ref: str
    scope_ref: str
    budget_after: Mapping[str, object]
    secret_scope: str
    policy_revision: str
    mutated_intent_digest: str | None = None

    def __post_init__(self) -> None:
        for name in ("rule_ref", "scope_ref", "secret_scope", "policy_revision"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty")
        for name in ("rule_ref", "scope_ref", "secret_scope"):
            _assert_internal_opaque_ref(getattr(self, name), field=name)
        _assert_opaque_token(self.policy_revision, field="policy_revision")
        _assert_no_secret_payload(self.budget_after, path="budget_after")
        if not isinstance(self.decision, GateDecision):
            raise TypeError("decision must be a GateDecision")
        _canonical(self.budget_after)
        if self.decision is GateDecision.MUTATE and not self.mutated_intent_digest:
            raise ValueError("mutate Gate decision requires mutated_intent_digest")
        if self.decision is GateDecision.MUTATE and not re.fullmatch(r"[0-9a-f]{64}", self.mutated_intent_digest or ""):
            raise ValueError("mutate Gate decision requires a BLAKE3 intent digest")
        if self.decision is not GateDecision.MUTATE and self.mutated_intent_digest is not None:
            raise ValueError("only mutate Gate decisions may carry mutated_intent_digest")

    @property
    def decision_digest(self) -> str:
        return blake3(_canonical({
            "decision": self.decision.value, "rule_ref": self.rule_ref,
            "scope_ref": self.scope_ref, "budget_after": dict(self.budget_after),
            "secret_scope": self.secret_scope, "policy_revision": self.policy_revision,
            "mutated_intent_digest": self.mutated_intent_digest,
        }).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EffectReceipt:
    receipt_ref: str
    receipt_kind: str
    receipt_schema_version: str
    intent_schema_version: str

    def __post_init__(self) -> None:
        for name in ("receipt_ref", "receipt_kind", "receipt_schema_version", "intent_schema_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")


@dataclass(frozen=True)
class EffectIntent:
    session_id: str
    root_id: str
    step_key: str
    kind: str
    effect_class: EffectClass
    intent_ref: str
    gate_decision_id: str
    rev_set: Mapping[str, object]
    payload: Mapping[str, object]
    purpose: EffectPurpose = EffectPurpose.PRIMARY
    turn_id: str | None = None
    parent_id: str | None = None
    idem_key: str | None = None
    operation_id_override: str | None = None
    contract_version: str = LEGACY_V1
    intent_schema_version: str = "legacy-v1"
    expected_receipt_kind: str | None = None
    expected_receipt_schema_version: str | None = None

    def __post_init__(self) -> None:
        required = {
            "session_id": self.session_id,
            "root_id": self.root_id,
            "step_key": self.step_key,
            "kind": self.kind,
            "intent_ref": self.intent_ref,
            "gate_decision_id": self.gate_decision_id,
        }
        for field_name, value in required.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        if not isinstance(self.effect_class, EffectClass):
            raise TypeError("effect_class must be an EffectClass")
        if not isinstance(self.purpose, EffectPurpose):
            raise TypeError("purpose must be an EffectPurpose")
        if self.contract_version not in {LEGACY_V1, EFFECT_V2}:
            raise ValueError("unsupported Effect contract version")
        if not isinstance(self.rev_set, Mapping) or not self.rev_set:
            raise ValueError("rev_set must freeze at least one revision fact")
        for key, value in self.rev_set.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("rev_set keys must be non-empty strings")
            if value is None or isinstance(value, (dict, list, set)):
                raise ValueError("rev_set values must be non-null scalar revision facts")
            if not str(value).strip():
                raise ValueError("rev_set values must be non-empty revision facts")
        try:
            _canonical(self.payload)
            _canonical(self.rev_set)
        except (TypeError, ValueError) as error:
            raise ValueError("effect intent must be canonically serializable") from error
        if self.contract_version == EFFECT_V2:
            if self.operation_id_override is not None:
                raise ValueError("effect-v2 forbids operation_id_override")
            if tuple(sorted(self.rev_set)) != tuple(sorted(V2_REVISION_KEYS)):
                raise ValueError("v2 rev_set must contain the closed authority revision set")
            if any(not isinstance(value, str) or not value.strip() for value in self.rev_set.values()):
                raise ValueError("v2 authority revisions must be non-empty strings or not_applicable")
            for key, value in self.rev_set.items():
                if value != NOT_APPLICABLE:
                    _assert_opaque_token(value, field=f"rev_set.{key}")
            _assert_no_secret_payload(self.payload)
            _assert_internal_opaque_ref(self.intent_ref, field="intent_ref")
            for name in ("session_id", "root_id", "step_key", "kind"):
                _assert_opaque_token(getattr(self, name), field=name)
            for name in ("turn_id", "parent_id", "idem_key"):
                value = getattr(self, name)
                if value is not None:
                    _assert_opaque_token(value, field=name)
            if not self.expected_receipt_kind or not self.expected_receipt_schema_version:
                raise ValueError("v2 intent requires expected receipt kind and schema version")
            _assert_opaque_token(self.gate_decision_id, field="gate_decision_id")
            _assert_opaque_token(self.intent_schema_version, field="intent_schema_version")
            _assert_opaque_token(self.expected_receipt_kind, field="expected_receipt_kind")
            _assert_opaque_token(self.expected_receipt_schema_version, field="expected_receipt_schema_version")

    @property
    def intent_digest(self) -> str:
        material = _canonical(self.payload).encode("utf-8")
        return (blake3(material).hexdigest() if self.contract_version == EFFECT_V2
                else hashlib.blake2b(material, digest_size=20).hexdigest())

    @property
    def authority_set_id(self) -> str:
        if self.contract_version != EFFECT_V2:
            return "legacy-not-applicable"
        return "auth_" + blake3(_canonical(dict(self.rev_set)).encode("utf-8")).hexdigest()

    @property
    def identity_algorithm(self) -> str:
        return V2_IDENTITY_ALGORITHM if self.contract_version == EFFECT_V2 else LEGACY_IDENTITY_ALGORITHM

    @property
    def revision_schema_version(self) -> str:
        return V2_REVISION_SCHEMA_VERSION if self.contract_version == EFFECT_V2 else LEGACY_REVISION_SCHEMA_VERSION

    @property
    def operation_id(self) -> str:
        if self.operation_id_override is not None:
            if not self.operation_id_override.strip():
                raise ValueError("operation_id_override must be non-empty")
            return self.operation_id_override
        if self.contract_version == EFFECT_V2:
            return derive_v2_operation_id(
                session_id=self.session_id, root_id=self.root_id, step_key=self.step_key,
                intent_digest=self.intent_digest,
            )
        return derive_operation_id(
            session_id=self.session_id,
            root_id=self.root_id,
            step_key=self.step_key,
            intent=self.payload,
        )


@dataclass(frozen=True)
class Effect:
    operation_id: str
    session_id: str
    turn_id: str | None
    root_id: str
    parent_id: str | None
    step_key: str
    kind: str
    effect_class: EffectClass
    purpose: EffectPurpose
    intent_ref: str
    intent_digest: str
    gate_decision_id: str
    rev_set: Mapping[str, object]
    idem_key: str | None
    state: EffectState
    attempt: int
    lease_owner: str | None
    lease_expires_at: float | None
    probe_ref: str | None
    result_ref: str | None
    error_ref: str | None
    occurred_at: int
    recorded_at: int
    settled_at: int | None
    contract_version: str = LEGACY_V1
    authority_set_id: str = "legacy-not-applicable"
    intent_schema_version: str = "legacy-v1"
    expected_receipt_kind: str | None = None
    expected_receipt_schema_version: str | None = None
    identity_algorithm: str = LEGACY_IDENTITY_ALGORITHM
    revision_schema_version: str = LEGACY_REVISION_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class EffectLeaseFence:
    """Immutable execution generation presented by an Effect worker.

    Owner identity alone cannot fence two threads from the same process.  The
    persisted Effect attempt is therefore the claim generation and the exact
    lease expiry is part of the compare-and-set identity.
    """

    operation_id: str
    owner_id: str
    generation: int
    lease_expires_at: float

    @classmethod
    def from_effect(cls, effect: Effect) -> "EffectLeaseFence":
        if effect.state is not EffectState.INFLIGHT:
            raise InvalidEffectTransition("Effect lease fence requires INFLIGHT state")
        if not isinstance(effect.lease_owner, str) or not effect.lease_owner.strip():
            raise InvalidEffectTransition("Effect lease fence requires an owner")
        if effect.attempt < 0:
            raise InvalidEffectTransition("Effect lease fence requires a non-negative generation")
        if not isinstance(effect.lease_expires_at, (int, float)):
            raise InvalidEffectTransition("Effect lease fence requires an expiry")
        return cls(
            operation_id=effect.operation_id,
            owner_id=effect.lease_owner,
            generation=effect.attempt,
            lease_expires_at=float(effect.lease_expires_at),
        )


class EffectLog:
    """SQLite authority for effect intent, lease and terminal outcome.

    A connection is opened per operation so separate runners can coordinate through
    SQLite.  State mutation always uses BEGIN IMMEDIATE and compare-and-set clauses.
    """

    def __init__(self, database: str | Path):
        resolved = Path(database).expanduser().resolve(strict=False)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self.database = str(resolved)
        self._schema_lock = threading.Lock()
        self._ensure_schema()

    @reusable_connection(row_factory=sqlite3.Row, isolation_level=None, timeout_ms=30000)
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None, check_same_thread=False)
        observe_connection(connection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _ensure_schema(self) -> None:
        with self._schema_lock, self._connect() as connection:
            meta_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='effect_contract_meta'",
            ).fetchone() is not None
            if meta_exists:
                schema_row = connection.execute(
                    "SELECT value FROM effect_contract_meta WHERE key='schema_version'",
                ).fetchone()
                if schema_row is not None:
                    try:
                        stored_schema = int(str(schema_row[0]))
                    except (TypeError, ValueError) as error:
                        raise RuntimeError("Effect schema version is invalid") from error
                    if stored_schema < 1:
                        raise RuntimeError("Effect schema version is invalid")
                    if stored_schema > EFFECT_SCHEMA_VERSION:
                        raise RuntimeError("Effect schema version is newer than this runtime")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS effect (
                  operation_id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  turn_id TEXT,
                  root_id TEXT NOT NULL,
                  parent_id TEXT,
                  step_key TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  effect_class TEXT NOT NULL CHECK(effect_class IN ('PURE','IDEMPOTENT','QUERYABLE','AT_MOST_ONCE','NEEDS_REAUTH')),
                  purpose TEXT NOT NULL CHECK(purpose IN ('primary','aux','probe')),
                  intent_ref TEXT NOT NULL,
                  intent_digest TEXT NOT NULL,
                  gate_decision_id TEXT NOT NULL,
                  rev_set TEXT NOT NULL,
                  idem_key TEXT,
                  state TEXT NOT NULL CHECK(state IN ('PLANNED','INFLIGHT','SETTLED_OK','SETTLED_ERR','UNKNOWN','COMPENSATED','ABANDONED')),
                  attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
                  lease_owner TEXT,
                  lease_expires_at INTEGER,
                  probe_ref TEXT,
                  result_ref TEXT,
                  error_ref TEXT,
                  occurred_at INTEGER NOT NULL,
                  recorded_at INTEGER NOT NULL,
                  settled_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS ix_effect_recover ON effect(state, lease_expires_at);
                CREATE INDEX IF NOT EXISTS ix_effect_tree ON effect(root_id, occurred_at, operation_id);
                CREATE TABLE IF NOT EXISTS effect_receipt (
                  operation_id TEXT PRIMARY KEY REFERENCES effect(operation_id),
                  receipt_ref TEXT NOT NULL UNIQUE,
                  receipt_kind TEXT NOT NULL,
                  recorded_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS effect_contract_meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS effect_gate_fact (
                  decision_id TEXT PRIMARY KEY,
                  decision TEXT NOT NULL,
                  rule_ref TEXT NOT NULL,
                  scope_ref TEXT NOT NULL,
                  budget_after TEXT NOT NULL,
                  secret_scope TEXT NOT NULL,
                  policy_revision TEXT NOT NULL,
                  mutated_intent_digest TEXT,
                  decision_digest TEXT NOT NULL UNIQUE,
                  recorded_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS effect_intent_fact (
                  operation_id TEXT PRIMARY KEY REFERENCES effect(operation_id),
                  intent_ref TEXT NOT NULL,
                  intent_digest TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  schema_version TEXT NOT NULL,
                  recorded_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS effect_cancellation_request (
                  operation_id TEXT PRIMARY KEY REFERENCES effect(operation_id),
                  request_ref TEXT NOT NULL,
                  requested_at INTEGER NOT NULL
                );
                """
            )
            # ALTERs are intentionally idempotent for databases created by v1.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(effect)")}
            for name, definition in (
                ("contract_version", "TEXT NOT NULL DEFAULT 'legacy-v1'"),
                ("authority_set_id", "TEXT NOT NULL DEFAULT 'legacy-not-applicable'"),
                ("intent_schema_version", "TEXT NOT NULL DEFAULT 'legacy-v1'"),
                ("expected_receipt_kind", "TEXT"),
                ("expected_receipt_schema_version", "TEXT"),
                ("identity_algorithm", "TEXT NOT NULL DEFAULT 'blake2b-160'"),
                ("revision_schema_version", "TEXT NOT NULL DEFAULT 'legacy-v1'"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE effect ADD COLUMN {name} {definition}")
            receipt_columns = {row[1] for row in connection.execute("PRAGMA table_info(effect_receipt)")}
            if "receipt_schema_version" not in receipt_columns:
                connection.execute(
                    "ALTER TABLE effect_receipt ADD COLUMN receipt_schema_version TEXT NOT NULL DEFAULT 'legacy-v1'"
                )
            connection.execute(
                "INSERT OR REPLACE INTO effect_contract_meta(key,value) VALUES('schema_version',?)",
                (str(EFFECT_SCHEMA_VERSION),),
            )
            duplicate = connection.execute(
                "SELECT result_ref FROM effect WHERE state='SETTLED_OK' AND result_ref IS NOT NULL "
                "GROUP BY result_ref HAVING COUNT(*)>1 LIMIT 1"
            ).fetchone()
            if duplicate is not None:
                raise RuntimeError("legacy Effect Receipt reference is shared by multiple Effects")
            connection.execute(
                "INSERT OR IGNORE INTO effect_receipt(operation_id,receipt_ref,receipt_kind,recorded_at) "
                "SELECT operation_id,result_ref,kind || '-legacy-receipt',"
                "COALESCE(settled_at,recorded_at) FROM effect "
                "WHERE state='SETTLED_OK' AND result_ref IS NOT NULL"
            )
            drift = connection.execute(
                "SELECT e.operation_id FROM effect e JOIN effect_receipt r "
                "ON r.operation_id=e.operation_id WHERE e.state='SETTLED_OK' "
                "AND e.result_ref<>r.receipt_ref LIMIT 1"
            ).fetchone()
            if drift is not None:
                raise RuntimeError("legacy Effect Receipt binding drifted")

    def request_cancellation(
        self, operation_id: str, *, request_ref: str, now: int,
    ) -> Effect:
        """Record idempotent user coordination without changing execution state."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            effect = self.request_cancellation_in_connection(
                connection, operation_id, request_ref=request_ref, now=now,
            )
            connection.commit()
        return effect

    def request_cancellation_in_connection(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        *,
        request_ref: str,
        now: int,
    ) -> Effect:
        """Record cancellation inside a caller-owned transaction."""

        if not connection.in_transaction:
            raise RuntimeError("Effect cancellation requires a caller-owned transaction")
        if not request_ref.strip():
            raise ValueError("cancellation request_ref must be non-empty")
        effect = self.get_in_connection(connection, operation_id)
        if effect.contract_version == EFFECT_V2:
            _assert_internal_opaque_ref(request_ref, field="request_ref")
        if effect.state not in {EffectState.PLANNED, EffectState.INFLIGHT}:
            raise InvalidEffectTransition("terminal Effect cannot be cancelled")
        connection.execute(
            "INSERT OR IGNORE INTO effect_cancellation_request"
            "(operation_id,request_ref,requested_at) VALUES(?,?,?)",
            (operation_id, request_ref, now),
        )
        return effect

    def cancellation_requested(self, operation_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM effect_cancellation_request WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return row is not None

    def plan(self, intent: EffectIntent, *, now: int) -> tuple[Effect, bool]:
        if intent.contract_version == EFFECT_V2:
            raise ValueError("v2 Effect planning requires a durable GateDecisionFact")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            effect, created = self.plan_in_connection(connection, intent, now=now)
            connection.commit()
        return effect, created

    def plan_v2(
        self, intent: EffectIntent, *, gate_decision_id: str, gate_fact: GateDecisionFact,
        now: int,
    ) -> tuple[Effect, bool]:
        """Persist Gate fact, immutable intent fact and PLANNED Effect atomically."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                effect, created = self.plan_v2_in_connection(
                    connection,
                    intent,
                    gate_decision_id=gate_decision_id,
                    gate_fact=gate_fact,
                    now=now,
                )
                connection.commit()
                return effect, created
            except Exception:
                connection.rollback()
                raise

    def require_v2_execution_facts(
        self,
        intent: EffectIntent,
        *,
        gate_decision_id: str,
        gate_fact: GateDecisionFact,
    ) -> Effect:
        """Verify the complete immutable v2 authority before Handler work.

        Planning freezes three records in one transaction: the Effect row, its
        reference-only intent fact, and the Gate decision fact.  A Handler or
        recovery probe must be able to prove all three still match a trusted
        reconstruction without rewriting or repairing any of them.
        """

        self._require_v2_gate_binding(
            intent,
            gate_decision_id=gate_decision_id,
            gate_fact=gate_fact,
        )
        with self._connect() as connection:
            effect = self.get_in_connection(connection, intent.operation_id)
            self._require_frozen_effect_identity(effect, intent)
            stored_intent = connection.execute(
                "SELECT intent_ref,intent_digest,payload_json,schema_version "
                "FROM effect_intent_fact WHERE operation_id=?",
                (intent.operation_id,),
            ).fetchone()
            expected_intent = (
                intent.intent_ref,
                intent.intent_digest,
                _canonical(intent.payload),
                intent.intent_schema_version,
            )
            if stored_intent is None or tuple(stored_intent) != expected_intent:
                raise RuntimeError("v2 immutable intent fact drifted")
            stored_gate = connection.execute(
                "SELECT decision,rule_ref,scope_ref,budget_after,secret_scope,"
                "policy_revision,mutated_intent_digest,decision_digest "
                "FROM effect_gate_fact WHERE decision_id=?",
                (gate_decision_id,),
            ).fetchone()
            if stored_gate is None or tuple(stored_gate) != self._gate_fact_identity(gate_fact):
                raise RuntimeError("immutable Gate fact drifted")
            return effect

    def plan_v2_in_connection(
        self,
        connection: sqlite3.Connection,
        intent: EffectIntent,
        *,
        gate_decision_id: str,
        gate_fact: GateDecisionFact,
        now: int,
    ) -> tuple[Effect, bool]:
        """Compose v2 planning into a caller-owned transaction.

        The caller owns ``BEGIN``, commit and rollback so domain facts can be
        persisted atomically with the immutable Gate, intent and Effect facts.
        """
        self._require_v2_gate_binding(
            intent,
            gate_decision_id=gate_decision_id,
            gate_fact=gate_fact,
        )
        self._record_gate_fact_in_connection(connection, gate_decision_id, gate_fact, now=now)
        effect, created = self._plan_v2_in_connection(connection, intent, now=now)
        connection.execute(
            "INSERT OR IGNORE INTO effect_intent_fact(operation_id,intent_ref,intent_digest,payload_json,schema_version,recorded_at) "
            "VALUES(?,?,?,?,?,?)",
            (effect.operation_id, intent.intent_ref, intent.intent_digest,
             _canonical(intent.payload), intent.intent_schema_version, now),
        )
        stored = connection.execute(
            "SELECT intent_ref,intent_digest,payload_json,schema_version FROM effect_intent_fact WHERE operation_id=?",
            (effect.operation_id,),
        ).fetchone()
        assert stored is not None
        if tuple(stored) != (intent.intent_ref, intent.intent_digest, _canonical(intent.payload), intent.intent_schema_version):
            raise RuntimeError("v2 immutable intent fact drifted")
        return effect, created

    @staticmethod
    def _record_gate_fact_in_connection(
        connection: sqlite3.Connection, decision_id: str, fact: GateDecisionFact, *, now: int,
    ) -> None:
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise ValueError("gate_decision_id must be non-empty")
        connection.execute(
            "INSERT OR IGNORE INTO effect_gate_fact(decision_id,decision,rule_ref,scope_ref,budget_after,secret_scope,policy_revision,mutated_intent_digest,decision_digest,recorded_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (decision_id, fact.decision.value, fact.rule_ref, fact.scope_ref,
             _canonical(fact.budget_after), fact.secret_scope, fact.policy_revision,
             fact.mutated_intent_digest, fact.decision_digest, now),
        )
        row = connection.execute(
            "SELECT decision,rule_ref,scope_ref,budget_after,secret_scope,policy_revision,mutated_intent_digest,decision_digest FROM effect_gate_fact WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        expected = EffectLog._gate_fact_identity(fact)
        if row is None or tuple(row) != expected:
            raise RuntimeError("immutable Gate fact drifted")

    @staticmethod
    def _gate_fact_identity(fact: GateDecisionFact) -> tuple[object, ...]:
        return (
            fact.decision.value,
            fact.rule_ref,
            fact.scope_ref,
            _canonical(fact.budget_after),
            fact.secret_scope,
            fact.policy_revision,
            fact.mutated_intent_digest,
            fact.decision_digest,
        )

    @staticmethod
    def _require_v2_gate_binding(
        intent: EffectIntent,
        *,
        gate_decision_id: str,
        gate_fact: GateDecisionFact,
    ) -> None:
        if intent.contract_version != EFFECT_V2:
            raise ValueError("v2 execution facts require an effect-v2 intent")
        if not isinstance(gate_fact, GateDecisionFact):
            raise TypeError("v2 execution facts require a GateDecisionFact")
        if intent.gate_decision_id != gate_decision_id:
            raise ValueError("v2 Gate decision id drifted from intent")
        if gate_fact.decision in {GateDecision.DENY, GateDecision.ASK}:
            raise InvalidEffectTransition(
                "deny or ask Gate decision cannot plan or authorize an Effect"
            )
        if (
            gate_fact.decision is GateDecision.MUTATE
            and gate_fact.mutated_intent_digest != intent.intent_digest
        ):
            raise ValueError("mutated v2 intent digest does not match Gate fact")
        if gate_fact.policy_revision != str(intent.rev_set["policy"]):
            raise ValueError("Gate policy revision drifted from v2 authority set")

    def plan_in_connection(
        self, connection: sqlite3.Connection, intent: EffectIntent, *, now: int,
    ) -> tuple[Effect, bool]:
        """Plan inside a caller-owned transaction for atomic composition."""
        if intent.contract_version == EFFECT_V2:
            raise ValueError("v2 Effect planning is private to plan_v2 Gate/Intent composition")
        return self._plan_in_connection(connection, intent, now=now)

    def _plan_v2_in_connection(
        self, connection: sqlite3.Connection, intent: EffectIntent, *, now: int,
    ) -> tuple[Effect, bool]:
        gate = connection.execute(
            "SELECT 1 FROM effect_gate_fact WHERE decision_id=?", (intent.gate_decision_id,),
        ).fetchone()
        if gate is None:
            raise RuntimeError("v2 Gate fact was not persisted")
        return self._plan_in_connection(connection, intent, now=now)

    def _plan_in_connection(
        self, connection: sqlite3.Connection, intent: EffectIntent, *, now: int,
    ) -> tuple[Effect, bool]:
        operation_id = intent.operation_id
        values = (
            operation_id,
            intent.session_id,
            intent.turn_id,
            intent.root_id,
            intent.parent_id,
            intent.step_key,
            intent.kind,
            intent.effect_class.value,
            intent.purpose.value,
            intent.intent_ref,
            intent.intent_digest,
            intent.gate_decision_id,
            _canonical(intent.rev_set),
            intent.idem_key,
            EffectState.PLANNED.value,
            now,
            now,
            intent.contract_version,
            intent.authority_set_id,
            intent.intent_schema_version,
            intent.expected_receipt_kind,
            intent.expected_receipt_schema_version,
            intent.identity_algorithm,
            intent.revision_schema_version,
        )
        cursor = connection.execute(
            """INSERT OR IGNORE INTO effect(
            operation_id,session_id,turn_id,root_id,parent_id,step_key,kind,effect_class,purpose,
            intent_ref,intent_digest,gate_decision_id,rev_set,idem_key,state,occurred_at,recorded_at,
            contract_version,authority_set_id,intent_schema_version,expected_receipt_kind,expected_receipt_schema_version
            ,identity_algorithm,revision_schema_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        row = connection.execute("SELECT * FROM effect WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise RuntimeError("effect planning did not produce a row")
        effect = _row_to_effect(row)
        self._require_frozen_effect_identity(effect, intent)
        return effect, cursor.rowcount == 1

    @staticmethod
    def _require_frozen_effect_identity(effect: Effect, intent: EffectIntent) -> None:
        frozen_identity = (
            effect.session_id,
            effect.turn_id,
            effect.root_id,
            effect.parent_id,
            effect.step_key,
            effect.kind,
            effect.effect_class,
            effect.purpose,
            effect.intent_ref,
            effect.intent_digest,
            effect.gate_decision_id,
            _canonical(effect.rev_set),
            effect.idem_key,
            effect.contract_version,
            effect.authority_set_id,
            effect.intent_schema_version,
            effect.expected_receipt_kind,
            effect.expected_receipt_schema_version,
            effect.identity_algorithm,
            effect.revision_schema_version,
        )
        requested_identity = (
            intent.session_id,
            intent.turn_id,
            intent.root_id,
            intent.parent_id,
            intent.step_key,
            intent.kind,
            intent.effect_class,
            intent.purpose,
            intent.intent_ref,
            intent.intent_digest,
            intent.gate_decision_id,
            _canonical(intent.rev_set),
            intent.idem_key,
            intent.contract_version,
            intent.authority_set_id,
            intent.intent_schema_version,
            intent.expected_receipt_kind,
            intent.expected_receipt_schema_version,
            intent.identity_algorithm,
            intent.revision_schema_version,
        )
        if frozen_identity != requested_identity:
            raise RuntimeError("operation id collision with a different frozen intent")

    def get(self, operation_id: str) -> Effect:
        with self._connect() as connection:
            return self.get_in_connection(connection, operation_id)

    def get_in_connection(
        self, connection: sqlite3.Connection, operation_id: str,
    ) -> Effect:
        row = connection.execute(
            "SELECT * FROM effect WHERE operation_id=?", (operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return _row_to_effect(row)

    def transition(
        self,
        operation_id: str,
        *,
        expected: EffectState,
        target: EffectState,
        now: int,
        lease_owner: str | None = None,
        lease_expires_at: int | float | None = None,
        probe_ref: str | None = None,
        result_ref: str | None = None,
        error_ref: str | None = None,
        increment_attempt: bool = False,
        fence: EffectLeaseFence | None = None,
        fence_must_be_expired: bool = False,
    ) -> Effect:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            effect = self.transition_in_connection(
                connection,
                operation_id,
                expected=expected,
                target=target,
                now=now,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
                probe_ref=probe_ref,
                result_ref=result_ref,
                error_ref=error_ref,
                increment_attempt=increment_attempt,
                fence=fence,
                fence_must_be_expired=fence_must_be_expired,
            )
            connection.commit()
        return effect

    def transition_in_connection(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        *,
        expected: EffectState,
        target: EffectState,
        now: int,
        lease_owner: str | None = None,
        lease_expires_at: int | float | None = None,
        probe_ref: str | None = None,
        result_ref: str | None = None,
        error_ref: str | None = None,
        increment_attempt: bool = False,
        fence: EffectLeaseFence | None = None,
        fence_must_be_expired: bool = False,
    ) -> Effect:
        """Transition inside a caller-owned transaction."""
        existing_row = connection.execute(
            "SELECT contract_version FROM effect WHERE operation_id=?", (operation_id,),
        ).fetchone()
        is_v2 = existing_row is not None and str(existing_row[0]) == EFFECT_V2
        if is_v2:
            for name, value in (("probe_ref", probe_ref), ("result_ref", result_ref), ("error_ref", error_ref)):
                if value is not None:
                    _assert_internal_opaque_ref(value, field=name)
        if target is EffectState.SETTLED_OK:
            if is_v2:
                raise InvalidEffectTransition("effect-v2 SETTLED_OK requires Receipt binding API")
        if target not in _TRANSITIONS[expected]:
            raise InvalidEffectTransition(f"{expected.value} -> {target.value}")
        if fence is not None:
            if expected is not EffectState.INFLIGHT:
                raise InvalidEffectTransition("Effect lease fence requires INFLIGHT source state")
            if fence.operation_id != operation_id:
                raise InvalidEffectTransition("Effect lease fence operation drifted")
            self._assert_fence_in_connection(
                connection,
                fence,
                now=now,
                must_be_expired=fence_must_be_expired,
            )
        elif fence_must_be_expired:
            raise ValueError("expired lease check requires an Effect fence")
        if target is EffectState.INFLIGHT:
            if not isinstance(lease_owner, str) or not lease_owner.strip():
                raise InvalidEffectTransition("INFLIGHT requires a lease owner")
            if (
                not isinstance(lease_expires_at, (int, float))
                or isinstance(lease_expires_at, bool)
                or lease_expires_at <= now
            ):
                raise InvalidEffectTransition("INFLIGHT requires a future lease expiry")
        if target is EffectState.SETTLED_OK:
            if not isinstance(result_ref, str) or not result_ref.strip():
                raise InvalidEffectTransition("SETTLED_OK requires an immutable receipt reference")
        if target is EffectState.SETTLED_ERR:
            if not isinstance(error_ref, str) or not error_ref.strip():
                raise InvalidEffectTransition("SETTLED_ERR requires an error reference")
        settled_at = now if target in {
            EffectState.SETTLED_OK,
            EffectState.SETTLED_ERR,
            EffectState.COMPENSATED,
            EffectState.ABANDONED,
        } else None
        fence_sql, fence_values = self._fence_where(
            fence, now=now, must_be_expired=fence_must_be_expired,
        )
        cursor = connection.execute(
            """UPDATE effect SET state=?, attempt=attempt+?, lease_owner=?, lease_expires_at=?,
            probe_ref=COALESCE(?,probe_ref), result_ref=COALESCE(?,result_ref),
            error_ref=COALESCE(?,error_ref), settled_at=?
            WHERE operation_id=? AND state=?""" + fence_sql,
            (
                target.value,
                int(increment_attempt),
                lease_owner,
                lease_expires_at,
                probe_ref,
                result_ref,
                error_ref,
                settled_at,
                operation_id,
                expected.value,
                *fence_values,
            ),
        )
        if cursor.rowcount != 1:
            row = connection.execute(
                "SELECT state FROM effect WHERE operation_id=?", (operation_id,)
            ).fetchone()
            found = str(row[0]) if row else "missing"
            raise InvalidEffectTransition(f"expected {expected.value}, found {found}")
        row = connection.execute("SELECT * FROM effect WHERE operation_id=?", (operation_id,)).fetchone()
        assert row is not None
        return _row_to_effect(row)

    def assert_active_fence_in_connection(
        self,
        connection: sqlite3.Connection,
        fence: EffectLeaseFence,
        *,
        now: int | float,
    ) -> Effect:
        """Authorize a domain checkpoint inside its caller-owned transaction."""

        return self._assert_fence_in_connection(
            connection, fence, now=now, must_be_expired=False,
        )

    def _assert_fence_in_connection(
        self,
        connection: sqlite3.Connection,
        fence: EffectLeaseFence,
        *,
        now: int | float,
        must_be_expired: bool,
    ) -> Effect:
        if not isinstance(fence, EffectLeaseFence):
            raise TypeError("fence must be an EffectLeaseFence")
        comparison = "<=" if must_be_expired else ">"
        row = connection.execute(
            "SELECT * FROM effect WHERE operation_id=? AND state='INFLIGHT' "
            "AND lease_owner=? AND attempt=? AND lease_expires_at=? "
            f"AND lease_expires_at {comparison} ?",
            (
                fence.operation_id,
                fence.owner_id,
                fence.generation,
                fence.lease_expires_at,
                now,
            ),
        ).fetchone()
        if row is None:
            raise InvalidEffectTransition("Effect lease fence is stale or expired")
        return _row_to_effect(row)

    @staticmethod
    def _fence_where(
        fence: EffectLeaseFence | None,
        *,
        now: int | float,
        must_be_expired: bool,
    ) -> tuple[str, tuple[object, ...]]:
        if fence is None:
            return "", ()
        comparison = "<=" if must_be_expired else ">"
        return (
            " AND lease_owner=? AND attempt=? AND lease_expires_at=? "
            f"AND lease_expires_at {comparison} ?",
            (
                fence.owner_id,
                fence.generation,
                fence.lease_expires_at,
                now,
            ),
        )

    def settle_ok_with_receipt_in_connection(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        *,
        expected: EffectState,
        receipt_ref: str,
        receipt_kind: str,
        receipt_schema_version: str = "legacy-v1",
        intent_schema_version: str | None = None,
        now: int | float,
        fence: EffectLeaseFence | None = None,
        fence_must_be_expired: bool = False,
    ) -> Effect:
        """Bind one immutable Receipt fact and settle its Effect atomically.

        The Receipt payload remains in its domain-owned immutable fact store.
        Core stores only the opaque reference and forbids one Receipt from
        becoming the outcome of two Effects.
        """

        for name, value in (
            ("operation_id", operation_id),
            ("receipt_ref", receipt_ref),
            ("receipt_kind", receipt_kind),
            ("receipt_schema_version", receipt_schema_version),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if expected not in {EffectState.INFLIGHT, EffectState.UNKNOWN}:
            raise InvalidEffectTransition("Receipt binding requires INFLIGHT or UNKNOWN Effect")
        if expected is EffectState.INFLIGHT:
            if fence is not None:
                if fence.operation_id != operation_id:
                    raise InvalidEffectTransition("Effect lease fence operation drifted")
                self._assert_fence_in_connection(
                    connection,
                    fence,
                    now=now,
                    must_be_expired=fence_must_be_expired,
                )
            elif fence_must_be_expired:
                raise ValueError("expired lease check requires an Effect fence")
        elif fence is not None or fence_must_be_expired:
            raise InvalidEffectTransition("UNKNOWN Receipt verification cannot use an execution fence")
        existing = connection.execute(
            "SELECT receipt_ref,receipt_kind,receipt_schema_version FROM effect_receipt WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        effect_row = connection.execute("SELECT * FROM effect WHERE operation_id=?", (operation_id,)).fetchone()
        if effect_row is None:
            raise KeyError(operation_id)
        target_effect = _row_to_effect(effect_row)
        if target_effect.contract_version == EFFECT_V2:
            _assert_internal_opaque_ref(receipt_ref, field="receipt_ref")
            if (receipt_kind != target_effect.expected_receipt_kind or
                    receipt_schema_version != target_effect.expected_receipt_schema_version or
                    intent_schema_version != target_effect.intent_schema_version):
                raise InvalidEffectTransition("v2 Receipt contract does not match frozen intent")
        if existing is not None:
            if (str(existing[0]), str(existing[1]), str(existing[2])) != (receipt_ref, receipt_kind, receipt_schema_version):
                raise InvalidEffectTransition("Effect Receipt binding drifted")
            projected = target_effect
            if projected.state is EffectState.SETTLED_OK and projected.result_ref == receipt_ref:
                return projected
            if projected.state is expected:
                return self._settle_ok_after_receipt_binding_in_connection(
                    connection, operation_id, expected=expected, now=now,
                    result_ref=receipt_ref, fence=fence,
                    fence_must_be_expired=fence_must_be_expired,
                )
            raise InvalidEffectTransition("Effect Receipt terminal state drifted")
        try:
            connection.execute(
                "INSERT INTO effect_receipt(operation_id,receipt_ref,receipt_kind,receipt_schema_version,recorded_at) "
                "VALUES(?,?,?,?,?)",
                (operation_id, receipt_ref, receipt_kind, receipt_schema_version, now),
            )
        except sqlite3.IntegrityError as error:
            raise InvalidEffectTransition("Receipt is already bound to another Effect") from error
        try:
            # Bypass generic transition only after the immutable binding exists.
            return self._settle_ok_after_receipt_binding_in_connection(
                connection,
                operation_id,
                expected=expected,
                now=now,
                result_ref=receipt_ref,
                fence=fence,
                fence_must_be_expired=fence_must_be_expired,
            )
        except Exception:
            connection.execute(
                "DELETE FROM effect_receipt WHERE operation_id=? AND receipt_ref=?",
                (operation_id, receipt_ref),
            )
            raise

    def settle_ok_with_receipt(
        self,
        operation_id: str,
        *,
        expected: EffectState,
        receipt_ref: str,
        receipt_kind: str,
        receipt_schema_version: str = "legacy-v1",
        intent_schema_version: str | None = None,
        now: int | float,
        fence: EffectLeaseFence | None = None,
        fence_must_be_expired: bool = False,
    ) -> Effect:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            effect = self.settle_ok_with_receipt_in_connection(
                connection,
                operation_id,
                expected=expected,
                receipt_ref=receipt_ref,
                receipt_kind=receipt_kind,
                receipt_schema_version=receipt_schema_version,
                intent_schema_version=intent_schema_version,
                now=now,
                fence=fence,
                fence_must_be_expired=fence_must_be_expired,
            )
            connection.commit()
            return effect

    def _settle_ok_after_receipt_binding_in_connection(
        self, connection: sqlite3.Connection, operation_id: str, *, expected: EffectState,
        now: int | float, result_ref: str,
        fence: EffectLeaseFence | None = None,
        fence_must_be_expired: bool = False,
    ) -> Effect:
        fence_sql, fence_values = self._fence_where(
            fence, now=now, must_be_expired=fence_must_be_expired,
        )
        cursor = connection.execute(
            "UPDATE effect SET state='SETTLED_OK', result_ref=?, lease_owner=NULL, lease_expires_at=NULL, settled_at=? "
            "WHERE operation_id=? AND state=?" + fence_sql,
            (result_ref, now, operation_id, expected.value, *fence_values),
        )
        if cursor.rowcount != 1:
            row = connection.execute("SELECT state FROM effect WHERE operation_id=?", (operation_id,)).fetchone()
            raise InvalidEffectTransition(f"expected {expected.value}, found {str(row[0]) if row else 'missing'}")
        row = connection.execute("SELECT * FROM effect WHERE operation_id=?", (operation_id,)).fetchone()
        assert row is not None
        return _row_to_effect(row)

    def renew_or_take_over_lease_in_connection(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        *,
        lease_owner: str,
        lease_expires_at: int | float,
        now: int | float,
        fence: EffectLeaseFence | None = None,
    ) -> Effect:
        """Renew the same owner or fence and replace an expired owner."""

        if not isinstance(lease_owner, str) or not lease_owner.strip():
            raise ValueError("lease_owner must be non-empty")
        if (
            not isinstance(lease_expires_at, (int, float))
            or isinstance(lease_expires_at, bool)
            or lease_expires_at <= now
        ):
            raise InvalidEffectTransition("Effect lease expiry must be in the future")
        if fence is not None:
            if fence.operation_id != operation_id or fence.owner_id != lease_owner:
                raise InvalidEffectTransition("Effect renewal fence identity drifted")
            self.assert_active_fence_in_connection(connection, fence, now=now)
        row = connection.execute(
            "SELECT state,lease_owner,lease_expires_at,attempt FROM effect WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        if EffectState(str(row[0])) is not EffectState.INFLIGHT:
            raise InvalidEffectTransition("only INFLIGHT Effect lease can be renewed")
        current_owner = row[1]
        current_expiry = row[2]
        current_generation = int(row[3])
        same_owner = current_owner == lease_owner
        expired = current_expiry is None or float(current_expiry) <= now
        if not same_owner and not expired:
            raise InvalidEffectTransition("Effect has an active lease")
        changed = connection.execute(
            "UPDATE effect SET lease_owner=?,lease_expires_at=?,attempt=attempt+? "
            "WHERE operation_id=? AND state='INFLIGHT' AND lease_owner IS ? "
            "AND lease_expires_at IS ? AND attempt=?",
            (
                lease_owner,
                lease_expires_at,
                int(expired),
                operation_id,
                current_owner,
                current_expiry,
                current_generation,
            ),
        )
        if changed.rowcount != 1:
            raise InvalidEffectTransition("Effect lease compare-and-set conflict")
        updated = connection.execute(
            "SELECT * FROM effect WHERE operation_id=?", (operation_id,),
        ).fetchone()
        if updated is None:
            raise KeyError(operation_id)
        return _row_to_effect(updated)

    def expired_inflight(self, *, now: int, limit: int = 100) -> list[Effect]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM effect WHERE state='INFLIGHT'
                AND lease_expires_at IS NOT NULL AND lease_expires_at<=?
                ORDER BY lease_expires_at,operation_id LIMIT ?""",
                (now, limit),
            ).fetchall()
        return [_row_to_effect(row) for row in rows]

    def planned_for_kinds(
        self, kinds: tuple[str, ...], *, limit: int = 100,
    ) -> list[Effect]:
        if limit <= 0 or not kinds:
            return []
        placeholders = ",".join("?" for _ in kinds)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM effect WHERE state='PLANNED' AND kind IN ({placeholders}) "
                "ORDER BY recorded_at,operation_id LIMIT ?",
                (*kinds, limit),
            ).fetchall()
        return [_row_to_effect(row) for row in rows]


Handler = Callable[[Effect], str | EffectReceipt]


class EffectRunner:
    def __init__(
        self, log: EffectLog, *, owner_id: str, lease_seconds: int | float = 30,
        lease_heartbeat_seconds: int | float | None = None,
    ):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if lease_heartbeat_seconds is not None and not 0 < lease_heartbeat_seconds < lease_seconds:
            raise ValueError("lease heartbeat must be positive and shorter than the lease")
        self.log = log
        self.owner_id = owner_id
        self.lease_seconds = lease_seconds
        self.lease_heartbeat_seconds = lease_heartbeat_seconds

    def execute(self, intent: EffectIntent, handler: Handler, *, now: int) -> Effect:
        if intent.contract_version == EFFECT_V2:
            raise ValueError("effect-v2 execution requires execute_v2 and a GateDecisionFact")
        planned, _ = self.log.plan(intent, now=now)
        return self.execute_planned(
            planned.operation_id,
            handler,
            now=now,
            receipt_kind=f"{planned.kind}-receipt",
        )

    def execute_v2(
        self, intent: EffectIntent, handler: Handler, *, gate_decision_id: str,
        gate_fact: GateDecisionFact, now: int,
    ) -> Effect:
        planned, _ = self.log.plan_v2(
            intent, gate_decision_id=gate_decision_id, gate_fact=gate_fact, now=now,
        )
        return self.execute_planned(
            planned.operation_id, handler, now=now, receipt_kind=planned.expected_receipt_kind,
        )

    def execute_planned(
        self,
        operation_id: str,
        handler: Handler,
        *,
        now: int,
        receipt_kind: str | None = None,
        lease_expires_at: int | float | None = None,
    ) -> Effect:
        """Claim and execute one existing PLANNED Effect.

        A Handler exception leaves the Effect INFLIGHT. Core Reaper owns the
        retry or UNKNOWN decision after the lease expires.
        """
        inflight, claimed = self.claim_planned(
            operation_id, now=now, lease_expires_at=lease_expires_at,
        )
        if not claimed:
            return inflight
        current = [inflight]
        lost = [False]
        stopped = threading.Event()
        heartbeat: threading.Thread | None = None
        started_at = time.monotonic()

        def logical_now() -> float:
            # ``now`` may be a deterministic test clock.  Advancing it with a
            # monotonic duration keeps the renewal fence in the same time
            # domain as the original claim without depending on wall-clock
            # jumps.
            return now + (time.monotonic() - started_at)

        if self.lease_heartbeat_seconds is not None:
            def renew_loop() -> None:
                while not stopped.wait(self.lease_heartbeat_seconds):
                    now_live = logical_now()
                    try:
                        with self.log._connect() as connection:
                            connection.execute("BEGIN IMMEDIATE")
                            current[0] = self.renew(
                                current[0], now=now_live,
                                lease_expires_at=now_live + self.lease_seconds,
                                connection=connection,
                            )
                            connection.commit()
                    except Exception:
                        # A short SQLite writer collision is not proof that
                        # the execution fence was lost.  Keep retrying while
                        # the last confirmed fence is still active; once it
                        # expires, the handler may no longer settle.
                        if float(current[0].lease_expires_at or 0) <= logical_now():
                            lost[0] = True
                            return
            heartbeat = threading.Thread(target=renew_loop, daemon=True)
            heartbeat.start()
        abandoned: EffectHandlerAbandoned | None = None
        deferred: EffectHandlerDeferred | None = None
        try:
            result = handler(inflight)
        except EffectHandlerAbandoned as error:
            abandoned = error
            result = None
        except EffectHandlerDeferred as error:
            deferred = error
            result = None
        finally:
            stopped.set()
            if heartbeat is not None:
                heartbeat.join(timeout=self.lease_heartbeat_seconds + 1)
        if lost[0]:
            raise InvalidEffectTransition("Effect lease renewal lost its fence")
        # A Turn runner may renew this model-call Effect in a separate SQLite
        # transaction while the provider wire is in flight. Re-read under the
        # terminal transaction so we use that same attempt's fresh fence; never
        # accept a takeover, owner drift, state drift, or expired renewal.
        with self.log._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                fresh = self.log.get_in_connection(connection, operation_id)
                settled_at = logical_now()
                if (
                    fresh.operation_id != current[0].operation_id
                    or fresh.lease_owner != current[0].lease_owner
                    or fresh.attempt != current[0].attempt
                ):
                    raise InvalidEffectTransition("Effect lease generation drifted before terminal settlement")
                self._require_owned(fresh, now=settled_at)
                if abandoned is not None:
                    outcome = self.abandon(fresh, now=settled_at, error_ref=abandoned.error_ref, connection=connection)
                elif deferred is not None:
                    outcome = self.release_to_planned(fresh, now=settled_at, probe_ref=deferred.probe_ref, connection=connection)
                else:
                    if fresh.contract_version == EFFECT_V2 and not isinstance(result, EffectReceipt):
                        raise TypeError("effect-v2 Handler must return an immutable EffectReceipt")
                    outcome = self.settle_ok(
                        fresh, now=settled_at,
                        receipt_ref=result.receipt_ref if isinstance(result, EffectReceipt) else result,
                        receipt_kind=(result.receipt_kind if isinstance(result, EffectReceipt) else receipt_kind or f"{fresh.kind}-receipt"),
                        receipt_schema_version=(result.receipt_schema_version if isinstance(result, EffectReceipt) else "legacy-v1"),
                        intent_schema_version=(result.intent_schema_version if isinstance(result, EffectReceipt) else None),
                        connection=connection,
                    )
                connection.commit()
                return outcome
            except Exception:
                connection.rollback()
                raise

    def begin_planned(
        self, operation_id: str, *, now: int,
        connection: sqlite3.Connection | None = None,
        lease_expires_at: int | float | None = None,
        probe_ref: str | None = None,
    ) -> Effect:
        return self.claim_planned(
            operation_id, now=now, connection=connection,
            lease_expires_at=lease_expires_at, probe_ref=probe_ref,
        )[0]

    def claim_planned(
        self, operation_id: str, *, now: int,
        connection: sqlite3.Connection | None = None,
        lease_expires_at: int | float | None = None,
        probe_ref: str | None = None,
    ) -> tuple[Effect, bool]:
        """CAS-claim PLANNED and report whether this call won execution."""

        planned = self.log.get_in_connection(connection, operation_id) if connection else self.log.get(operation_id)
        if planned.state is not EffectState.PLANNED:
            return planned, False
        transition = self.log.transition_in_connection if connection else self.log.transition
        try:
            claimed = transition(
                connection, operation_id,
                expected=EffectState.PLANNED,
                target=EffectState.INFLIGHT,
                now=now,
                lease_owner=self.owner_id,
                lease_expires_at=lease_expires_at or now + self.lease_seconds,
                probe_ref=probe_ref,
                increment_attempt=True,
            ) if connection else transition(
                operation_id,
                expected=EffectState.PLANNED,
                target=EffectState.INFLIGHT,
                now=now,
                lease_owner=self.owner_id,
                lease_expires_at=lease_expires_at or now + self.lease_seconds,
                probe_ref=probe_ref,
                increment_attempt=True,
            )
            return claimed, True
        except InvalidEffectTransition:
            winner = self.log.get_in_connection(connection, operation_id) if connection else self.log.get(operation_id)
            return winner, False

    def settle_ok(
        self, effect: Effect, *, receipt_ref: str, receipt_kind: str, now: int,
        receipt_schema_version: str = "legacy-v1", intent_schema_version: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> Effect:
        fence = self._require_owned(effect, now=now)
        if connection:
            return self.log.settle_ok_with_receipt_in_connection(
                connection, effect.operation_id, expected=EffectState.INFLIGHT,
                receipt_ref=receipt_ref, receipt_kind=receipt_kind, now=now,
                receipt_schema_version=receipt_schema_version, intent_schema_version=intent_schema_version,
                fence=fence,
            )
        return self.log.settle_ok_with_receipt(
            effect.operation_id, expected=EffectState.INFLIGHT,
            receipt_ref=receipt_ref, receipt_kind=receipt_kind, now=now,
            receipt_schema_version=receipt_schema_version, intent_schema_version=intent_schema_version,
            fence=fence,
        )

    def settle_verified_ok(
        self, effect: Effect, *, receipt_ref: str, receipt_kind: str | None = None, now: int,
        receipt_schema_version: str | None = None, intent_schema_version: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> Effect:
        """Bind a verified Receipt to an owned or previously UNKNOWN Effect."""

        fence: EffectLeaseFence | None = None
        if effect.state is EffectState.INFLIGHT:
            fence = self._require_owned(effect, now=now)
        elif effect.state is not EffectState.UNKNOWN:
            raise InvalidEffectTransition("verified settlement requires INFLIGHT or UNKNOWN")
        if effect.contract_version == EFFECT_V2:
            receipt_kind = effect.expected_receipt_kind
            receipt_schema_version = effect.expected_receipt_schema_version
            intent_schema_version = effect.intent_schema_version
        if receipt_kind is None:
            raise ValueError("verified settlement requires receipt_kind")
        if connection:
            return self.log.settle_ok_with_receipt_in_connection(
                connection, effect.operation_id, expected=effect.state,
                receipt_ref=receipt_ref, receipt_kind=receipt_kind, now=now,
                receipt_schema_version=receipt_schema_version or "legacy-v1",
                intent_schema_version=intent_schema_version,
                fence=fence,
            )
        return self.log.settle_ok_with_receipt(
            effect.operation_id, expected=effect.state,
            receipt_ref=receipt_ref, receipt_kind=receipt_kind, now=now,
            receipt_schema_version=receipt_schema_version or "legacy-v1",
            intent_schema_version=intent_schema_version,
            fence=fence,
        )

    def settle_error(
        self, effect: Effect, *, error_ref: str, now: int,
        connection: sqlite3.Connection | None = None,
    ) -> Effect:
        fence = self._require_owned(effect, now=now)
        transition = self.log.transition_in_connection if connection else self.log.transition
        kwargs = dict(
            expected=EffectState.INFLIGHT, target=EffectState.SETTLED_ERR,
            now=now, error_ref=error_ref, fence=fence,
        )
        return transition(connection, effect.operation_id, **kwargs) if connection else transition(effect.operation_id, **kwargs)

    def settle_verified_error(
        self, effect: Effect, *, error_ref: str, now: int,
        connection: sqlite3.Connection | None = None,
    ) -> Effect:
        """Resolve owned INFLIGHT or UNKNOWN from verified domain evidence."""

        fence: EffectLeaseFence | None = None
        if effect.state is EffectState.INFLIGHT:
            fence = self._require_owned(effect, now=now)
        elif effect.state is not EffectState.UNKNOWN:
            raise InvalidEffectTransition("verified error settlement requires INFLIGHT or UNKNOWN")
        transition = self.log.transition_in_connection if connection else self.log.transition
        kwargs = dict(
            expected=effect.state, target=EffectState.SETTLED_ERR,
            now=now, error_ref=error_ref, fence=fence,
        )
        return transition(connection, effect.operation_id, **kwargs) if connection else transition(effect.operation_id, **kwargs)

    def mark_unknown(
        self, effect: Effect, *, error_ref: str, now: int,
        connection: sqlite3.Connection | None = None,
        probe_ref: str | None = None,
    ) -> Effect:
        fence = self._require_owned(effect, now=now)
        transition = self.log.transition_in_connection if connection else self.log.transition
        kwargs = dict(
            expected=EffectState.INFLIGHT, target=EffectState.UNKNOWN, now=now,
            error_ref=error_ref, probe_ref=probe_ref, fence=fence,
        )
        return transition(connection, effect.operation_id, **kwargs) if connection else transition(effect.operation_id, **kwargs)

    def abandon(
        self, effect: Effect, *, error_ref: str, now: int,
        connection: sqlite3.Connection | None = None,
    ) -> Effect:
        fence = self._require_owned(effect, now=now)
        transition = self.log.transition_in_connection if connection else self.log.transition
        kwargs = dict(
            expected=EffectState.INFLIGHT, target=EffectState.ABANDONED,
            now=now, error_ref=error_ref, fence=fence,
        )
        return transition(connection, effect.operation_id, **kwargs) if connection else transition(effect.operation_id, **kwargs)

    def release_to_planned(
        self, effect: Effect, *, now: int, probe_ref: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> Effect:
        fence = self._require_owned(effect, now=now)
        transition = self.log.transition_in_connection if connection else self.log.transition
        kwargs = dict(
            expected=EffectState.INFLIGHT, target=EffectState.PLANNED,
            now=now, probe_ref=probe_ref, fence=fence,
        )
        return transition(connection, effect.operation_id, **kwargs) if connection else transition(effect.operation_id, **kwargs)

    def renew(
        self, effect: Effect, *, lease_expires_at: int | float, now: int,
        connection: sqlite3.Connection,
    ) -> Effect:
        fence = self._require_owned(effect, now=now)
        return self.log.renew_or_take_over_lease_in_connection(
            connection,
            operation_id=effect.operation_id,
            lease_owner=self.owner_id,
            lease_expires_at=lease_expires_at,
            now=now,
            fence=fence,
        )

    def reauthorize_unknown(
        self, effect: Effect, *, probe_ref: str, now: int,
        connection: sqlite3.Connection | None = None,
    ) -> Effect:
        """Return an UNKNOWN Effect to PLANNED after explicit reviewed evidence."""

        if effect.state is not EffectState.UNKNOWN:
            raise InvalidEffectTransition("reauthorization requires UNKNOWN Effect")
        if effect.contract_version == EFFECT_V2:
            _assert_user_decision_ref(probe_ref, field="reauthorization_decision_ref")
        transition = self.log.transition_in_connection if connection else self.log.transition
        kwargs = dict(
            expected=EffectState.UNKNOWN, target=EffectState.PLANNED,
            now=now, probe_ref=probe_ref,
        )
        return transition(connection, effect.operation_id, **kwargs) if connection else transition(effect.operation_id, **kwargs)

    def abandon_unknown(
        self, effect: Effect, *, decision_ref: str, now: int,
        connection: sqlite3.Connection | None = None,
    ) -> Effect:
        """Explicit user abandonment of an UNKNOWN Effect with durable evidence."""
        if effect.state is not EffectState.UNKNOWN:
            raise InvalidEffectTransition("abandonment requires UNKNOWN Effect")
        if effect.contract_version == EFFECT_V2:
            _assert_user_decision_ref(decision_ref, field="abandonment_decision_ref")
        transition = self.log.transition_in_connection if connection else self.log.transition
        kwargs = dict(expected=EffectState.UNKNOWN, target=EffectState.ABANDONED, now=now, error_ref=decision_ref)
        return transition(connection, effect.operation_id, **kwargs) if connection else transition(effect.operation_id, **kwargs)

    def _require_owned(self, effect: Effect, *, now: int | float) -> EffectLeaseFence:
        if effect.state is not EffectState.INFLIGHT or effect.lease_owner != self.owner_id:
            raise InvalidEffectTransition("Effect is not owned by this Runner")
        fence = EffectLeaseFence.from_effect(effect)
        if fence.lease_expires_at <= now:
            raise InvalidEffectTransition("Effect lease fence is expired")
        return fence


@dataclass(frozen=True)
class RecoveryOutcome:
    operation_id: str
    state: EffectState
    reason: str


Probe = Callable[[Effect], tuple[EffectState, str | None]]


class EffectReaper:
    def __init__(self, log: EffectLog):
        self.log = log

    def recover_expired(
        self, *, now: int, probes: Mapping[object, Probe] | None = None,
        verifiers: Mapping[object, Probe] | None = None,
        reauthorizers: Mapping[object, Probe] | None = None, limit: int = 100,
    ) -> list[RecoveryOutcome]:
        probe_map = probes or {}
        verifier_map = verifiers or {}
        reauthorize_map = reauthorizers or {}
        outcomes: list[RecoveryOutcome] = []
        for effect in self.log.expired_inflight(now=now, limit=limit):
            outcomes.append(self._recover_one(
                effect, now=now, probes=probe_map, verifiers=verifier_map,
                reauthorizers=reauthorize_map,
            ))
        return outcomes

    def _recover_one(
        self, effect: Effect, *, now: int, probes: Mapping[object, Probe],
        verifiers: Mapping[object, Probe], reauthorizers: Mapping[object, Probe],
    ) -> RecoveryOutcome:
        """Recover one fenced Effect without aborting the remaining scan.

        A domain strategy failure is itself uncertainty, never permission to
        replay an external operation.  Concurrent Reapers use Effect CAS as
        the fence; the loser reports the winner's durable state.
        """
        fence = EffectLeaseFence.from_effect(effect)
        try:
            with self.log._connect() as connection:
                receipt = connection.execute(
                    "SELECT receipt_ref,receipt_kind,receipt_schema_version FROM effect_receipt WHERE operation_id=?",
                    (effect.operation_id,),
                ).fetchone()
            if receipt is not None and effect.state in {EffectState.INFLIGHT, EffectState.UNKNOWN}:
                receipt_kind = (effect.expected_receipt_kind if effect.contract_version == EFFECT_V2
                                else str(receipt[1]))
                receipt_schema_version = (effect.expected_receipt_schema_version
                                          if effect.contract_version == EFFECT_V2 else str(receipt[2]))
                recovered = self.log.settle_ok_with_receipt(
                    effect.operation_id, expected=effect.state, receipt_ref=str(receipt[0]),
                    receipt_kind=receipt_kind, receipt_schema_version=receipt_schema_version,
                    intent_schema_version=(effect.intent_schema_version if effect.contract_version == EFFECT_V2 else None),
                    now=now,
                    fence=fence,
                    fence_must_be_expired=True,
                )
                return RecoveryOutcome(effect.operation_id, recovered.state, "receipt_already_bound")
            target: EffectState
            result_ref: str | None = None
            error_ref: str | None = None
            probe_ref: str | None = None
            reason: str
            versioned_key = (effect.kind, effect.contract_version)
            verifier = verifiers.get(versioned_key)
            if verifier is None and effect.contract_version == LEGACY_V1:
                verifier = verifiers.get(effect.kind)
            if verifier is not None:
                try:
                    target, outcome_ref = verifier(effect)
                except Exception as error:
                    target = EffectState.UNKNOWN
                    outcome_ref = (_v2_error_ref(error) if effect.contract_version == EFFECT_V2
                                   else f"{type(error).__name__}:{error}")
                    reason = "verifier_failed"
                else:
                    reason = "verifier_resolved"
                if target not in {
                    EffectState.PLANNED,
                    EffectState.SETTLED_OK,
                    EffectState.SETTLED_ERR,
                    EffectState.UNKNOWN,
                }:
                    raise InvalidEffectTransition("verifier returned an invalid recovery state")
                if target is EffectState.SETTLED_OK:
                    result_ref = outcome_ref
                elif target is EffectState.PLANNED:
                    if effect.contract_version == EFFECT_V2:
                        _assert_internal_opaque_ref(outcome_ref or "", field="recovery_evidence_ref")
                    probe_ref = outcome_ref
                elif target in {EffectState.SETTLED_ERR, EffectState.UNKNOWN}:
                    error_ref = outcome_ref
            elif effect.effect_class in {EffectClass.PURE, EffectClass.IDEMPOTENT}:
                target, reason = EffectState.PLANNED, "safe_to_retry"
            elif effect.effect_class is EffectClass.QUERYABLE:
                probe = probes.get(versioned_key)
                if probe is None and effect.contract_version == LEGACY_V1:
                    probe = probes.get(effect.kind)
                if probe is None:
                    target, reason = EffectState.UNKNOWN, "probe_unavailable"
                else:
                    try:
                        target, outcome_ref = probe(effect)
                    except Exception as error:
                        target = EffectState.UNKNOWN
                        outcome_ref = (_v2_error_ref(error) if effect.contract_version == EFFECT_V2
                                       else f"{type(error).__name__}:{error}")
                        reason = "probe_failed"
                    else:
                        reason = "probe_resolved"
                    if target not in {
                        EffectState.PLANNED,
                        EffectState.SETTLED_OK,
                        EffectState.SETTLED_ERR,
                        EffectState.UNKNOWN,
                    }:
                        raise InvalidEffectTransition("probe returned an invalid recovery state")
                    if target is EffectState.SETTLED_OK:
                        result_ref = outcome_ref
                    elif target is EffectState.PLANNED:
                        if effect.contract_version == EFFECT_V2:
                            _assert_internal_opaque_ref(outcome_ref or "", field="recovery_evidence_ref")
                        probe_ref = outcome_ref
                    elif target in {EffectState.SETTLED_ERR, EffectState.UNKNOWN}:
                        error_ref = outcome_ref
            elif effect.effect_class is EffectClass.AT_MOST_ONCE:
                target, reason = EffectState.UNKNOWN, "at_most_once_uncertain"
            else:
                reauthorize = reauthorizers.get(versioned_key)
                if reauthorize is None and effect.contract_version == LEGACY_V1:
                    reauthorize = reauthorizers.get(effect.kind)
                if reauthorize is None:
                    target, reason = EffectState.UNKNOWN, "reauthorization_required"
                else:
                    try:
                        target, outcome_ref = reauthorize(effect)
                    except Exception as error:
                        target = EffectState.UNKNOWN
                        outcome_ref = (_v2_error_ref(error) if effect.contract_version == EFFECT_V2
                                       else f"{type(error).__name__}:{error}")
                        reason = "reauthorization_failed"
                    else:
                        reason = "reauthorization_resolved"
                    if target not in {EffectState.PLANNED, EffectState.UNKNOWN}:
                        raise InvalidEffectTransition(
                            "reauthorizer returned an invalid recovery state"
                        )
                    if target is EffectState.UNKNOWN:
                        error_ref = outcome_ref
                    elif target is EffectState.PLANNED:
                        if effect.contract_version == EFFECT_V2:
                            _assert_user_decision_ref(outcome_ref or "", field="reauthorization_decision_ref")
                        probe_ref = outcome_ref
            if target is EffectState.SETTLED_OK:
                if result_ref is None:
                    raise InvalidEffectTransition("probe success requires a Receipt reference")
                receipt_kind = (effect.expected_receipt_kind if effect.contract_version == EFFECT_V2
                                else f"{effect.kind}-probe-receipt")
                receipt_schema_version = (effect.expected_receipt_schema_version
                                          if effect.contract_version == EFFECT_V2 else "legacy-v1")
                intent_schema_version = (effect.intent_schema_version
                                         if effect.contract_version == EFFECT_V2 else None)
                recovered = self.log.settle_ok_with_receipt(
                    effect.operation_id,
                    expected=EffectState.INFLIGHT,
                    receipt_ref=result_ref,
                    receipt_kind=receipt_kind,
                    receipt_schema_version=receipt_schema_version,
                    intent_schema_version=intent_schema_version,
                    now=now,
                    fence=fence,
                    fence_must_be_expired=True,
                )
            else:
                recovered = self.log.transition(
                    effect.operation_id,
                    expected=EffectState.INFLIGHT,
                    target=target,
                    now=now,
                    error_ref=error_ref,
                    probe_ref=probe_ref,
                    fence=fence,
                    fence_must_be_expired=True,
                )
            return RecoveryOutcome(effect.operation_id, recovered.state, reason)
        except (InvalidEffectTransition, TypeError, ValueError) as error:
            winner = self.log.get(effect.operation_id)
            if (
                winner.state is not EffectState.INFLIGHT
                or EffectLeaseFence.from_effect(winner) != fence
            ):
                return RecoveryOutcome(
                    effect.operation_id, winner.state, "concurrent_reaper_won",
                )
            error_ref = (_v2_error_ref(error) if effect.contract_version == EFFECT_V2
                         else f"{type(error).__name__}:{error}")
            try:
                isolated = self.log.transition(
                    effect.operation_id,
                    expected=EffectState.INFLIGHT,
                    target=EffectState.UNKNOWN,
                    now=now,
                    error_ref=error_ref,
                    fence=fence,
                    fence_must_be_expired=True,
                )
            except InvalidEffectTransition:
                winner = self.log.get(effect.operation_id)
                if (
                    winner.state is EffectState.INFLIGHT
                    and EffectLeaseFence.from_effect(winner) == fence
                ):
                    raise
                return RecoveryOutcome(
                    effect.operation_id, winner.state, "concurrent_reaper_won",
                )
            return RecoveryOutcome(
                effect.operation_id, isolated.state, "recovery_contract_invalid",
            )


def _row_to_effect(row: sqlite3.Row) -> Effect:
    if not isinstance(row, sqlite3.Row):
        columns = (
            "operation_id", "session_id", "turn_id", "root_id", "parent_id", "step_key",
            "kind", "effect_class", "purpose", "intent_ref", "intent_digest",
            "gate_decision_id", "rev_set", "idem_key", "state", "attempt", "lease_owner",
            "lease_expires_at", "probe_ref", "result_ref", "error_ref", "occurred_at",
            "recorded_at", "settled_at", "contract_version", "authority_set_id",
            "intent_schema_version", "expected_receipt_kind", "expected_receipt_schema_version",
            "identity_algorithm", "revision_schema_version",
        )
        row = dict(zip(columns, row))  # type: ignore[assignment]
    return Effect(
        operation_id=str(row["operation_id"]),
        session_id=str(row["session_id"]),
        turn_id=row["turn_id"],
        root_id=str(row["root_id"]),
        parent_id=row["parent_id"],
        step_key=str(row["step_key"]),
        kind=str(row["kind"]),
        effect_class=EffectClass(str(row["effect_class"])),
        purpose=EffectPurpose(str(row["purpose"])),
        intent_ref=str(row["intent_ref"]),
        intent_digest=str(row["intent_digest"]),
        gate_decision_id=str(row["gate_decision_id"]),
        rev_set=json.loads(str(row["rev_set"])),
        idem_key=row["idem_key"],
        state=EffectState(str(row["state"])),
        attempt=int(row["attempt"]),
        lease_owner=row["lease_owner"],
        lease_expires_at=(
            float(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None
        ),
        probe_ref=row["probe_ref"],
        result_ref=row["result_ref"],
        error_ref=row["error_ref"],
        occurred_at=int(row["occurred_at"]),
        recorded_at=int(row["recorded_at"]),
        settled_at=row["settled_at"],
        contract_version=(row["contract_version"] if "contract_version" in row.keys() else LEGACY_V1),
        authority_set_id=(row["authority_set_id"] if "authority_set_id" in row.keys() else "legacy-not-applicable"),
        intent_schema_version=(row["intent_schema_version"] if "intent_schema_version" in row.keys() else "legacy-v1"),
        expected_receipt_kind=(row["expected_receipt_kind"] if "expected_receipt_kind" in row.keys() else None),
        expected_receipt_schema_version=(row["expected_receipt_schema_version"] if "expected_receipt_schema_version" in row.keys() else None),
        identity_algorithm=(row["identity_algorithm"] if "identity_algorithm" in row.keys() else LEGACY_IDENTITY_ALGORITHM),
        revision_schema_version=(row["revision_schema_version"] if "revision_schema_version" in row.keys() else LEGACY_REVISION_SCHEMA_VERSION),
    )
