"""Vault status ownership for the product API."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep

from core.product_core.local_memory_vault import (
    GetVaultStatus,
    VaultStatusError,
    serialize_vault_status,
)
from core.storage_provider import RebuildStorageSettings

from . import http as product_http
from . import recovery_points as product_recovery_points
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.get("/api/rebuild/vault-status")
def vault_status(request: Request, container: ApiContainerDep) -> JSONResponse:
    """阶段 6：本地记忆资产库 Vault 状态。

    返回 vault 路径（脱敏）、原始资料数量、记忆资产层统计、
    索引状态、备份就绪、存储占用估算。
    """
    store, settings = product_repositories._object_store(container.root_dir)
    vault_path = str(container.root_dir / ".rebuild-data")
    app_data_dir = _platform_app_data_dir(settings)
    target_vault_path = (
        str(app_data_dir / "vault" / ".rebuild-data")
        if app_data_dir is not None
        else ""
    )
    _active_root, snapshots_root, _operations_root = product_recovery_points._vault_recovery_roots(
        container.root_dir
    )
    use_case = GetVaultStatus(
        store,
        namespace_id=settings.namespace_id,
        vault_path=vault_path,
        app_data_dir_path=str(app_data_dir) if app_data_dir is not None else "",
        target_vault_path=target_vault_path,
        app_root_uri=settings.app_root_uri,
        root_uri=settings.root_uri,
        storage_version=settings.storage_version,
        backup_enabled=settings.backup_enabled,
        backup_retention_count=settings.backup_retention_count,
        backup_ready=settings.backup_ready,
        backup_destination_path=str(snapshots_root),
        backup_destination_uri=f"{settings.root_uri}backups/",
        pre_restore_required=settings.pre_restore_required,
    )
    try:
        result = use_case.execute()
        body = serialize_vault_status(result)
        return product_http._json_response(200, body, product_http._no_store_headers())
    except VaultStatusError as error:
        return product_http._json_response(400, {"detail": str(error)}, product_http._no_store_headers())


def _platform_app_data_dir(settings: RebuildStorageSettings) -> Path | None:
    desktop_user_data = os.environ.get("CHRIPTMAS_COMPANION_USER_DATA_ROOT", "").strip()
    if desktop_user_data:
        return Path(desktop_user_data).expanduser().resolve(strict=False)
    app_slug = settings.app_root_uri.removeprefix("platform-app-data://")
    if not app_slug:
        return None
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        return None
    folder_name = "".join(
        part.capitalize() for part in app_slug.replace("_", "-").split("-") if part
    ) or "ChriptmasReplay"
    return (Path(base) / folder_name).resolve(strict=False)
