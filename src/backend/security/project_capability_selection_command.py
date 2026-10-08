"""Durable project Tool selection commands."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from threading import Lock

from backend.security.project_boundary_mutation_reservation import (
    ProjectBoundaryMutationReservation,
    ProjectBoundaryMutationReservationConflict,
)
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import (
    ProjectCapabilityProfileConflict,
    ProjectCapabilityProfileStore,
    ProjectCapabilityProfileStoreError,
)
from backend.shared.interprocess_lock import interprocess_file_lock
from core.ai_tooling import (
    ProjectCapabilityCatalogProjector,
    ToolSelectionBinding,
    tool_boundary_target_identity,
    tool_contract_binding_identity,
    tool_from_capability,
)

_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ACTIVE = {"prepared", "capability_updated"}
_STATUSES = _ACTIVE | {"completed", "requires_repair"}
_ACTIONS = {"exclude", "select", "reset_exclusion"}
_LOCK = Lock()


class CapabilitySelectionCommandError(ValueError):
    pass


class CapabilitySelectionCommandConflict(CapabilitySelectionCommandError):
    pass


@dataclass(frozen=True, slots=True)
class CapabilitySelectionTarget:
    stable_id: str
    contract_identity: str

    def __post_init__(self) -> None:
        ToolSelectionBinding(self.stable_id, self.contract_identity)


@dataclass(frozen=True, slots=True)
class CapabilitySelectionCommandReceipt:
    command_id: str
    project_id: str
    action: str
    target_stable_id: str
    target_contract_identity: str | None
    status: str
    expected_boundary_revision: int
    expected_capability_revision: int
    expected_registry_generation: int
    capability_revision: int | None
    actor_id: str
    created_at: str
    updated_at: str

    def public(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "project_id": self.project_id,
            "action": self.action,
            "target_stable_id": self.target_stable_id,
            "status": self.status,
            "expected_boundary_revision": self.expected_boundary_revision,
            "expected_capability_revision": self.expected_capability_revision,
            "expected_registry_generation": self.expected_registry_generation,
            "capability_revision": self.capability_revision,
            "actor_id": self.actor_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ProjectCapabilitySelectionCommandService:
    def __init__(self, root_dir: Path) -> None:
        root = Path(root_dir)
        path = root / ".rebuild-data" / "capability-selection-commands.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with interprocess_file_lock(path):
                initialize_journal = not path.exists()
                self._conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA busy_timeout=5000")
                if initialize_journal:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("""CREATE TABLE IF NOT EXISTS capability_selection_commands (
                    command_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    action TEXT NOT NULL, target_stable_id TEXT NOT NULL,
                    target_contract_identity TEXT,
                    status TEXT NOT NULL, expected_boundary_revision INTEGER NOT NULL,
                    expected_capability_revision INTEGER NOT NULL,
                    expected_registry_generation INTEGER NOT NULL,
                    capability_revision INTEGER, actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )""")
                columns = {
                    row[1] for row in self._conn.execute(
                        "PRAGMA table_info(capability_selection_commands)"
                    )
                }
                if "target_contract_identity" not in columns:
                    self._conn.execute(
                        "ALTER TABLE capability_selection_commands "
                        "ADD COLUMN target_contract_identity TEXT"
                    )
                self._conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS
                    one_active_capability_selection_command_per_project
                    ON capability_selection_commands(project_id)
                    WHERE status IN ('prepared','capability_updated')""")
                self._conn.commit()
        except (sqlite3.OperationalError, TimeoutError) as error:
            if hasattr(self, "_conn"):
                self._conn.close()
            raise CapabilitySelectionCommandConflict(
                "Capability selection receipt authority is busy"
            ) from error
        self._boundary = ProjectBoundaryProfileStore(root)
        self._capability = ProjectCapabilityProfileStore(root)
        self._reservation = ProjectBoundaryMutationReservation(root)

    def close(self) -> None:
        self._conn.close()

    def replay(
        self, *, project_id: str, command_id: str, target_stable_id: str,
        expected_boundary_revision: int, expected_capability_revision: int,
        expected_registry_generation: int, action: str = "exclude",
    ) -> CapabilitySelectionCommandReceipt | None:
        command_id = _command(command_id)
        _revisions(expected_boundary_revision, expected_capability_revision)
        _generation(expected_registry_generation)
        receipt = self.get(command_id)
        if receipt is None:
            return None
        _match(
            receipt, project_id, action, target_stable_id,
            expected_boundary_revision, expected_capability_revision,
            expected_registry_generation,
        )
        if receipt.status in {"completed", "requires_repair"}:
            self._release(receipt)
        return receipt

    def exclude(
        self, *, project_id: str, command_id: str, target_stable_id: str,
        expected_boundary_revision: int, expected_capability_revision: int,
        expected_registry_generation: int,
    ) -> CapabilitySelectionCommandReceipt:
        command_id = _command(command_id)
        _revisions(expected_boundary_revision, expected_capability_revision)
        _generation(expected_registry_generation)
        existing = self.replay(
            project_id=project_id, command_id=command_id,
            target_stable_id=target_stable_id,
            expected_boundary_revision=expected_boundary_revision,
            expected_capability_revision=expected_capability_revision,
            expected_registry_generation=expected_registry_generation,
            action="exclude",
        )
        if existing is not None and existing.status in {"completed", "requires_repair"}:
            return existing
        semantic = (
            f"exclude|{target_stable_id}|{expected_boundary_revision}|"
            f"{expected_capability_revision}|{expected_registry_generation}"
        )
        try:
            execution = self._reservation.execution(project_id)
            with _LOCK, execution:
                self._reservation.reserve(
                    project_id=project_id, command_id=command_id,
                    command_kind="capability-exclude", semantic=semantic,
                )
                receipt = existing or self._prepare(
                    project_id=project_id, command_id=command_id,
                    action="exclude", target_contract_identity=None,
                    target_stable_id=target_stable_id,
                    expected_boundary_revision=expected_boundary_revision,
                    expected_capability_revision=expected_capability_revision,
                    expected_registry_generation=expected_registry_generation,
                )
                _match(
                    receipt, project_id, "exclude", target_stable_id,
                    expected_boundary_revision, expected_capability_revision,
                    expected_registry_generation,
                )
                return self._advance(receipt)
        except ProjectBoundaryMutationReservationConflict as error:
            raise CapabilitySelectionCommandConflict(
                "another project governance mutation is active"
            ) from error
        except Exception:
            if self.get(command_id) is None:
                self._reservation.release(
                    project_id=project_id, command_id=command_id,
                    command_kind="capability-exclude",
                )
            raise

    def select(
        self, *, project_id: str, command_id: str,
        action: str, target: CapabilitySelectionTarget,
        expected_boundary_revision: int, expected_capability_revision: int,
        expected_registry_generation: int,
    ) -> CapabilitySelectionCommandReceipt:
        if action not in {"select", "reset_exclusion"}:
            raise CapabilitySelectionCommandError("selection action is unsupported")
        command_id = _command(command_id)
        _revisions(expected_boundary_revision, expected_capability_revision)
        _generation(expected_registry_generation)
        existing = self.replay(
            project_id=project_id, command_id=command_id, action=action,
            target_stable_id=target.stable_id,
            expected_boundary_revision=expected_boundary_revision,
            expected_capability_revision=expected_capability_revision,
            expected_registry_generation=expected_registry_generation,
        )
        if existing is not None and existing.status in {"completed", "requires_repair"}:
            return existing
        semantic = _semantic(
            action, target.stable_id, target.contract_identity,
            expected_boundary_revision, expected_capability_revision,
            expected_registry_generation,
        )
        try:
            execution = self._reservation.execution(project_id)
            with _LOCK, execution:
                self._reservation.reserve(
                    project_id=project_id, command_id=command_id,
                    command_kind="capability-selection", semantic=semantic,
                )
                receipt = existing or self._prepare(
                    project_id=project_id, command_id=command_id, action=action,
                    target_stable_id=target.stable_id,
                    target_contract_identity=target.contract_identity,
                    expected_boundary_revision=expected_boundary_revision,
                    expected_capability_revision=expected_capability_revision,
                    expected_registry_generation=expected_registry_generation,
                )
                _match(
                    receipt, project_id, action, target.stable_id,
                    expected_boundary_revision, expected_capability_revision,
                    expected_registry_generation,
                )
                if receipt.target_contract_identity != target.contract_identity:
                    raise CapabilitySelectionCommandConflict(
                        "command target contract has different semantics"
                    )
                return self._advance(receipt)
        except ProjectBoundaryMutationReservationConflict as error:
            raise CapabilitySelectionCommandConflict(
                "another project governance mutation is active"
            ) from error
        except Exception:
            if self.get(command_id) is None:
                self._reservation.release(
                    project_id=project_id, command_id=command_id,
                    command_kind="capability-selection",
                )
            raise

    def require_repair(
        self, receipt: CapabilitySelectionCommandReceipt,
    ) -> CapabilitySelectionCommandReceipt:
        if receipt.status not in _ACTIVE:
            return receipt
        semantic = _receipt_semantic(receipt)
        with _LOCK, self._reservation.execution(receipt.project_id):
            self._reservation.reserve(
                project_id=receipt.project_id, command_id=receipt.command_id,
                command_kind=_command_kind(receipt.action), semantic=semantic,
            )
            current = self.get(receipt.command_id)
            if current != receipt:
                raise CapabilitySelectionCommandConflict(
                    "Capability selection receipt drifted"
                )
            return self._set(receipt, "requires_repair")

    def complete_if_profile_written(
        self, receipt: CapabilitySelectionCommandReceipt,
    ) -> CapabilitySelectionCommandReceipt | None:
        """Repair the receipt-only crash window without consulting Runtime."""
        if receipt.status not in _ACTIVE:
            return receipt
        semantic = _receipt_semantic(receipt)
        with _LOCK, self._reservation.execution(receipt.project_id):
            self._reservation.reserve(
                project_id=receipt.project_id, command_id=receipt.command_id,
                command_kind=_command_kind(receipt.action), semantic=semantic,
            )
            current = self.get(receipt.command_id)
            if current != receipt:
                raise CapabilitySelectionCommandConflict(
                    "Capability selection receipt drifted"
                )
            boundary = self._boundary.get(receipt.project_id).profile
            capability = self._capability.get(receipt.project_id).profile
            if not _profile_has_result(receipt, boundary, capability):
                return None
            if current.status == "prepared":
                current = self._set(
                    current, "capability_updated",
                    capability_revision=capability.revision,
                )
            return self._set(
                current, "completed", capability_revision=capability.revision,
            )

    def get(self, command_id: str) -> CapabilitySelectionCommandReceipt | None:
        try:
            row = self._conn.execute(
                "SELECT * FROM capability_selection_commands WHERE command_id=?",
                (_command(command_id),),
            ).fetchone()
        except sqlite3.OperationalError as error:
            raise CapabilitySelectionCommandConflict(
                "Capability selection receipt authority is busy"
            ) from error
        return _receipt(row) if row is not None else None

    def _prepare(self, **values: object) -> CapabilitySelectionCommandReceipt:
        now = _now()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute("""INSERT INTO capability_selection_commands (
                command_id, project_id, action, target_stable_id,
                target_contract_identity, status,
                expected_boundary_revision, expected_capability_revision,
                expected_registry_generation, capability_revision, actor_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'prepared', ?, ?, ?, NULL, 'desktop-user', ?, ?)""", (
                values["command_id"], values["project_id"], values["action"],
                values["target_stable_id"], values["target_contract_identity"],
                values["expected_boundary_revision"], values["expected_capability_revision"],
                values["expected_registry_generation"], now, now,
            ))
            self._conn.commit()
        except sqlite3.IntegrityError as error:
            self._conn.rollback()
            concurrent = self.get(str(values["command_id"]))
            if concurrent is not None:
                return concurrent
            raise CapabilitySelectionCommandConflict(
                "another Capability selection command is active"
            ) from error
        except sqlite3.OperationalError as error:
            self._conn.rollback()
            raise CapabilitySelectionCommandConflict(
                "Capability selection receipt authority is busy"
            ) from error
        receipt = self.get(str(values["command_id"]))
        assert receipt is not None
        return receipt

    def _advance(
        self, receipt: CapabilitySelectionCommandReceipt,
    ) -> CapabilitySelectionCommandReceipt:
        if receipt.status in {"completed", "requires_repair"}:
            return receipt
        boundary = self._boundary.get(receipt.project_id).profile
        capability = self._capability.get(receipt.project_id).profile
        target_revision = receipt.expected_capability_revision + 1
        written = (
            capability.revision == target_revision
            and _profile_selection_matches(receipt, capability)
            and capability.boundary_profile_id == boundary.profile_id
            and capability.boundary_profile_revision == boundary.revision
        )
        if receipt.status == "prepared":
            if written:
                receipt = self._set(
                    receipt, "capability_updated",
                    capability_revision=capability.revision,
                )
            elif (
                boundary.revision != receipt.expected_boundary_revision
                or capability.revision != receipt.expected_capability_revision
                or capability.boundary_profile_id != boundary.profile_id
                or capability.boundary_profile_revision != boundary.revision
            ):
                return self._set(receipt, "requires_repair")
            else:
                try:
                    if receipt.action == "exclude":
                        updated = self._capability.exclude_tool(
                            receipt.project_id, tool_id=receipt.target_stable_id,
                            expected_revision=receipt.expected_capability_revision,
                        )
                    else:
                        if receipt.target_contract_identity is None:
                            return self._set(receipt, "requires_repair")
                        updated = self._capability.select_tool(
                            receipt.project_id,
                            tool_binding=ToolSelectionBinding(
                                receipt.target_stable_id,
                                receipt.target_contract_identity,
                            ),
                            expected_revision=receipt.expected_capability_revision,
                            require_existing_exclusion=(
                                receipt.action == "reset_exclusion"
                            ),
                        )
                except (
                    ProjectCapabilityProfileConflict,
                    ProjectCapabilityProfileStoreError,
                ):
                    return self._set(receipt, "requires_repair")
                receipt = self._set(
                    receipt, "capability_updated",
                    capability_revision=updated.profile.revision,
                )
        capability = self._capability.get(receipt.project_id).profile
        if not (
            capability.revision == target_revision
            and _profile_selection_matches(receipt, capability)
        ):
            return self._set(receipt, "requires_repair")
        return self._set(
            receipt, "completed", capability_revision=capability.revision,
        )

    def _set(
        self, receipt: CapabilitySelectionCommandReceipt, status: str,
        *, capability_revision: int | None = None,
    ) -> CapabilitySelectionCommandReceipt:
        revision = receipt.capability_revision if capability_revision is None else capability_revision
        now = _now()
        try:
            self._conn.execute(
                """UPDATE capability_selection_commands SET status=?,
                capability_revision=?, updated_at=? WHERE command_id=?""",
                (status, revision, now, receipt.command_id),
            )
            self._conn.commit()
        except sqlite3.OperationalError as error:
            self._conn.rollback()
            raise CapabilitySelectionCommandConflict(
                "Capability selection receipt authority is busy"
            ) from error
        updated = self.get(receipt.command_id)
        assert updated is not None
        if status in {"completed", "requires_repair"}:
            self._release(updated)
        return updated

    def _release(self, receipt: CapabilitySelectionCommandReceipt) -> None:
        self._reservation.release(
            project_id=receipt.project_id, command_id=receipt.command_id,
            command_kind=_command_kind(receipt.action),
        )


