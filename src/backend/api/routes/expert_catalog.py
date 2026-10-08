"""Expert catalog HTTP 管理面：目录/绑定/选择预览/快照冻结。

领域语义全部由 ``product_core.expert_catalog`` / ``expert_binding_snapshot`` 承担
（CAS、lint、fail-closed），本模块只做 loopback + desktop session 门禁、JSON 映射
与状态码。所有端点均要求已授权的本地会话。
"""

from __future__ import annotations

from backend.security.device_identity import server_mode, server_authorized

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.desktop_session import (
    DESKTOP_SESSION_HEADER,
    desktop_session,
    desktop_session_authorized,
)
from core.product_core.expert_binding_snapshot import (
    ExpertBindingSnapshotError,
    freeze_expert_binding_snapshot,
)
from core.product_core.expert_catalog import (
    ExpertCatalog,
    ExpertCatalogConflict,
    ExpertCatalogError,
    ExpertCatalogNotFound,
    ExpertProjectBindingStore,
    ExpertConfigurationResolver,
)

router = APIRouter(prefix="/api/ai", tags=["expert-catalog"])


def _local_request(request: Request) -> bool:
    if server_mode(request):
        return server_authorized(request)
    import ipaddress

    host = request.client.host if request.client is not None else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _authorized_request(request: Request) -> bool:
    if server_mode(request):
        return server_authorized(request)
    if not _local_request(request):
        return False
    try:
        session = desktop_session()
    except RuntimeError:
        return False
    return session is not None and desktop_session_authorized(
        request.headers.get(DESKTOP_SESSION_HEADER)
    )


def _guard(request: Request) -> JSONResponse | None:
    if not _authorized_request(request):
        return JSONResponse(status_code=403, content={"detail": "local desktop session required"})
    return None


def _container_root(request: Request) -> Path:
    container = getattr(request.app.state, "container", None)
    root_dir = getattr(container, "root_dir", None)
    if root_dir is None:
        raise RuntimeError("expert catalog data root is unavailable")
    return Path(root_dir)


def _catalog(request: Request) -> ExpertCatalog:
    return ExpertCatalog(_container_root(request))


def _bindings(request: Request) -> ExpertProjectBindingStore:
    return ExpertProjectBindingStore(_container_root(request))


def _error_response(error: ExpertCatalogError) -> JSONResponse:
    if isinstance(error, ExpertCatalogNotFound):
        status = 404
    elif isinstance(error, ExpertCatalogConflict):
        status = 409
    else:
        status = 400
    return JSONResponse(status_code=status, content={"detail": str(error)})


async def _json_body(request: Request) -> dict[str, object]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise ExpertCatalogError("request body must be a JSON object")
    return payload


@router.get("/experts")
async def list_experts(request: Request):
    forbidden = _guard(request)
    if forbidden is not None:
        return forbidden
    catalog = _catalog(request)
    return {
        "registry_revision": catalog.registry_revision,
        "experts": [deepcopy_expert(item) for item in catalog.list_experts()],
    }


def deepcopy_expert(profile: dict[str, object]) -> dict[str, object]:
    return dict(profile)


@router.post("/experts")
async def create_expert(request: Request):
    forbidden = _guard(request)
    if forbidden is not None:
        return forbidden
    try:
        body = await _json_body(request)
        profile = body.get("profile")
        if not isinstance(profile, dict):
            raise ExpertCatalogError("profile is required")
        expected = body.get("expected_registry_revision")
        if not isinstance(expected, int):
            raise ExpertCatalogError("expected_registry_revision must be an int")
        created = _catalog(request).create(profile, expected_registry_revision=expected)
    except ExpertCatalogError as error:
        return _error_response(error)
    return JSONResponse(status_code=201, content=created)


@router.post("/experts/{expert_id}/upgrade")
async def upgrade_expert(expert_id: str, request: Request):
    forbidden = _guard(request)
    if forbidden is not None:
        return forbidden
    try:
        body = await _json_body(request)
        profile = body.get("profile")
        if not isinstance(profile, dict):
            raise ExpertCatalogError("profile is required")
        expected_expert = body.get("expected_expert_revision")
        expected_registry = body.get("expected_registry_revision")
        if not isinstance(expected_expert, int) or not isinstance(expected_registry, int):
            raise ExpertCatalogError("expected revisions must be ints")
        upgraded = _catalog(request).upgrade(
            expert_id,
            profile,
            expected_expert_revision=expected_expert,
            expected_registry_revision=expected_registry,
        )
    except ExpertCatalogError as error:
        return _error_response(error)
    return upgraded


@router.post("/experts/{expert_id}/status")
async def set_expert_status(expert_id: str, request: Request):
    forbidden = _guard(request)
    if forbidden is not None:
        return forbidden
    try:
        body = await _json_body(request)
        status = body.get("status")
        reason = body.get("reason")
        expected_expert = body.get("expected_expert_revision")
        expected_registry = body.get("expected_registry_revision")
        if not isinstance(status, str) or not isinstance(reason, str):
            raise ExpertCatalogError("status and reason must be strings")
        if not isinstance(expected_expert, int) or not isinstance(expected_registry, int):
            raise ExpertCatalogError("expected revisions must be ints")
        updated = _catalog(request).set_status(
            expert_id,
            status,
            reason=reason,
            expected_expert_revision=expected_expert,
            expected_registry_revision=expected_registry,
        )
    except ExpertCatalogError as error:
        return _error_response(error)
    return updated


