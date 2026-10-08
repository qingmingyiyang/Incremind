"""Project-scoped authority for explicitly authorized Xiaohongshu cookies.

This module owns *authorization metadata*, not cookie contents.  Cookie
plaintext is written only to ``SecretStore`` and is intentionally absent from
SQLite rows, public DTOs, errors, and command receipts.  It is also deliberately
not a browser-cookie importer: callers must supply a value through an explicit
grant or rotation command.

The authority is a control-plane fence.  A later provider-side credential proxy
must use :meth:`verify` immediately before each governed wire operation and must
not treat a previously verified result as a reusable login session.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from threading import Lock
from uuid import uuid4

from backend.security.secrets import SecretSnapshot, SecretStore
from backend.security.secret_egress import SecretEgressBroker
from backend.shared.interprocess_lock import interprocess_file_lock


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PROVIDER = "xiaohongshu"
_LOCK = Lock()


class XiaohongshuControlledCredentialError(ValueError):
    """Safe, non-secret controlled-credential failure."""


class XiaohongshuControlledCredentialConflict(XiaohongshuControlledCredentialError):
    """CAS or exact-command-replay conflict."""


class XiaohongshuControlledCredentialIndeterminate(XiaohongshuControlledCredentialError):
    """A durable command intent exists but its Secret effect is uncertain."""


@dataclass(frozen=True, slots=True)
class XiaohongshuControlledCredentialAuthorization:
    """Non-secret authorization state safe for manifests and receipts."""

    authorization_id: str
    project_id: str
    provider: str
    credential_subject_id: str
    boundary_profile_id: str
    boundary_revision: int
    authorization_revision: int
    state: str
    expires_at: str
    secret_generation: int

    def public(self) -> dict[str, object]:
        return {
            "authorization_id": self.authorization_id,
            "project_id": self.project_id,
            "provider": self.provider,
            "credential_subject_id": self.credential_subject_id,
            "boundary_profile_id": self.boundary_profile_id,
            "boundary_revision": self.boundary_revision,
            "authorization_revision": self.authorization_revision,
            "state": self.state,
            "expires_at": self.expires_at,
            "secret_generation": self.secret_generation,
        }


@dataclass(frozen=True, slots=True)
class VerifiedXiaohongshuControlledCredential:
    """A non-secret, one-operation authorization witness.

    This object deliberately omits both the internal SecretStore key and its
    plaintext.  The future OS-owned proxy can use its public identity to ask the
    authority for a fresh secret snapshot; it must verify again for every wire
    operation.
    """

    authorization_id: str
    project_id: str
    provider: str
    credential_subject_id: str
    boundary_profile_id: str
    boundary_revision: int
    authorization_revision: int
    secret_generation: int
    expires_at: str

    def public(self) -> dict[str, object]:
        return {
            "authorization_id": self.authorization_id,
            "project_id": self.project_id,
            "provider": self.provider,
            "credential_subject_id": self.credential_subject_id,
            "boundary_profile_id": self.boundary_profile_id,
            "boundary_revision": self.boundary_revision,
            "authorization_revision": self.authorization_revision,
            "secret_generation": self.secret_generation,
            "expires_at": self.expires_at,
        }


@dataclass(slots=True)
class XiaohongshuControlledCredentialWireLease:
    """OS-owned, non-projectable credential lease for exactly one wire use.

    ``cookie_header_value`` is intentionally the only plaintext-bearing method.
    It rechecks the durable authorization and Secret Store generation every time
    it is called, so a lease captured before rotation, expiry, or revocation
    cannot issue another request.  This object must stay inside the controlled
    provider/network adapter and must never enter a Manifest, Receipt, Event,
    log, cache key, or model-visible DTO.
    """

    witness: VerifiedXiaohongshuControlledCredential
    _authority: "XiaohongshuControlledCredentialAuthority" = field(repr=False, compare=False)
    _use_lock: object = field(default_factory=Lock, repr=False, compare=False)
    _used: bool = field(default=False, init=False, repr=False, compare=False)

    def public(self) -> dict[str, object]:
        """Return only the non-secret frozen binding for bounded receipts."""

        return self.witness.public()

    def generation_current(self) -> bool:
        """Re-read the Secret Store and verify the entire frozen binding."""

        return self._authority._lease_generation_current(self.witness)

    def cookie_header_value(self) -> str:
        """Return plaintext once only to the OS-owned governed request adapter."""

        # A lease represents exactly one request construction.  Claim only
        # after the fresh fence succeeds, so a transient pre-wire failure does
        # not consume it, while a successful caller cannot reuse it to issue a
        # second platform request.
        with self._use_lock:  # type: ignore[union-attr]
            if self._used:
                raise XiaohongshuControlledCredentialError("controlled credential wire lease is already used")
            value = self._authority._lease_cookie_value(self.witness)
            self._used = True
            return value


class XiaohongshuControlledCredentialAuthority:
    """Durable explicit-grant authority backed by SQLite plus ``SecretStore``.

    A revoked authorization retains its secret in the Secret Store.  That is
    intentional: a failed revoke must never delete a pre-existing secret, and a
    revoked row is a fail-closed use fence.  Secret garbage collection, if ever
    needed, is a separate audited lifecycle operation.
    """

    _DATABASE = "xiaohongshu-controlled-credentials.sqlite3"

    def __init__(self, root_dir: Path, *, secret_store: SecretStore) -> None:
        root = Path(root_dir)
        self._security = root / ".rebuild-data" / "security"
        self._path = self._security / self._DATABASE
        self._secrets = secret_store

    def current(
        self, *, project_id: str, credential_subject_id: str
    ) -> XiaohongshuControlledCredentialAuthorization | None:
        project_id = _identity(project_id, "project identity")
        credential_subject_id = _identity(credential_subject_id, "credential subject")
        with _LOCK, closing(self._connection()) as conn:
            row = self._current_row(conn, project_id, credential_subject_id)
            return _authorization(row) if row is not None else None

    def grant(
        self,
        *,
        project_id: str,
        credential_subject_id: str,
        boundary_profile_id: str,
        boundary_revision: int,
        expires_at: str,
        cookie_value: str,
        expected_authorization_revision: int,
        command_id: str,
    ) -> XiaohongshuControlledCredentialAuthorization:
        """Create or explicitly re-authorize one project/subject binding.

        The Secret Store write comes before SQLite commit.  If SQLite fails we
        leave an unreachable new secret rather than deleting anything that may
        have existed before the command.  The record remains unavailable until a
        later successful grant, so this is fail-closed.
        """

        values = _grant_values(
            project_id=project_id,
            credential_subject_id=credential_subject_id,
            boundary_profile_id=boundary_profile_id,
            boundary_revision=boundary_revision,
            expires_at=expires_at,
            cookie_value=cookie_value,
            expected_authorization_revision=expected_authorization_revision,
            command_id=command_id,
        )
        with _LOCK, closing(self._connection()) as conn:
            replay = self._replay(conn, command_id=values["command_id"], operation="grant", semantic=_semantic(values))
            if replay is not None:
                return replay
            current = self._current_row(conn, values["project_id"], values["credential_subject_id"])
            current_revision = 0 if current is None else int(current["authorization_revision"])
            if current_revision != values["expected_authorization_revision"]:
                raise XiaohongshuControlledCredentialConflict("controlled credential authorization revision changed")
            authorization_id = uuid4().hex if current is None else str(current["authorization_id"])
            secret_key = _new_secret_key() if current is None else str(current["secret_key"])
            self._reserve_secret_command(
                conn, command_id=str(values["command_id"]), operation="grant",
                semantic=_semantic(values), project_id=str(values["project_id"]),
                subject_id=str(values["credential_subject_id"]),
            )
            snapshot = self._write_secret(secret_key, str(values["cookie_value"]))
            revision = current_revision + 1
            self._begin(conn)
            try:
                if current is None:
                    conn.execute(
                        "INSERT INTO xhs_controlled_credential_authorizations "
                        "(authorization_id, project_id, provider, credential_subject_id, secret_key, boundary_profile_id, boundary_revision, authorization_revision, state, expires_at, secret_generation, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                        (authorization_id, values["project_id"], _PROVIDER, values["credential_subject_id"], secret_key, values["boundary_profile_id"], values["boundary_revision"], revision, values["expires_at"], snapshot.generation, _now(), _now()),
                    )
                else:
                    conn.execute(
                        "UPDATE xhs_controlled_credential_authorizations SET boundary_profile_id = ?, boundary_revision = ?, authorization_revision = ?, state = 'active', expires_at = ?, secret_generation = ?, updated_at = ? WHERE authorization_id = ?",
                        (values["boundary_profile_id"], values["boundary_revision"], revision, values["expires_at"], snapshot.generation, _now(), authorization_id),
                    )
                result = _authorization(self._row_by_id(conn, authorization_id))
                self._record_command(conn, values["command_id"], "grant", _semantic(values), authorization_id, result)
                conn.execute("DELETE FROM xhs_controlled_credential_command_intents WHERE command_id = ?", (values["command_id"],))
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def rotate(
        self,
        *,
        project_id: str,
        credential_subject_id: str,
        boundary_profile_id: str,
        boundary_revision: int,
        expires_at: str,
        cookie_value: str,
        expected_authorization_revision: int,
        command_id: str,
    ) -> XiaohongshuControlledCredentialAuthorization:
        values = _grant_values(
            project_id=project_id, credential_subject_id=credential_subject_id,
            boundary_profile_id=boundary_profile_id, boundary_revision=boundary_revision,
            expires_at=expires_at, cookie_value=cookie_value,
            expected_authorization_revision=expected_authorization_revision,
            command_id=command_id,
        )
        with _LOCK, closing(self._connection()) as conn:
            replay = self._replay(conn, command_id=values["command_id"], operation="rotate", semantic=_semantic(values))
            if replay is not None:
                return replay
            current = self._require_active_current(conn, values)
            self._reserve_secret_command(
                conn, command_id=str(values["command_id"]), operation="rotate",
                semantic=_semantic(values), project_id=str(values["project_id"]),
                subject_id=str(values["credential_subject_id"]),
            )
            snapshot = self._write_secret(str(current["secret_key"]), str(values["cookie_value"]))
            self._begin(conn)
            try:
                revision = int(current["authorization_revision"]) + 1
                conn.execute(
                    "UPDATE xhs_controlled_credential_authorizations SET authorization_revision = ?, expires_at = ?, secret_generation = ?, updated_at = ? WHERE authorization_id = ?",
                    (revision, values["expires_at"], snapshot.generation, _now(), current["authorization_id"]),
                )
                result = _authorization(self._row_by_id(conn, str(current["authorization_id"])))
                self._record_command(conn, values["command_id"], "rotate", _semantic(values), result.authorization_id, result)
                conn.execute("DELETE FROM xhs_controlled_credential_command_intents WHERE command_id = ?", (values["command_id"],))
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def revoke(
        self,
        *,
        project_id: str,
        credential_subject_id: str,
        expected_authorization_revision: int,
        command_id: str,
    ) -> XiaohongshuControlledCredentialAuthorization:
        project_id = _identity(project_id, "project identity")
        credential_subject_id = _identity(credential_subject_id, "credential subject")
        expected_authorization_revision = _revision(expected_authorization_revision)
        command_id = _command_id(command_id)
        semantic = {"project_id": project_id, "credential_subject_id": credential_subject_id, "expected_authorization_revision": expected_authorization_revision}
        with _LOCK, closing(self._connection()) as conn:
            replay = self._replay(conn, command_id=command_id, operation="revoke", semantic=semantic)
            if replay is not None:
                return replay
            current = self._current_row(conn, project_id, credential_subject_id)
            if current is None or current["state"] != "active" or int(current["authorization_revision"]) != expected_authorization_revision:
                raise XiaohongshuControlledCredentialConflict("controlled credential authorization revision changed")
            self._begin(conn)
            try:
                conn.execute(
                    "UPDATE xhs_controlled_credential_authorizations SET authorization_revision = ?, state = 'revoked', updated_at = ? WHERE authorization_id = ?",
                    (expected_authorization_revision + 1, _now(), current["authorization_id"]),
                )
                result = _authorization(self._row_by_id(conn, str(current["authorization_id"])))
                self._record_command(conn, command_id, "revoke", semantic, result.authorization_id, result)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def quarantine_indeterminate(
        self, *, project_id: str, credential_subject_id: str,
        pending_command_id: str, reconciliation_command_id: str,
    ) -> XiaohongshuControlledCredentialAuthorization | None:
        """Manually close an uncertain Secret effect without replaying it.

        An existing authorization is revoked in the same SQLite transaction;
        an interrupted first grant has no reachable authorization to revoke.
        The possibly-written Secret remains quarantined and unreachable.
        """
        project_id = _identity(project_id, "project identity")
        credential_subject_id = _identity(credential_subject_id, "credential subject")
        pending_command_id = _command_id(pending_command_id)
        reconciliation_command_id = _command_id(reconciliation_command_id)
        with _LOCK, closing(self._connection()) as conn:
            existing = conn.execute(
                "SELECT * FROM xhs_controlled_credential_reconciliations "
                "WHERE reconciliation_command_id = ?",
                (reconciliation_command_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["pending_command_id"] != pending_command_id
                    or existing["project_id"] != project_id
                    or existing["credential_subject_id"] != credential_subject_id
                ):
                    raise XiaohongshuControlledCredentialConflict(
                        "controlled credential reconciliation identity is already used"
                    )
                payload = existing["result_payload"]
                return None if payload == "null" else _authorization_from_payload(str(payload))
            intent = conn.execute(
                "SELECT * FROM xhs_controlled_credential_command_intents WHERE command_id = ?",
                (pending_command_id,),
            ).fetchone()
            if (
                intent is None or intent["project_id"] != project_id
                or intent["credential_subject_id"] != credential_subject_id
            ):
                raise XiaohongshuControlledCredentialConflict(
                    "controlled credential indeterminate command is unavailable"
                )
            self._begin(conn)
            try:
                current = self._current_row(conn, project_id, credential_subject_id)
                result = None
                if current is not None:
                    conn.execute(
                        "UPDATE xhs_controlled_credential_authorizations SET "
                        "authorization_revision = ?, state = 'revoked', updated_at = ? "
                        "WHERE authorization_id = ?",
                        (int(current["authorization_revision"]) + 1, _now(), current["authorization_id"]),
                    )
                    result = _authorization(self._row_by_id(conn, str(current["authorization_id"])))
                conn.execute(
                    "INSERT INTO xhs_controlled_credential_reconciliations "
                    "(reconciliation_command_id, pending_command_id, project_id, "
                    "credential_subject_id, result_payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (reconciliation_command_id, pending_command_id, project_id,
                     credential_subject_id,
                     "null" if result is None else _canonical(result.public()), _now()),
                )
                conn.execute(
                    "DELETE FROM xhs_controlled_credential_command_intents WHERE command_id = ?",
                    (pending_command_id,),
                )
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def verify(
        self,
        *,
        project_id: str,
        provider: str,
        credential_subject_id: str,
        boundary_profile_id: str,
        boundary_revision: int,
        authorization_revision: int,
        secret_generation: int,
        now: datetime | None = None,
    ) -> VerifiedXiaohongshuControlledCredential:
        """Validate a frozen credential binding immediately before wire traffic."""

        project_id = _identity(project_id, "project identity")
        credential_subject_id = _identity(credential_subject_id, "credential subject")
        boundary_profile_id = _identity(boundary_profile_id, "Boundary profile identity")
        if provider != _PROVIDER:
            raise XiaohongshuControlledCredentialError("controlled credential provider is not supported")
        boundary_revision = _revision(boundary_revision)
        authorization_revision = _revision(authorization_revision)
        secret_generation = _revision(secret_generation)
        current_time = _clock(now)
        witness, _ = self._verify_snapshot(
            project_id=project_id, provider=provider,
            credential_subject_id=credential_subject_id,
            boundary_profile_id=boundary_profile_id, boundary_revision=boundary_revision,
            authorization_revision=authorization_revision, secret_generation=secret_generation,
            now=current_time,
        )
        return witness

    def issue_wire_lease(
        self,
        *,
        project_id: str,
        provider: str,
        credential_subject_id: str,
        boundary_profile_id: str,
        boundary_revision: int,
        authorization_revision: int,
        secret_generation: int,
        now: datetime | None = None,
    ) -> XiaohongshuControlledCredentialWireLease:
        """Issue a non-public lease after one complete control-plane fence."""

        witness, _ = self._verify_snapshot(
            project_id=project_id, provider=provider,
            credential_subject_id=credential_subject_id,
            boundary_profile_id=boundary_profile_id, boundary_revision=boundary_revision,
            authorization_revision=authorization_revision, secret_generation=secret_generation,
            now=_clock(now),
        )
        return XiaohongshuControlledCredentialWireLease(
            witness=witness, _authority=self,
        )

    def _verify_snapshot(
        self,
        *,
        project_id: str,
        provider: str,
        credential_subject_id: str,
        boundary_profile_id: str,
        boundary_revision: int,
        authorization_revision: int,
        secret_generation: int,
        now: datetime,
    ) -> tuple[VerifiedXiaohongshuControlledCredential, str]:
        project_id = _identity(project_id, "project identity")
        credential_subject_id = _identity(credential_subject_id, "credential subject")
        boundary_profile_id = _identity(boundary_profile_id, "Boundary profile identity")
        if provider != _PROVIDER:
            raise XiaohongshuControlledCredentialError("controlled credential provider is not supported")
        boundary_revision = _revision(boundary_revision)
        authorization_revision = _revision(authorization_revision)
        secret_generation = _revision(secret_generation)
        current_time = _clock(now)
        with _LOCK, closing(self._connection()) as conn:
            current = self._current_row(conn, project_id, credential_subject_id)
            if current is None or current["state"] != "active":
                raise XiaohongshuControlledCredentialError("controlled credential authorization is unavailable")
            if (
                current["provider"] != _PROVIDER
                or current["boundary_profile_id"] != boundary_profile_id
                or int(current["boundary_revision"]) != boundary_revision
                or int(current["authorization_revision"]) != authorization_revision
                or int(current["secret_generation"]) != secret_generation
                or _parse_expiry(str(current["expires_at"])) <= current_time
            ):
                raise XiaohongshuControlledCredentialError("controlled credential authorization drifted")
            secret_key = str(current["secret_key"])
            if not self._secrets.has_secret(secret_key) or self._secrets.get_generation(secret_key) != secret_generation:
                raise XiaohongshuControlledCredentialError("controlled credential secret drifted")
            return (
                VerifiedXiaohongshuControlledCredential(
                    authorization_id=str(current["authorization_id"]), project_id=project_id,
                    provider=_PROVIDER, credential_subject_id=credential_subject_id,
                    boundary_profile_id=boundary_profile_id, boundary_revision=boundary_revision,
                    authorization_revision=authorization_revision, secret_generation=secret_generation,
                    expires_at=str(current["expires_at"]),
                ),
                secret_key,
            )

    def _lease_generation_current(self, witness: VerifiedXiaohongshuControlledCredential) -> bool:
        try:
            self._verify_snapshot(**_witness_inputs(witness), now=_clock(None))
            return True
        except XiaohongshuControlledCredentialError:
            return False

    def _lease_cookie_value(self, witness: VerifiedXiaohongshuControlledCredential) -> str:
        _, secret_key = self._verify_snapshot(**_witness_inputs(witness), now=_clock(None))
        revision = f"boundary:{witness.boundary_revision}"
        def current_revision(project: str) -> str:
            if project != witness.project_id:
                return "denied"
            self._verify_snapshot(**_witness_inputs(witness), now=_clock(None))
            return revision
        broker = SecretEgressBroker(self._secrets, boundary_revision_reader=current_revision)
        lease = broker.grant(
            project_id=witness.project_id, secret_ref=secret_key, purpose="xiaohongshu_cookie",
            allowed_hosts=("www.xiaohongshu.com",), boundary_revision=revision, ttl_seconds=30,
        )
        try:
            return broker.materialize_for_sdk(
                lease, project_id=witness.project_id, purpose="xiaohongshu_cookie",
                boundary_revision=revision, url="https://www.xiaohongshu.com/",
            )
        finally:
            broker.revoke(lease.lease_id)

    def _connection(self) -> sqlite3.Connection:
        self._security.mkdir(parents=True, exist_ok=True)
        try:
            with interprocess_file_lock(self._path):
                conn = sqlite3.connect(self._path, timeout=5.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                self._initialize(conn)
                check = conn.execute("PRAGMA quick_check").fetchone()
                if check is None or check[0] != "ok":
                    conn.close()
                    raise sqlite3.DatabaseError("quick check failed")
                return conn
        except (sqlite3.Error, TimeoutError) as error:
            raise XiaohongshuControlledCredentialError("controlled credential authority is unavailable") from error

    @staticmethod
    def _begin(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")

    def _initialize(self, conn: sqlite3.Connection) -> None:
        self._begin(conn)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS xhs_controlled_credential_authorizations ("
                "authorization_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, provider TEXT NOT NULL, credential_subject_id TEXT NOT NULL, secret_key TEXT NOT NULL, boundary_profile_id TEXT NOT NULL, boundary_revision INTEGER NOT NULL, authorization_revision INTEGER NOT NULL, state TEXT NOT NULL, expires_at TEXT NOT NULL, secret_generation INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(project_id, provider, credential_subject_id))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS xhs_controlled_credential_commands ("
                "command_id TEXT PRIMARY KEY, operation TEXT NOT NULL, semantic TEXT NOT NULL, authorization_id TEXT NOT NULL, result_revision INTEGER NOT NULL, result_payload TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(authorization_id) REFERENCES xhs_controlled_credential_authorizations(authorization_id))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS xhs_controlled_credential_command_intents ("
                "command_id TEXT PRIMARY KEY, operation TEXT NOT NULL, semantic TEXT NOT NULL, "
                "project_id TEXT NOT NULL, credential_subject_id TEXT NOT NULL, created_at TEXT NOT NULL, "
                "UNIQUE(project_id, credential_subject_id))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS xhs_controlled_credential_reconciliations ("
                "reconciliation_command_id TEXT PRIMARY KEY, pending_command_id TEXT NOT NULL UNIQUE, "
                "project_id TEXT NOT NULL, credential_subject_id TEXT NOT NULL, "
                "result_payload TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            conn.execute("CREATE TABLE IF NOT EXISTS xhs_controlled_credential_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT OR REPLACE INTO xhs_controlled_credential_meta(key, value) VALUES ('schema_version', '2')")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _current_row(conn: sqlite3.Connection, project_id: str, subject_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM xhs_controlled_credential_authorizations WHERE project_id = ? AND provider = ? AND credential_subject_id = ?",
            (project_id, _PROVIDER, subject_id),
        ).fetchone()

    @staticmethod
    def _row_by_id(conn: sqlite3.Connection, authorization_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM xhs_controlled_credential_authorizations WHERE authorization_id = ?", (authorization_id,)).fetchone()
        if row is None:
            raise XiaohongshuControlledCredentialError("controlled credential authorization is unavailable")
        return row

    def _require_active_current(self, conn: sqlite3.Connection, values: dict[str, object]) -> sqlite3.Row:
        current = self._current_row(conn, str(values["project_id"]), str(values["credential_subject_id"]))
        if current is None or current["state"] != "active" or int(current["authorization_revision"]) != values["expected_authorization_revision"]:
            raise XiaohongshuControlledCredentialConflict("controlled credential authorization revision changed")
        if current["boundary_profile_id"] != values["boundary_profile_id"] or int(current["boundary_revision"]) != values["boundary_revision"]:
            raise XiaohongshuControlledCredentialConflict("controlled credential Boundary binding changed")
        return current

    def _replay(self, conn: sqlite3.Connection, *, command_id: str, operation: str, semantic: dict[str, object]) -> XiaohongshuControlledCredentialAuthorization | None:
        row = conn.execute("SELECT * FROM xhs_controlled_credential_commands WHERE command_id = ?", (command_id,)).fetchone()
        if row is None:
            intent = conn.execute(
                "SELECT * FROM xhs_controlled_credential_command_intents WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if intent is None:
                return None
            if intent["operation"] != operation or intent["semantic"] != _canonical(semantic):
                raise XiaohongshuControlledCredentialConflict("controlled credential command identity is already used")
            raise XiaohongshuControlledCredentialIndeterminate(
                "controlled credential command outcome is indeterminate"
            )
        if row["operation"] != operation or row["semantic"] != _canonical(semantic):
            raise XiaohongshuControlledCredentialConflict("controlled credential command identity is already used")
        return _authorization_from_payload(str(row["result_payload"]))

    def _reserve_secret_command(
        self, conn: sqlite3.Connection, *, command_id: str, operation: str,
        semantic: dict[str, object], project_id: str, subject_id: str,
    ) -> None:
        self._begin(conn)
        try:
            pending = conn.execute(
                "SELECT command_id FROM xhs_controlled_credential_command_intents "
                "WHERE project_id = ? AND credential_subject_id = ?",
                (project_id, subject_id),
            ).fetchone()
            if pending is not None:
                raise XiaohongshuControlledCredentialIndeterminate(
                    "controlled credential command outcome is indeterminate"
                )
            conn.execute(
                "INSERT INTO xhs_controlled_credential_command_intents "
                "(command_id, operation, semantic, project_id, credential_subject_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (command_id, operation, _canonical(semantic), project_id, subject_id, _now()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _record_command(conn: sqlite3.Connection, command_id: str, operation: str, semantic: dict[str, object], authorization_id: str, result: XiaohongshuControlledCredentialAuthorization) -> None:
        conn.execute(
            "INSERT INTO xhs_controlled_credential_commands (command_id, operation, semantic, authorization_id, result_revision, result_payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (command_id, operation, _canonical(semantic), authorization_id, result.authorization_revision, _canonical(result.public()), _now()),
        )

    def _write_secret(self, secret_key: str, value: str):
        try:
            self._secrets.set(secret_key, value)
            generation = self._secrets.get_generation(secret_key)
        except Exception as error:
            raise XiaohongshuControlledCredentialError("controlled credential secret update failed") from error
        if not self._secrets.has_secret(secret_key):
            raise XiaohongshuControlledCredentialError("controlled credential secret is unavailable")
        return SecretSnapshot("", generation)


def _grant_values(**raw: object) -> dict[str, object]:
    values = {
        "project_id": _identity(raw["project_id"], "project identity"),
        "credential_subject_id": _identity(raw["credential_subject_id"], "credential subject"),
        "boundary_profile_id": _identity(raw["boundary_profile_id"], "Boundary profile identity"),
        "boundary_revision": _revision(raw["boundary_revision"]),
        "expires_at": _expiry(raw["expires_at"]),
        "cookie_value": _cookie(raw["cookie_value"]),
        "expected_authorization_revision": _nonnegative_revision(raw["expected_authorization_revision"]),
        "command_id": _command_id(raw["command_id"]),
    }
    return values


def _semantic(values: dict[str, object]) -> dict[str, object]:
    # Never include the cookie string or a content-derived fingerprint here.
    return {key: value for key, value in values.items() if key != "cookie_value"}


def _authorization(row: sqlite3.Row) -> XiaohongshuControlledCredentialAuthorization:
    return XiaohongshuControlledCredentialAuthorization(
        authorization_id=str(row["authorization_id"]), project_id=str(row["project_id"]), provider=str(row["provider"]),
        credential_subject_id=str(row["credential_subject_id"]), boundary_profile_id=str(row["boundary_profile_id"]),
        boundary_revision=int(row["boundary_revision"]), authorization_revision=int(row["authorization_revision"]),
        state=str(row["state"]), expires_at=str(row["expires_at"]), secret_generation=int(row["secret_generation"]),
    )


def _authorization_from_payload(payload: str) -> XiaohongshuControlledCredentialAuthorization:
    try:
        raw = json.loads(payload)
        if not isinstance(raw, dict) or set(raw) != {
            "authorization_id", "project_id", "provider", "credential_subject_id",
            "boundary_profile_id", "boundary_revision", "authorization_revision",
            "state", "expires_at", "secret_generation",
        }:
            raise ValueError
        if raw["provider"] != _PROVIDER or raw["state"] not in {"active", "revoked"}:
            raise ValueError
        return XiaohongshuControlledCredentialAuthorization(
            authorization_id=_identity(raw["authorization_id"], "authorization identity"),
            project_id=_identity(raw["project_id"], "project identity"), provider=_PROVIDER,
            credential_subject_id=_identity(raw["credential_subject_id"], "credential subject"),
            boundary_profile_id=_identity(raw["boundary_profile_id"], "Boundary profile identity"),
            boundary_revision=_revision(raw["boundary_revision"]),
            authorization_revision=_revision(raw["authorization_revision"]), state=str(raw["state"]),
            expires_at=_canonical_expiry(raw["expires_at"]), secret_generation=_revision(raw["secret_generation"]),
        )
    except (TypeError, ValueError, XiaohongshuControlledCredentialError) as error:
        raise XiaohongshuControlledCredentialError("controlled credential command receipt is invalid") from error


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise XiaohongshuControlledCredentialError(f"{label} is invalid")
    return value


def _command_id(value: object) -> str:
    if not isinstance(value, str) or not _COMMAND_ID.fullmatch(value):
        raise XiaohongshuControlledCredentialError("controlled credential command identity is invalid")
    return value


def _revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise XiaohongshuControlledCredentialError("controlled credential revision is invalid")
    return value


def _nonnegative_revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise XiaohongshuControlledCredentialError("controlled credential revision is invalid")
    return value


def _cookie(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise XiaohongshuControlledCredentialError("controlled credential value is unavailable")
    return value.strip()


def _expiry(value: object) -> str:
    parsed = _canonical_expiry(value)
    parsed_at = _parse_expiry(parsed)
    if parsed_at <= datetime.now(timezone.utc):
        raise XiaohongshuControlledCredentialError("controlled credential expiry is invalid")
    return parsed


def _canonical_expiry(value: object) -> str:
    if not isinstance(value, str):
        raise XiaohongshuControlledCredentialError("controlled credential expiry is invalid")
    parsed = _parse_expiry(value)
    return parsed.isoformat().replace("+00:00", "Z")


def _parse_expiry(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise XiaohongshuControlledCredentialError("controlled credential expiry is invalid") from error
    if parsed.tzinfo is None:
        raise XiaohongshuControlledCredentialError("controlled credential expiry is invalid")
    return parsed.astimezone(timezone.utc)


def _clock(value: datetime | None) -> datetime:
    now = datetime.now(timezone.utc) if value is None else value
    if now.tzinfo is None:
        raise XiaohongshuControlledCredentialError("controlled credential clock is invalid")
    return now.astimezone(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _witness_inputs(witness: VerifiedXiaohongshuControlledCredential) -> dict[str, object]:
    return {
        "project_id": witness.project_id,
        "provider": witness.provider,
        "credential_subject_id": witness.credential_subject_id,
        "boundary_profile_id": witness.boundary_profile_id,
        "boundary_revision": witness.boundary_revision,
        "authorization_revision": witness.authorization_revision,
        "secret_generation": witness.secret_generation,
    }


def _new_secret_key() -> str:
    return f"xhs-controlled-{uuid4().hex}"
