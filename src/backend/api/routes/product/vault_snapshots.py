"""Vault snapshots ownership for the product API."""
from __future__ import annotations

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
    serialize_memory_asset_package,
    serialize_memory_restore_confirmation,
    serialize_memory_rollback_plan,
    serialize_memory_snapshot,
    serialize_memory_snapshot_summary,
)
from core.storage_provider import (
    VaultBackupRestoreError,
    VaultOperationalRecoveryError,
    prepare_vault_recovery,
)

from . import http as product_http
from . import recovery_points as product_recovery_points
from . import repositories as product_repositories
from core.storage_provider.vault_backup_restore import verification_metadata
from backend.memory_app.backup import backup_runtime
from backend.memory_app.runtime_recovery import (
    build_restore_plan,
    ensure_no_pending_recovery,
    prepare_runtime_recovery,
    runtime_recovery_summary,
    server_restore_operation_id,
)
from backend.memory_app.runtime_restore_preview import RuntimeRestorePreviews, RestorePreviewError

router = APIRouter(tags=["rebuild-product-core"])


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
    _active_root, snapshots_root, operations_root = product_recovery_points._vault_recovery_roots(
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
        "restore_operations": runtime_recovery_summary(operations_root=operations_root),
    }
    return product_http._json_response(200, body, product_http._no_store_headers())