def resolve_exclusion_target(
    *, stable_id: str, profile, boundary, capabilities,
    registry_generation: int, expected_registry_generation: int,
) -> str:
    _generation(expected_registry_generation)
    if registry_generation != expected_registry_generation:
        raise CapabilitySelectionCommandError("target_unavailable")
    catalog = ProjectCapabilityCatalogProjector().project(
        profile=profile, boundary=boundary, capabilities=capabilities,
        registry_generation=registry_generation,
    )
    if not any(
        item.stable_id == stable_id and item.selected and item.state == "available"
        for item in catalog.entries
    ):
        raise CapabilitySelectionCommandError("target_unavailable")
    return stable_id


def resolve_selection_target(
    *, action: str, stable_id: str, profile, boundary, capabilities,
    registry_generation: int, expected_registry_generation: int,
) -> CapabilitySelectionTarget:
    """Resolve one expansion target without enabling its container authority."""
    _generation(expected_registry_generation)
    if action not in {"select", "reset_exclusion"}:
        raise CapabilitySelectionCommandError("target_unavailable")
    if registry_generation != expected_registry_generation:
        raise CapabilitySelectionCommandError("target_unavailable")
    if (
        profile.project_id != boundary.project_id
        or profile.boundary_profile_id != boundary.profile_id
        or profile.boundary_profile_revision != boundary.revision
    ):
        raise CapabilitySelectionCommandError("target_unavailable")
    capability = next(
        (item for item in capabilities if item.capability_id == stable_id), None,
    )
    if capability is None:
        raise CapabilitySelectionCommandError("target_unavailable")
    try:
        tool = tool_from_capability(capability)
    except (TypeError, ValueError) as error:
        raise CapabilitySelectionCommandError("target_unavailable") from error
    if (
        not tool.available
        or tool.source not in profile.enabled_sources
        or (
            tool.source == "plugin"
            and tool.owner_id not in profile.enabled_plugin_ids
        )
        or (
            tool.source == "mcp"
            and tool.owner_id not in profile.enabled_mcp_server_ids
        )
    ):
        raise CapabilitySelectionCommandError("target_unavailable")
    if action == "reset_exclusion":
        if stable_id not in profile.denied_tool_ids:
            raise CapabilitySelectionCommandError("target_unavailable")
    else:
        catalog = ProjectCapabilityCatalogProjector().project(
            profile=profile, boundary=boundary, capabilities=capabilities,
            registry_generation=registry_generation,
        )
        if any(item.stable_id == stable_id for item in catalog.entries):
            raise CapabilitySelectionCommandError("target_unavailable")
    denied = tuple(item for item in profile.denied_tool_ids if item != stable_id)
    allowed = profile.allowed_tool_ids
    if (
        profile.tool_discovery_policy != "auto_discover" or allowed
    ) and stable_id not in allowed:
        allowed = (*allowed, stable_id)
    bindings = tuple(
        item for item in profile.tool_selection_bindings
        if item.stable_id != stable_id
    ) + (ToolSelectionBinding(stable_id, tool_contract_binding_identity(tool)),)
    candidate = replace(
        profile, allowed_tool_ids=allowed, denied_tool_ids=denied,
        tool_selection_bindings=bindings,
    )
    candidate_catalog = ProjectCapabilityCatalogProjector().project(
        profile=candidate, boundary=boundary, capabilities=capabilities,
        registry_generation=registry_generation,
    )
    if (
        tool.effect in boundary.denied_effects
        or not any(
            item.stable_id == stable_id
            and item.selected and item.state == "available"
            for item in candidate_catalog.entries
        )
    ):
        raise CapabilitySelectionCommandError("target_unavailable")
    boundary_target = tool_boundary_target_identity(tool, capability.capability_id)
    now = datetime.now(timezone.utc)
    if any(
        grant.target_id == boundary_target and grant.is_active_at(now)
        for grant in boundary.persistent_grants
    ):
        raise CapabilitySelectionCommandError("target_unavailable")
    return CapabilitySelectionTarget(
        stable_id=tool.tool_id,
        contract_identity=tool_contract_binding_identity(tool),
    )