@router.get("/projects/{project_id}/expert-bindings")
async def list_project_bindings(project_id: str, request: Request):
    forbidden = _guard(request)
    if forbidden is not None:
        return forbidden
    bindings = _bindings(request)
    return {
        "store_revision": bindings.store_revision,
        "bindings": [dict(item) for item in bindings.list_for_project(project_id)],
    }


@router.post("/projects/{project_id}/expert-bindings")
async def bind_expert(project_id: str, request: Request):
    forbidden = _guard(request)
    if forbidden is not None:
        return forbidden
    try:
        body = await _json_body(request)
        expert_id = body.get("expert_id")
        enabled_revision = body.get("enabled_expert_revision")
        expected_store = body.get("expected_store_revision")
        if not isinstance(expert_id, str) or not expert_id.strip():
            raise ExpertCatalogError("expert_id is required")
        if not isinstance(enabled_revision, int) or not isinstance(expected_store, int):
            raise ExpertCatalogError("revisions must be ints")
        mode = body.get("selection_mode") or "manual"
        default = body.get("default") is True
        reason = body.get("reason")
        if not isinstance(reason, str):
            raise ExpertCatalogError("reason must be a string")
        binding = _bindings(request).bind(
            project_id,
            expert_id.strip(),
            catalog=_catalog(request),
            enabled_expert_revision=enabled_revision,
            selection_mode=mode,
            default=default,
            reason=reason,
            expected_store_revision=expected_store,
        )
    except ExpertCatalogError as error:
        return _error_response(error)
    return JSONResponse(status_code=201, content=binding)


@router.post("/projects/{project_id}/expert-bindings/{expert_id}/update")
async def update_binding(project_id: str, expert_id: str, request: Request):
    forbidden = _guard(request)
    if forbidden is not None:
        return forbidden
    try:
        body = await _json_body(request)
        expected_binding = body.get("expected_binding_revision")
        expected_store = body.get("expected_store_revision")
        if not isinstance(expected_binding, int) or not isinstance(expected_store, int):
            raise ExpertCatalogError("revisions must be ints")
        updated = _bindings(request).update(
            project_id,
            expert_id,
            expected_binding_revision=expected_binding,
            expected_store_revision=expected_store,
            enabled_expert_revision=body.get("enabled_expert_revision"),
            selection_mode=body.get("selection_mode"),
            default=body.get("default"),
            reason=body.get("reason") or "",
        )
    except ExpertCatalogError as error:
        return _error_response(error)
    return updated


@router.post("/projects/{project_id}/expert-bindings/{expert_id}/unbind")
async def unbind_expert(project_id: str, expert_id: str, request: Request):
    forbidden = _guard(request)
    if forbidden is not None:
        return forbidden
    try:
        body = await _json_body(request)
        expected_binding = body.get("expected_binding_revision")
        expected_store = body.get("expected_store_revision")
        reason = body.get("reason")
        if not isinstance(expected_binding, int) or not isinstance(expected_store, int):
            raise ExpertCatalogError("revisions must be ints")
        if not isinstance(reason, str):
            raise ExpertCatalogError("reason must be a string")
        removed = _bindings(request).unbind(
            project_id,
            expert_id,
            reason=reason,
            expected_binding_revision=expected_binding,
            expected_store_revision=expected_store,
        )
    except ExpertCatalogError as error:
        return _error_response(error)
    return removed


@router.post("/experts/selection-preview")
async def selection_preview(request: Request):
    forbidden = _guard(request)
    if forbidden is not None:
        return forbidden
    try:
        body = await _json_body(request)
        project_id = body.get("project_id")
        task_intents = body.get("task_intents")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ExpertCatalogError("project_id is required")
        if not isinstance(task_intents, list):
            raise ExpertCatalogError("task_intents must be a list")
        requested = body.get("requested_expert_id")
        receipt = ExpertConfigurationResolver(_catalog(request), _bindings(request)).select(
            project_id.strip(),
            [item for item in task_intents if isinstance(item, str)],
            requested_expert_id=requested if isinstance(requested, str) else None,
            budget=body.get("budget") if isinstance(body.get("budget"), str) else None,
        )
    except ExpertCatalogError as error:
        return _error_response(error)
    return receipt


@router.post("/experts/binding-snapshots")
async def freeze_binding_snapshot(request: Request):
    forbidden = _guard(request)
    if forbidden is not None:
        return forbidden
    try:
        body = await _json_body(request)
        receipt = body.get("selection_receipt")
        if not isinstance(receipt, dict):
            raise ExpertCatalogError("selection_receipt is required")
        runtime = body.get("runtime_revisions")
        if not isinstance(runtime, dict):
            raise ExpertCatalogError("runtime_revisions is required")
        tools = runtime.get("tool_capability_revisions")
        if not isinstance(tools, dict):
            raise ExpertCatalogError("runtime_revisions.tool_capability_revisions is required")
        snapshot = freeze_expert_binding_snapshot(
            receipt,
            catalog=_catalog(request),
            bindings=_bindings(request),
            context_manifest_revision=runtime.get("context_manifest_revision"),
            boundary_revision=runtime.get("boundary_revision"),
            model_route_revision=runtime.get("model_route_revision"),
            tool_capability_revisions=tools,
            budget=body.get("budget") if isinstance(body.get("budget"), str) else None,
        )
    except ExpertCatalogError as error:
        return _error_response(error)
    return snapshot
