"""Narrow HTTP facade for governed external-extension installation.

The route deliberately accepts only user intent and an immutable preview
reference.  Core Effect, Gate and revision identities are derived by the
server-side workflow and must never become HTTP parameters.
"""

from __future__ import annotations

from backend.security.device_identity import server_mode, server_identity

from collections.abc import Iterable
import ipaddress
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.api.external_extension_install_workflow import (
    ExternalExtensionInstallConfirmation,
    ExternalExtensionInstallPreview,
    ExternalExtensionInstallWorkflowError,
)
from backend.api.external_extension_mcp_import import ExternalExtensionMCPImportError
from backend.security.mcp_approved_server_migration import (
    MCPApprovedServerMigrationConflict,
    MCPApprovedServerMigrationError,
)
from backend.api.desktop_session import (
    DESKTOP_SESSION_HEADER,
    desktop_session,
    desktop_session_authorized,
)
from core.external_extension_runtime.fact_store import (
    ExternalExtensionFactConflict,
    ExternalExtensionFactError,
)
from core.external_extension_runtime.installation import (
    ExternalExtensionInstallationConflict,
    ExternalExtensionInstallationError,
)
from core.external_extension_runtime.gate_authority import (
    ExternalExtensionGateAuthorizationError,
)
from core.external_extension_runtime.lifecycle_commands import (
    ExternalExtensionLifecycleCommandConflict,
    ExternalExtensionLifecycleCommandError,
)


router = APIRouter(prefix="/api/rebuild/external-extensions", tags=["external-extensions"])


