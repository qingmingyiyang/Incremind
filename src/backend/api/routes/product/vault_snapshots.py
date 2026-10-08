"""Vault snapshots ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from backend.api.container import ApiContainerDep

from core.product_core.local_memory_vault import (
    CreateMemorySnapshot,
    ExportMemoryAssetPackage,
    ListMemorySnapshots,
    MemorySnapshotError,
    PrepareMemoryRestoreConfirmation,
    RollbackToMemorySnapshot,
    serialize_memory_asset_package,
    serialize_memory_restore_confirmation,
    serialize_memory_rollback_plan,
    serialize_memory_snapshot,
    serialize_memory_snapshot_summary,
)
from core.storage_provider import (
    JsonObjectStore,
    VaultBackupRestoreError,
    VaultOperationalRecoveryError,
    create_vault_backup,
    prepare_vault_recovery,
    verify_vault_backup,
)

from core.storage_provider.vault_backup_restore import fingerprint_vault_restore_source

from . import http as product_http
from . import recovery_points as product_recovery_points
from . import repositories as product_repositories
from core.storage_provider.vault_backup_restore import verification_metadata

router = APIRouter(tags=["rebuild-product-core"])


def _snapshot_record_for_restore(
    *, store: JsonObjectStore, snapshots_root: Path, snapshot_id: str
) -> Mapping[str, object] | None:
    external = product_recovery_points._recovery_point_record(snapshots_root / snapshot_id)
    if external is not None:
        return external
    stored = store.read("memory_snapshots", snapshot_id)
    if isinstance(stored, Mapping) and stored.get("restorable") is True:
        raise MemorySnapshotError(
            "full recovery point payload or catalog is missing"
        )
    return stored


@router.post("/api/rebuild/memory-assets/export")
def export_memory_assets(request: Request, container: ApiContainerDep) -> JSONResponse:
    """阶段 6：导出私人记忆资产包。

    返回 manifest + 各层 JSON（已脱敏），不含 secret/cookie/api_key。
    """
    store, settings = product_repositories._object_store(container.root_dir)
    vault_path = str(container.root_dir / ".rebuild-data")
    use_case = ExportMemoryAssetPackage(
        store,
        namespace_id=settings.namespace_id,
        vault_path=vault_path,
    )
    try:
        result = use_case.execute()
        body = serialize_memory_asset_package(result)
        return product_http._json_response(200, body, product_http._no_store_headers())
    except Exception as error:  # noqa: BLE001
        return product_http._json_response(400, {"detail": str(error)}, product_http._no_store_headers())


def _snapshot_size(root: Path) -> int | None:
    """Read file lengths only; missing or inaccessible snapshots remain unknown."""
    try:
        if not root.is_dir() or root.is_symlink():
            return None
        total = 0
        for path in root.rglob('*'):
            if path.is_symlink():
                return None
            if path.is_file():
                total += path.stat().st_size
        return total
    except OSError:
        return None


@router.get("/api/rebuild/memory-snapshots")
def list_memory_snapshots(request: Request, container: ApiContainerDep) -> JSONResponse:
    """阶段 6：列出所有记忆快照。"""
    store, _settings = product_repositories._object_store(container.root_dir)
    use_case = ListMemorySnapshots(store)
    _active_root, snapshots_root, _operations_root = product_recovery_points._vault_recovery_roots(
        container.root_dir
    )
    external_records = product_recovery_points._external_recovery_point_records(snapshots_root)
    result = use_case.execute(
        additional_records=external_records,
        restorable_record_ids=frozenset(str(record["id"]) for record in external_records),
    )
    body = {
        "snapshots": [
            {**serialize_memory_snapshot_summary(s), "size_bytes": _snapshot_size(snapshots_root / s.snapshot_id),
             **_verification_view(snapshots_root / s.snapshot_id)} for s in result
        ],
    }
    return product_http._json_response(200, body, product_http._no_store_headers())


@router.post("/api/rebuild/memory-snapshots")
async def create_memory_snapshot(request: Request, container: ApiContainerDep) -> JSONResponse:
    """Create an immutable full-Vault recovery point plus its product projection."""
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    label = str(body.get("label", "") or "").strip()
    notes = str(body.get("notes", "") or "").strip()
    snapshot_id = f"snap-{time.time_ns():x}"
    active_root, snapshots_root, operations_root = product_recovery_points._vault_recovery_roots(container.root_dir)
    use_case = CreateMemorySnapshot(
        store,
        namespace_id=settings.namespace_id,
    )
    try:
        backup = await run_in_threadpool(create_vault_backup,
            source_root=active_root,
            backups_root=snapshots_root,
            snapshot_id=snapshot_id,
            sqlite_online=True, exclude_logs=True, restore_verify=True,
        )
        result = use_case.execute(
            label=label,
            notes=notes,
            snapshot_id=snapshot_id,
            vault_fingerprint=backup.source_fingerprint,
            backup_file_count=backup.file_count,
        )
        record = serialize_memory_snapshot(result)
        product_recovery_points._write_recovery_point_record(
            backup.snapshot_root,
            {
                "schema_version": "2.0.0",
                "id": result.snapshot_id,
                "created_at": result.created_at,
                "label": result.label,
                "namespace_id": result.namespace_id,
                "layer_fingerprints": dict(result.layer_fingerprints),
                "layer_counts": dict(result.layer_counts),
                "notes": result.notes,
                "restorable": result.restorable,
                "vault_fingerprint": result.vault_fingerprint,
                "backup_file_count": result.backup_file_count,
            },
        )
        return product_http._json_response(
            200, {**record, **_verification_view(backup.snapshot_root)}, product_http._no_store_headers()
        )
    except (OSError, MemorySnapshotError, VaultBackupRestoreError) as error:
        owner = getattr(request.app.state, 'memory_backup', None)
        if owner is not None:
            owner.fail(snapshot_id, automatic=False, reason_code=getattr(error, 'reason_code', 'backup_failed'))
        return product_http._json_response(400, {"detail": getattr(error, "reason_code", "backup_failed")}, product_http._no_store_headers())


def _verification_view(snapshot):
    try:
        value = verification_metadata(snapshot)
    except (OSError, ValueError):
        return {'verified': False, 'verification_reason': 'backup_verification_failed'}
    if 'verified' not in value:
        return {'verified': None, 'verification_reason': None}
    return {'verified': value['verified'] is True, 'verification_reason':
        None if value['verified'] is True else 'backup_verification_failed'}


@router.post("/api/rebuild/memory-snapshots/{snapshot_id}/rollback-plan")
def create_rollback_plan(
    request: Request,
    container: ApiContainerDep,
    snapshot_id: str,
) -> JSONResponse:
    """阶段 6：准备整库回滚计划（需要用户二次确认）。"""
    store, settings = product_repositories._object_store(container.root_dir)
    use_case = RollbackToMemorySnapshot(
        store,
        namespace_id=settings.namespace_id,
    )
    try:
        active_root, snapshots_root, _operations_root = product_recovery_points._vault_recovery_roots(
            container.root_dir
        )
        result = use_case.execute(
            target_snapshot_id=snapshot_id,
            current_vault_fingerprint=fingerprint_vault_restore_source(active_root),
            snapshot_record=_snapshot_record_for_restore(
                store=store,
                snapshots_root=snapshots_root,
                snapshot_id=snapshot_id,
            ),
        )
        if not result.target_vault_fingerprint:
            raise MemorySnapshotError(
                "legacy manifest-only snapshot cannot restore Vault contents"
            )
        return product_http._json_response(
            200, serialize_memory_rollback_plan(result), product_http._no_store_headers()
        )
    except (MemorySnapshotError, VaultBackupRestoreError) as error:
        return product_http._json_response(400, {"detail": str(error)}, product_http._no_store_headers())


@router.post("/api/rebuild/memory-snapshots/{snapshot_id}/rollback")
async def prepare_restore_confirmation(
    request: Request,
    container: ApiContainerDep,
    snapshot_id: str,
) -> JSONResponse:
    """Validate confirmation and prepare a verified offline Vault adoption."""
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    rollback_id = str(body.get("rollback_id", "") or "").strip()
    confirm = bool(body.get("confirm", False))
    try:
        active_root, snapshots_root, operations_root = product_recovery_points._vault_recovery_roots(
            container.root_dir
        )
        plan = RollbackToMemorySnapshot(
            store,
            namespace_id=settings.namespace_id,
        ).execute(
            target_snapshot_id=snapshot_id,
            current_vault_fingerprint=fingerprint_vault_restore_source(
                active_root
            ),
            snapshot_record=_snapshot_record_for_restore(
                store=store,
                snapshots_root=snapshots_root,
                snapshot_id=snapshot_id,
            ),
        )
        if not plan.target_vault_fingerprint:
            raise MemorySnapshotError(
                "legacy manifest-only snapshot cannot restore Vault contents"
            )
        result = PrepareMemoryRestoreConfirmation().execute(
            target_snapshot_id=snapshot_id,
            rollback_id=rollback_id,
            expected_rollback_id=plan.rollback_id,
            confirm=confirm,
        )
        if result.status != "blocked_pre_restore_only":
            return product_http._json_response(
                200, serialize_memory_restore_confirmation(result), product_http._no_store_headers()
            )
        if fingerprint_vault_restore_source(active_root) != plan.current_vault_fingerprint:
            raise MemorySnapshotError("Vault changed after rollback plan; create a new plan")
        operation_id = f"restore-{rollback_id.removeprefix('rb-')}"
        operation = prepare_vault_recovery(
            snapshot_root=snapshots_root / snapshot_id,
            active_root=active_root,
            operations_root=operations_root,
            operation_id=operation_id,
            expected_source_fingerprint=plan.current_vault_fingerprint,
        )
        return product_http._json_response(
            202,
            {
                "rollback_id": rollback_id,
                "target_snapshot_id": snapshot_id,
                "status": "prepared_restart_required",
                "restore_executed": False,
                "requires_confirmation": False,
                "confirmation_received": True,
                "operation_id": operation.operation_id,
                "prepared_file_count": operation.file_count,
                "reason": "恢复内容已在隔离目录完成校验，应用重启后离线切换 Vault。",
                "next_step": "确认系统重启提示；切换完成后应用会从所选恢复点重新启动。",
            },
            product_http._no_store_headers(),
        )
    except (MemorySnapshotError, VaultBackupRestoreError, VaultOperationalRecoveryError) as error:
        return product_http._json_response(400, {"detail": str(error)}, product_http._no_store_headers())
