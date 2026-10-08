"""Public, path-free HTTP projection for the PPT Master installer."""
from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from backend.api.ppt_master_installation_runtime import (
    PptMasterInstallationError,
    PptMasterWorkflowBoundaryError,
)


router = APIRouter(prefix="/api/rebuild/developer-studio/ppt-master-installation", tags=["ppt-master-installation"])
_PROJECT_ID = "default"
_CONFIRMATIONS = {
    "governed_source": "network-download",
    "isolated_runtime": "third-party-content",
    "activation_rollback": "project-skill-activation",
}


def _runtime(request: Request):
    value = getattr(request.app.state, "ppt_master_installation_runtime", None)
    if value is None:
        raise PptMasterInstallationError("PPT Master installation runtime is unavailable")
    return value


def _capability(request: Request):
    value = getattr(request.app.state, "ppt_master_capability", None)
    if value is None or not callable(getattr(value, "resolve_result", None)):
        raise PptMasterInstallationError("PPTX result runtime is unavailable")
    return value


def _message(error: Exception) -> str:
    # Domain errors are deliberately stable and never interpolate paths, URLs,
    # network details, subprocess output, or exception repr.
    text = str(error)
    return text if text and len(text) <= 160 and all(mark not in text for mark in ("\\", "/", "\n", "\r")) else "installation operation was rejected"


def _response(status: int, payload: Mapping[str, object]) -> JSONResponse:
    return JSONResponse(status_code=status, content=dict(payload), headers={"Cache-Control": "no-store"})


def _state(value: object) -> str:
    if value in {"installed", "SETTLED_OK"}:
        return "active"
    if value == "rolled_back":
        return "rolled_back"
    if value in {"PLANNED", "INFLIGHT"}:
        return "pending"
    return "inactive"


def _public_receipt_id(value: object) -> str | None:
    """Expose the Effect operation identity, never the internal receipt ref."""
    if not isinstance(value, str) or len(value) > 120:
        return None
    return value if value.replace("-", "").replace("_", "").isalnum() else None


@router.get("/status")
def status(request: Request) -> JSONResponse:
    try:
        current = _runtime(request).status(_PROJECT_ID)
        state = _state(current.get("state"))
        return _response(200, {"receipt_id": _public_receipt_id(current.get("operation_id")), "activation": {"status": state}})
    except PptMasterInstallationError as error:
        return _response(503, {"message": _message(error)})


@router.get("/preview")
def preview(request: Request) -> JSONResponse:
    try:
        value = _runtime(request).preview(_PROJECT_ID)
        compatible = bool(value.get("runtime_self_manifest_revision"))
        return _response(200, {
            "preview_id": value.get("preview_token"),
            "source": {"label": "GitHub 受治理固定来源"},
            "package": {"version": value.get("revision", "待预检确认")},
            "runtime": {"label": "隔离 Python 运行时"},
            "summary": "预览不下载、不执行、不激活。确认后创建受治理 Effect。",
            "risks": ["网络下载", "第三方内容", "项目级 Skill 激活"],
            "runtime_self_manifest": {"compatible": compatible},
            "required_confirmations": [{"id": item, "label": label} for item, label in (
                ("governed_source", "确认仅使用受治理来源和暂存区"),
                ("isolated_runtime", "确认使用隔离运行时"),
                ("activation_rollback", "确认激活可回滚且仅影响后续新 Turn"),
            )],
        })
    except PptMasterInstallationError as error:
        return _response(422, {"message": _message(error)})


@router.post("/confirm")
async def confirm(request: Request) -> JSONResponse:
    body = await _body(request)
    direct_fields = {"preview_id", "confirmations"}
    egress_fields = direct_fields | {"network_egress", "network_egress_confirmation"}
    if body is None or set(body) not in {frozenset(direct_fields), frozenset(egress_fields)}:
        return _response(400, {"message": "exact preview_id and confirmations are required"})
    preview_id, confirmations = body.get("preview_id"), body.get("confirmations")
    if not isinstance(preview_id, str) or not isinstance(confirmations, list) or set(confirmations) != set(_CONFIRMATIONS):
        return _response(400, {"message": "all required confirmations are required"})
    has_egress = set(body) == egress_fields
    if has_egress and body.get("network_egress_confirmation") != "confirm_loopback_network_egress":
        return _response(400, {"message": "explicit loopback network egress confirmation is required"})
    try:
        result = _runtime(request).confirm(
            project_id=_PROJECT_ID, preview_token=preview_id, confirm=True,
            risk_acknowledgements=tuple(_CONFIRMATIONS[item] for item in confirmations),
            network_egress=body.get("network_egress") if has_egress else None,
            confirm_loopback_egress=has_egress,
        )
        return _response(200, {"receipt_id": _public_receipt_id(result.get("operation_id")), "effect": {"status": "settled" if result.get("state") == "SETTLED_OK" else "inflight"}, "activation": {"status": "active" if result.get("state") == "SETTLED_OK" else "pending"}})
    except PptMasterWorkflowBoundaryError as error:
        return _response(409, {"message": _message(error), "workflow": error.projection})
    except PptMasterInstallationError as error:
        return _response(409, {"message": _message(error)})


@router.post("/smoke")
async def smoke(request: Request) -> JSONResponse:
    body = await _body(request)
    receipt = body.get("receipt_id") if body else None
    runner = getattr(request.app.state, "ppt_master_smoke", None)
    if _public_receipt_id(receipt) is None:
        return _response(400, {"message": "installation receipt is required"})
    if not callable(runner):
        return _response(503, {"message": "PPT Master smoke runtime is unavailable"})
    try:
        outcome = runner(receipt)
    except Exception:
        return _response(503, {"status": "failed"})
    return _response(200, {"status": "passed" if outcome is True or outcome == "passed" else "failed"})


@router.post("/rollback")
async def rollback(request: Request) -> JSONResponse:
    body = await _body(request)
    if body is None or set(body) != {"receipt_id", "confirmation"} or body.get("confirmation") != "rollback_ppt_master":
        return _response(400, {"message": "explicit rollback confirmation is required"})
    try:
        current = _runtime(request).status(_PROJECT_ID)
        if body.get("receipt_id") != current.get("operation_id"):
            return _response(409, {"message": "installation receipt drifted"})
        result = _runtime(request).rollback(project_id=_PROJECT_ID, confirm=True)
        if result.get("state") == "SETTLED_OK":
            return _response(200, {"status": "rolled_back"})
        if result.get("state") in {"PLANNED", "INFLIGHT"}:
            return _response(202, {"status": "pending"})
        return _response(503, {"status": "failed"})
    except PptMasterInstallationError as error:
        return _response(409, {"message": _message(error)})


@router.get("/results/{result_id}")
def download_result(result_id: str, request: Request):
    try:
        output = _capability(request).resolve_result(project_id=_PROJECT_ID, result_id=result_id)
    except PptMasterInstallationError as error:
        return _response(503, {"message": _message(error)})
    except Exception:
        return _response(404, {"message": "presentation result is unavailable"})
    return FileResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename="presentation.pptx",
        headers={"Cache-Control": "no-store"},
    )


async def _body(request: Request) -> Mapping[str, object] | None:
    try:
        value = await request.json()
    except Exception:
        return None
    return value if isinstance(value, Mapping) else None
