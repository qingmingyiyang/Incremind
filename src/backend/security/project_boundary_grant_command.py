"""Durable, server-derived project Boundary grant commands.

Receipts contain only stable UI identities and revision coordinates. The
complete Tool/MCP target remains exclusively in the Boundary profile.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3
from threading import Lock
from uuid import uuid4

from backend.security.project_boundary_profiles import (
    ProjectBoundaryProfileConflict,
    ProjectBoundaryProfileStore,
)
from backend.security.project_boundary_mutation_reservation import (
    ProjectBoundaryMutationReservation,
    ProjectBoundaryMutationReservationConflict,
)
from backend.security.project_capability_profiles import (
    ProjectCapabilityProfileConflict,
    ProjectCapabilityProfileStore,
)
from backend.shared.interprocess_lock import interprocess_file_lock
from core.ai_boundary import BoundaryGrant
from core.ai_tooling import (
    ProjectCapabilityCatalogProjector,
    tool_boundary_target_identity,
    tool_from_capability,
)

_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DURATIONS = {
    "24_hours": timedelta(hours=24),
    "30_days": timedelta(days=30),
    "90_days": timedelta(days=90),
    "until_revoked": None,
}
_ACTIVE = {"prepared", "boundary_updated"}
_STATUSES = _ACTIVE | {"completed", "requires_repair"}
_LOCK = Lock()


class BoundaryGrantCommandError(ValueError):
    pass


class BoundaryGrantCommandConflict(BoundaryGrantCommandError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedGrantTarget:
    stable_id: str
    target_id: str
    effect: str
    destination: str
    data_classes: tuple[str, ...]
    redaction_required: bool


@dataclass(frozen=True, slots=True)
class GrantCommandReceipt:
    command_id: str
    project_id: str
    kind: str
    status: str
    grant_id: str
    target_stable_id: str | None
    duration: str | None
    expires_at: str | None
    expected_boundary_revision: int
    expected_capability_revision: int
    expected_grant_revision: int | None
    boundary_revision: int | None
    capability_revision: int | None
    grant_revision: int | None
    actor_id: str
    created_at: str
    updated_at: str

    def public(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "project_id": self.project_id,
            "kind": self.kind,
            "status": self.status,
            "grant_id": self.grant_id,
            "target_stable_id": self.target_stable_id,
            "duration": self.duration,
            "expires_at": self.expires_at,
            "expected_boundary_revision": self.expected_boundary_revision,
            "expected_capability_revision": self.expected_capability_revision,
            "expected_grant_revision": self.expected_grant_revision,
            "boundary_revision": self.boundary_revision,
            "capability_revision": self.capability_revision,
            "grant_revision": self.grant_revision,
            "actor_id": self.actor_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ProjectBoundaryGrantCommandService:
    def __init__(self, root_dir: Path) -> None:
        root = Path(root_dir)
        path = root / ".rebuild-data" / "boundary-grant-commands.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with interprocess_file_lock(path):
                initialize_journal = not path.exists()
                self._conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA busy_timeout=5000")
                if initialize_journal:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("""CREATE TABLE IF NOT EXISTS boundary_grant_commands (
                    command_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL, grant_id TEXT NOT NULL,
                    target_stable_id TEXT, duration TEXT, expires_at TEXT,
                    expected_boundary_revision INTEGER NOT NULL,
                    expected_capability_revision INTEGER NOT NULL,
                    expected_grant_revision INTEGER,
                    boundary_revision INTEGER, capability_revision INTEGER,
                    grant_revision INTEGER, actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )""")
                self._conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS
                    one_active_boundary_grant_command_per_project
                    ON boundary_grant_commands(project_id)
                    WHERE status IN ('prepared','boundary_updated')""")
                self._conn.execute("""CREATE TABLE IF NOT EXISTS boundary_grant_command_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, command_id TEXT NOT NULL,
                    status TEXT NOT NULL, at TEXT NOT NULL
                )""")
                self._conn.commit()
        except (sqlite3.OperationalError, TimeoutError) as error:
            if hasattr(self, "_conn"):
                self._conn.close()
            raise BoundaryGrantCommandConflict("Boundary grant receipt authority is busy") from error
        self._boundary = ProjectBoundaryProfileStore(root)
        self._capability = ProjectCapabilityProfileStore(root)
        self._reservation = ProjectBoundaryMutationReservation(root)

    def close(self) -> None:
        self._conn.close()

    def create(
        self,
        *,
        project_id: str,
        command_id: str,
        target: ResolvedGrantTarget,
        duration: str,
        expected_boundary_revision: int,
        expected_capability_revision: int,
    ) -> GrantCommandReceipt:
        command_id = _command(command_id)
        _revisions(expected_boundary_revision, expected_capability_revision)
        if duration not in _DURATIONS or not _eligible(target, duration):
            raise BoundaryGrantCommandError("target_unavailable")
        existing = self.get(command_id)
        if existing is not None:
            _match_create(existing, project_id, target.stable_id, duration, expected_boundary_revision, expected_capability_revision)
            if existing.status in {"completed", "requires_repair"}:
                self._release(existing)
                return existing
        try:
            execution = self._reservation.execution(project_id)
        except ProjectBoundaryMutationReservationConflict as error:
            raise BoundaryGrantCommandConflict("Boundary mutation execution is busy") from error
        with _LOCK, execution:
            semantic = f"{target.stable_id}|{duration}|{expected_boundary_revision}|{expected_capability_revision}"
            try:
                self._reservation.reserve(
                    project_id=project_id, command_id=command_id,
                    command_kind="grant-create", semantic=semantic,
                )
                if existing is None:
                    _require_current_binding(
                        self._boundary.get(project_id).profile,
                        self._capability.get(project_id).profile,
                    )
                receipt = existing or self._prepare_create(
                    project_id=project_id,
                    command_id=command_id,
                    target_stable_id=target.stable_id,
                    duration=duration,
                    expected_boundary_revision=expected_boundary_revision,
                    expected_capability_revision=expected_capability_revision,
                )
            except ProjectBoundaryMutationReservationConflict as error:
                raise BoundaryGrantCommandConflict(
                    "another Boundary mutation command is active for this project"
                ) from error
            except Exception:
                if self.get(command_id) is None:
                    self._reservation.release(
                        project_id=project_id, command_id=command_id,
                        command_kind="grant-create",
                    )
                raise
            _match_create(receipt, project_id, target.stable_id, duration, expected_boundary_revision, expected_capability_revision)
            return self._advance_create(receipt, target)

    def revoke(
        self,
        *,
        project_id: str,
        command_id: str,
        grant_id: str,
        expected_boundary_revision: int,
        expected_capability_revision: int,
        expected_grant_revision: int,
    ) -> GrantCommandReceipt:
        command_id = _command(command_id)
        _revisions(expected_boundary_revision, expected_capability_revision, expected_grant_revision)
        grant_id = _identity(grant_id, "grant identity")
        existing = self.get(command_id)
        if existing is not None:
            _match_revoke(existing, project_id, grant_id, expected_boundary_revision, expected_capability_revision, expected_grant_revision)
            if existing.status in {"completed", "requires_repair"}:
                self._release(existing)
                return existing
        try:
            execution = self._reservation.execution(project_id)
        except ProjectBoundaryMutationReservationConflict as error:
            raise BoundaryGrantCommandConflict("Boundary mutation execution is busy") from error
        with _LOCK, execution:
            semantic = f"{grant_id}|{expected_boundary_revision}|{expected_capability_revision}|{expected_grant_revision}"
            try:
                self._reservation.reserve(
                    project_id=project_id, command_id=command_id,
                    command_kind="grant-revoke", semantic=semantic,
                )
                if existing is None:
                    _require_current_binding(
                        self._boundary.get(project_id).profile,
                        self._capability.get(project_id).profile,
                    )
                receipt = existing or self._prepare_revoke(
                    project_id=project_id,
                    command_id=command_id,
                    grant_id=grant_id,
                    expected_boundary_revision=expected_boundary_revision,
                    expected_capability_revision=expected_capability_revision,
                    expected_grant_revision=expected_grant_revision,
                )
            except ProjectBoundaryMutationReservationConflict as error:
                raise BoundaryGrantCommandConflict(
                    "another Boundary mutation command is active for this project"
                ) from error
            except Exception:
                if self.get(command_id) is None:
                    self._reservation.release(
                        project_id=project_id, command_id=command_id,
                        command_kind="grant-revoke",
                    )
                raise
            _match_revoke(receipt, project_id, grant_id, expected_boundary_revision, expected_capability_revision, expected_grant_revision)
            return self._advance_revoke(receipt)

    def get(self, command_id: str) -> GrantCommandReceipt | None:
        command_id = _command(command_id)
        try:
            row = self._conn.execute(
                "SELECT * FROM boundary_grant_commands WHERE command_id=?", (command_id,),
            ).fetchone()
        except sqlite3.OperationalError as error:
            raise BoundaryGrantCommandConflict("Boundary grant receipt authority is busy") from error
        return _receipt(row) if row is not None else None

    def create_replay(
        self, *, project_id: str, command_id: str, target_stable_id: str,
        duration: str, expected_boundary_revision: int,
        expected_capability_revision: int,
    ) -> GrantCommandReceipt | None:
        """Return an exact receipt without requiring a live Runtime."""
        command_id = _command(command_id)
        _revisions(expected_boundary_revision, expected_capability_revision)
        if duration not in _DURATIONS:
            raise BoundaryGrantCommandError("grant duration is unsupported")
        receipt = self.get(command_id)
        if receipt is None:
            return None
        _match_create(
            receipt, project_id, target_stable_id, duration,
            expected_boundary_revision, expected_capability_revision,
        )
        if receipt.status in {"completed", "requires_repair"}:
            self._release(receipt)
        return receipt

    def require_repair_create(self, receipt: GrantCommandReceipt) -> GrantCommandReceipt:
        """Close an active create whose frozen target can no longer be resolved."""
        if receipt.kind != "create":
            raise BoundaryGrantCommandError("grant command kind is invalid")
        if receipt.status in {"completed", "requires_repair"}:
            return receipt
        semantic = (
            f"{receipt.target_stable_id}|{receipt.duration}|"
            f"{receipt.expected_boundary_revision}|{receipt.expected_capability_revision}"
        )
        with _LOCK, self._reservation.execution(receipt.project_id):
            self._reservation.reserve(
                project_id=receipt.project_id, command_id=receipt.command_id,
                command_kind="grant-create", semantic=semantic,
            )
            current = self.get(receipt.command_id)
            if current is None or current != receipt:
                raise BoundaryGrantCommandConflict("grant command receipt drifted")
            return self._set(current, "requires_repair")

    def _prepare_create(self, **values: object) -> GrantCommandReceipt:
        now = _now()
        delta = _DURATIONS[str(values["duration"])]
        expiry = None if delta is None else (datetime.fromisoformat(now) + delta).isoformat()
        return self._insert(
            kind="create", status="prepared", grant_id=uuid4().hex,
            expires_at=expiry, expected_grant_revision=None,
            grant_revision=None, actor_id="desktop-user", created_at=now,
            updated_at=now, boundary_revision=None, capability_revision=None,
            **values,
        )

    def _prepare_revoke(self, **values: object) -> GrantCommandReceipt:
        now = _now()
        return self._insert(
            kind="revoke", status="prepared", target_stable_id=None,
            duration=None, expires_at=None, grant_revision=None,
            actor_id="desktop-user", created_at=now, updated_at=now,
            boundary_revision=None, capability_revision=None, **values,
        )

    def _insert(self, **values: object) -> GrantCommandReceipt:
        fields = tuple(values)
        placeholders = ",".join("?" for _ in fields)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                f"INSERT INTO boundary_grant_commands ({','.join(fields)}) VALUES ({placeholders})",  # noqa: S608
                tuple(values[field] for field in fields),
            )
            self._transition(str(values["command_id"]), "prepared", str(values["created_at"]))
            self._conn.commit()
        except sqlite3.IntegrityError as error:
            self._conn.rollback()
            concurrent = self.get(str(values["command_id"]))
            if concurrent is not None:
                return concurrent
            raise BoundaryGrantCommandConflict("another Boundary grant command is active for this project") from error
        except sqlite3.OperationalError as error:
            self._conn.rollback()
            raise BoundaryGrantCommandConflict("Boundary grant receipt authority is busy") from error
        receipt = self.get(str(values["command_id"]))
        assert receipt is not None
        return receipt

    def _advance_create(self, receipt: GrantCommandReceipt, target: ResolvedGrantTarget) -> GrantCommandReceipt:
        if receipt.status in {"completed", "requires_repair"}:
            return receipt
        boundary = self._boundary.get(receipt.project_id).profile
        capability = self._capability.get(receipt.project_id).profile
        target_boundary = receipt.expected_boundary_revision + 1
        target_capability = receipt.expected_capability_revision + 1
        expiry = datetime.fromisoformat(receipt.expires_at) if receipt.expires_at else None
        expected_grant = BoundaryGrant(
            receipt.grant_id, "ai-kernel", receipt.project_id, target.target_id,
            (target.effect,), target.data_classes, (target.destination,), expiry, 1,
            False, target.redaction_required,
        )
        if receipt.status == "prepared":
            written = _find_grant(boundary, receipt.grant_id)
            if boundary.revision == target_boundary and written == expected_grant:
                receipt = self._set(receipt, "boundary_updated", boundary_revision=boundary.revision, grant_revision=1)
            elif boundary.revision != receipt.expected_boundary_revision or capability.revision != receipt.expected_capability_revision:
                return self._set(receipt, "requires_repair")
            else:
                try:
                    updated = self._boundary.create_grant(
                        receipt.project_id, grant=expected_grant,
                        expected_revision=receipt.expected_boundary_revision,
                    )
                except ProjectBoundaryProfileConflict:
                    return self._set(receipt, "requires_repair")
                receipt = self._set(receipt, "boundary_updated", boundary_revision=updated.profile.revision, grant_revision=1)
        return self._finish_binding(receipt, target_boundary, target_capability)

    def _advance_revoke(self, receipt: GrantCommandReceipt) -> GrantCommandReceipt:
        if receipt.status in {"completed", "requires_repair"}:
            return receipt
        boundary = self._boundary.get(receipt.project_id).profile
        capability = self._capability.get(receipt.project_id).profile
        target_boundary = receipt.expected_boundary_revision + 1
        target_capability = receipt.expected_capability_revision + 1
        target_grant_revision = receipt.expected_grant_revision + 1  # type: ignore[operator]
        if receipt.status == "prepared":
            written = _find_grant(boundary, receipt.grant_id)
            if (
                boundary.revision == target_boundary and written is not None
                and written.revoked and written.revision == target_grant_revision
            ):
                receipt = self._set(receipt, "boundary_updated", boundary_revision=boundary.revision, grant_revision=written.revision)
            elif boundary.revision != receipt.expected_boundary_revision or capability.revision != receipt.expected_capability_revision:
                return self._set(receipt, "requires_repair")
            else:
                try:
                    updated = self._boundary.revoke_grant(
                        receipt.project_id, grant_id=receipt.grant_id,
                        expected_revision=receipt.expected_boundary_revision,
                        expected_grant_revision=receipt.expected_grant_revision,  # type: ignore[arg-type]
                    )
                except ProjectBoundaryProfileConflict:
                    return self._set(receipt, "requires_repair")
                written = _find_grant(updated.profile, receipt.grant_id)
                assert written is not None
                receipt = self._set(receipt, "boundary_updated", boundary_revision=updated.profile.revision, grant_revision=written.revision)
        return self._finish_binding(receipt, target_boundary, target_capability)

    def _finish_binding(self, receipt: GrantCommandReceipt, target_boundary: int, target_capability: int) -> GrantCommandReceipt:
        boundary = self._boundary.get(receipt.project_id).profile
        capability = self._capability.get(receipt.project_id).profile
        if boundary.revision != target_boundary:
            return self._set(receipt, "requires_repair")
        if capability.revision == target_capability and (
            capability.boundary_profile_id, capability.boundary_profile_revision
        ) == (boundary.profile_id, boundary.revision):
            return self._set(receipt, "completed", boundary_revision=boundary.revision, capability_revision=capability.revision)
        if capability.revision != receipt.expected_capability_revision:
            return self._set(receipt, "requires_repair")
        try:
            rebound = self._capability.rebind_boundary(
                receipt.project_id, expected_revision=receipt.expected_capability_revision,
                boundary_profile_id=boundary.profile_id,
                boundary_profile_revision=boundary.revision,
            )
        except ProjectCapabilityProfileConflict:
            return self._set(receipt, "requires_repair")
        return self._set(receipt, "completed", boundary_revision=boundary.revision, capability_revision=rebound.profile.revision)

    def _set(self, receipt: GrantCommandReceipt, status: str, **changes: object) -> GrantCommandReceipt:
        values = {**receipt.public(), **changes, "status": status, "updated_at": _now()}
        try:
            self._conn.execute(
                """UPDATE boundary_grant_commands SET status=?, boundary_revision=?,
                capability_revision=?, grant_revision=?, updated_at=? WHERE command_id=?""",
                (status, values["boundary_revision"], values["capability_revision"],
                 values["grant_revision"], values["updated_at"], receipt.command_id),
            )
            self._transition(receipt.command_id, status, str(values["updated_at"]))
            self._conn.commit()
        except sqlite3.OperationalError as error:
            self._conn.rollback()
            raise BoundaryGrantCommandConflict("Boundary grant receipt authority is busy") from error
        updated = self.get(receipt.command_id)
        assert updated is not None
        if status in {"completed", "requires_repair"}:
            self._release(updated)
        return updated

    def _release(self, receipt: GrantCommandReceipt) -> None:
        try:
            self._reservation.release(
                project_id=receipt.project_id, command_id=receipt.command_id,
                command_kind="grant-create" if receipt.kind == "create" else "grant-revoke",
            )
        except ProjectBoundaryMutationReservationConflict as error:
            raise BoundaryGrantCommandConflict(
                "Boundary mutation reservation is busy"
            ) from error

    def _transition(self, command_id: str, status: str, at: str) -> None:
        self._conn.execute(
            "INSERT INTO boundary_grant_command_transitions(command_id,status,at) VALUES(?,?,?)",
            (command_id, status, at),
        )


