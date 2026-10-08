from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.mcp_migration_runtime import (
    MCPApprovedServerMigrationRuntime,
    MCPMigrationRuntimeError,
)
from backend.security.mcp_approved_server_migration import (
    MCPApprovedServerMigrationAuthority,
    MCPApprovedServerMigrationConflict,
    MCPApprovedServerMigrationError,
)
from backend.security.mcp_approved_servers import JsonMCPApprovedServerStore
from backend.security.project_capability_profiles import (
    ProjectCapabilityProfileConflict,
    ProjectCapabilityProfileStore,
    ProjectCapabilityProfileStoreError,
)


router = APIRouter(tags=["mcp-migrations"])


@router.post("/api/ai/governance/mcp/migrations/preview")
async def preview_migration(request: Request, container: ApiContainerDep) -> JSONResponse:
    body = await _body(request)
    if body is None or set(body) != {"candidate", "command_id"}:
        return _response(400, {"status": "invalid_request"})
    try:
        result = await asyncio.to_thread(
            MCPApprovedServerMigrationAuthority(container.root_dir).preview,
            candidate=body["candidate"], command_id=body["command_id"],
        )
    except (MCPApprovedServerMigrationError, ValueError) as error:
        return _migration_error(error)
    return _response(200, result.public())


@router.get("/api/ai/governance/mcp/migrations/{migration_id}")
async def migration_status(migration_id: str, container: ApiContainerDep) -> JSONResponse:
    try:
        result = await asyncio.to_thread(
            MCPApprovedServerMigrationAuthority(container.root_dir).status, migration_id,
        )
    except MCPApprovedServerMigrationError as error:
        return _migration_error(error)
    if result is None:
        return _response(404, {"status": "migration_not_found"})
    return _response(200, result.public())


@router.post("/api/ai/governance/mcp/migrations/{migration_id}/confirm")
async def confirm_migration(
    migration_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(request)
    if body is None or set(body) != {"expected_revision", "command_id", "confirmed"}:
        return _response(400, {"status": "invalid_request"})
    try:
        result = await asyncio.to_thread(
            MCPApprovedServerMigrationAuthority(container.root_dir).confirm,
            migration_id=migration_id, expected_revision=body["expected_revision"],
            command_id=body["command_id"], confirmed=body["confirmed"],
        )
    except (MCPApprovedServerMigrationError, ValueError) as error:
        return _migration_error(error)
    return _response(200, result.public())


@router.post("/api/ai/governance/mcp/migrations/{migration_id}/cutover")
async def cutover_migration(
    migration_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    return await _switch(migration_id, request, container, operation="cutover")


@router.post("/api/ai/governance/mcp/migrations/{migration_id}/rollback")
async def rollback_migration(
    migration_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    return await _switch(migration_id, request, container, operation="rollback")


@router.post("/api/ai/governance/mcp/projects/{project_id}/servers/{server_id}/rebind")
async def rebind_project_mcp_server(
    project_id: str, server_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(request)
    if body is None or set(body) != {"expected_revision"}:
        return _response(400, {"status": "invalid_request"})
    try:
        snapshot = await asyncio.to_thread(JsonMCPApprovedServerStore(container.root_dir).snapshot)
        record = next(
            (item for item in snapshot.enabled_servers if item.server_id == server_id), None,
        )
        if record is None:
            return _response(404, {"status": "approved_server_not_found"})
        host = record.host_connection
        result = await asyncio.to_thread(
            ProjectCapabilityProfileStore(container.root_dir).rebind_mcp_server,
            project_id,
            expected_revision=body["expected_revision"],
            server_id=server_id,
            protocol_profile=host.protocol_profile,
            manifest_revision=host.manifest_revision,
            endpoint_identity=host.endpoint_identity,
            credential_subject_id=host.credential_subject_id,
            transport_generation=host.transport_generation,
        )
    except ProjectCapabilityProfileConflict as error:
        return _response(409, {"status": "project_profile_conflict", "reason": str(error)})
    except (ProjectCapabilityProfileStoreError, ValueError) as error:
        return _response(400, {"status": "project_rebind_rejected", "reason": str(error)})
    return _response(200, {
        "status": "project_rebound",
        "project_id": result.profile.project_id,
        "server_id": server_id,
        "profile_revision": result.store_revision,
    })


async def _switch(
    migration_id: str,
    request: Request,
    container: ApiContainerDep,
    *,
    operation: str,
) -> JSONResponse:
    body = await _body(request)
    if body is None or set(body) != {"expected_revision", "command_id"}:
        return _response(400, {"status": "invalid_request"})
    manager = getattr(request.app.state, "ai_mcp_connection_manager", None)
    if manager is None:
        return _response(503, {"status": "mcp_runtime_unavailable"})
    runtime = MCPApprovedServerMigrationRuntime(container.root_dir, manager=manager)
    try:
        method = runtime.cutover if operation == "cutover" else runtime.rollback
        result = await asyncio.to_thread(
            method,
            migration_id=migration_id,
            expected_revision=body["expected_revision"],
            command_id=body["command_id"],
        )
    except (MCPApprovedServerMigrationError, MCPMigrationRuntimeError, ValueError) as error:
        return _migration_error(error)
    return _response(200, result.public())


async def _body(request: Request) -> Mapping[str, Any] | None:
    try:
        payload = await request.json()
    except Exception:
        return None
    return payload if isinstance(payload, Mapping) else None


def _migration_error(error: Exception) -> JSONResponse:
    status_code = 409 if isinstance(error, MCPApprovedServerMigrationConflict) else 400
    if isinstance(error, MCPMigrationRuntimeError):
        status_code = 503
    return _response(status_code, {"status": "migration_rejected", "reason": str(error)})


def _response(status_code: int, payload: Mapping[str, object]) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=dict(payload),
        headers={"Cache-Control": "no-store"},
    )
