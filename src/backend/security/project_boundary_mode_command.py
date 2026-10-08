"""Durable, local-only Boundary mode command.

The receipt is an audit/recovery record, not a policy authority.  Profile
stores remain the sole policy and capability-selection authorities.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from threading import Lock

from backend.security.project_boundary_profiles import ProjectBoundaryProfileConflict, ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileConflict, ProjectCapabilityProfileStore
from backend.shared.interprocess_lock import interprocess_file_lock
from backend.security.project_boundary_mutation_reservation import (
    ProjectBoundaryMutationReservation,
    ProjectBoundaryMutationReservationConflict,
)

_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MODES = {"open": "allow", "guarded": "review", "sealed": "deny"}
_ACTIVE = {"prepared", "boundary_updated"}
_STATUSES = _ACTIVE | {"completed", "requires_repair"}
_ACTOR_ID = "desktop-user"
_LOCK = Lock()


class BoundaryModeCommandError(ValueError):
    pass


class BoundaryModeCommandConflict(BoundaryModeCommandError):
    pass


@dataclass(frozen=True, slots=True)
class BoundaryModeCommandReceipt:
    command_id: str
    project_id: str
    mode: str
    remote_default: str
    actor_id: str
    status: str
    expected_boundary_revision: int
    expected_capability_revision: int
    boundary_revision: int | None
    capability_revision: int | None
    created_at: str
    updated_at: str

    def public(self) -> dict[str, object]:
        return {
            "command_id": self.command_id, "project_id": self.project_id,
            "mode": self.mode, "remote_default": self.remote_default,
            "actor_id": self.actor_id, "status": self.status,
            "expected_boundary_revision": self.expected_boundary_revision,
            "expected_capability_revision": self.expected_capability_revision,
            "boundary_revision": self.boundary_revision,
            "capability_revision": self.capability_revision,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


class ProjectBoundaryModeCommandService:
    def __init__(self, root_dir: Path) -> None:
        root = Path(root_dir)
        path = root / ".rebuild-data" / "boundary-mode-commands.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with interprocess_file_lock(path):
                initialize_journal = not path.exists()
                self._conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA busy_timeout=5000")
                if initialize_journal:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("""CREATE TABLE IF NOT EXISTS boundary_mode_commands (
                    command_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, mode TEXT NOT NULL,
                    remote_default TEXT NOT NULL, actor_id TEXT NOT NULL,
                    expected_boundary_revision INTEGER NOT NULL, expected_capability_revision INTEGER NOT NULL,
                    status TEXT NOT NULL, boundary_revision INTEGER, capability_revision INTEGER,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )""")
                columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(boundary_mode_commands)")}
                if "remote_default" not in columns:
                    self._conn.execute("ALTER TABLE boundary_mode_commands ADD COLUMN remote_default TEXT")
                    self._conn.execute("""UPDATE boundary_mode_commands SET remote_default = CASE mode
                        WHEN 'open' THEN 'allow' WHEN 'guarded' THEN 'review' WHEN 'sealed' THEN 'deny' END""")
                if "actor_id" not in columns:
                    self._conn.execute("ALTER TABLE boundary_mode_commands ADD COLUMN actor_id TEXT NOT NULL DEFAULT 'desktop-user'")
                self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_boundary_mode_command_per_project ON boundary_mode_commands(project_id) WHERE status IN ('prepared','boundary_updated')")
                self._conn.execute("""CREATE TABLE IF NOT EXISTS boundary_mode_command_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, command_id TEXT NOT NULL, status TEXT NOT NULL,
                    at TEXT NOT NULL
                )""")
                self._conn.commit()
        except (sqlite3.OperationalError, TimeoutError) as error:
            if hasattr(self, "_conn"):
                self._conn.close()
            raise BoundaryModeCommandConflict("Boundary mode receipt authority is busy") from error
        self._boundary = ProjectBoundaryProfileStore(root)
        self._capability = ProjectCapabilityProfileStore(root)
        self._reservation = ProjectBoundaryMutationReservation(root)

    def close(self) -> None:
        self._conn.close()

    def submit(self, *, project_id: str, command_id: str, mode: str, expected_boundary_revision: int, expected_capability_revision: int) -> BoundaryModeCommandReceipt:
        command_id = _command(command_id)
        if mode not in _MODES:
            raise BoundaryModeCommandError("boundary mode is unsupported")
        if not _revision(expected_boundary_revision) or not _revision(expected_capability_revision):
            raise BoundaryModeCommandError("expected profile revisions must be positive integers")
        # Validate the project identity before creating an active durable receipt.
        self._boundary.get(project_id)
        self._capability.get(project_id)
        try:
            execution = self._reservation.execution(project_id)
        except ProjectBoundaryMutationReservationConflict as error:
            raise BoundaryModeCommandConflict("Boundary mutation execution is busy") from error
        with _LOCK, execution:
            reserved_new = False
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._get(command_id)
                semantic = f"{mode}|{expected_boundary_revision}|{expected_capability_revision}"
                if existing is None:
                    self._reservation.reserve(
                        project_id=project_id, command_id=command_id,
                        command_kind="mode", semantic=semantic,
                    )
                    reserved_new = True
                    boundary = self._boundary.get(project_id).profile
                    capability = self._capability.get(project_id).profile
                    if (
                        capability.boundary_profile_id,
                        capability.boundary_profile_revision,
                    ) != (boundary.profile_id, boundary.revision):
                        raise BoundaryModeCommandConflict(
                            "project Capability and Boundary binding drifted"
                        )
                    now = _now()
                    self._conn.execute("""INSERT INTO boundary_mode_commands (
                        command_id, project_id, mode, remote_default, actor_id,
                        expected_boundary_revision, expected_capability_revision, status,
                        boundary_revision, capability_revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', NULL, NULL, ?, ?)""", (
                        command_id, project_id, mode, _MODES[mode], _ACTOR_ID,
                        expected_boundary_revision, expected_capability_revision, now, now,
                    ))
                    self._transition(command_id, "prepared", now)
                    existing = self._get(command_id)
                elif (existing.project_id, existing.mode, existing.expected_boundary_revision, existing.expected_capability_revision) != (project_id, mode, expected_boundary_revision, expected_capability_revision):
                    raise BoundaryModeCommandConflict("command id has different semantics")
                elif existing.status in _ACTIVE:
                    self._reservation.reserve(
                        project_id=project_id, command_id=command_id,
                        command_kind="mode", semantic=semantic,
                    )
                else:
                    # Repair a crash after the terminal receipt commit but
                    # before the separate reservation release.
                    self._reservation.release(
                        project_id=project_id, command_id=command_id,
                        command_kind="mode",
                    )
                self._conn.commit()
            except sqlite3.IntegrityError as error:
                self._conn.rollback()
                if reserved_new and self._get(command_id) is None:
                    self._reservation.release(
                        project_id=project_id, command_id=command_id,
                        command_kind="mode",
                    )
                raise BoundaryModeCommandConflict("another Boundary mode command is active for this project") from error
            except sqlite3.OperationalError as error:
                self._conn.rollback()
                if reserved_new:
                    self._reservation.release(
                        project_id=project_id, command_id=command_id,
                        command_kind="mode",
                    )
                raise BoundaryModeCommandConflict("Boundary mode receipt authority is busy") from error
            except ProjectBoundaryMutationReservationConflict as error:
                self._conn.rollback()
                raise BoundaryModeCommandConflict("another Boundary mode command is active for this project") from error
            except Exception:
                self._conn.rollback()
                if reserved_new and self._get(command_id) is None:
                    self._reservation.release(
                        project_id=project_id, command_id=command_id,
                        command_kind="mode",
                    )
                raise
            assert existing is not None
            try:
                return self._advance(existing)
            except TimeoutError as error:
                raise BoundaryModeCommandConflict("project profile authority is busy") from error
            except sqlite3.OperationalError as error:
                self._conn.rollback()
                raise BoundaryModeCommandConflict("Boundary mode receipt authority is busy") from error

    def get(self, command_id: str) -> BoundaryModeCommandReceipt | None:
        with _LOCK:
            try:
                return self._get(_command(command_id))
            except sqlite3.OperationalError as error:
                raise BoundaryModeCommandConflict("Boundary mode receipt authority is busy") from error

    def _advance(self, receipt: BoundaryModeCommandReceipt) -> BoundaryModeCommandReceipt:
        if receipt.status in {"completed", "requires_repair"}:
            return receipt
        boundary = self._boundary.get(receipt.project_id).profile
        capability = self._capability.get(receipt.project_id).profile
        target_boundary = _written_revision(receipt.expected_boundary_revision)
        target_capability = _written_revision(receipt.expected_capability_revision)
        if receipt.status == "prepared":
            # A crash immediately after the profile write is recognized by the
            # target revision and resumed without a second Boundary mutation.
            if boundary.revision == target_boundary and boundary.mode == receipt.mode and boundary.remote_default == receipt.remote_default:
                receipt = self._set(receipt, "boundary_updated", boundary_revision=boundary.revision)
            elif boundary.revision != receipt.expected_boundary_revision or capability.revision != receipt.expected_capability_revision:
                return self._set(receipt, "requires_repair")
            else:
                try:
                    updated = self._boundary.set_mode(receipt.project_id, mode=receipt.mode, remote_default=receipt.remote_default, expected_revision=receipt.expected_boundary_revision)
                except ProjectBoundaryProfileConflict:
                    return self._set(receipt, "requires_repair")
                receipt = self._set(receipt, "boundary_updated", boundary_revision=updated.profile.revision)
        boundary = self._boundary.get(receipt.project_id).profile
        capability = self._capability.get(receipt.project_id).profile
        if (boundary.revision != target_boundary or boundary.mode != receipt.mode or boundary.remote_default != receipt.remote_default):
            return self._set(receipt, "requires_repair")
        if capability.revision == target_capability and (capability.boundary_profile_id, capability.boundary_profile_revision) == (boundary.profile_id, boundary.revision):
            return self._set(receipt, "completed", boundary_revision=boundary.revision, capability_revision=capability.revision)
        if capability.revision != receipt.expected_capability_revision:
            return self._set(receipt, "requires_repair")
        try:
            rebound = self._capability.rebind_boundary(receipt.project_id, expected_revision=receipt.expected_capability_revision, boundary_profile_id=boundary.profile_id, boundary_profile_revision=boundary.revision)
        except ProjectCapabilityProfileConflict:
            return self._set(receipt, "requires_repair")
        return self._set(receipt, "completed", boundary_revision=boundary.revision, capability_revision=rebound.profile.revision)

    def _get(self, command_id: str) -> BoundaryModeCommandReceipt | None:
        row = self._conn.execute("SELECT * FROM boundary_mode_commands WHERE command_id = ?", (command_id,)).fetchone()
        return _receipt(row) if row is not None else None

    def _set(self, receipt: BoundaryModeCommandReceipt, status: str, *, boundary_revision: int | None = None, capability_revision: int | None = None) -> BoundaryModeCommandReceipt:
        now = _now()
        boundary_revision = receipt.boundary_revision if boundary_revision is None else boundary_revision
        capability_revision = receipt.capability_revision if capability_revision is None else capability_revision
        self._conn.execute("UPDATE boundary_mode_commands SET status=?, boundary_revision=?, capability_revision=?, updated_at=? WHERE command_id=?", (status, boundary_revision, capability_revision, now, receipt.command_id))
        self._transition(receipt.command_id, status, now)
        self._conn.commit()
        if status in {"completed", "requires_repair"}:
            self._reservation.release(
                project_id=receipt.project_id, command_id=receipt.command_id,
                command_kind="mode",
            )
        result = self._get(receipt.command_id)
        assert result is not None
        return result

    def _transition(self, command_id: str, status: str, at: str) -> None:
        self._conn.execute("INSERT INTO boundary_mode_command_transitions(command_id, status, at) VALUES (?, ?, ?)", (command_id, status, at))


def _receipt(row: sqlite3.Row) -> BoundaryModeCommandReceipt:
    values = dict(row)
    mode = values.get("mode")
    if mode not in _MODES or values.get("remote_default") != _MODES[mode]:
        raise BoundaryModeCommandError("Boundary mode receipt semantics are invalid")
    if values.get("actor_id") != _ACTOR_ID or values.get("status") not in _STATUSES:
        raise BoundaryModeCommandError("Boundary mode receipt authority is invalid")
    return BoundaryModeCommandReceipt(**values)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _command(value: str) -> str:
    value = str(value).strip()
    if not _COMMAND_ID.fullmatch(value):
        raise BoundaryModeCommandError("command identity is invalid")
    return value


def _revision(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _written_revision(expected_revision: int) -> int:
    return expected_revision + 1