def resolve_grant_target(
    *, stable_id: str, profile, boundary, capabilities,
    registry_generation: int, expected_registry_generation: int,
    allow_pending_boundary_revision: bool = False,
) -> ResolvedGrantTarget:
    if (
        not isinstance(expected_registry_generation, int)
        or isinstance(expected_registry_generation, bool)
        or expected_registry_generation < 0
        or registry_generation != expected_registry_generation
    ):
        raise BoundaryGrantCommandError("target_unavailable")
    catalog_boundary = boundary
    if (
        allow_pending_boundary_revision
        and profile.boundary_profile_id == boundary.profile_id
        and profile.boundary_profile_revision + 1 == boundary.revision
    ):
        # An active grant Saga may have committed its narrow Boundary write
        # before the Capability binding. The mutation preserves all catalog
        # policy fields, so reconstruct only the previously attested revision
        # for target visibility. The service still verifies the written grant.
        catalog_boundary = replace(
            boundary, revision=profile.boundary_profile_revision,
            persistent_grants=(),
        )
    catalog = ProjectCapabilityCatalogProjector().project(
        profile=profile, boundary=catalog_boundary, capabilities=capabilities,
        registry_generation=registry_generation,
    )
    if not any(
        item.stable_id == stable_id and item.selected and item.state == "available"
        for item in catalog.entries
    ):
        raise BoundaryGrantCommandError("target_unavailable")
    capability = next((item for item in capabilities if item.capability_id == stable_id), None)
    if capability is None:
        raise BoundaryGrantCommandError("target_unavailable")
    tool = tool_from_capability(capability)
    if (
        boundary.mode == "sealed"
        or tool.effect in boundary.denied_effects
        or tool.effect in {"delete", "platform"}
        or tool.destination == "platform"
        or tool.mutability == "irreversible"
        or capability.requires_approval
    ):
        raise BoundaryGrantCommandError("target_unavailable")
    return ResolvedGrantTarget(
        stable_id=tool.tool_id,
        target_id=tool_boundary_target_identity(tool, capability.capability_id),
        effect=tool.effect,
        destination=tool.destination,
        data_classes=tool.data_classes,
        redaction_required=tool.egress_class == "remote",
    )