@router.post("/api/rebuild/memory-snapshots")
async def create_memory_snapshot(request: Request, container: ApiContainerDep) -> JSONResponse:
    """创建完整运行根快照，沿用现有备份列表与产品投影。"""
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    label = str(body.get("label", "") or "").strip()
    notes = str(body.get("notes", "") or "").strip()
    snapshot_id = f"snap-{time.time_ns():x}"
    _active_root, snapshots_root, _operations_root = product_recovery_points._vault_recovery_roots(container.root_dir)
    use_case = CreateMemorySnapshot(
        store,
        namespace_id=settings.namespace_id,
    )
    try:
        backup = await run_in_threadpool(backup_runtime,
            source=container.root_dir,
            backups=snapshots_root,
            snapshot_id=snapshot_id,
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


def _restore_plan(container, snapshot_id):
    store, settings = product_repositories._object_store(container.root_dir)
    return build_restore_plan(container.root_dir, snapshot_id, store=store, namespace_id=settings.namespace_id)


def _restore_operation_id(request, plan):
    context = getattr(request.app.state, 'server_context', None)
    if context is None:
        return f"restore-{plan.rollback_id.removeprefix('rb-')}"
    authority = context.restores.confirmation_identity(request)
    request.scope.setdefault('state', {})['runtime_restore_authority'] = authority
    return server_restore_operation_id(plan.rollback_id, authority)


def _preview_owner(request, container):
    # 旧 Vault 应用没有每日维护 owner，保留其原确认接口。
    if not hasattr(request.app.state, 'memory_daily_jobs'):
        return None
    owner = getattr(request.app.state, 'runtime_restore_previews', None)
    if owner is not None:
        if owner.root != Path(container.root_dir).resolve():
            raise RestorePreviewError('restore_preview_identity_changed')
        return owner
    _store, settings = product_repositories._object_store(container.root_dir)
    application = request.app

    def identity_for(current):
        if current.app is not application or application.state.container is not container:
            raise RestorePreviewError('restore_preview_identity_changed')
        transfers = getattr(application.state, 'memory_backup_transfers', None)
        if transfers is not None:
            transfers.authorize(current)
        context = getattr(application.state, 'server_context', None)
        if context is not None:
            identity = context.restores.confirmation_identity(current)
        else:
            from backend.api.desktop_session import desktop_session, current_desktop_session
            from backend.api.worker_auth import worker_config
            session, live = desktop_session(), current_desktop_session()
            if session != live:
                raise RestorePreviewError('restore_preview_identity_changed')
            config = worker_config()
            identity = ('desktop', session, config[1] if config is not None else 'local-runtime')
        return (str(Path(container.root_dir).resolve()), settings.namespace_id, identity)

    owner = RuntimeRestorePreviews(application, root=container.root_dir,
        namespace=settings.namespace_id, identity_for=identity_for)
    application.state.runtime_restore_previews = owner
    return owner


@router.post("/api/rebuild/memory-snapshots/{snapshot_id}/rollback-plan")
async def create_rollback_plan(
    request: Request,
    container: ApiContainerDep,
    snapshot_id: str,
) -> JSONResponse:
    """阶段 6：准备整库回滚计划（需要用户二次确认）。"""
    owner, preview = _preview_owner(request, container), None
    try:
        preview = await owner.begin(request, snapshot_id) if owner is not None else None
        result, runtime, _active, _snapshots, _operations = await run_in_threadpool(_restore_plan, container, snapshot_id)
        operation_id = _restore_operation_id(request, result)
        lease = owner.bind(request, preview, result, operation_id) if owner is not None else {}
        return product_http._json_response(
            200, {**serialize_memory_rollback_plan(result), "restore_kind": "runtime" if runtime else "vault",
                  "operation_id": operation_id, **lease}, product_http._no_store_headers()
        )
    except RestorePreviewError as error:
        if preview is not None:
            await owner.finish(preview)
        return product_http._json_response(409, {'detail': str(error)}, product_http._no_store_headers())
    except (MemorySnapshotError, VaultBackupRestoreError) as error:
        if preview is not None:
            await owner.finish(preview)
        return product_http._json_response(400, {"detail": str(error)}, product_http._no_store_headers())
    except BaseException:
        if preview is not None:
            await owner.finish(preview)
        raise


@router.post("/api/rebuild/memory-snapshots/{snapshot_id}/rollback")
async def prepare_restore_confirmation(
    request: Request,
    container: ApiContainerDep,
    snapshot_id: str,
) -> JSONResponse:
    """Validate confirmation and prepare a verified offline Vault adoption."""
    body = await product_http._json_body(request) or {}
    rollback_id = str(body.get("rollback_id", "") or "").strip()
    confirm = body.get("confirm") is True
    owner = getattr(request.app.state, 'runtime_restore_previews', None)
    preview = None
    try:
        if owner is not None:
            preview = await owner.confirm(request, snapshot_id, rollback_id)
        plan, runtime, active_root, snapshots_root, operations_root = await run_in_threadpool(_restore_plan, container, snapshot_id)
        operation_id = _restore_operation_id(request, plan)
        if getattr(request.app.state, 'server_context', None) is not None:
            if ((preview is not None and preview.operation_id != operation_id)
                    or (body.get('execute') is True and body.get('operation_id') != operation_id)):
                raise MemorySnapshotError('restore_plan_changed')
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
        ensure_no_pending_recovery(operations_root=operations_root, operation_id=operation_id)
        authorization = {"server_identity": request.scope.get("state", {}).get("runtime_restore_authority")} if runtime else {}
        operation = await run_in_threadpool(
            prepare_runtime_recovery if runtime else prepare_vault_recovery,
            snapshot_root=snapshots_root / snapshot_id,
            active_root=container.root_dir if runtime else active_root,
            operations_root=operations_root, operation_id=operation_id,
            expected_source_fingerprint=plan.current_vault_fingerprint,
            **authorization,
        )
        queued = False
        if runtime and body.get("execute") is True:
            host = getattr(request.app.state, "runtime_restore_host", None)
            if host is None:
                raise VaultOperationalRecoveryError("runtime_restore_host_unavailable")
            await host.request_execution(operation.operation_id, request=request)
            queued = True
        return product_http._json_response(
            202,
            {
                "rollback_id": rollback_id,
                "target_snapshot_id": snapshot_id,
                "status": "restore_queued" if queued else "prepared_restart_required",
                "restore_executed": False,
                "requires_confirmation": False,
                "confirmation_received": True,
                "operation_id": operation.operation_id,
                "prepared_file_count": operation.file_count,
                "restore_kind": "runtime" if runtime else "vault",
                "reason": "恢复内容已在隔离目录完成校验，尚未采用。",
                "next_step": "桌面应用确认重启后离线采用；网页需由运行宿主停止应用并采用恢复操作。",
            },
            product_http._no_store_headers(),
        )
    except RestorePreviewError as error:
        return product_http._json_response(400 if str(error) == 'restore_plan_changed' else 409,
            {'detail': str(error)}, product_http._no_store_headers())
    except (MemorySnapshotError, VaultBackupRestoreError, VaultOperationalRecoveryError) as error:
        return product_http._json_response(400, {"detail": str(error)}, product_http._no_store_headers())
    finally:
        if preview is not None:
            await owner.finish(preview)
