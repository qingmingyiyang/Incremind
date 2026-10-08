from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.mcp_migration_runtime import (
    MCPApprovedServerMigrationRuntime,
    MCPMigrationRuntimeError,
)
from backend.security.mcp_approved_server_migration import (
    MCPApprovedServerMigrationAuthority,
    MCPApprovedServerMigrationError,
)
from backend.api.routes.mcp_migrations import router as migration_router


class _Manager:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid
        self.blocked: tuple[str, ...] = ()
        self.released: list[tuple[str, ...]] = []
        self.receipt = object()

    def revoke_for_migration(self, server_ids: tuple[str, ...]) -> object:
        self.blocked = server_ids
        return self.receipt

    def validate_migration_revocation(
        self, receipt: object, server_ids: tuple[str, ...],
    ) -> bool:
        return self.valid and receipt is self.receipt and server_ids == self.blocked

    def release_migration_block(self, server_ids: tuple[str, ...]) -> None:
        assert server_ids == self.blocked
        self.released.append(server_ids)
        self.blocked = ()


def test_cutover_and_rollback_require_runtime_revocation_before_pointer_change(
    tmp_path: Path,
) -> None:
    _write_legacy(tmp_path, _candidate("calendar-v1", 1))
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    preview = authority.preview(candidate=_candidate("calendar-v2", 2), command_id="preview")
    confirmed = authority.confirm(
        migration_id=preview.migration_id, expected_revision=preview.revision,
        command_id="confirm", confirmed=True,
    )
    manager = _Manager()
    runtime = MCPApprovedServerMigrationRuntime(tmp_path, manager=manager)

    committed = runtime.cutover(
        migration_id=preview.migration_id, expected_revision=confirmed.revision,
        command_id="cutover",
    )
    assert committed.state == "cutover_committed"
    assert manager.released == [("calendar-server",)]
    assert authority.active_snapshot().servers[0].host_connection.endpoint_identity == "calendar-v2"
    _delete_outer_receipt(tmp_path, "cutover")
    assert runtime.cutover(
        migration_id=preview.migration_id, expected_revision=confirmed.revision,
        command_id="cutover",
    ) == committed

    rolled_back = runtime.rollback(
        migration_id=preview.migration_id, expected_revision=committed.revision,
        command_id="rollback",
    )
    assert rolled_back.state == "rolled_back"
    assert manager.released == [("calendar-server",), ("calendar-server",)]
    assert authority.active_snapshot().servers[0].host_connection.endpoint_identity == "calendar-v1"
    _delete_outer_receipt(tmp_path, "rollback")
    assert runtime.rollback(
        migration_id=preview.migration_id, expected_revision=committed.revision,
        command_id="rollback",
    ) == rolled_back
    assert manager.released == [("calendar-server",), ("calendar-server",)]


def test_invalid_revocation_receipt_leaves_pointer_and_switch_fenced(tmp_path: Path) -> None:
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    preview = authority.preview(candidate=_candidate("calendar-v2", 2), command_id="preview")
    confirmed = authority.confirm(
        migration_id=preview.migration_id, expected_revision=preview.revision,
        command_id="confirm", confirmed=True,
    )
    with pytest.raises(MCPMigrationRuntimeError, match="proof is invalid"):
        MCPApprovedServerMigrationRuntime(tmp_path, manager=_Manager(valid=False)).cutover(
            migration_id=preview.migration_id, expected_revision=confirmed.revision,
            command_id="cutover",
        )
    status = authority.status(preview.migration_id)
    assert status is not None and status.state == "cutover_started"
    assert status.active_snapshot_revision == 1
    recovered = MCPApprovedServerMigrationRuntime(
        tmp_path, manager=_Manager(),
    ).cutover(
        migration_id=preview.migration_id, expected_revision=confirmed.revision,
        command_id="cutover",
    )
    assert recovered.state == "cutover_committed"


def test_outer_command_reserves_space_for_durable_subcommand_ids(tmp_path: Path) -> None:
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    preview = authority.preview(candidate=_candidate("calendar-v2", 2), command_id="preview")
    confirmed = authority.confirm(
        migration_id=preview.migration_id, expected_revision=preview.revision,
        command_id="confirm", confirmed=True,
    )
    with pytest.raises(MCPApprovedServerMigrationError, match="command identity is invalid"):
        MCPApprovedServerMigrationRuntime(tmp_path, manager=_Manager()).cutover(
            migration_id=preview.migration_id,
            expected_revision=confirmed.revision,
            command_id="x" * 121,
        )
    assert authority.status(preview.migration_id) == confirmed


