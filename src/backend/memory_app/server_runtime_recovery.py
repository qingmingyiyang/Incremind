from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import Context
from dataclasses import dataclass
from functools import partial
import gc
import logging
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from backend.security.device_identity import DeviceIdentity
from backend.security.user_context import USER_ACCESS, UserAccess, UserError, authorize_user, user_action_allowed, user_context
from backend.shared.deployment import runtime_backup_roots
from backend.shared.runtime_logging import close_runtime_logging
from backend.shared.server_resources import resource_context
from backend.vault_recovery_cli import _reconcile_dead_lease
from core.storage_provider.vault_operational_recovery import VaultOperationalRecoveryConflict, VaultOperationalRecoveryError

from .rebuild_storage import build_rebuild_object_store
from .runtime_recovery import (
    RuntimeRestoreArchiveInvalid, adopt_prepared_runtime, build_restore_plan, fail_runtime_execution,
    invalidate_runtime_preparation,
    load_runtime_recovery_operation,
    queue_runtime_recovery, reconcile_runtime_preparation, record_runtime_activation, runtime_recovery_summary,
    server_restore_operation_id,
)
from .server_jobs import retain_until_finished
from .v2.devices import DEVICES
from .v2.server_users import DELETIONS, USERS, current_caller


@dataclass
class _Reservation:
    operation_id: str
    token: object
    authority: dict
    access: object
    task: object = None
    unavailable: bool = False


class _BoundRestoreOwner:
    def __init__(self, coordinator, user_id):
        self.coordinator, self.user_id = coordinator, user_id

    async def request_execution(self, operation_id, *, request=None):
        if request is None:
            raise VaultOperationalRecoveryError('restore_identity_unavailable')
        await self.coordinator.request_execution(self.user_id, operation_id, request)