def _eligible(target: ResolvedGrantTarget, duration: str) -> bool:
    if duration == "until_revoked":
        return target.destination == "local" and target.effect == "read"
    if target.destination in {"mcp", "provider"}:
        return duration in {"24_hours", "30_days"}
    return duration in {"24_hours", "30_days", "90_days"}


def _find_grant(profile, grant_id: str) -> BoundaryGrant | None:
    return next((item for item in profile.persistent_grants if item.grant_id == grant_id), None)


def _require_current_binding(boundary, capability) -> None:
    if (
        capability.boundary_profile_id,
        capability.boundary_profile_revision,
    ) != (boundary.profile_id, boundary.revision):
        raise BoundaryGrantCommandConflict(
            "project Capability and Boundary binding drifted"
        )


def _receipt(row: sqlite3.Row) -> GrantCommandReceipt:
    values = dict(row)
    if values.get("kind") not in {"create", "revoke"} or values.get("status") not in _STATUSES:
        raise BoundaryGrantCommandError("Boundary grant receipt authority is invalid")
    if values.get("actor_id") != "desktop-user":
        raise BoundaryGrantCommandError("Boundary grant receipt authority is invalid")
    return GrantCommandReceipt(**values)


def _match_create(receipt: GrantCommandReceipt, project_id: str, stable_id: str, duration: str, eb: int, ec: int) -> None:
    if (receipt.project_id, receipt.kind, receipt.target_stable_id, receipt.duration, receipt.expected_boundary_revision, receipt.expected_capability_revision) != (project_id, "create", stable_id, duration, eb, ec):
        raise BoundaryGrantCommandConflict("command id has different semantics")


def _match_revoke(receipt: GrantCommandReceipt, project_id: str, grant_id: str, eb: int, ec: int, eg: int) -> None:
    if (receipt.project_id, receipt.kind, receipt.grant_id, receipt.expected_boundary_revision, receipt.expected_capability_revision, receipt.expected_grant_revision) != (project_id, "revoke", grant_id, eb, ec, eg):
        raise BoundaryGrantCommandConflict("command id has different semantics")


def _command(value: str) -> str:
    value = str(value).strip()
    if not _COMMAND_ID.fullmatch(value):
        raise BoundaryGrantCommandError("command identity is invalid")
    return value


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not _COMMAND_ID.fullmatch(value):
        raise BoundaryGrantCommandError(f"{label} is invalid")
    return value


def _revisions(*values: object) -> None:
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in values):
        raise BoundaryGrantCommandError("expected revisions must be positive integers")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