def _profile_has_result(receipt, boundary, capability) -> bool:
    return (
        capability.revision == receipt.expected_capability_revision + 1
        and _profile_selection_matches(receipt, capability)
        and capability.boundary_profile_id == boundary.profile_id
        and capability.boundary_profile_revision == boundary.revision
    )


def _profile_selection_matches(receipt, capability) -> bool:
    if receipt.action == "exclude":
        return (
            receipt.target_stable_id in capability.denied_tool_ids
            and receipt.target_stable_id not in capability.allowed_tool_ids
        )
    binding = next(
        (
            item.contract_identity for item in capability.tool_selection_bindings
            if item.stable_id == receipt.target_stable_id
        ),
        None,
    )
    selected_by_policy = (
        receipt.target_stable_id in capability.allowed_tool_ids
        or (
            capability.tool_discovery_policy == "auto_discover"
            and not capability.allowed_tool_ids
        )
    )
    return (
        receipt.action in {"select", "reset_exclusion"}
        and receipt.target_stable_id not in capability.denied_tool_ids
        and binding == receipt.target_contract_identity
        and selected_by_policy
    )


def _receipt(row: sqlite3.Row) -> CapabilitySelectionCommandReceipt:
    values = dict(row)
    if (
        values.get("action") not in _ACTIONS
        or values.get("status") not in _STATUSES
        or values.get("actor_id") != "desktop-user"
    ):
        raise CapabilitySelectionCommandError(
            "Capability selection receipt authority is invalid"
        )
    receipt = CapabilitySelectionCommandReceipt(**values)
    if receipt.action != "exclude":
        try:
            ToolSelectionBinding(
                receipt.target_stable_id,
                receipt.target_contract_identity or "",
            )
        except ValueError as error:
            raise CapabilitySelectionCommandError(
                "Capability selection receipt authority is invalid"
            ) from error
    return receipt


