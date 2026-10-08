from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.tokenhub_asr_provider import tokenhub_egress_manifest
from backend.security.provider_egress import ProviderEgressError, ProviderEgressPolicyStore
from core.product_core.cloud_asr_provider_settings import (
    TOKENHUB_ASR_SECRET_REF,
    CloudAsrProviderSettingsError,
    GetCloudAsrProviderSettings,
    SaveCloudAsrProviderSettings,
)


router = APIRouter(tags=["cloud-asr-settings"])
_PATH = "/api/rebuild/settings/cloud-asr-provider"


@router.get(_PATH)
async def get_cloud_asr_settings(container: ApiContainerDep) -> JSONResponse:
    return _response(200, _public(container))


@router.put(_PATH)
async def put_cloud_asr_settings(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, Mapping) or set(body) != {"enabled", "confirm_enable"}:
        return _response(400, {"detail": "cloud_asr_settings_invalid"})
    store, _settings = build_rebuild_object_store(Path(container.root_dir))
    try:
        SaveCloudAsrProviderSettings(
            store,
            now=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        ).execute(
            enabled=body.get("enabled") is True,
            confirm_enable=body.get("confirm_enable") is True,
        )
    except CloudAsrProviderSettingsError as error:
        return _response(409, {"detail": str(error)})
    return _response(200, _public(container))


@router.post(f"{_PATH}/egress-consent")
async def grant_cloud_asr_consent(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = None
    manifest = tokenhub_egress_manifest(Path(container.root_dir))
    if not isinstance(body, Mapping) or set(body) != {"manifest_id", "confirm"}:
        return _response(400, {"detail": "cloud_asr_consent_invalid"})
    try:
        ProviderEgressPolicyStore(Path(container.root_dir)).grant(
            manifest,
            manifest_id=str(body.get("manifest_id") or ""),
            confirm=body.get("confirm") is True,
        )
    except ProviderEgressError as error:
        return _response(409, {"detail": str(error)})
    return _response(200, _public(container))


@router.delete(f"{_PATH}/egress-consent")
async def revoke_cloud_asr_consent(container: ApiContainerDep) -> JSONResponse:
    ProviderEgressPolicyStore(Path(container.root_dir)).revoke("tokenhub-asr")
    return _response(200, _public(container))


def _public(container: object) -> dict[str, object]:
    root = Path(getattr(container, "root_dir"))
    store, _storage = build_rebuild_object_store(root)
    settings = GetCloudAsrProviderSettings(store).execute()
    manifest = tokenhub_egress_manifest(root)
    has_key = container.secret_store.has_secret(TOKENHUB_ASR_SECRET_REF)
    consented = ProviderEgressPolicyStore(root).is_consented(manifest)
    status = "disabled" if not settings.enabled else "needs_api_key" if not has_key else "needs_consent" if not consented else "ready"
    return {
        "status": status,
        "enabled": settings.enabled,
        "provider_id": settings.provider_id,
        "provider_name": settings.provider_name,
        "model": settings.model,
        "endpoint": settings.endpoint,
        "max_audio_bytes": settings.max_audio_bytes,
        "max_request_bytes": settings.max_request_bytes,
        "remote_processing": True,
        "settings_revision": settings.settings_revision,
        "has_api_key": has_key,
        "secret_generation": container.secret_store.get_generation(TOKENHUB_ASR_SECRET_REF),
        "egress_manifest": manifest.public_dict(consented=consented),
        "long_audio_mode": "cloud_overlap_chunks",
        "chunk_duration_seconds": 60,
        "chunk_overlap_seconds": 5,
        "memory_publication": "not_started",
    }


def _response(status: int, body: Mapping[str, object]) -> JSONResponse:
    return JSONResponse(status_code=status, content=dict(body), headers={"Cache-Control": "no-store"})
