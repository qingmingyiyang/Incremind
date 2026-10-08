"""Durable, single-use confirmations for project capability selections."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Callable

from backend.shared.interprocess_lock import interprocess_file_lock


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ACTIONS = frozenset({"select", "reset_exclusion"})
_TTL_SECONDS = 5 * 60


class ProjectCapabilitySelectionConfirmationError(ValueError):
    """A confirmation is invalid, expired, already used, or does not bind."""


class ProjectCapabilitySelectionConfirmationConflict(
    ProjectCapabilitySelectionConfirmationError,
):
    """The durable confirmation authority cannot currently be used."""


class ProjectCapabilitySelectionConfirmationToken(str):
    """A newly-issued raw token, with expiry metadata for its caller."""

    expires_at: str

    def __new__(cls, value: str, *, expires_at: str) -> ProjectCapabilitySelectionConfirmationToken:
        instance = super().__new__(cls, value)
        instance.expires_at = expires_at
        return instance

    @property
    def token(self) -> str:
        return str(self)

    def __getnewargs_ex__(self) -> tuple[tuple[str], dict[str, str]]:
        return (str(self),), {"expires_at": self.expires_at}


@dataclass(frozen=True, slots=True)
class ProjectCapabilitySelectionConfirmation:
    project_id: str
    action: str
    target_stable_id: str
    command_id: str
    expected_boundary_revision: int
    expected_capability_revision: int
    expected_registry_generation: int
    contract_version: int
    created_at: str
    expires_at: str


class ProjectCapabilitySelectionConfirmationStore:
    """SQLite-backed authority for short-lived, exactly-once confirmations."""

    def __init__(self, root_dir: Path, *, clock: Callable[[], float] | None = None) -> None:
        self._path = (
            Path(root_dir) / ".rebuild-data" / "capability-selection-confirmations.sqlite3"
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or time.time
        self._initialize()

    def close(self) -> None:
        """Kept for symmetry with other durable security authorities."""

    def issue(
        self, *, project_id: str, action: str, target_stable_id: str,
        command_id: str, expected_boundary_revision: int,
        expected_capability_revision: int, expected_registry_generation: int,
        contract_version: int,
    ) -> ProjectCapabilitySelectionConfirmationToken:
        values = _validated_binding(
            project_id=project_id, action=action, target_stable_id=target_stable_id,
            command_id=command_id, expected_boundary_revision=expected_boundary_revision,
            expected_capability_revision=expected_capability_revision,
            expected_registry_generation=expected_registry_generation,
            contract_version=contract_version,
        )
        raw_token = secrets.token_urlsafe(32)
        verifier = _verifier(raw_token)
        now = self._clock()
        expires = now + _TTL_SECONDS
        try:
            with interprocess_file_lock(self._path):
                with closing(self._connection()) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("DELETE FROM capability_selection_confirmations WHERE expires_at <= ?", (now,))
                    conn.execute(
                        """INSERT INTO capability_selection_confirmations (
                            token_verifier, project_id, action, target_stable_id, command_id,
                            expected_boundary_revision, expected_capability_revision,
                            expected_registry_generation, contract_version, created_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (verifier, *values, now, expires),
                    )
                    conn.commit()
        except (sqlite3.Error, TimeoutError) as error:
            raise ProjectCapabilitySelectionConfirmationConflict(
                "Capability selection confirmation authority is busy"
            ) from error
        return ProjectCapabilitySelectionConfirmationToken(
            raw_token, expires_at=_timestamp(expires),
        )

    create = issue

    def consume_exact(
        self, *, token: str, project_id: str, action: str, target_stable_id: str,
        command_id: str, expected_boundary_revision: int,
        expected_capability_revision: int, expected_registry_generation: int,
        contract_version: int,
    ) -> ProjectCapabilitySelectionConfirmation:
        values = _validated_binding(
            project_id=project_id, action=action, target_stable_id=target_stable_id,
            command_id=command_id, expected_boundary_revision=expected_boundary_revision,
            expected_capability_revision=expected_capability_revision,
            expected_registry_generation=expected_registry_generation,
            contract_version=contract_version,
        )
        verifier = _verifier(token)
        now = self._clock()
        try:
            with interprocess_file_lock(self._path):
                with closing(self._connection()) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("DELETE FROM capability_selection_confirmations WHERE expires_at <= ?", (now,))
                    row = conn.execute(
                        """DELETE FROM capability_selection_confirmations
                        WHERE token_verifier=? AND project_id=? AND action=?
                          AND target_stable_id=? AND command_id=?
                          AND expected_boundary_revision=?
                          AND expected_capability_revision=?
                          AND expected_registry_generation=? AND contract_version=?
                          AND expires_at > ?
                        RETURNING project_id, action, target_stable_id, command_id,
                                  expected_boundary_revision, expected_capability_revision,
                                  expected_registry_generation, contract_version,
                                  created_at, expires_at""",
                        (verifier, *values, now),
                    ).fetchone()
                    conn.commit()
        except (sqlite3.Error, TimeoutError) as error:
            raise ProjectCapabilitySelectionConfirmationConflict(
                "Capability selection confirmation authority is busy"
            ) from error
        if row is None:
            raise ProjectCapabilitySelectionConfirmationError(
                "Capability selection confirmation is invalid, expired, or already used"
            )
        return ProjectCapabilitySelectionConfirmation(
            *row[:8], created_at=_timestamp(row[8]), expires_at=_timestamp(row[9]),
        )

    def _initialize(self) -> None:
        try:
            with interprocess_file_lock(self._path):
                with closing(self._connection()) as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(
                        """CREATE TABLE IF NOT EXISTS capability_selection_confirmations (
                            token_verifier TEXT PRIMARY KEY,
                            project_id TEXT NOT NULL, action TEXT NOT NULL,
                            target_stable_id TEXT NOT NULL, command_id TEXT NOT NULL,
                            expected_boundary_revision INTEGER NOT NULL,
                            expected_capability_revision INTEGER NOT NULL,
                            expected_registry_generation INTEGER NOT NULL,
                            contract_version INTEGER NOT NULL,
                            created_at REAL NOT NULL, expires_at REAL NOT NULL
                        )"""
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS capability_selection_confirmations_expiry "
                        "ON capability_selection_confirmations(expires_at)"
                    )
                    conn.commit()
        except (sqlite3.Error, TimeoutError) as error:
            raise ProjectCapabilitySelectionConfirmationConflict(
                "Capability selection confirmation authority is busy"
            ) from error

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn


