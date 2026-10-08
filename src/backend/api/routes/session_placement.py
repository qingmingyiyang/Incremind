"""Production routes for bounded, host-authoritative local Session Placement."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.session_placement_runtime import (
    build_local_session_placement_runtime,
    derive_local_resume_export,
    list_local_resume_candidates,
    local_host_identity,
)
from backend.security.host_signing_identity_store import HostSigningIdentityStore
from core.product_core.session_placement import (
    DeviceIdentity,
    PairedDeviceRegistry,
    SessionPlacementConflict,
    SessionPlacementError,
)
from core.product_core.workspace_resume_reconcile import WorkspaceResumeReconcileError


router = APIRouter(tags=["session-placement"])
_BASE = "/api/rebuild/session-placement"
_NO_STORE = {"Cache-Control": "no-store"}
_DESKTOP_CONTROL_HEADER = "X-Chriptmas-Session-Placement-Control"


@router.get(f"{_BASE}/identity")
async def get_local_session_placement_identity(container: ApiContainerDep) -> JSONResponse:
    try:
        identity = local_host_identity(host_signer=_host_signer(container))
    except (RuntimeError, SessionPlacementError, ValueError):
        return _error(503, "session_placement_identity_unavailable")
    return JSONResponse(content=asdict(identity), headers=_NO_STORE)


@router.post(f"{_BASE}/pairings")
async def pair_session_placement_device(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(
        request,
        {"device_id", "public_key", "expected_revision", "confirm"},
    )
    if (
        body is None
        or body.get("confirm") is not True
        or not _integer(body.get("expected_revision"))
        or not isinstance(body.get("device_id"), str)
        or not isinstance(body.get("public_key"), str)
    ):
        return _error(400, "session_placement_pairing_invalid")
    try:
        revision = PairedDeviceRegistry(container.root_dir).trust(
            DeviceIdentity(
                device_id=str(body["device_id"]),
                public_key=str(body["public_key"]),
            ),
            expected_revision=int(body["expected_revision"]),
        )
    except SessionPlacementConflict:
        return _error(409, "session_placement_pairing_conflict")
    except (SessionPlacementError, ValueError):
        return _error(400, "session_placement_pairing_invalid")
    return JSONResponse(
        status_code=201,
        content={"device_id": body["device_id"], "trust_revision": revision},
        headers=_NO_STORE,
    )


@router.get(f"{_BASE}/pairings")
async def list_session_placement_pairings(container: ApiContainerDep) -> JSONResponse:
    return JSONResponse(content={"items": [
        {"device_id": item[0], "trust_revision": item[1], "trusted_at": item[2]}
        for item in PairedDeviceRegistry(container.root_dir).list_identities()
    ]}, headers=_NO_STORE)


@router.post(f"{_BASE}/pairings/{{device_id}}/revoke")
async def revoke_session_placement_pairing(
    device_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(request, {"expected_revision", "confirm"})
    if body is None or body.get("confirm") is not True or not _integer(body.get("expected_revision")):
        return _error(400, "session_placement_pairing_revoke_invalid")
    try:
        revision = PairedDeviceRegistry(container.root_dir).revoke(
            device_id,
            expected_revision=int(body["expected_revision"]),
        )
    except SessionPlacementConflict:
        return _error(409, "session_placement_pairing_conflict")
    except (SessionPlacementError, ValueError):
        return _error(400, "session_placement_pairing_revoke_invalid")
    return JSONResponse(content={
        "device_id": device_id,
        "trust_revision": revision,
        "trust_state": "revoked",
    }, headers=_NO_STORE)


@router.get(f"{_BASE}/export-candidates")
async def list_session_placement_export_candidates(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        project_id, limit = _project_query(request)
        items = list_local_resume_candidates(root_dir=container.root_dir, project_id=project_id, limit=limit)
    except (SessionPlacementError, ValueError):
        return _error(400, "session_placement_project_scope_invalid")
    return JSONResponse(content={"items": list(items)}, headers=_NO_STORE)


@router.get(f"{_BASE}/recoveries")
async def list_session_placement_recoveries(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        project_id, limit = _project_query(request)
        items = _runtime(request, container).list_recoveries(project_id=project_id, limit=limit)
    except (RuntimeError, SessionPlacementError, ValueError):
        return _error(400, "session_placement_project_scope_invalid")
    return JSONResponse(content={"items": [_recovery_list_item(item) for item in items]}, headers=_NO_STORE)


@router.post(f"{_BASE}/exports")
async def export_session_resume_bundle(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    if request.headers.get(_DESKTOP_CONTROL_HEADER) != "main-v1":
        return _error(403, "session_placement_desktop_control_required")
    body = await _body(request, {
        "project_id", "session_id", "target_device_id", "expires_at", "workspace_path",
    })
    if body is None or any(not isinstance(body.get(key), str) for key in body):
        return _error(400, "session_placement_export_invalid")
    try:
        runtime = _runtime(request, container)
        bundle = runtime.export_local(derive_local_resume_export(
            root_dir=container.root_dir,
            project_id=str(body["project_id"]),
            session_id=str(body["session_id"]),
            target_device_id=str(body["target_device_id"]),
            expires_at=str(body["expires_at"]),
            workspace_root=str(body["workspace_path"]),
        ))
    except (RuntimeError, SessionPlacementError, ValueError):
        return _error(409, "session_placement_export_rejected")
    return JSONResponse(status_code=201, content=bundle.wire(), headers=_NO_STORE)


@router.post(f"{_BASE}/imports")
async def import_session_resume_bundle(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(request, {"bundle"})
    if body is None or not isinstance(body.get("bundle"), Mapping):
        return _error(400, "session_placement_import_invalid")
    try:
        identity = local_host_identity(host_signer=_host_signer(container))
        projection = _runtime(request, container).import_local(
            body["bundle"],
            local_device_id=identity.device_id,
        )
    except SessionPlacementConflict:
        return _error(409, "session_placement_bundle_already_imported")
    except (RuntimeError, SessionPlacementError, ValueError):
        return _error(409, "session_placement_import_rejected")
    return JSONResponse(
        status_code=201,
        content=_recovery_list_item(projection),
        headers=_NO_STORE,
    )


@router.get(f"{_BASE}/recoveries/{{bundle_id}}")
async def read_session_recovery(
    bundle_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    try:
        project_id = _required_project_id(request.query_params.get("project_id"))
        projection = _scoped_recovery(
            _runtime(request, container), bundle_id=bundle_id, project_id=project_id,
        )
    except (RuntimeError, SessionPlacementError, ValueError):
        return _error(409, "session_placement_recovery_rejected")
    if projection is None:
        return _error(404, "session_placement_recovery_not_found")
    return JSONResponse(content=_recovery_list_item(projection), headers=_NO_STORE)


@router.post(f"{_BASE}/recoveries/{{bundle_id}}/reconcile-plan")
async def plan_session_workspace_reconciliation(
    bundle_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    if request.headers.get(_DESKTOP_CONTROL_HEADER) != "main-v1":
        return _error(403, "session_placement_desktop_control_required")
    body = await _body(request, {"project_id", "workspace_path"})
    if (
        body is None
        or not isinstance(body.get("project_id"), str)
        or not isinstance(body.get("workspace_path"), str)
    ):
        return _error(400, "session_placement_reconcile_invalid")
    try:
        runtime = _runtime(request, container)
        projection = _scoped_recovery(
            runtime,
            bundle_id=bundle_id,
            project_id=_required_project_id(body["project_id"]),
        )
        if projection is None:
            return _error(404, "session_placement_recovery_not_found")
        plan = runtime.workspace_reconcile_plan(
            bundle_id=bundle_id,
            workspace_root=str(body["workspace_path"]),
        )
    except (RuntimeError, SessionPlacementError, WorkspaceResumeReconcileError, ValueError):
        return _error(409, "session_placement_reconcile_rejected")
    return JSONResponse(content=plan.as_dict(), headers=_NO_STORE)


@router.post(f"{_BASE}/recoveries/{{bundle_id}}/host-authorization-requests")
async def request_session_host_authorization(
    bundle_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    body = await _body(request, {
        "project_id", "operation_identity", "parameter_digest", "confirm",
    })
    if (
        body is None
        or body.get("confirm") is not True
        or not isinstance(body.get("project_id"), str)
        or not isinstance(body.get("operation_identity"), str)
        or not isinstance(body.get("parameter_digest"), str)
    ):
        return _error(400, "session_placement_authorization_request_invalid")
    try:
        runtime = _runtime(request, container)
        projection = _scoped_recovery(
            runtime,
            bundle_id=bundle_id,
            project_id=_required_project_id(body["project_id"]),
        )
        if projection is None:
            return _error(404, "session_placement_recovery_not_found")
        result = runtime.request_host_authorization(
            bundle_id=bundle_id,
            operation_identity=str(body["operation_identity"]),
            parameter_digest=str(body["parameter_digest"]),
        )
    except (RuntimeError, SessionPlacementError, ValueError):
        return _error(409, "session_placement_authorization_request_rejected")
    return JSONResponse(status_code=202, content=asdict(result), headers=_NO_STORE)


def _runtime(request: Request, container: ApiContainerDep):
    object_store, _settings = build_rebuild_object_store(container.root_dir)
    runtimes = tuple(
        runtime
        for name in ("effect_runtime", "ai_effect_runtime", "ppt_master_effect_runtime")
        if (runtime := getattr(request.app.state, name, None)) is not None
    )
    return build_local_session_placement_runtime(
        root_dir=container.root_dir,
        host_signer=_host_signer(container),
        object_store=object_store,
        effect_runtimes=runtimes,
    )


def _host_signer(container: ApiContainerDep) -> HostSigningIdentityStore:
    return HostSigningIdentityStore(container.secret_store)


def _recovery_list_item(value) -> dict[str, object]:
    """Discovery returns only identifiers and durable read-only state."""
    return {
        "bundle_id": value.bundle_id,
        "session_id": value.session_ref,
        "project_id": value.project_id,
        "read_only": True,
        "created_at": value.created_at,
        "source_device_id": value.source_device_id,
    }


def _project_query(request: Request) -> tuple[str, int]:
    project_id = _required_project_id(request.query_params.get("project_id"))
    raw_limit = request.query_params.get("limit", "32")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as error:
        raise SessionPlacementError("session placement limit is invalid") from error
    if str(limit) != raw_limit or not 1 <= limit <= 32:
        raise SessionPlacementError("session placement limit is invalid")
    return project_id, limit


def _required_project_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise SessionPlacementError("project scope is invalid")
    return value


def _scoped_recovery(runtime, *, bundle_id: str, project_id: str):
    projection = runtime.read_recovery(bundle_id)
    if projection is None or not hmac.compare_digest(projection.project_id, project_id):
        return None
    return projection


async def _body(request: Request, fields: set[str]) -> Mapping[str, object] | None:
    try:
        value = await request.json()
    except Exception:
        return None
    return value if isinstance(value, Mapping) and set(value) == fields else None


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail}, headers=_NO_STORE)
