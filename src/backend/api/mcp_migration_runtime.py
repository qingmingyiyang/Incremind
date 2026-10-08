from __future__ import annotations

from pathlib import Path
from typing import Protocol

from backend.security.mcp_approved_server_migration import (
    MCPApprovedServerMigrationAuthority,
    MCPApprovedServerMigrationConflict,
    MCPApprovedServerMigrationError,
    MCPApprovedServerMigrationStatus,
)


class MCPMigrationRuntimeError(RuntimeError):
    """A safe orchestration failure that retains the durable switching fence."""


class MCPMigrationConnectionManager(Protocol):
    def revoke_for_migration(self, server_ids: tuple[str, ...]) -> object: ...

    def validate_migration_revocation(
        self, receipt: object, server_ids: tuple[str, ...],
    ) -> bool: ...

    def release_migration_block(self, server_ids: tuple[str, ...]) -> None: ...


class MCPApprovedServerMigrationRuntime:
    """Orders durable pointer changes around real MCP runtime revocation."""

    def __init__(
        self,
        root_dir: Path,
        *,
        manager: MCPMigrationConnectionManager | None,
    ) -> None:
        self._authority = MCPApprovedServerMigrationAuthority(root_dir)
        self._manager = manager
        self._startup_recovery = False

    def cutover(
        self, *, migration_id: str, expected_revision: int, command_id: str,
    ) -> MCPApprovedServerMigrationStatus:
        command_id = _outer_command_id(command_id)
        completed = self._authority.terminal_operation_receipt(
            migration_id=migration_id, expected_revision=expected_revision,
            command_id=command_id, operation="cutover",
        )
        if completed is not None:
            return completed
        current = self._required_status(migration_id)
        if current.state == "cutover_committed":
            # The pointer commit may have survived while the outer receipt did
            # not.  Authenticate the complete subcommand chain and seal the
            # terminal result without touching the current target runtime.
            return self._authority.terminal_operation_receipt(
                migration_id=migration_id, expected_revision=expected_revision,
                command_id=command_id, operation="cutover", record=True,
            ) or current
        # Re-enter through the first durable command even after a lost terminal
        # response.  The authority returns its historical exact result only for
        # the same command/operation/revision tuple; a stale different command
        # remains a conflict.
        status = self._authority.begin_cutover(
            migration_id=migration_id,
            expected_revision=expected_revision,
            command_id=f"{command_id}.begin",
        )
        result = self._finish_cutover(status, command_id)
        return self._authority.terminal_operation_receipt(
            migration_id=migration_id, expected_revision=expected_revision,
            command_id=command_id, operation="cutover", record=True,
        ) or result

    def _finish_cutover(
        self, status: MCPApprovedServerMigrationStatus, command_id: str,
    ) -> MCPApprovedServerMigrationStatus:
        if status.state == "cutover_started":
            receipt = self._revoke(status.affected_server_ids)
            self._require_receipt(receipt, status.affected_server_ids)
            status = self._authority.mark_old_revoked(
                migration_id=status.migration_id,
                expected_revision=status.revision,
                command_id=f"{command_id}.revoked",
            )
        if status.state == "old_revoked":
            status = self._authority.commit_cutover(
                migration_id=status.migration_id,
                expected_revision=status.revision,
                command_id=f"{command_id}.commit",
            )
            self._release(status.affected_server_ids)
        if status.state != "cutover_committed":
            raise MCPApprovedServerMigrationConflict("migration is not ready for cutover")
        return status

    def rollback(
        self, *, migration_id: str, expected_revision: int, command_id: str,
    ) -> MCPApprovedServerMigrationStatus:
        command_id = _outer_command_id(command_id)
        completed = self._authority.terminal_operation_receipt(
            migration_id=migration_id, expected_revision=expected_revision,
            command_id=command_id, operation="rollback",
        )
        if completed is not None:
            return completed
        current = self._required_status(migration_id)
        if current.state == "rolled_back":
            return self._authority.terminal_operation_receipt(
                migration_id=migration_id, expected_revision=expected_revision,
                command_id=command_id, operation="rollback", record=True,
            ) or current
        status = self._authority.begin_rollback(
            migration_id=migration_id,
            expected_revision=expected_revision,
            command_id=f"{command_id}.begin",
        )
        result = self._finish_rollback(status, command_id)
        return self._authority.terminal_operation_receipt(
            migration_id=migration_id, expected_revision=expected_revision,
            command_id=command_id, operation="rollback", record=True,
        ) or result

    def _finish_rollback(
        self, status: MCPApprovedServerMigrationStatus, command_id: str,
    ) -> MCPApprovedServerMigrationStatus:
        if status.state == "rollback_started":
            receipt = self._revoke(status.affected_server_ids)
            self._require_receipt(receipt, status.affected_server_ids)
            status = self._authority.mark_new_revoked(
                migration_id=status.migration_id,
                expected_revision=status.revision,
                command_id=f"{command_id}.revoked",
            )
        if status.state == "new_revoked":
            status = self._authority.commit_rollback(
                migration_id=status.migration_id,
                expected_revision=status.revision,
                command_id=f"{command_id}.commit",
            )
            self._release(status.affected_server_ids)
        if status.state != "rolled_back":
            raise MCPApprovedServerMigrationConflict("migration is not ready for rollback")
        return status

    def recover_interrupted_switch(self) -> MCPApprovedServerMigrationStatus | None:
        """Finish an interrupted switch before a new MCP manager is provisioned.

        At process startup no prior in-process connection or registry lease can
        survive.  The absent manager is therefore the revocation proof; the
        active pointer is advanced only through the same ordered durable states.
        """
        if self._manager is not None:
            raise MCPMigrationRuntimeError("startup recovery requires an absent MCP runtime")
        status = self._authority.blocking_migration()
        if status is None:
            return None
        command = f"startup-recovery-{status.migration_id}"
        self._startup_recovery = True
        try:
            if status.state in {"cutover_started", "old_revoked"}:
                return self._finish_cutover(status, command)
            return self._finish_rollback(status, command)
        finally:
            self._startup_recovery = False

    def _required_status(self, migration_id: str) -> MCPApprovedServerMigrationStatus:
        status = self._authority.status(migration_id)
        if status is None:
            raise MCPApprovedServerMigrationConflict("MCP approved migration was not found")
        return status

    def _revoke(self, server_ids: tuple[str, ...]) -> object | None:
        if self._manager is None:
            if self._startup_recovery:
                return None
            raise MCPMigrationRuntimeError("MCP runtime manager is unavailable")
        try:
            return self._manager.revoke_for_migration(server_ids)
        except Exception as error:
            raise MCPMigrationRuntimeError("MCP runtime revocation did not complete") from error

    def _require_receipt(self, receipt: object | None, server_ids: tuple[str, ...]) -> None:
        if self._manager is None:
            if self._startup_recovery:
                return
            raise MCPMigrationRuntimeError("MCP runtime revocation proof is unavailable")
        if receipt is None or not self._manager.validate_migration_revocation(receipt, server_ids):
            raise MCPMigrationRuntimeError("MCP runtime revocation proof is invalid")

    def _release(self, server_ids: tuple[str, ...]) -> None:
        if self._manager is None:
            return
        try:
            self._manager.release_migration_block(server_ids)
        except Exception as error:
            # Pointer commit is authoritative and durable.  Keeping the manager
            # blocked is fail-closed; a later reconcile/restart completes it.
            raise MCPMigrationRuntimeError("MCP migration committed but runtime release failed") from error


def _outer_command_id(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > 120:
        raise MCPApprovedServerMigrationError("migration command identity is invalid")
    return value
