import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from backend.shared.deployment import runtime_backup_roots
from core.storage_provider.vault_operational_recovery import load_vault_recovery_operation

from .runtime_recovery import load_runtime_recovery_operation, recovery_operation_kind
from .v2.policies import get


class RestorePreviewError(ValueError):
    pass


@dataclass
class _Preview:
    preview_id: str
    snapshot_id: str
    identity: object
    token: object
    deadline: float
    expires_at: str
    rollback_id: str | None = None
    operation_id: str | None = None
    timer: object = None
    confirming: bool = False
    settlement: dict | None = None


class RuntimeRestorePreviews:
    def __init__(self, application, *, root, namespace, identity_for):
        self.application, self.root, self.namespace = application, Path(root).resolve(), namespace
        self.identity_for = identity_for
        self.operations = runtime_backup_roots(self.root)[2]
        self.daily = application.state.memory_daily_jobs
        self.policy = get('restore_preview', version='@1')()
        self.current = None
        self._handed_off = {}
        application.router.on_shutdown.append(self.close)

    async def begin(self, request, snapshot_id):
        identity = self.identity_for(request)
        for preview in tuple(self._handed_off.values()):
            if await asyncio.to_thread(self._accepted, preview):
                raise RestorePreviewError('restore_preview_busy')
            self.daily.resume_maintenance(preview.token)
            self._handed_off.pop(preview.preview_id, None)
        if self.current is not None:
            if asyncio.get_running_loop().time() >= self.current.deadline:
                await self._release(self.current)
            if self.current is not None:
                raise RestorePreviewError('restore_preview_busy')
        lifetime = self.policy['lifetime_seconds']
        preview = _Preview('preview-' + uuid4().hex, snapshot_id, identity, object(),
            asyncio.get_running_loop().time() + lifetime,
            (datetime.now(timezone.utc) + timedelta(seconds=lifetime)).isoformat())
        self.current = preview
        preview.timer = asyncio.create_task(self._expire(preview))
        try:
            await asyncio.wait_for(self.daily.pause_maintenance(preview.token), self.policy['drain_seconds'])
            self._require(request, preview)
            return preview
        except TimeoutError:
            await self._release(preview)
            raise RestorePreviewError('restore_preview_busy') from None
        except BaseException:
            await self._release(preview)
            raise

    def _require(self, request, preview):
        if self.identity_for(request) != preview.identity:
            raise RestorePreviewError('restore_preview_identity_changed')
        if self.current is not preview or asyncio.get_running_loop().time() >= preview.deadline:
            raise RestorePreviewError('restore_preview_expired')

    def bind(self, request, preview, plan, operation_id):
        self._require(request, preview)
        if plan.target_snapshot_id != preview.snapshot_id:
            raise RestorePreviewError('restore_preview_identity_changed')
        preview.rollback_id, preview.operation_id = plan.rollback_id, operation_id
        return {'preview_id': preview.preview_id, 'preview_expires_at': preview.expires_at,
            'preview_lifetime_seconds': self.policy['lifetime_seconds']}

    async def confirm(self, request, snapshot_id, rollback_id):
        preview = self.current
        if preview is None:
            raise RestorePreviewError('restore_preview_expired')
        current = self.identity_for(request)
        if current != preview.identity:
            before, after = preview.identity[2], current[2]
            same_actor = (isinstance(before, dict) and isinstance(after, dict)
                and all(before[key] == after[key] for key in ('actor_user_id', 'actor_device_id', 'target_user_id')))
            if (same_actor and preview.identity[:2] == current[:2]
                    and preview.snapshot_id == snapshot_id and preview.rollback_id == rollback_id):
                await self._release(preview)
            raise RestorePreviewError('restore_plan_changed')
        if preview.confirming:
            raise RestorePreviewError('restore_confirmation_pending')
        self._require(request, preview)
        if preview.snapshot_id != snapshot_id or preview.rollback_id != rollback_id:
            raise RestorePreviewError('restore_preview_changed')
        # 人已确认后由实际准备操作接管暂停；断连清理仍须核对持久受理事实。
        preview.timer.cancel()
        preview.confirming = True
        return preview

    def _accepted(self, preview):
        if preview.operation_id is None:
            return False
        path = self.operations / (preview.operation_id + '.json')
        if not path.exists():
            return False
        kind = recovery_operation_kind(operations_root=self.operations, operation_id=preview.operation_id)
        operation = (load_runtime_recovery_operation if kind == 'runtime' else load_vault_recovery_operation)(
            operations_root=self.operations, operation_id=preview.operation_id)
        if Path(operation.snapshot_root).name != preview.snapshot_id:
            raise RestorePreviewError('restore_preview_identity_changed')
        return operation.state != 'failed' or getattr(operation, 'activation_state', 'pending') == 'failed'

    async def _release(self, preview, *, finished=False):
        if preview.settlement is not None:
            return preview.settlement
        if preview.confirming and not finished:
            return {'preview_id': preview.preview_id, 'status': 'confirmation_pending'}
        # 只结算本次预览。已准备或排队的恢复继续持有原维护暂停，迟到取消不能打开旧应用。
        accepted = await asyncio.to_thread(self._accepted, preview)
        # 真实日志读取让出了执行权；确认与另一次结算均可先完成状态接纳。
        if preview.settlement is not None:
            return preview.settlement
        if preview.confirming and not finished:
            return {'preview_id': preview.preview_id, 'status': 'confirmation_pending'}
        preview.settlement = {'preview_id': preview.preview_id, 'status': 'restore_accepted' if accepted else 'released'}
        if not accepted:
            self.daily.resume_maintenance(preview.token)
            self._handed_off.pop(preview.preview_id, None)
        else:
            self._handed_off[preview.preview_id] = preview
        if self.current is preview:
            self.current = None
        if preview.timer is not asyncio.current_task() and preview.timer is not None:
            preview.timer.cancel()
            await asyncio.gather(preview.timer, return_exceptions=True)
        return preview.settlement

    async def release(self, request, preview_id):
        identity = self.identity_for(request)
        preview = self.current if self.current is not None and self.current.preview_id == preview_id else self._handed_off.get(preview_id)
        if preview is None:
            return {'preview_id': preview_id, 'status': 'settled'}
        if identity != preview.identity:
            raise RestorePreviewError('restore_preview_identity_changed')
        return await self._release(preview, finished=preview.preview_id in self._handed_off)

    async def finish(self, preview):
        return await self._release(preview, finished=True)

    async def _expire(self, preview):
        await asyncio.sleep(max(0, preview.deadline - asyncio.get_running_loop().time()))
        await self._release(preview)

    async def close(self):
        if self.current is not None:
            await self._release(self.current, finished=True)
