"""Compatibility facade for the approved MCP server authority.

The immutable record and payload contracts live in
``mcp_approved_server_contracts`` so the SQLite migration authority can
validate snapshots without importing this JSON compatibility reader.
"""
from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from backend.security.mcp_approved_server_contracts import (
    MCPApprovedServer,
    MCPApprovedServerSnapshot,
    MCPApprovedServerStoreError,
    canonical_mcp_approved_server_payload,
    parse_mcp_approved_server_payload,
)

__all__ = [
    "JsonMCPApprovedServerStore",
    "MCPApprovedServer",
    "MCPApprovedServerSnapshot",
    "MCPApprovedServerStoreError",
    "canonical_mcp_approved_server_payload",
    "parse_mcp_approved_server_payload",
]


class JsonMCPApprovedServerStore:
    """Read the legacy JSON bootstrap or delegate to the SQLite authority."""

    def __init__(self, root_dir: Path) -> None:
        self._security = Path(root_dir) / ".rebuild-data" / "security"

    def snapshot(self) -> MCPApprovedServerSnapshot:
        unified = self._security / "mcp-approved-servers.json"
        legacy = self._security / "mcp-approved-stdio.json"
        # Once the durable authority exists, JSON is deliberately only a
        # bootstrap artifact. Keep this compatibility facade so current
        # composition cannot accidentally create a second active authority.
        from backend.security.mcp_approved_server_migration import (
            MCPApprovedServerMigrationAuthority,
            MCPApprovedServerMigrationError,
        )

        if MCPApprovedServerMigrationAuthority.database_exists(self._security):
            try:
                return MCPApprovedServerMigrationAuthority(
                    self._security.parent.parent
                ).active_snapshot()
            except MCPApprovedServerMigrationError:
                # Existing composition intentionally catches StoreError and
                # projects it as authority_invalid. The SQLite authority is
                # internal; it must not create a second error contract.
                raise MCPApprovedServerStoreError(
                    "MCP approved server authority is invalid"
                ) from None
        if unified.exists() and legacy.exists():
            raise MCPApprovedServerStoreError(
                "MCP approved authority migration is incomplete"
            )
        self._path = unified if unified.exists() else legacy
        if not self._path.is_file():
            return MCPApprovedServerSnapshot(MappingProxyType({}))
        try:
            return parse_mcp_approved_server_payload(self._path.read_bytes())
        except MCPApprovedServerStoreError:
            raise
        except Exception:
            raise MCPApprovedServerStoreError(
                "MCP approved server authority is invalid"
            ) from None

    def load(self) -> MCPApprovedServerSnapshot:
        return self.snapshot()