def _match(
    receipt: CapabilitySelectionCommandReceipt, project_id: str,
    action: str, target_stable_id: str, boundary_revision: int,
    capability_revision: int, registry_generation: int,
) -> None:
    if (
        receipt.project_id, receipt.action, receipt.target_stable_id,
        receipt.expected_boundary_revision, receipt.expected_capability_revision,
        receipt.expected_registry_generation,
    ) != (
        project_id, action, target_stable_id, boundary_revision,
        capability_revision, registry_generation,
    ):
        raise CapabilitySelectionCommandConflict(
            "command id has different semantics"
        )


def _semantic(
    action: str, stable_id: str, contract_identity: str | None,
    boundary_revision: int, capability_revision: int, registry_generation: int,
) -> str:
    if action == "exclude":
        return (
            f"exclude|{stable_id}|{boundary_revision}|"
            f"{capability_revision}|{registry_generation}"
        )
    return (
        f"{action}|{stable_id}|{contract_identity}|{boundary_revision}|"
        f"{capability_revision}|{registry_generation}"
    )


def _receipt_semantic(receipt: CapabilitySelectionCommandReceipt) -> str:
    return _semantic(
        receipt.action, receipt.target_stable_id,
        receipt.target_contract_identity,
        receipt.expected_boundary_revision,
        receipt.expected_capability_revision,
        receipt.expected_registry_generation,
    )


def _command_kind(action: str) -> str:
    return "capability-exclude" if action == "exclude" else "capability-selection"


def _command(value: str) -> str:
    value = str(value).strip()
    if not _COMMAND_ID.fullmatch(value):
        raise CapabilitySelectionCommandError("command identity is invalid")
    return value


def _revisions(*values: object) -> None:
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 1
        for value in values
    ):
        raise CapabilitySelectionCommandError(
            "expected revisions must be positive integers"
        )


def _generation(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CapabilitySelectionCommandError(
            "expected registry generation must be non-negative"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
