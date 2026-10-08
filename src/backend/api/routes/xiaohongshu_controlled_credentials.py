"""Local governance endpoints for Xiaohongshu controlled credentials.

The request is the only point where a Cookie value is accepted.  It is passed
straight to the authority and is never copied into a response, error payload,
or log record.  Boundary identity is deliberately derived from the current
project authority instead of being client supplied.
"""
from __future__ import annotations

from backend.security.device_identity import server_mode, server_authorized

import asyncio
from collections.abc import Mapping
import ipaddress
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.desktop_session import (
    DESKTOP_SESSION_HEADER,
    desktop_session,
    desktop_session_authorized,
)
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.xiaohongshu_controlled_credentials import (
    XiaohongshuControlledCredentialAuthority,
    XiaohongshuControlledCredentialConflict,
    XiaohongshuControlledCredentialError,
)


router = APIRouter(tags=["xiaohongshu-controlled-credentials"])

_PATH = "/api/ai/projects/{project_id}/xiaohongshu-controlled-credentials"
_MUTATION_FIELDS = frozenset({
    "subject", "cookie_value", "expires_at", "expected_authorization_revision",
    "command_id", "confirm",
})
_REVOKE_FIELDS = frozenset({"subject", "expected_authorization_revision", "command_id", "confirm"})
_RECONCILE_FIELDS = frozenset({
    "subject", "pending_command_id", "reconciliation_command_id", "confirm_quarantine",
})


@router.get(f"{_PATH}/current")
async def current_controlled_credential(
    project_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _local_governance_request(request):
        return _response(403, {"detail": "Controlled credential governance is local-only"})
    subject = request.query_params.get("subject")
    if subject is None or set(request.query_params) != {"subject"}:
        return _response(400, {"detail": "Controlled credential subject is required"})
    authority = _authority_or_none(container)
    if authority is None:
        return _response(503, {"detail": "Controlled credential Secret Store is unavailable"})
    try:
        current = await asyncio.to_thread(
            authority.current, project_id=project_id, credential_subject_id=subject,
        )
    except (XiaohongshuControlledCredentialError, ValueError) as error:
        return _error_response(error)
    return _response(200, {"authorization": None if current is None else current.public()})


@router.post(f"{_PATH}/grant")
async def grant_controlled_credential(
    project_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    return await _mutate(project_id, request, container, operation="grant")


@router.post(f"{_PATH}/rotate")
async def rotate_controlled_credential(
    project_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    return await _mutate(project_id, request, container, operation="rotate")


@router.post(f"{_PATH}/revoke")
async def revoke_controlled_credential(
    project_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _local_governance_request(request):
        return _response(403, {"detail": "Controlled credential governance is local-only"})
    body = await _body(request)
    if body is None or frozenset(body) != _REVOKE_FIELDS or body.get("confirm") is not True:
        return _response(400, {"detail": "Controlled credential revoke request is invalid"})
    authority = _authority_or_none(container)
    if authority is None:
        return _response(503, {"detail": "Controlled credential Secret Store is unavailable"})
    try:
        result = await asyncio.to_thread(
            authority.revoke,
            project_id=project_id,
            credential_subject_id=body["subject"],
            expected_authorization_revision=body["expected_authorization_revision"],
            command_id=body["command_id"],
        )
    except (XiaohongshuControlledCredentialError, ValueError) as error:
        return _error_response(error)
    return _response(200, {"authorization": result.public()})


@router.post(f"{_PATH}/reconcile-indeterminate")
async def reconcile_indeterminate_controlled_credential(
    project_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    if not _local_governance_request(request):
        return _response(403, {"detail": "Controlled credential governance is local-only"})
    body = await _body(request)
    if (
        body is None or frozenset(body) != _RECONCILE_FIELDS
        or body.get("confirm_quarantine") is not True
    ):
        return _response(400, {"detail": "Controlled credential reconciliation request is invalid"})
    authority = _authority_or_none(container)
    if authority is None:
        return _response(503, {"detail": "Controlled credential Secret Store is unavailable"})
    try:
        result = await asyncio.to_thread(
            authority.quarantine_indeterminate,
            project_id=project_id, credential_subject_id=body["subject"],
            pending_command_id=body["pending_command_id"],
            reconciliation_command_id=body["reconciliation_command_id"],
        )
    except (XiaohongshuControlledCredentialError, ValueError) as error:
        return _error_response(error)
    return _response(200, {
        "outcome": "quarantined",
        "authorization": None if result is None else result.public(),
    })


async def _mutate(
    project_id: str,
    request: Request,
    container: object,
    *,
    operation: str,
) -> JSONResponse:
    if not _local_governance_request(request):
        return _response(403, {"detail": "Controlled credential governance is local-only"})
    body = await _body(request)
    if body is None or frozenset(body) != _MUTATION_FIELDS or body.get("confirm") is not True:
        return _response(400, {"detail": "Controlled credential request is invalid"})
    authority = _authority_or_none(container)
    if authority is None:
        return _response(503, {"detail": "Controlled credential Secret Store is unavailable"})
    try:
        boundary = await asyncio.to_thread(ProjectBoundaryProfileStore(container.root_dir).get, project_id)
        method = authority.grant if operation == "grant" else authority.rotate
        result = await asyncio.to_thread(
            method,
            project_id=project_id,
            credential_subject_id=body["subject"],
            boundary_profile_id=boundary.profile.profile_id,
            boundary_revision=boundary.profile.revision,
            expires_at=body["expires_at"],
            cookie_value=body["cookie_value"],
            expected_authorization_revision=body["expected_authorization_revision"],
            command_id=body["command_id"],
        )
    except (XiaohongshuControlledCredentialError, ValueError) as error:
        return _error_response(error)
    return _response(200, {"authorization": result.public()})


async def _body(request: Request) -> Mapping[str, Any] | None:
    try:
        payload = await request.json()
    except Exception:
        return None
    return payload if isinstance(payload, Mapping) else None


def _authority_or_none(container: object) -> XiaohongshuControlledCredentialAuthority | None:
    """Require the live atomic Secret Store even for non-mutating reads."""

    root_dir = getattr(container, "root_dir", None)
    secret_store = getattr(container, "secret_store", None)
    if root_dir is None or secret_store is None:
        return None
    if not callable(getattr(secret_store, "set", None)) or not callable(getattr(secret_store, "get_snapshot", None)):
        return None
    return XiaohongshuControlledCredentialAuthority(root_dir, secret_store=secret_store)


def _error_response(error: Exception) -> JSONResponse:
    status = 409 if isinstance(error, XiaohongshuControlledCredentialConflict) else 400
    # Authority error strings are intentionally closed, fixed-vocabulary text;
    # do not attach request values, especially the Cookie payload.
    return _response(status, {"detail": "Controlled credential request was rejected", "reason": str(error)})


def _response(status_code: int, payload: Mapping[str, object]) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=dict(payload), headers={"Cache-Control": "no-store"})


def _local_governance_request(request: Request) -> bool:
    if server_mode(request):
        return server_authorized(request)
    host = request.client.host if request.client is not None else ""
    if host not in {"localhost", "testclient"}:
        try:
            if not ipaddress.ip_address(host).is_loopback:
                return False
        except ValueError:
            return False
    try:
        session = desktop_session()
    except RuntimeError:
        return False
    return session is None or desktop_session_authorized(request.headers.get(DESKTOP_SESSION_HEADER))