def test_startup_recovery_finishes_interrupted_cutover_without_old_process(
    tmp_path: Path,
) -> None:
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    preview = authority.preview(candidate=_candidate("calendar-v2", 2), command_id="preview")
    confirmed = authority.confirm(
        migration_id=preview.migration_id, expected_revision=preview.revision,
        command_id="confirm", confirmed=True,
    )
    authority.begin_cutover(
        migration_id=preview.migration_id, expected_revision=confirmed.revision,
        command_id="begin-before-crash",
    )
    recovered = MCPApprovedServerMigrationRuntime(
        tmp_path, manager=None,
    ).recover_interrupted_switch()
    assert recovered is not None and recovered.state == "cutover_committed"
    assert authority.blocking_migration() is None


def test_local_api_never_echoes_candidate_and_rebinds_from_active_authority(
    tmp_path: Path,
) -> None:
    _write_legacy(tmp_path, _candidate("calendar-v1", 1))
    app = FastAPI()
    app.include_router(migration_router)
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.state.ai_mcp_connection_manager = _Manager()
    client = TestClient(app)

    candidate = _candidate("calendar-v2", 2)
    preview_response = client.post(
        "/api/ai/governance/mcp/migrations/preview",
        json={"candidate": candidate, "command_id": "api-preview"},
    )
    assert preview_response.status_code == 200
    assert preview_response.headers["cache-control"] == "no-store"
    for private_value in (
        "calendar-v2", str(Path(sys.executable).resolve()),
        "calendar.search", "crp://input", "calendar-user",
    ):
        assert private_value not in preview_response.text
    preview = preview_response.json()
    confirmed = client.post(
        f"/api/ai/governance/mcp/migrations/{preview['migration_id']}/confirm",
        json={
            "expected_revision": preview["revision"],
            "command_id": "api-confirm", "confirmed": True,
        },
    ).json()
    committed_response = client.post(
        f"/api/ai/governance/mcp/migrations/{preview['migration_id']}/cutover",
        json={
            "expected_revision": confirmed["revision"],
            "command_id": "api-cutover",
        },
    )
    assert committed_response.status_code == 200
    assert committed_response.json()["state"] == "cutover_committed"

    rebound = client.post(
        "/api/ai/governance/mcp/projects/project-a/servers/calendar-server/rebind",
        json={"expected_revision": 1},
    )
    assert rebound.status_code == 200, rebound.text
    assert rebound.json() == {
        "status": "project_rebound", "project_id": "project-a",
        "server_id": "calendar-server", "profile_revision": 2,
    }


def _write_legacy(root: Path, value: object) -> None:
    path = root / ".rebuild-data" / "security" / "mcp-approved-stdio.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _delete_outer_receipt(root: Path, command_id: str) -> None:
    path = root / ".rebuild-data" / "security" / "mcp-approved-server-authority.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "DELETE FROM mcp_approved_migration_commands WHERE command_id = ?",
            (command_id,),
        )


def _candidate(endpoint_identity: str, revision: int) -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "servers": [{
            "server_id": "calendar-server", "enabled": True,
            "approval_status": "approved", "approval_revision": revision,
            "host_connection": {
                "server_id": "calendar-server", "manifest_revision": revision,
                "endpoint_identity": endpoint_identity,
                "credential_subject_id": "calendar-user",
                "transport_generation": revision, "catalog_revision": revision,
            },
            "launch_manifest": {
                "server_id": "calendar-server", "manifest_revision": revision,
                "endpoint_identity": endpoint_identity,
                "credential_subject_id": "calendar-user",
                "transport_generation": revision, "approval_revision": revision,
                "approval_status": "approved",
                "executable": str(Path(sys.executable).resolve()), "argv": [],
                "cwd": None, "environment": None, "secret_env_refs": None,
            },
            "tool_policies": [{
                "tool_name": "calendar.search", "tool_id": "calendar.search",
                "version": 1, "display_name": "Search", "description": "Reviewed",
                "effect": "read", "data_classes": ["calendar_event"],
                "input_schema_uri": "crp://input", "output_schema_uri": "crp://output",
                "receipt_schema_uri": None, "operation_semantics": "read_only",
                "execution_mode": "parallel", "resource_locks": ["mcp:calendar"],
                "idempotency": "never_retry",
                "retry_policy": {"max_attempts": 1, "backoff_ms": 0, "retryable_error_codes": []},
                "verification_tool_id": None, "compensation_tool_id": None,
                "mutability": "read_only", "egress_class": "remote",
                "network_scope": ["mcp:calendar"], "data_egress_scope": ["calendar_event"],
                "timeout_ms": 1000, "required_scopes": ["calendar.read"],
                "boundary_requirements": ["mcp_enabled"], "requires_approval": False,
                "tool_schema_revision": 1, "reviewed_input_schema": {"type": "object"},
                "reviewed_output_schema": None, "available": True,
                "remote_receipt_field": None, "reviewed_receipt_schema": None,
            }],
        }],
    }
