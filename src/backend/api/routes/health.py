from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from backend.api.desktop_session import (
    DESKTOP_SESSION_HEADER,
    desktop_health_payload,
    desktop_session,
    desktop_session_authorized,
    rotate_desktop_session,
)
from backend.api.effect_operations_health import build_effect_operations_health
from backend.api.responses import HealthResponse
from backend.api.runtime_root_config import (
    RuntimeRootConfigError,
    health_runtime_roots,
    load_runtime_root_config,
    runtime_root_environment_present,
    verify_runtime_roots,
)
from backend.api.storage_topology import storage_topology_payload
from backend.api.worker_auth import (
    WORKER_CHALLENGE_HEADER,
    WORKER_IDENTITY_HEADER,
    worker_health_identity,
)

router = APIRouter()


@router.post("/api/desktop/session/rotate")
async def rotate_desktop_session_route(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except (TypeError, ValueError):
        payload = None
    status, result = rotate_desktop_session(payload, request.headers.get(DESKTOP_SESSION_HEADER))
    return JSONResponse(result, status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/api/health", response_model=HealthResponse)
def health(request: Request, response: Response) -> HealthResponse:
    container = getattr(request.app.state, "container", None)
    root_dir = getattr(container, "root_dir", None)
    try:
        identity = worker_health_identity(
            request.headers.get(WORKER_CHALLENGE_HEADER),
            container_root=root_dir,
            recognition_root=getattr(request.app.state, "recognition_runtime_root", None),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if identity is not None:
        response.headers[WORKER_IDENTITY_HEADER] = identity
    runtime_roots = _observe_runtime_roots(root_dir)
    return HealthResponse(
        status="ok",
        desktop_session=desktop_health_payload(),
        storage=storage_topology_payload(),
        effect_operations=build_effect_operations_health(
            root_dir,
            getattr(request.app.state, "effect_recovery_service", None),
            recovery_coordinator=getattr(
                request.app.state, "effect_recovery_coordinator", None,
            ),
            application_state=request.app.state,
        ),
        runtime_roots=runtime_roots,
    )


@router.post("/api/desktop/runtime-roots/verify")
def verify_desktop_runtime_roots(request: Request) -> dict[str, object]:
    """Run a write probe only for the authenticated desktop sidecar owner."""
    if desktop_session() is None or not desktop_session_authorized(
        request.headers.get(DESKTOP_SESSION_HEADER)
    ):
        raise HTTPException(status_code=403, detail="desktop_session_unauthorized")
    container = getattr(request.app.state, "container", None)
    root_dir = getattr(container, "root_dir", None)
    try:
        return verify_runtime_roots(
            load_runtime_root_config(Path(root_dir)), container_root=Path(root_dir)
        )
    except (RuntimeRootConfigError, TypeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _observe_runtime_roots(root_dir: object) -> dict[str, object] | None:
    try:
        root = Path(root_dir)
        return health_runtime_roots(load_runtime_root_config(root), container_root=root)
    except (RuntimeRootConfigError, TypeError) as error:
        # Test and integration compositions can provide an absent synthetic
        # container root without launching through desktop root resolution.
        # A desktop-provided contract remains fail-closed.
        if not runtime_root_environment_present():
            return None
        raise HTTPException(status_code=503, detail=str(error)) from error
