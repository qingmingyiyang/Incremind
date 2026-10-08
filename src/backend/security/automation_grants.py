"""Exact, durable automation authorization grants.

This is a control-plane authority only.  It does not run an Effect, materialize
a secret, or retain request bodies.  A caller must claim a grant immediately
before submitting its already-authorized Effect, then complete the claim with
the resulting receipt reference.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from backend.security.secrets import SecretStore
from backend.shared.interprocess_lock import interprocess_file_lock


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_RECEIPT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_COMMAND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SECRET_FIELD = re.compile(r"(?:secret|token|password|credential|cookie|authorization|api[_-]?key|body|content|prompt|message)", re.I)
_STATES = frozenset({"active", "exhausted", "revoked", "invalidated"})


class AutomationGrantError(ValueError):
    """A safe, non-secret automation authorization failure."""


class AutomationGrantConflict(AutomationGrantError):
    """A command or current grant revision changed concurrently."""


@dataclass(frozen=True, slots=True)
class AutomationGrantBinding:
    project_id: str
    operation_id: str
    parameter_digest: str
    effect_kind: str
    capability_revision: int
    target: str
    boundary_profile_id: str
    boundary_revision: int
    secret_refs: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class AutomationGrant:
    grant_id: str
    binding: AutomationGrantBinding
    expires_at: str
    max_uses: int
    uses_consumed: int
    state: str
    revision: int
    command_id: str
    receipt_ref: str | None

    def public(self) -> dict[str, object]:
        return {
            "grant_id": self.grant_id, "project_id": self.binding.project_id,
            "operation_id": self.binding.operation_id,
            "parameter_digest": self.binding.parameter_digest,
            "effect_kind": self.binding.effect_kind,
            "capability_revision": self.binding.capability_revision,
            "target": self.binding.target,
            "boundary_profile_id": self.binding.boundary_profile_id,
            "boundary_revision": self.binding.boundary_revision,
            "secret_refs": [{"ref": ref, "generation": generation} for ref, generation in self.binding.secret_refs],
            "expires_at": self.expires_at, "max_uses": self.max_uses,
            "uses_consumed": self.uses_consumed, "state": self.state,
            "revision": self.revision, "command_id": self.command_id,
            "receipt_ref": self.receipt_ref,
        }


@dataclass(frozen=True, slots=True)
class AutomationGrantClaim:
    claim_id: str
    grant: AutomationGrant


def canonical_parameter_digest(parameters: object) -> str:
    """Digest a small structural parameter summary, never a request body.

    Secret-like field names and free-text / byte values are refused rather than
    normalized.  This prevents an authorization record from becoming a second
    channel for prompts, content, credentials, or arbitrary request bodies.
    """
    normalized = _safe_parameters(parameters)
    encoded = json.dumps(normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AutomationGrantRepository:
    """SQLite-backed exact-grant authority; intentionally no Runner exists."""

    _DATABASE = "automation-grants.sqlite3"

    def __init__(self, root_dir: Path, *, secret_store: SecretStore) -> None:
        root = Path(root_dir)
        self._path = root / ".rebuild-data" / "security" / self._DATABASE
        self._secrets = secret_store
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create(
        self, *, binding: AutomationGrantBinding, expires_at: str, max_uses: int,
        command_id: str, expected_revision: int = 0,
    ) -> AutomationGrant:
        binding = _binding(binding)
        expires_at = _future_expiry(expires_at)
        max_uses = _positive(max_uses, "maximum uses")
        command_id = _command(command_id)
        if expected_revision != 0:
            raise AutomationGrantConflict("automation grant does not yet exist")
        semantic = _canonical({"binding": _binding_public(binding), "expires_at": expires_at, "max_uses": max_uses})
        with self._transaction() as conn:
            prior = conn.execute("SELECT result_payload, semantic FROM automation_grant_commands WHERE command_id=?", (command_id,)).fetchone()
            if prior is not None:
                if str(prior["semantic"]) != semantic:
                    raise AutomationGrantConflict("automation grant command identity is already used")
                return _grant_from_payload(str(prior["result_payload"]))
            grant = AutomationGrant(uuid4().hex, binding, expires_at, max_uses, 0, "active", 1, command_id, None)
            conn.execute(
                "INSERT INTO automation_grants VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _row_values(grant),
            )
            conn.execute("INSERT INTO automation_grant_commands VALUES (?,?,?,?)", (command_id, semantic, _canonical(grant.public()), _now()))
            return grant

    def get(self, grant_id: str) -> AutomationGrant | None:
        grant_id = _identity(grant_id, "grant identity")
        with closing(self._connection()) as conn:
            row = conn.execute("SELECT * FROM automation_grants WHERE grant_id=?", (grant_id,)).fetchone()
            return _grant(row) if row is not None else None

    def claim(self, *, grant_id: str, binding: AutomationGrantBinding, expected_revision: int, now: datetime | None = None) -> AutomationGrantClaim:
        grant_id = _identity(grant_id, "grant identity")
        binding = _binding(binding)
        expected_revision = _positive(expected_revision, "grant revision")
        current = _clock(now)
        failure: str | None = None
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM automation_grants WHERE grant_id=?", (grant_id,)).fetchone()
            if row is None:
                raise AutomationGrantError("automation grant is unavailable")
            grant = _grant(row)
            reason = self._invalid_reason(grant, binding, current)
            if reason is not None:
                if reason in {"binding_drift", "secret_drift"} and grant.state == "active":
                    grant = _replace_state(conn, grant, "invalidated")
                failure = reason
            elif grant.revision != expected_revision:
                raise AutomationGrantConflict("automation grant revision changed")
            else:
                uses = grant.uses_consumed + 1
                state = "exhausted" if uses >= grant.max_uses else "active"
                updated = AutomationGrant(grant.grant_id, grant.binding, grant.expires_at, grant.max_uses, uses, state, grant.revision + 1, grant.command_id, grant.receipt_ref)
                changed = conn.execute("UPDATE automation_grants SET uses_consumed=?, state=?, revision=? WHERE grant_id=? AND revision=?", (uses, state, updated.revision, grant.grant_id, grant.revision)).rowcount
                if changed != 1:
                    raise AutomationGrantConflict("automation grant revision changed")
                claim_id = uuid4().hex
                conn.execute("INSERT INTO automation_grant_claims VALUES (?,?,?,?,?)", (claim_id, grant.grant_id, updated.revision, None, _now()))
                return AutomationGrantClaim(claim_id, updated)
        raise AutomationGrantError(f"automation grant {failure}")

    def complete(self, *, claim_id: str, receipt_ref: str) -> AutomationGrant:
        claim_id = _identity(claim_id, "claim identity")
        receipt_ref = _receipt_reference(receipt_ref)
        with self._transaction() as conn:
            row = conn.execute("SELECT grant_id, receipt_ref FROM automation_grant_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if row is None:
                raise AutomationGrantError("automation grant claim is unavailable")
            if row["receipt_ref"] is not None:
                if str(row["receipt_ref"]) == receipt_ref:
                    grant_row = conn.execute("SELECT * FROM automation_grants WHERE grant_id=?", (str(row["grant_id"]),)).fetchone()
                    return _grant(grant_row)
                raise AutomationGrantConflict("automation grant claim is already completed")
            conn.execute("UPDATE automation_grant_claims SET receipt_ref=? WHERE claim_id=?", (receipt_ref, claim_id))
            grant_row = conn.execute("SELECT * FROM automation_grants WHERE grant_id=?", (str(row["grant_id"]),)).fetchone()
            if grant_row is None:
                raise AutomationGrantError("automation grant is unavailable")
            grant = _grant(grant_row)
            updated = AutomationGrant(grant.grant_id, grant.binding, grant.expires_at, grant.max_uses, grant.uses_consumed, grant.state, grant.revision + 1, grant.command_id, receipt_ref)
            conn.execute("UPDATE automation_grants SET receipt_ref=?, revision=? WHERE grant_id=?", (receipt_ref, updated.revision, grant.grant_id))
            return updated

    def revoke(self, *, grant_id: str, expected_revision: int) -> AutomationGrant:
        grant_id = _identity(grant_id, "grant identity")
        expected_revision = _positive(expected_revision, "grant revision")
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM automation_grants WHERE grant_id=?", (grant_id,)).fetchone()
            if row is None:
                raise AutomationGrantError("automation grant is unavailable")
            grant = _grant(row)
            if grant.revision != expected_revision:
                raise AutomationGrantConflict("automation grant revision changed")
            return _replace_state(conn, grant, "revoked")

    def _invalid_reason(self, grant: AutomationGrant, binding: AutomationGrantBinding, now: datetime) -> str | None:
        if grant.state == "revoked": return "revoked"
        if grant.state == "invalidated": return "invalidated"
        if grant.state == "exhausted" or grant.uses_consumed >= grant.max_uses: return "exhausted"
        if _parse_expiry(grant.expires_at) <= now: return "expired"
        if grant.binding != binding: return "binding_drift"
        for ref, generation in grant.binding.secret_refs:
            if not self._secrets.has_secret(ref) or self._secrets.get_generation(ref) != generation:
                return "secret_drift"
        return None

    def _initialize(self) -> None:
        with self._transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS automation_grants (grant_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, operation_id TEXT NOT NULL, parameter_digest TEXT NOT NULL, effect_kind TEXT NOT NULL, capability_revision INTEGER NOT NULL, target TEXT NOT NULL, boundary_profile_id TEXT NOT NULL, boundary_revision INTEGER NOT NULL, secret_refs TEXT NOT NULL, expires_at TEXT NOT NULL, max_uses INTEGER NOT NULL, uses_consumed INTEGER NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL, command_id TEXT NOT NULL, receipt_ref TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(command_id))")
            conn.execute("CREATE TABLE IF NOT EXISTS automation_grant_commands (command_id TEXT PRIMARY KEY, semantic TEXT NOT NULL, result_payload TEXT NOT NULL, created_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS automation_grant_claims (claim_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, claimed_revision INTEGER NOT NULL, receipt_ref TEXT, created_at TEXT NOT NULL)")

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _transaction(self):
        return _Transaction(self._path, self._connection)


class _Transaction:
    def __init__(self, path: Path, connection): self.path, self.connection = path, connection
    def __enter__(self):
        self.lock = interprocess_file_lock(self.path); self.lock.__enter__()
        self.conn = self.connection(); self.conn.execute("BEGIN IMMEDIATE"); return self.conn
    def __exit__(self, kind, value, trace):
        try:
            self.conn.commit() if kind is None else self.conn.rollback()
        finally:
            self.conn.close(); self.lock.__exit__(kind, value, trace)


def _binding(value: AutomationGrantBinding) -> AutomationGrantBinding:
    if not isinstance(value, AutomationGrantBinding): raise AutomationGrantError("automation grant binding is invalid")
    return AutomationGrantBinding(
        _identity(value.project_id, "project identity"),
        _identity(value.operation_id, "operation identity"),
        _identity(value.parameter_digest, "parameter digest"),
        _identity(value.effect_kind, "effect kind"),
        _positive(value.capability_revision, "capability revision"),
        _identity(value.target, "target"),
        _identity(value.boundary_profile_id, "Boundary profile identity"),
        _positive(value.boundary_revision, "Boundary revision"),
        tuple(sorted((_identity(ref, "secret reference"), _positive(generation, "secret generation")) for ref, generation in value.secret_refs)),
    )


def _binding_public(binding: AutomationGrantBinding) -> dict[str, object]:
    return {"project_id": binding.project_id, "operation_id": binding.operation_id, "parameter_digest": binding.parameter_digest, "effect_kind": binding.effect_kind, "capability_revision": binding.capability_revision, "target": binding.target, "boundary_profile_id": binding.boundary_profile_id, "boundary_revision": binding.boundary_revision, "secret_refs": list(binding.secret_refs)}


def _row_values(grant: AutomationGrant) -> tuple[object, ...]:
    now = _now(); b = grant.binding
    return (grant.grant_id,b.project_id,b.operation_id,b.parameter_digest,b.effect_kind,b.capability_revision,b.target,b.boundary_profile_id,b.boundary_revision,_canonical(list(b.secret_refs)),grant.expires_at,grant.max_uses,grant.uses_consumed,grant.state,grant.revision,grant.command_id,grant.receipt_ref,now,now)


def _grant(row: sqlite3.Row) -> AutomationGrant:
    refs = tuple((str(item[0]), int(item[1])) for item in json.loads(str(row["secret_refs"])))
    return AutomationGrant(str(row["grant_id"]), _binding(AutomationGrantBinding(str(row["project_id"]),str(row["operation_id"]),str(row["parameter_digest"]),str(row["effect_kind"]),int(row["capability_revision"]),str(row["target"]),str(row["boundary_profile_id"]),int(row["boundary_revision"]),refs)), str(row["expires_at"]), int(row["max_uses"]), int(row["uses_consumed"]), str(row["state"]), int(row["revision"]), str(row["command_id"]), None if row["receipt_ref"] is None else str(row["receipt_ref"]))


def _grant_from_payload(payload: str) -> AutomationGrant:
    raw = json.loads(payload)
    binding = AutomationGrantBinding(raw["project_id"],raw["operation_id"],raw["parameter_digest"],raw["effect_kind"],raw["capability_revision"],raw["target"],raw["boundary_profile_id"],raw["boundary_revision"],tuple((item["ref"],item["generation"]) for item in raw["secret_refs"]))
    return AutomationGrant(_identity(raw["grant_id"],"grant identity"),_binding(binding),_future_expiry(raw["expires_at"]),_positive(raw["max_uses"],"maximum uses"),int(raw["uses_consumed"]),str(raw["state"]),_positive(raw["revision"],"grant revision"),_command(raw["command_id"]),raw["receipt_ref"])


def _replace_state(conn: sqlite3.Connection, grant: AutomationGrant, state: str) -> AutomationGrant:
    updated = AutomationGrant(grant.grant_id,grant.binding,grant.expires_at,grant.max_uses,grant.uses_consumed,state,grant.revision+1,grant.command_id,grant.receipt_ref)
    conn.execute("UPDATE automation_grants SET state=?, revision=?, updated_at=? WHERE grant_id=?", (state,updated.revision,_now(),grant.grant_id)); return updated


def _safe_parameters(value: object) -> object:
    if value is None or isinstance(value, (bool, int)): return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")): raise AutomationGrantError("automation parameters are invalid")
        return value
    if isinstance(value, str):
        if len(value) > 256 or "\n" in value or "\r" in value: raise AutomationGrantError("automation parameters may not contain body text")
        return value
    if isinstance(value, (bytes, bytearray)): raise AutomationGrantError("automation parameters may not contain bodies")
    if isinstance(value, (list, tuple)):
        if len(value) > 32: raise AutomationGrantError("automation parameters are too large")
        return [_safe_parameters(item) for item in value]
    if isinstance(value, dict):
        if len(value) > 32: raise AutomationGrantError("automation parameters are too large")
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str) or _SECRET_FIELD.search(key): raise AutomationGrantError("automation parameters contain a protected field")
            normalized[key] = _safe_parameters(item)
        return normalized
    raise AutomationGrantError("automation parameters are invalid")


def _identity(value: object, label: str) -> str:
    if not isinstance(value,str) or not _IDENTITY.fullmatch(value): raise AutomationGrantError(f"automation grant {label} is invalid")
    return value
def _receipt_reference(value: object) -> str:
    if not isinstance(value, str) or not _RECEIPT_REF.fullmatch(value):
        raise AutomationGrantError("automation grant receipt reference is invalid")
    return value
def _command(value: object) -> str:
    if not isinstance(value,str) or not _COMMAND.fullmatch(value): raise AutomationGrantError("automation grant command identity is invalid")
    return value
def _positive(value: object, label: str) -> int:
    if not isinstance(value,int) or isinstance(value,bool) or value < 1: raise AutomationGrantError(f"automation grant {label} is invalid")
    return value
def _future_expiry(value: object) -> str:
    parsed = _parse_expiry(value)
    if parsed <= _clock(None): raise AutomationGrantError("automation grant expiry is invalid")
    return parsed.isoformat().replace("+00:00","Z")
def _parse_expiry(value: object) -> datetime:
    if not isinstance(value,str): raise AutomationGrantError("automation grant expiry is invalid")
    try: parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError as error: raise AutomationGrantError("automation grant expiry is invalid") from error
    if parsed.tzinfo is None: raise AutomationGrantError("automation grant expiry is invalid")
    return parsed.astimezone(timezone.utc)
def _clock(value: datetime | None) -> datetime:
    now=datetime.now(timezone.utc) if value is None else value
    if now.tzinfo is None: raise AutomationGrantError("automation grant clock is invalid")
    return now.astimezone(timezone.utc)
def _now() -> str: return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
def _canonical(value: object) -> str: return json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(",",":"))