class ExternalExtensionPreviewRequest(BaseModel):
    """Caller-controlled request fields for parsing and quarantining a source."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=12_000)
    project_id: str = Field(min_length=2, max_length=160)
    requested_ref: str | None = Field(default=None, min_length=1, max_length=256)
    subpath: str | None = Field(default=None, min_length=1, max_length=512)


class ExternalExtensionConfirmRequest(BaseModel):
    """Caller-controlled acknowledgement for one immutable preview only."""

    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(min_length=1, max_length=512)
    project_id: str = Field(min_length=2, max_length=160)
    confirmations: tuple[str, ...] = Field(max_length=64)
    expected_state_revision: int = Field(default=0, ge=0)
    reason: str = Field(
        default="Reviewed the quarantined external extension.",
        min_length=1,
        max_length=1_000,
    )


class ExternalExtensionLifecycleRequest(BaseModel):
    """Authenticated, CAS-bound user intent for a reversible lifecycle action."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=2, max_length=160)
    action: str = Field(pattern="^(disable|rollback|uninstall)$")
    expected_state_revision: int = Field(ge=0)
    target_revision_ref: str | None = Field(default=None, min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_target(self):
        if self.action == "rollback" and self.target_revision_ref is None:
            raise ValueError("rollback requires an exact target revision")
        if self.action in {"disable", "uninstall"} and self.target_revision_ref is not None:
            raise ValueError(f"{self.action} does not accept a target revision")
        return self


class ExternalExtensionUpgradeRequest(BaseModel):
    """Exact reviewed intake selected for a project installation upgrade."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=2, max_length=160)
    intake_ref: str = Field(min_length=1, max_length=512)
    confirmations: tuple[str, ...] = Field(max_length=64)
    expected_state_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=1_000)


class ExternalExtensionMCPImportPreviewRequest(BaseModel):
    """Reviewed disabled MCP authority candidate bound to one frozen intake."""

    model_config = ConfigDict(extra="forbid")

    intake_ref: str = Field(min_length=1, max_length=512)
    project_id: str = Field(min_length=2, max_length=160)
    reviewed_candidate: dict[str, Any]
    confirmations: tuple[str, ...] = Field(max_length=64)
    reason: str = Field(
        default="Reviewed the disabled MCP server candidate.",
        min_length=1,
        max_length=1_000,
    )


@router.post("/install/preview")
def preview_external_extension_install(
    body: ExternalExtensionPreviewRequest,
    request: Request,
) -> dict[str, object]:
    _request_actor_or_403(request)
    workflow = _workflow_or_503(request)
    try:
        preview = workflow.preview(
            body.prompt,
            body.project_id,
            requested_ref=body.requested_ref,
            subpath=body.subpath,
        )
    except _WORKFLOW_ERRORS as error:
        _raise_workflow_error(error)
    return _preview_projection(preview)


@router.post("/install/confirm")
def confirm_external_extension_install(
    body: ExternalExtensionConfirmRequest,
    request: Request,
) -> dict[str, object]:
    actor = _request_actor_or_403(request)
    workflow = _workflow_or_503(request)
    try:
        confirmation = workflow.confirm(
            body.preview_id,
            project_id=body.project_id,
            confirmations=body.confirmations,
            expected_state_revision=body.expected_state_revision,
            actor=actor,
            reason=body.reason,
        )
    except _WORKFLOW_ERRORS as error:
        _raise_workflow_error(error)
    return _confirmation_projection(confirmation)


@router.post("/install/mcp/preview")
def preview_external_extension_mcp_import(
    body: ExternalExtensionMCPImportPreviewRequest,
    request: Request,
) -> dict[str, object]:
    actor = _request_actor_or_403(request)
    workflow = _mcp_workflow_or_503(request)
    try:
        preview = workflow.preview_mcp_import(
            intake_ref=body.intake_ref,
            project_id=body.project_id,
            reviewed_candidate=body.reviewed_candidate,
            confirmations=body.confirmations,
            actor=actor,
            reason=body.reason,
        )
    except _WORKFLOW_ERRORS as error:
        _raise_workflow_error(error)
    return {
        "status": preview.state,
        "review_receipt_ref": preview.review_receipt_ref,
        "migration_id": preview.migration_id,
        "migration_revision": preview.migration_revision,
        "active_snapshot_revision": preview.active_snapshot_revision,
        "server_ids": list(preview.server_ids),
    }


@router.get("/installations/{extension_id}")
def read_external_extension_installation(
    extension_id: str,
    project_id: str,
    request: Request,
) -> dict[str, object]:
    _request_actor_or_403(request)
    workflow = _workflow_or_503(request)
    try:
        snapshot, history = workflow.installation_status(
            extension_id, project_id=project_id,
        )
    except _WORKFLOW_ERRORS as error:
        _raise_workflow_error(error)
    return {
        "extension_id": snapshot.extension_id,
        "status": snapshot.status,
        "state_revision": snapshot.state_revision,
        "active_revision_ref": snapshot.active_revision_ref,
        "candidate_revision_ref": snapshot.candidate_revision_ref,
        "revisions": [
            {
                "revision": item.revision,
                "revision_ref": item.revision_ref,
                "intake_ref": item.intake_ref,
                "artifact_receipt_ref": item.artifact_receipt_ref,
                "review_confirmation_ref": item.review_confirmation_ref,
                "projection": item.projection,
                "health_verified": item.health_verified,
                "active": item.active,
                "candidate": item.candidate,
            }
            for item in history
        ],
    }


@router.post("/installations/{extension_id}/lifecycle")
def execute_external_extension_lifecycle(
    extension_id: str,
    body: ExternalExtensionLifecycleRequest,
    request: Request,
) -> dict[str, object]:
    actor = _request_actor_or_403(request)
    workflow = _workflow_or_503(request)
    try:
        result = workflow.execute_lifecycle_action(
            extension_id,
            project_id=body.project_id,
            action=body.action,
            expected_state_revision=body.expected_state_revision,
            actor=actor,
            reason=body.reason,
            target_revision_ref=body.target_revision_ref,
        )
    except _WORKFLOW_ERRORS as error:
        _raise_workflow_error(error)
    snapshot = result.snapshot
    return {
        "command_id": result.command_id,
        "action": body.action,
        "effect": _effects_projection((result.effect,))[0],
        "installation": None if snapshot is None else {
            "extension_id": snapshot.extension_id,
            "status": snapshot.status,
            "state_revision": snapshot.state_revision,
            "active_revision_ref": snapshot.active_revision_ref,
        },
    }


@router.post("/installations/{extension_id}/upgrade")
def upgrade_external_extension(
    extension_id: str,
    body: ExternalExtensionUpgradeRequest,
    request: Request,
) -> dict[str, object]:
    actor = _request_actor_or_403(request)
    workflow = _workflow_or_503(request)
    try:
        outcome = workflow.upgrade_from_intake(
            extension_id,
            intake_ref=body.intake_ref,
            project_id=body.project_id,
            confirmations=body.confirmations,
            expected_state_revision=body.expected_state_revision,
            actor=actor,
            reason=body.reason,
        )
    except _WORKFLOW_ERRORS as error:
        _raise_workflow_error(error)
    return _confirmation_projection(outcome)


_WORKFLOW_ERRORS = (
    ExternalExtensionInstallWorkflowError,
    ExternalExtensionFactError,
    ExternalExtensionInstallationError,
    ExternalExtensionLifecycleCommandError,
    ExternalExtensionGateAuthorizationError,
    ExternalExtensionMCPImportError,
    MCPApprovedServerMigrationError,
)
_CONFLICT_ERRORS = (
    ExternalExtensionFactConflict,
    ExternalExtensionInstallationConflict,
    ExternalExtensionLifecycleCommandConflict,
    MCPApprovedServerMigrationConflict,
)


def _workflow_or_503(request: Request) -> Any:
    workflow = getattr(request.app.state, "external_extension_install_workflow", None)
    if (
        workflow is None
        or not callable(getattr(workflow, "preview", None))
        or not callable(getattr(workflow, "confirm", None))
        or not callable(getattr(workflow, "installation_status", None))
        or not callable(getattr(workflow, "execute_lifecycle_action", None))
        or not callable(getattr(workflow, "upgrade_from_intake", None))
    ):
        raise HTTPException(
            status_code=503,
            detail="external extension installation runtime is unavailable",
        )
    return workflow


def _mcp_workflow_or_503(request: Request) -> Any:
    workflow = _workflow_or_503(request)
    if not callable(getattr(workflow, "preview_mcp_import", None)):
        raise HTTPException(
            status_code=503,
            detail="external extension MCP import runtime is unavailable",
        )
    return workflow


def _request_actor_or_403(request: Request) -> str:
    """Return the authoritative local principal for a governance mutation.

    This is a single-user desktop product: an authenticated desktop session is
    the authority for the local workspace's project namespaces.  A project id
    remains an operation scope (and is rechecked by the immutable workflow
    facts), never an assertion of a remote caller's identity.  Development
    retains the established loopback-only principal so local tests and the
    non-desktop product mode do not silently become internet-facing.
    """

    if server_mode(request):
        identity = server_identity(request)
        if identity is None:
            raise HTTPException(status_code=401, detail='device_unauthorized')
        return f'device:{identity.device_id}'
    host = request.client.host if request.client is not None else ""
    if not _is_loopback(host):
        raise HTTPException(status_code=403, detail="local desktop session required")
    try:
        session = desktop_session()
    except RuntimeError as error:
        raise HTTPException(
            status_code=503,
            detail="desktop session configuration is unavailable",
        ) from error
    if session is None:
        return "local-development-session"
    if not desktop_session_authorized(request.headers.get(DESKTOP_SESSION_HEADER)):
        raise HTTPException(status_code=403, detail="local desktop session required")
    return f"desktop:{session.instance_id}"


def _is_loopback(host: str) -> bool:
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _raise_workflow_error(error: Exception) -> None:
    """Map immutable-reference absence separately from state/review conflicts."""

    message = str(error)
    if isinstance(error, _CONFLICT_ERRORS):
        raise HTTPException(status_code=409, detail=message) from error
    normalized = message.lower()
    if any(token in normalized for token in ("not found", "is missing", "does not belong")):
        # Cross-project references intentionally use the same response as absent
        # references, so the endpoint does not disclose another project's intake.
        raise HTTPException(status_code=404, detail="external extension preview is unavailable") from error
    raise HTTPException(status_code=409, detail=message) from error


def _effects_projection(effects: Iterable[object]) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for effect in effects:
        state = getattr(effect, "state", None)
        values.append(
            {
                "operation_id": getattr(effect, "operation_id", None),
                "state": getattr(state, "value", state),
                "receipt_ref": getattr(effect, "receipt_ref", None),
            }
        )
    return values


def _preview_projection(preview: ExternalExtensionInstallPreview) -> dict[str, object]:
    return {
        "preview_id": preview.preview_id,
        "intake_ref": preview.intake_ref,
        "status": preview.status,
        "risks": list(preview.risks),
        "confirmation_ids": list(preview.confirmation_ids),
        "extension_id": preview.extension_id,
        "effects": _effects_projection(preview.effects),
    }


def _confirmation_projection(
    confirmation: ExternalExtensionInstallConfirmation,
) -> dict[str, object]:
    snapshot = confirmation.snapshot
    snapshot_projection = None
    if snapshot is not None:
        snapshot_projection = {
            "extension_id": snapshot.extension_id,
            "status": snapshot.status,
            "state_revision": snapshot.state_revision,
            "active_revision_ref": snapshot.active_revision_ref,
        }
    return {
        "preview_id": confirmation.preview_id,
        "intake_ref": confirmation.intake_ref,
        "status": confirmation.status,
        "pending_action": confirmation.pending_action,
        "resolved_revision": confirmation.resolved_revision,
        "installation": snapshot_projection,
        "effects": _effects_projection(confirmation.effects),
    }
