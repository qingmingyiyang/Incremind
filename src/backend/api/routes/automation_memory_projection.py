"""Explicit Automation Grant flow for derived Memory Projection rebuilds."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.memory_projection_effect_runtime import (
    execute_granted_memory_projection_rebuild,
    memory_projection_rebuild_authority,
    memory_projection_rebuild_automation_binding,
    preview_memory_projection_rebuild_automation,
)
from backend.security.automation_grants import (
    AutomationGrantConflict,
    AutomationGrantError,
    AutomationGrantRepository,
)


router = APIRouter(tags=["automation-memory-projection"])
_BASE = "/api/rebuild/automations/memory-projection-rebuild"
_NO_STORE = {"Cache-Control": "no-store"}
_GRANT_LIFETIME = timedelta(minutes=10)


@router.post(f"{_BASE}/preview")
async def preview_memory_projection_automation_grant(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(request, {"project_id"})
    if body is None:
        return _error(400, "automation_preview_invalid")
    try:
        now = datetime.now(timezone.utc)
        binding, fingerprint = preview_memory_projection_rebuild_automation(
            memory_projection_rebuild_authority(container.root_dir),
            project_id=str(body["project_id"]),
            admitted_at=int(now.timestamp()),
        )
    except (TypeError, ValueError):
        return _error(409, "automation_preview_rejected")
    return JSONResponse(
        content={
            "project_id": binding.project_id,
            "authority_fingerprint": fingerprint,
            "authority_fingerprint_hint": fingerprint[:12],
            "binding": _binding_payload(binding),
            "grant_policy": {
                "expires_at": (now + _GRANT_LIFETIME).isoformat().replace("+00:00", "Z"),
                "max_uses": 1,
            },
            "requires_confirmation": True,
        },
        headers=_NO_STORE,
    )


@router.post(f"{_BASE}/grants")
async def create_memory_projection_automation_grant(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(request, {"project_id", "authority_fingerprint", "expires_at", "command_id"})
    if body is None:
        return _error(400, "automation_grant_invalid")
    try:
        current = datetime.now(timezone.utc)
        now = int(current.timestamp())
        authority = memory_projection_rebuild_authority(container.root_dir)
        binding = memory_projection_rebuild_automation_binding(
            authority,
            project_id=str(body["project_id"]),
            admitted_at=now,
            expected_authority_fingerprint=str(body["authority_fingerprint"]),
        )
        expires_at = _approved_expiry(str(body["expires_at"]), now=current)
        grant = AutomationGrantRepository(
            container.root_dir, secret_store=container.secret_store,
        ).create(
            binding=binding,
            expires_at=expires_at,
            max_uses=1,
            command_id=str(body["command_id"]),
        )
    except (AutomationGrantError, AutomationGrantConflict, TypeError, ValueError):
        return _error(409, "automation_grant_rejected")
    return JSONResponse(status_code=201, content=grant.public(), headers=_NO_STORE)


@router.get(f"{_BASE}/grants/{{grant_id}}")
def get_memory_projection_automation_grant(
    grant_id: str, project_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    try:
        grant = AutomationGrantRepository(
            container.root_dir, secret_store=container.secret_store,
        ).get(grant_id)
    except (AutomationGrantError, TypeError, ValueError):
        return _error(400, "automation_grant_invalid")
    if grant is None or not isinstance(project_id, str) or not hmac.compare_digest(grant.binding.project_id, project_id):
        return _error(404, "automation_grant_unavailable")
    runtime = getattr(request.app.state, "effect_runtime", None)
    effect = None
    if runtime is not None:
        try:
            effect = runtime.log.get(grant.binding.operation_id)
        except KeyError:
            effect = None
    return JSONResponse(
        content={"grant": grant.public(), "effect": _effect_payload(effect)},
        headers=_NO_STORE,
    )


@router.post(f"{_BASE}/grants/{{grant_id}}/execute")
async def execute_memory_projection_automation_grant(
    grant_id: str, request: Request, container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(
        request, {"project_id", "authority_fingerprint", "expected_grant_revision"},
    )
    if (
        body is None
        or not isinstance(body.get("expected_grant_revision"), int)
        or isinstance(body.get("expected_grant_revision"), bool)
    ):
        return _error(400, "automation_grant_invalid")
    runtime = getattr(request.app.state, "effect_runtime", None)
    if runtime is None:
        return _error(503, "automation_runtime_unavailable")
    try:
        now = int(datetime.now(timezone.utc).timestamp())
        settled, grant = execute_granted_memory_projection_rebuild(
            runtime,
            memory_projection_rebuild_authority(container.root_dir),
            AutomationGrantRepository(container.root_dir, secret_store=container.secret_store),
            project_id=str(body["project_id"]),
            admitted_at=now,
            grant_id=grant_id,
            expected_grant_revision=int(body["expected_grant_revision"]),
            expected_authority_fingerprint=str(body["authority_fingerprint"]),
        )
    except (AutomationGrantError, AutomationGrantConflict, TypeError, ValueError):
        return _error(409, "automation_execution_rejected")
    completed = settled.state.value == "SETTLED_OK" and isinstance(
        settled.result_ref, str,
    )
    return JSONResponse(
        status_code=200 if completed else 202,
        content={
            "status": "completed" if completed else "accepted",
            "operation_id": settled.operation_id,
            "receipt_ref": settled.result_ref,
            "grant": grant.public(),
            "effect": _effect_payload(settled),
        },
        headers=_NO_STORE,
    )


async def _body(request: Request, fields: set[str]) -> Mapping[str, object] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    if not isinstance(body, Mapping) or set(body) != fields:
        return None
    if any(not isinstance(body.get(field), str) for field in fields - {"expected_grant_revision"}):
        return None
    return body


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail}, headers=_NO_STORE)


def _binding_payload(binding) -> dict[str, object]:
    return {
        "project_id": binding.project_id,
        "operation_id": binding.operation_id,
        "parameter_digest": binding.parameter_digest,
        "effect_kind": binding.effect_kind,
        "capability_revision": binding.capability_revision,
        "target": binding.target,
        "boundary_profile_id": binding.boundary_profile_id,
        "boundary_revision": binding.boundary_revision,
        "secret_ref_count": len(binding.secret_refs),
    }


def _effect_payload(effect) -> dict[str, object] | None:
    if effect is None:
        return None
    state = getattr(getattr(effect, "state", None), "value", "UNKNOWN")
    status = {
        "PLANNED": "pending",
        "INFLIGHT": "running",
        "SETTLED_OK": "completed",
        "SETTLED_ERR": "failed",
        "UNKNOWN": "unknown",
    }.get(state, "unknown")
    return {
        "operation_id": getattr(effect, "operation_id", None),
        "status": status,
        "receipt_ref": getattr(effect, "result_ref", None),
        "error_recorded": bool(getattr(effect, "error_ref", None)),
    }


def _approved_expiry(value: str, *, now: datetime) -> str:
    try:
        expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("automation grant expiry is invalid") from error
    if expires_at.tzinfo is None:
        raise ValueError("automation grant expiry is invalid")
    expires_at = expires_at.astimezone(timezone.utc)
    if expires_at <= now or expires_at > now + _GRANT_LIFETIME:
        raise ValueError("automation grant expiry exceeds the preview policy")
    return expires_at.isoformat().replace("+00:00", "Z")