class ServerRuntimeRestoreCoordinator:
    def __init__(self, context):
        self.context = context
        self._reservations = {}
        self._unavailable_users = set()
        self._closing = False

    def for_user(self, user_id):
        self.context.users.root_for(user_id)
        return _BoundRestoreOwner(self, user_id)

    def _roots(self, user_id):
        root = self.context.users.root_for(user_id)
        working, _backups, operations = runtime_backup_roots(root)
        return root, working, operations

    def admitted(self, user_id, *, token=None, control=False):
        reservation = self._reservations.get(user_id)
        if reservation is not None:
            return token is reservation.token or (control and reservation.unavailable
                and (reservation.task is None or reservation.task.done()))
        if user_id in self._unavailable_users:
            return control
        if self._closing:
            return False
        _root, _working, operations = self._roots(user_id)
        history = runtime_recovery_summary(operations_root=operations)
        return control or not history or history[-1]['state'] != 'adopted' or not history[-1]['error_code']

    def _access(self, request, user_id):
        access = request.scope.get('state', {}).get('user_access')
        if (access is None or access != USER_ACCESS.get() or access.target_user_id != user_id
                or request.scope.get('state', {}).get('server_dispatch_authority') not in {None, self.context.auth}
                or authorize_user(self.context.users, access.caller, user_id) != access):
            raise UserError('user_unauthorized')
        if request.app.state.server_context is not self.context:
            raise UserError('user_unauthorized')
        if request.scope.get('state', {}).get('server_dispatch_authority') is self.context.auth:
            child = self.context.pool._children.get(user_id)
            if child is None or child.application is not request.app:
                raise UserError('user_unauthorized')
        return access

    def _authority(self, access):
        with self.context.users.records.begin_read() as tx:
            return self._read_authority(tx, access)

    def _read_authority(self, tx, access):
        caller = current_caller(tx, access.caller)
        target_id = access.target_user_id
        by = 'admin' if target_id != access.caller.user_id else None
        if access.by != by or not user_action_allowed(caller.payload['role'], access.caller.user_id, target_id, 'space'):
            raise UserError('user_forbidden')
        target = tx.read(USERS, target_id)
        if target is None:
            raise UserError('user_unauthorized')
        if tx.read(DELETIONS, target_id) is not None:
            raise UserError('user_deletion_in_progress')
        return {'actor_user_id': access.caller.user_id, 'actor_device_id': access.caller.device_id,
                'actor_device_revision': access.caller.revision, 'target_user_id': access.target_user_id,
                'target_user_revision': target.revision}

    def confirmation_identity(self, request):
        access = request.scope.get('state', {}).get('user_access')
        if access is None:
            raise UserError('user_unauthorized')
        return self._authority(self._access(request, access.target_user_id))

    def _validate_authority(self, user_id, authority):
        with self.context.users.records.begin_read() as tx:
            return self._require_authority(tx, user_id, authority)

    def _require_authority(self, tx, user_id, authority):
        if authority is None or authority['target_user_id'] != user_id:
            raise UserError('user_unauthorized')
        identity = DeviceIdentity(authority['actor_device_id'], authority['actor_user_id'], authority['actor_device_revision'])
        access = UserAccess(identity, user_id, 'admin' if identity.user_id != user_id else None)
        if self._read_authority(tx, access) != authority:
            raise UserError('user_unauthorized')
        return access

    @contextmanager
    def _authorize_adoption(self, user_id, authority, operation):
        if operation.state != 'queued' or authority is None or operation.server_identity != authority:
            raise VaultOperationalRecoveryConflict('runtime_restore_authority_changed')
        # 同一中央写事务确定撤销设备、更新账号与写入采用意图的先后。
        with self.context.users.records.begin() as tx:
            try:
                self._require_authority(tx, user_id, authority)
            except UserError:
                raise VaultOperationalRecoveryConflict('runtime_restore_authority_changed') from None
            yield

    def _legacy_authority_available(self, user_id, operation_id, rollback_id):
        # 旧准备未存设备事实，只有原编号仍对应真实当前设备时才能保留。
        with self.context.users.records.begin_read() as tx:
            for device in tx.list(DEVICES):
                if device.payload.get('revoked_at') is not None:
                    continue
                identity = DeviceIdentity(device.object_id, device.payload['user_id'], device.revision)
                access = UserAccess(identity, user_id, 'admin' if identity.user_id != user_id else None)
                try:
                    authority = self._read_authority(tx, access)
                except UserError:
                    continue
                if server_restore_operation_id(rollback_id, authority) == operation_id:
                    return True
        return False

    def _queue_authorized(self, user_id, operation_id, authority, root, operations):
        operation = load_runtime_recovery_operation(operations_root=operations, operation_id=operation_id)
        frozen = operation.server_identity
        if frozen is not None and frozen != authority:
            try:
                self._validate_authority(user_id, frozen)
            except UserError:
                invalidate_runtime_preparation(operations_root=operations, operation=operation)
            raise VaultOperationalRecoveryConflict('runtime restore authority changed')
        legacy = None
        if frozen is None:
            if operation.state != 'prepared':
                raise VaultOperationalRecoveryConflict('runtime restore authority changed')
            store, settings = build_rebuild_object_store(root)
            try:
                plan, *_paths = build_restore_plan(root, Path(operation.snapshot_root).name, store=store,
                    namespace_id=settings.namespace_id, require_runtime_archive=True)
            except RuntimeRestoreArchiveInvalid:
                invalidate_runtime_preparation(operations_root=operations, operation=operation,
                                               reason='runtime_restore_archive_invalid')
                raise
            if (plan.current_vault_fingerprint != operation.original_fingerprint
                    or plan.target_vault_fingerprint != operation.snapshot_fingerprint):
                invalidate_runtime_preparation(operations_root=operations, operation=operation,
                                               reason='runtime_source_changed')
                raise VaultOperationalRecoveryConflict('runtime changed after restore confirmation')
            if server_restore_operation_id(plan.rollback_id, authority) != operation_id:
                if not self._legacy_authority_available(user_id, operation_id, plan.rollback_id):
                    invalidate_runtime_preparation(operations_root=operations, operation=operation)
                raise VaultOperationalRecoveryConflict('runtime restore authority changed')
            legacy = operation
        queue_runtime_recovery(operations_root=operations, operation_id=operation_id, server_identity=authority,
                               verified_legacy=legacy)

    async def request_execution(self, user_id, operation_id, request):
        access = self._access(request, user_id)
        authority = self._authority(access)
        expected = request.scope.get('state', {}).get('runtime_restore_authority')
        if expected is not None and expected != authority:
            _root, _working, operations = self._roots(user_id)
            operation = await asyncio.to_thread(load_runtime_recovery_operation, operations_root=operations,
                                               operation_id=operation_id)
            if operation.server_identity == expected:
                await asyncio.to_thread(invalidate_runtime_preparation, operations_root=operations, operation=operation)
            raise VaultOperationalRecoveryError('restore_plan_changed')
        if not self.admitted(user_id):
            raise VaultOperationalRecoveryError('runtime_restore_busy')
        reservation = _Reservation(operation_id, object(), authority, access)
        self._reservations[user_id] = reservation
        root, _working, operations = self._roots(user_id)
        try:
            await asyncio.to_thread(self._queue_authorized, user_id, operation_id, authority, root, operations)
        except BaseException:
            del self._reservations[user_id]
            raise
        reservation.task = asyncio.create_task(self._execute(user_id, reservation), context=Context())

    async def _wait_requests(self, user_id, control_token):
        pool = self.context.pool
        while True:
            child = pool._children.get(user_id)
            controls = pool._controls.get(user_id, ())
            if (not (child is not None and child.active) and not self.context.jobs.pending_for(user_id)
                    and not any(token is not control_token for token in controls)):
                return
            await asyncio.sleep(.05)

    async def _quiesce(self, app):
        daily = getattr(app.state, 'memory_daily_jobs', None)
        if daily is not None:
            await daily.stop()
        embeddings = getattr(app.state, 'memory_embedding_settings', None)
        if embeddings is not None:
            embeddings.begin_close()
        subscriptions = getattr(app.state.recognition_models, 'subscriptions', None)
        if subscriptions is not None:
            await retain_until_finished(asyncio.to_thread(subscriptions.quiesce_logins))
        await self.context.pool._settle_work(app, ignore_native=True)
        native = getattr(app.state, 'external_execution_host', None)
        if native is not None:
            await retain_until_finished(asyncio.to_thread(native.close))

    async def _execute(self, user_id, reservation, *, apply_restore=True):
        pool, audit_intent = self.context.pool, None
        root, working, operations = self._roots(user_id)
        with user_context(None), resource_context(self.context.resources):
            try:
                async with pool.control_lease({user_id}, restore_token=reservation.token) as control:
                    await self._wait_requests(user_id, control)
                    child = pool._children.get(user_id)
                    original = child.application if child is not None else None
                    if original is not None:
                        await self._quiesce(original)
                    access = self._validate_authority(user_id, reservation.authority)
                    from .server_audit import AdminAudit
                    audit = AdminAudit(self.context.users)
                    audit_intent = audit.intent(access, method='RESTORE', path='/api/rebuild/memory-restores/{operation_id}')
                    async with pool.deletion_lease(user_id,
                            lambda: self._validate_authority(user_id, reservation.authority), control_token=control):
                        if original is not None:
                            close_runtime_logging(original)
                        del original
                        gc.collect()
                        if apply_restore:
                            await asyncio.to_thread(adopt_prepared_runtime, operations_root=operations,
                                operation_id=reservation.operation_id, working_root=working, application_offline=True,
                                authorize_adoption=partial(self._authorize_adoption, user_id, reservation.authority))
                    async with pool.lease(user_id, restore_token=reservation.token) as app:
                        self._verify_loaded(user_id, app)
                    if apply_restore:
                        await asyncio.to_thread(record_runtime_activation, operations_root=operations,
                            operation_id=reservation.operation_id, ready=True)
                    audit.outcome(audit_intent, status=200)
                    audit_intent = None
                    del self._reservations[user_id]
            except BaseException as error:
                reservation.unavailable = True
                operation = load_runtime_recovery_operation(operations_root=operations, operation_id=reservation.operation_id)
                if operation.state in {'prepared', 'queued'}:
                    reason = 'runtime_restore_authority_changed' if isinstance(error, UserError) and str(error) in {
                        'user_unauthorized', 'user_forbidden', 'user_deletion_in_progress'} else 'runtime_restore_execution_failed'
                    await asyncio.to_thread(fail_runtime_execution, operations_root=operations, operation_id=reservation.operation_id, reason=reason)
                else:
                    await asyncio.to_thread(record_runtime_activation, operations_root=operations,
                        operation_id=reservation.operation_id, ready=False)
                if audit_intent is not None:
                    audit.outcome(audit_intent, status=500)

    def _verify_loaded(self, user_id, app):
        root = self.context.users.root_for(user_id)
        records = app.state.recognition_records
        if (app.state.server_context is not self.context or app.state.server_user_id != user_id
                or app.state.recognition_runtime_root != root
                or not Path(records.database_path).resolve().is_relative_to(root)):
            raise UserError('user_unavailable')
        tuple(records.list('v2_projects'))

    async def child_ready(self, user_id, app):
        if user_id in self._reservations:
            return
        _root, _working, operations = self._roots(user_id)
        adopted = [value for value in runtime_recovery_summary(operations_root=operations) if value['state'] == 'adopted']
        if adopted and not adopted[-1]['runtime_ready']:
            self._verify_loaded(user_id, app)
            await asyncio.to_thread(record_runtime_activation, operations_root=operations,
                operation_id=adopted[-1]['operation_id'], ready=True)

    def recover_before_open(self, user_id):
        if user_id in self._reservations:
            return
        _root, working, operations = self._roots(user_id)
        _reconcile_dead_lease(operations, working)
        for value in runtime_recovery_summary(operations_root=operations):
            if value['state'] == 'preparing':
                reconcile_runtime_preparation(operations_root=operations, operation_id=value['operation_id'])
                continue
            if value['state'] not in {'queued', 'adopting'}:
                continue
            operation = load_runtime_recovery_operation(operations_root=operations, operation_id=value['operation_id'])
            if operation.state == 'queued':
                try:
                    self._validate_authority(user_id, operation.server_identity)
                except UserError:
                    fail_runtime_execution(operations_root=operations, operation_id=operation.operation_id)
                    continue
            adopt_prepared_runtime(operations_root=operations, operation_id=operation.operation_id,
                working_root=working, application_offline=True,
                authorize_adoption=partial(self._authorize_adoption, user_id, operation.server_identity))

    def recover_existing(self):
        for user in self.context.users.records.list(USERS):
            try:
                self.recover_before_open(user.object_id)
            except (OSError, ValueError) as error:
                _root, _working, operations = self._roots(user.object_id)
                try:
                    values = runtime_recovery_summary(operations_root=operations)
                    if not values:
                        raise VaultOperationalRecoveryError('restore_status_unavailable')
                    operation = load_runtime_recovery_operation(operations_root=operations, operation_id=values[-1]['operation_id'])
                except (OSError, ValueError):
                    self._unavailable_users.add(user.object_id)
                    logging.getLogger(__name__).error('server_runtime_restore_unavailable exception_type=%s', type(error).__name__)
                    continue
                if operation.state == 'failed' and not (_working.parent / '.chriptmas-vault-recovery.lock').exists():
                    continue
                self._reservations[user.object_id] = _Reservation(operation.operation_id, object(), operation.server_identity, None,
                                                                  unavailable=True)

    def install(self, application):
        application.add_api_route('/api/rebuild/memory-restores/{operation_id}', self.status, methods=['GET'])
        application.add_api_route('/api/rebuild/memory-restores/{operation_id}/resume', self.resume, methods=['POST'])

    async def status(self, request: Request, operation_id: str):
        access = request.scope['state']['user_access']
        _root, _working, operations = self._roots(access.target_user_id)
        try:
            operation = await asyncio.to_thread(load_runtime_recovery_operation,
                operations_root=operations, operation_id=operation_id)
        except (OSError, ValueError) as error:
            missing = isinstance(error.__cause__, FileNotFoundError)
            return JSONResponse({'detail': 'restore_operation_not_found' if missing else 'restore_status_unavailable'},
                status_code=404 if missing else 400, headers={'Cache-Control': 'no-store'})
        reservation = self._reservations.get(access.target_user_id)
        active = reservation is not None and reservation.operation_id == operation_id and reservation.task is not None and not reservation.task.done()
        failed = operation.activation_state == 'failed' or operation.state == 'failed'
        return JSONResponse({'operation_id': operation_id, 'target_snapshot_id': Path(operation.snapshot_root).name,
            'state': operation.state, 'restore_executed': operation.state == 'adopted',
            'runtime_ready': operation.activation_state == 'ready',
            'requires_restart': reservation.unavailable if reservation is not None else operation.state == 'adopted' and failed,
            'status': 'restoring' if active else 'failed' if failed else
                'restored' if operation.activation_state == 'ready' else 'restoring'}, headers={'Cache-Control': 'no-store'})

    async def resume(self, request: Request, operation_id: str):
        access = request.scope['state']['user_access']
        user_id = access.target_user_id
        authority = self._authority(access)
        _root, _working, operations = self._roots(user_id)
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get('confirm') is not True:
                raise VaultOperationalRecoveryError('restore_confirmation_required')
            operation = await asyncio.to_thread(load_runtime_recovery_operation, operations_root=operations, operation_id=operation_id)
            reservation = self._reservations.get(user_id)
            if reservation is not None and reservation.operation_id != operation_id:
                raise VaultOperationalRecoveryError('runtime_restore_busy')
            if reservation is None and operation.state in {'prepared', 'queued'}:
                await self.request_execution(user_id, operation_id, request)
            elif reservation is not None and reservation.task is not None and not reservation.task.done():
                pass
            elif operation.state in {'adopting', 'adopted', 'failed'} and (reservation is not None or operation.activation_state == 'failed'):
                reservation = _Reservation(operation_id, object(), authority, access)
                self._reservations[user_id] = reservation
                reservation.task = asyncio.create_task(self._execute(user_id, reservation,
                    apply_restore=operation.state != 'failed'), context=Context())
            else:
                raise VaultOperationalRecoveryError('restore_restart_required')
            return JSONResponse({'status': 'restore_queued', 'operation_id': operation_id,
                'target_snapshot_id': Path(operation.snapshot_root).name, 'restore_executed': operation.state == 'adopted'},
                status_code=202, headers={'Cache-Control': 'no-store'})
        except (OSError, ValueError):
            return JSONResponse({'detail': 'restore_resume_unavailable'}, status_code=409, headers={'Cache-Control': 'no-store'})

    async def close(self):
        self._closing = True
        tasks = [value.task for value in self._reservations.values() if value.task is not None and not value.task.done()]
        for task in tasks:
            await retain_until_finished(task)