def _validated_binding(**values: object) -> tuple[object, ...]:
    for name in ("project_id", "action", "target_stable_id", "command_id"):
        values[name] = _identity(values[name], name.replace("_", " "))
    if values["action"] not in _ACTIONS:
        raise ProjectCapabilitySelectionConfirmationError(
            "Capability selection confirmation action is invalid"
        )
    for name in ("expected_boundary_revision", "expected_capability_revision", "contract_version"):
        values[name] = _positive_integer(values[name], name.replace("_", " "))
    generation = values["expected_registry_generation"]
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ProjectCapabilitySelectionConfirmationError(
            "expected registry generation must be a non-negative integer"
        )
    return tuple(values[name] for name in (
        "project_id", "action", "target_stable_id", "command_id",
        "expected_boundary_revision", "expected_capability_revision",
        "expected_registry_generation", "contract_version",
    ))


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ProjectCapabilitySelectionConfirmationError(
            f"Capability selection confirmation {label} is invalid"
        )
    return value


def _positive_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ProjectCapabilitySelectionConfirmationError(
            f"{label} must be a positive integer"
        )
    return value


def _verifier(token: object) -> str:
    if not isinstance(token, str) or not token or len(token) > 128:
        raise ProjectCapabilitySelectionConfirmationError(
            "Capability selection confirmation is invalid, expired, or already used"
        )
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()
