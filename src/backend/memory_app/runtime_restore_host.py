from __future__ import annotations

import asyncio
import gc
from contextlib import asynccontextmanager
from contextvars import Context
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketDisconnect

from backend.shared.deployment import runtime_backup_roots
from backend.shared.runtime_logging import close_runtime_logging
from core.storage_provider.vault_operational_recovery import VaultOperationalRecoveryError

from .runtime_recovery import (
    adopt_prepared_runtime,
    load_runtime_recovery_operation,
    queue_runtime_recovery,
    record_runtime_activation,
    runtime_recovery_summary,
)


class _SingleRuntimeRoot:
    def __init__(self, root: Path):
        self.root = root

    def root_for(self, user_id):
        if user_id != 'local-runtime':
            raise ValueError('runtime_identity_invalid')
        return self.root

    def deletion_pending(self, user_id):
        self.root_for(user_id)
        return False


class RuntimeRestoreHost:
    def __init__(self, *, root: Path, factory):
        from backend.vault_recovery_cli import _reconcile_dead_lease, _recover_pending
        self.root = Path(root).resolve()
        self.working, _snapshots, self.operations = runtime_backup_roots(self.root)
        # 先续接恢复，再允许任何真实工厂初始化固定运行目录。
        _reconcile_dead_lease(self.operations, self.working)
        _recover_pending(self.operations, self.working)
        self.factory, self.port = factory, None
        self._initial = factory()
        self._current = self._initial
        self._active, self._reservation, self._execution = 0, None, None
        self._unavailable, self._closing = False, False
        self._failure = None
        self._status_failure = None
        self._rebuilding = False
        self._drained = asyncio.Event()
        self._socket_close = asyncio.Event()
        self._drained.set()
        from .server_runtime import UserApplicationPool
        self.pool = UserApplicationPool(_SingleRuntimeRoot(self.root), factory=self._compose)
        self.application = FastAPI(lifespan=self._lifetime)
        self.application.state.deployment = self._initial.state.deployment
        from backend.api.app import create_worker_auth_middleware
        create_worker_auth_middleware(self.application)
        for middleware in self._initial.user_middleware:
            if middleware.cls is CORSMiddleware:
                self.application.add_middleware(middleware.cls, *middleware.args, **middleware.kwargs)
        self.application.add_api_route('/api/rebuild/memory-restores/{operation_id}', self._status, methods=['GET'])
        self.application.add_api_route('/api/rebuild/memory-restores/{operation_id}/resume', self._resume, methods=['POST'])
        self.application.mount('/', self._dispatch)

    @property
    def state(self):
        if self._current is None:
            raise RuntimeError('runtime_restore_in_progress')
        return self._current.state

    def configure(self, *, port, layout):
        if layout.mode != 'desktop' or layout.user_root != self.root:
            raise ValueError('deployment_runtime_conflict')
        self.port = port
        self.application.state.deployment = layout
        if self._current is not None:
            self._current.state.server_device_auth.configure_port(port)
            self._current.state.deployment = layout

    def _compose(self, root, _user_id):
        if root != self.root or self._reservation is not None and not self._rebuilding:
            raise RuntimeError('runtime_restore_in_progress')
        app = self._initial
        self._initial = None
        if app is None:
            app = self.factory()
        if (app.state.recognition_runtime_root != self.root or app.state.container.root_dir != self.root
                or app.state.deployment.mode != 'desktop'):
            raise RuntimeError('runtime_application_identity_invalid')
        if self.port is not None:
            app.state.server_device_auth.configure_port(self.port)
        app.state.runtime_restore_host = self
        self._current = app
        return app

    @asynccontextmanager
    async def _lifetime(self, _application):
        self._rebuilding = False
        async with self.pool.lease('local-runtime') as app:
            self._verify_loaded(app)
        await self._mark_ready()
        try:
            yield
        finally:
            self._closing = True
            if self._execution is not None:
                from .server_jobs import retain_until_finished
                await retain_until_finished(self._execution)
            await self.pool.close()
            if self._current is not None:
                close_runtime_logging(self._current)

    async def __call__(self, scope, receive, send):
        return await self.application(scope, receive, send)

    async def _dispatch(self, scope, receive, send):
        if self._closed_for_restore():
            if scope['type'] == 'websocket':
                return await send({'type': 'websocket.close', 'code': 1013})
            return await JSONResponse({'detail': 'runtime_restore_in_progress'}, status_code=503,
                headers={'Cache-Control': 'no-store', 'Retry-After': '1'})(scope, receive, send)
        self._active += 1
        self._drained.clear()
        try:
            async with self.pool.lease('local-runtime') as app:
                if scope['type'] == 'websocket':
                    return await self._websocket(app, scope, receive, send)
                return await app(scope, receive, send)
        finally:
            self._active -= 1
            if not self._active:
                self._drained.set()

    async def _websocket(self, app, scope, receive, send):
        closing, closed = self._socket_close, False

        async def close():
            nonlocal closed
            if not closed:
                closed = True
                await send({'type': 'websocket.close', 'code': 1012})

        async def guarded_receive():
            incoming, stopping = asyncio.create_task(receive()), asyncio.create_task(closing.wait())
            try:
                done, _pending = await asyncio.wait((incoming, stopping), return_when=asyncio.FIRST_COMPLETED)
                if stopping in done:
                    await close()
                    return {'type': 'websocket.disconnect', 'code': 1012}
                return incoming.result()
            finally:
                for task in (incoming, stopping):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(incoming, stopping, return_exceptions=True)

        async def guarded_send(message):
            if closing.is_set():
                await close()
                raise WebSocketDisconnect(1012)
            return await send(message)

        return await app(scope, guarded_receive, guarded_send)

    def _closed_for_restore(self):
        if self._closing or self._unavailable or self._reservation is not None:
            return True
        return any(value['state'] in {'queued', 'adopting'}
                   for value in runtime_recovery_summary(operations_root=self.operations))

    async def request_execution(self, operation_id, *, request=None):
        if self._reservation is not None or self._unavailable or self._closing:
            raise VaultOperationalRecoveryError('runtime_restore_busy')
        self._reservation = operation_id
        try:
            await run_in_threadpool(queue_runtime_recovery, operations_root=self.operations, operation_id=operation_id)
        except BaseException:
            self._reservation = None
            raise
        self._socket_close.set()
        self._execution = asyncio.create_task(self._execute(operation_id), context=Context())

    async def _execute(self, operation_id, *, apply_restore=True):
        try:
            await self._drained.wait()
            if self._current is not None:
                daily = getattr(self._current.state, 'memory_daily_jobs', None)
                if daily is not None:
                    await daily.stop()
                subscriptions = getattr(self._current.state.recognition_models, 'subscriptions', None)
                if subscriptions is not None:
                    from .server_jobs import retain_until_finished
                    await retain_until_finished(asyncio.to_thread(subscriptions.quiesce_logins))
                await self.pool._settle_work(self._current, ignore_native=True)
                native = getattr(self._current.state, 'external_execution_host', None)
                if native is not None:
                    from .server_jobs import retain_until_finished
                    await retain_until_finished(asyncio.to_thread(native.close))
            # 原 pool 在相同准入锁内观察执行任务并完整关闭旧应用。
            retired = self._current
            async with self.pool.deletion_lease('local-runtime', lambda: operation_id):
                self._current = None
                if retired is not None:
                    close_runtime_logging(retired)
                del retired
                gc.collect()
                if apply_restore:
                    await run_in_threadpool(adopt_prepared_runtime,
                        operations_root=self.operations, operation_id=operation_id,
                        working_root=self.working, application_offline=True)
            self._rebuilding = True
            try:
                async with self.pool.lease('local-runtime') as app:
                    self._verify_loaded(app)
            finally:
                self._rebuilding = False
            if apply_restore:
                await run_in_threadpool(record_runtime_activation,
                    operations_root=self.operations, operation_id=operation_id, ready=True)
            self._socket_close = asyncio.Event()
            self._unavailable = False
            self._failure = None
            self._reservation = None
        except BaseException as error:
            self._failure = error
            self._unavailable = True
            await run_in_threadpool(record_runtime_activation,
                operations_root=self.operations, operation_id=operation_id, ready=False)

    def _verify_loaded(self, app):
        records = app.state.recognition_records
        if not Path(records.database_path).resolve().is_relative_to(self.root):
            raise RuntimeError('runtime_application_identity_invalid')
        # 新工厂必须真实读取固定根中的恢复数据，才开放后续请求。
        tuple(records.list('v2_projects'))

    async def _mark_ready(self):
        adopted = [value for value in runtime_recovery_summary(operations_root=self.operations) if value['state'] == 'adopted']
        if adopted and not adopted[-1]['runtime_ready']:
            await run_in_threadpool(record_runtime_activation,
                operations_root=self.operations, operation_id=adopted[-1]['operation_id'], ready=True)

    async def _status(self, request: Request, operation_id: str):
        try:
            operation = await run_in_threadpool(load_runtime_recovery_operation,
                operations_root=self.operations, operation_id=operation_id)
        except (OSError, ValueError) as error:
            self._status_failure = error
            missing = isinstance(error.__cause__, FileNotFoundError)
            return JSONResponse({'detail': 'restore_operation_not_found' if missing else 'restore_status_unavailable'}, status_code=404 if missing else 400,
                headers={'Cache-Control': 'no-store'})
        active = self._reservation == operation_id and self._execution is not None and not self._execution.done()
        return JSONResponse({
            'operation_id': operation.operation_id,
            'target_snapshot_id': Path(operation.snapshot_root).name,
            'state': operation.state,
            'status': 'restored' if operation.activation_state == 'ready' and not self._reservation and not self._unavailable else
                'restoring' if active else 'failed' if operation.activation_state == 'failed' or operation.state == 'failed' else 'restoring',
            'restore_executed': operation.state == 'adopted',
            'runtime_ready': operation.activation_state == 'ready',
            'requires_restart': self._unavailable,
        }, headers={'Cache-Control': 'no-store'})

    async def _resume(self, request: Request, operation_id: str):
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get('confirm') is not True:
                return JSONResponse({'detail': 'restore_confirmation_required'}, status_code=400)
            operation = await run_in_threadpool(load_runtime_recovery_operation,
                operations_root=self.operations, operation_id=operation_id)
            if self._closing:
                raise VaultOperationalRecoveryError('runtime_restore_busy')
            if self._execution is not None and not self._execution.done():
                if self._reservation != operation_id:
                    raise VaultOperationalRecoveryError('runtime_restore_busy')
            elif operation.state in {'prepared', 'queued'} and not self._unavailable:
                await self.request_execution(operation_id)
            elif operation.state in {'adopting', 'adopted'} and self._unavailable and self._reservation == operation_id:
                self._reservation = operation_id
                self._execution = asyncio.create_task(self._execute(operation_id), context=Context())
            elif operation.state == 'failed' and self._unavailable and self._reservation == operation_id:
                self._execution = asyncio.create_task(self._execute(operation_id, apply_restore=False), context=Context())
            else:
                return JSONResponse({'detail': 'restore_restart_required'}, status_code=409)
            return JSONResponse({'status': 'restore_queued', 'operation_id': operation_id,
                'target_snapshot_id': Path(operation.snapshot_root).name, 'restore_executed': operation.state == 'adopted'}, status_code=202,
                headers={'Cache-Control': 'no-store'})
        except (OSError, ValueError):
            return JSONResponse({'detail': 'restore_resume_unavailable'}, status_code=409,
                headers={'Cache-Control': 'no-store'})
