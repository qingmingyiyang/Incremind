import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4

from fastapi import HTTPException

from backend.memory_app.backup_archive import (
    BackupArchiveError, backup_io_path, inspect_backup_archive, pack_backup_archive, safe_backup_path, unpack_backup_archive,
)
from backend.memory_app.server_jobs import retain_until_finished
from backend.security.user_context import USER_ACCESS, UserError, authorize_user
from backend.shared.deployment import runtime_backup_roots
from backend.shared.interprocess_lock import interprocess_file_lock
from core.storage_provider.vault_backup_restore import (
    read_backup_catalog, verification_metadata, verify_vault_backup, write_backup_catalog,
)
from core.storage_provider.connection_scope import with_connection_scope
from .policies import get


COMMANDS = 'v2_backup_import_commands'
_ID = re.compile(r'^[a-z0-9][a-z0-9_-]{7,63}$')
_UPLOAD = re.compile(r'^/api/v2/backups/imports/([a-z0-9][a-z0-9_-]{7,63})$')


def command_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise BackupArchiveError('backup_command_invalid')
    return value


class BackupTransfers:
    def __init__(self, application, runtime_root, records):
        self.application, self.root, self.records = application, safe_backup_path(runtime_root), records
        self.database = safe_backup_path(records.database_path)
        if not self.database.is_relative_to(self.root):
            raise BackupArchiveError('backup_space_changed')
        _working, self.snapshots, operations = runtime_backup_roots(self.root)
        self.area = operations.parent / 'transfers'
        self.identity = self._identity()

    @property
    def limits(self):
        return get('backup_archive', version='@1')({'operation': 'limits'})

    def body_limit(self, request):
        return self.limits['archive_bytes'] if request.method == 'PUT' and _UPLOAD.fullmatch(request.url.path) else None

    def _identity(self):
        root, database = safe_backup_path(self.root).stat(), safe_backup_path(self.database).stat()
        return {'root': [root.st_dev, root.st_ino], 'database': [database.st_dev, database.st_ino]}

    def authorize(self, request):
        if (self._identity() != self.identity or request.app is not self.application
                or self.application.state.recognition_records is not self.records
                or self.application.state.recognition_runtime_root != self.root):
            raise BackupArchiveError('backup_space_changed')
        context = getattr(self.application.state, 'server_context', None)
        if context is None:
            return None
        access = USER_ACCESS.get()
        user_id = self.application.state.server_user_id
        child = context.pool._children.get(user_id)
        try:
            if (access is None or request.scope['state'].get('user_access') != access
                    or access.target_user_id != user_id or context.users.root_for(user_id) != self.root
                    or child is None or child.application is not self.application or child.active < 1
                    or child.retiring is not None or not context.registry.is_current(access.caller)
                    or authorize_user(context.users, access.caller, user_id) != access
                    or context.users.deletion_pending(user_id)):
                raise UserError('user_forbidden')
            user = context.users.get(user_id)
            return {'actor_user_id': access.caller.user_id, 'actor_device_id': access.caller.device_id,
                'actor_device_revision': access.caller.revision, 'target_user_id': user_id,
                'target_user_revision': user['revision']}
        except UserError:
            raise HTTPException(403, 'backup_space_forbidden') from None

    @asynccontextmanager
    async def intake(self):
        context = getattr(self.application.state, 'server_context', None)
        if context is None:
            yield lambda _size: None
        else:
            async with context.jobs.intake(self.application.state.server_user_id) as reserve:
                yield reserve

    def _stage(self, identity):
        return safe_backup_path(self.area / 'imports' / command_id(identity))

    def _final(self, identity):
        return safe_backup_path(self.snapshots / ('snap-import-' + command_id(identity)))

    def _remove_stage(self, stage):
        stage = safe_backup_path(stage)
        if stage.parent != safe_backup_path(self.area / 'imports'):
            raise BackupArchiveError('backup_archive_path_unsafe')
        if stage.exists():
            for path in stage.rglob('*'):
                safe_backup_path(path)
            shutil.rmtree(backup_io_path(stage))

    def _update(self, identity, *, validate=None, **changes):
        with self.records.begin() as tx:
            if validate is not None:
                validate()
            prior = tx.read(COMMANDS, identity)
            payload = {**(prior.payload if prior else {}), **changes, 'updated_at': datetime.now(timezone.utc).isoformat()}
            row = tx.put(COMMANDS, identity, payload, expected_revision=prior.revision if prior else 0)
            if validate is not None:
                validate()
            tx.commit()
        return row.payload

    def _view(self, identity, value):
        fields = ('state', 'snapshot_id', 'original_snapshot_id', 'size_bytes', 'reason_code', 'updated_at')
        return {'command_id': identity, **{key: value[key] for key in fields if key in value}}

    def _completed(self, identity):
        final = self._final(identity)
        catalog = read_backup_catalog(final)
        if catalog is None or catalog.get('import_command_id') != identity:
            return None
        snapshot = verify_vault_backup(final)
        if snapshot.source_fingerprint != catalog['vault_fingerprint'] or verification_metadata(final).get('verified') is not True:
            raise BackupArchiveError('backup_verification_failed')
        return {'state': 'published', 'snapshot_id': final.name,
                'original_snapshot_id': catalog.get('original_snapshot_id'), 'size_bytes': catalog.get('archive_size_bytes')}

    def _validate(self, request, payload):
        if payload.get('origin') != self.identity or self.authorize(request) != payload.get('authority'):
            raise BackupArchiveError('backup_import_authority_changed')

    def _finish(self, identity, request, reserve):
        payload = self.records.read(COMMANDS, identity).payload
        self.authorize(request)
        completed = self._completed(identity)
        if completed is not None:
            result = self._update(identity, **completed)
            self._remove_stage(self._stage(identity))
            return self._view(identity, result)
        self._validate(request, payload)
        if payload['state'] not in {'uploaded', 'publishing'}:
            raise BackupArchiveError('backup_import_interrupted')
        stage = self._stage(identity)
        final = self._final(identity)
        if final.exists() and payload['state'] != 'publishing':
            raise BackupArchiveError('backup_import_destination_exists')
        unpacked = stage / 'unpacked'
        if payload['state'] == 'uploaded':
            if unpacked.exists():
                for path in unpacked.rglob('*'):
                    safe_backup_path(path)
                shutil.rmtree(backup_io_path(safe_backup_path(unpacked)))
            size = inspect_backup_archive(archive_path=stage / 'archive.zip', limits=self.limits)
            reserve(2 * size['unpacked_bytes'])
            imported = unpack_backup_archive(archive_path=stage / 'archive.zip', stage_root=unpacked,
                expected_scope='personal-runtime', limits=self.limits, snapshot_id=final.name)
            self._validate(request, payload)
            payload = self._update(identity, state='publishing', snapshot_id=final.name,
                original_snapshot_id=imported.original_snapshot_id, fingerprint=imported.snapshot.source_fingerprint)
        snapshot = verify_vault_backup(final if final.exists() else unpacked / 'snapshot')
        if snapshot.source_fingerprint != payload['fingerprint'] or verification_metadata(snapshot.snapshot_root).get('verified') is not True:
            raise BackupArchiveError('backup_verification_failed')
        self._validate(request, payload)
        if snapshot.snapshot_root != final:
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup_io_path(snapshot.snapshot_root), backup_io_path(final))
            snapshot = verify_vault_backup(final)
        self._validate(request, payload)
        catalog = read_backup_catalog(snapshot.snapshot_root)
        if catalog is None:
            write_backup_catalog(snapshot.snapshot_root, {'schema_version': '2.0.0', 'id': final.name,
                'created_at': datetime.now(timezone.utc).isoformat(), 'label': '导入备份', 'namespace_id': 'default',
                'layer_fingerprints': {}, 'layer_counts': {}, 'notes': '', 'restorable': True,
                'vault_fingerprint': snapshot.source_fingerprint, 'backup_file_count': snapshot.file_count,
                'import_command_id': identity, 'original_snapshot_id': payload['original_snapshot_id'],
                'archive_size_bytes': payload['size_bytes']})
        elif catalog.get('import_command_id') != identity or catalog['vault_fingerprint'] != snapshot.source_fingerprint:
            raise BackupArchiveError('backup_import_destination_exists')
        self._validate(request, payload)
        result = self._update(identity, state='published')
        self._remove_stage(stage)
        return self._view(identity, result)

    def _fail(self, identity, error):
        prior = self.records.read(COMMANDS, identity)
        if prior is None or prior.payload.get('state') in {'published', 'cancelled'}:
            return
        code = error.code if isinstance(error, BackupArchiveError) else 'backup_import_failed'
        self._update(identity, state='failed', reason_code=code)
        self._remove_stage(self._stage(identity))

    async def receive(self, identity, request):
        identity = command_id(identity)
        authority = self.authorize(request)
        if self.records.read(COMMANDS, identity) is not None:
            return await self.read(identity, request)
        completed = self._completed(identity)
        if completed is not None:
            return self._view(identity, completed)
        try:
            declared = int(request.headers.get('content-length', '0'))
        except ValueError:
            raise BackupArchiveError('backup_archive_invalid') from None
        if declared < 0 or declared > self.limits['archive_bytes']:
            raise BackupArchiveError('backup_archive_limit_exceeded')
        if request.headers.get('content-type', '').split(';')[0] not in {'application/zip', 'application/octet-stream'}:
            raise BackupArchiveError('backup_archive_invalid')
        async with self.intake() as reserve:
            with interprocess_file_lock(self.area / 'imports' / identity, timeout_seconds=0):
                existing = self.records.read(COMMANDS, identity)
                if existing is not None:
                    return await self._read_locked(identity, request, reserve)
                completed = self._completed(identity)
                if completed is not None:
                    return self._view(identity, completed)
                stage = self._stage(identity)
                if stage.exists():
                    raise BackupArchiveError('backup_import_stage_exists')
                self._update(identity, state='receiving', origin=self.identity, authority=authority,
                    validate=lambda: self._validate(request, {'origin': self.identity, 'authority': authority}))
                try:
                    reserve(declared)
                    reserved, received = declared, 0
                    stage.mkdir()
                    with backup_io_path(stage / 'archive.zip').open('xb') as output:
                        async for chunk in request.stream():
                            self._validate(request, {'origin': self.identity, 'authority': authority})
                            received += len(chunk)
                            if received > self.limits['archive_bytes']:
                                raise BackupArchiveError('backup_archive_limit_exceeded')
                            if received > reserved:
                                reserve(received - reserved)
                                reserved = received
                            await retain_until_finished(asyncio.to_thread(output.write, chunk))
                        await retain_until_finished(asyncio.to_thread(output.flush))
                        await retain_until_finished(asyncio.to_thread(os.fsync, output.fileno()))
                    if not received or declared and received != declared:
                        raise BackupArchiveError('backup_archive_invalid')
                    self._update(identity, state='uploaded', size_bytes=received)
                    return await retain_until_finished(asyncio.to_thread(self._finish, identity, request, reserve))
                except BaseException as error:
                    self._fail(identity, error)
                    raise

    async def _read_locked(self, identity, request, reserve):
        row = self.records.read(COMMANDS, identity)
        if row is None:
            completed = self._completed(identity)
            if completed is None and self._stage(identity).exists():
                return self._view(identity, {'state': 'failed', 'reason_code': 'backup_import_context_changed'})
            return self._view(identity, completed) if completed is not None else None
        if row.payload['state'] in {'failed', 'cancelled'}:
            return self._view(identity, row.payload)
        try:
            if row.payload['state'] == 'published':
                if self._completed(identity) is None:
                    raise BackupArchiveError('backup_import_output_missing')
                self._remove_stage(self._stage(identity))
                return self._view(identity, row.payload)
            return await retain_until_finished(asyncio.to_thread(self._finish, identity, request, reserve))
        except (OSError, ValueError) as error:
            if row.payload['state'] == 'published':
                return self._view(identity, {**row.payload, 'state': 'failed', 'reason_code': 'backup_import_output_unavailable'})
            self._fail(identity, error)
            return self._view(identity, self.records.read(COMMANDS, identity).payload)
        except BaseException as error:
            self._fail(identity, error)
            raise

    async def read(self, identity, request):
        identity = command_id(identity)
        self.authorize(request)
        if (self.records.read(COMMANDS, identity) is None and not self._stage(identity).exists()
                and not self._final(identity).exists()
                and not safe_backup_path(self.area / 'imports' / ('.' + identity + '.lock')).exists()):
            return None
        try:
            with interprocess_file_lock(self.area / 'imports' / identity, timeout_seconds=0):
                row = self.records.read(COMMANDS, identity)
                if row is None or row.payload['state'] != 'uploaded' or self._completed(identity) is not None:
                    return await self._read_locked(identity, request, lambda _size: None)
                async with self.intake() as reserve:
                    return await self._read_locked(identity, request, reserve)
        except TimeoutError:
            row = self.records.read(COMMANDS, identity)
            return self._view(identity, row.payload if row is not None else {'state': 'receiving'})

    async def cancel_unreceived(self, identity, request, *, expected_revision):
        identity = command_id(identity)
        if type(expected_revision) is not int or expected_revision != 0:
            raise BackupArchiveError('backup_cancel_revision_invalid')
        self.authorize(request)
        try:
            with interprocess_file_lock(self.area / 'imports' / identity, timeout_seconds=0):
                self.authorize(request)
                completed = self._completed(identity)
                if completed is not None:
                    return self._view(identity, completed)
                with self.records.begin() as tx:
                    authority = self.authorize(request)
                    existing = tx.read(COMMANDS, identity)
                    if existing is not None:
                        return self._view(identity, existing.payload)
                    if self._stage(identity).exists():
                        return self._view(identity, {'state': 'failed', 'reason_code': 'backup_import_context_changed'})
                    # 同一锁内的缺席 CAS 墓碑永久阻止迟到 PUT 接受此编号。
                    row = tx.put(COMMANDS, identity, {'state': 'cancelled', 'origin': self.identity,
                        'authority': authority, 'updated_at': datetime.now(timezone.utc).isoformat()},
                        expected_revision=expected_revision)
                    self._validate(request, {'origin': self.identity, 'authority': authority})
                    tx.commit()
                    return self._view(identity, row.payload)
        except TimeoutError:
            self.authorize(request)
            result = await self.read(identity, request)
            return result or self._view(identity, {'state': 'receiving'})

    @asynccontextmanager
    async def download(self, snapshot_id, request):
        self.authorize(request)
        stage = safe_backup_path(self.area / 'exports' / uuid4().hex)
        async with self.intake() as budget:
            context = getattr(self.application.state, 'server_context', None)
            cap = budget.reserve_up_to(self.limits['archive_bytes']) if context is not None else None
            try:
                archive = await retain_until_finished(asyncio.to_thread(with_connection_scope(self.export),
                    snapshot_id, request, stage=stage, consume=cap.consume if cap is not None else None))
                yield archive, cap
            finally:
                try:
                    # 路径在启动线程前已确定，生成期间取消也会等待真实写入结束后清理。
                    await retain_until_finished(asyncio.to_thread(self.release_export, stage / 'archive.zip'))
                finally:
                    if cap is not None:
                        cap.close()

    def export(self, snapshot_id, request, *, stage, consume):
        if not isinstance(snapshot_id, str) or not re.fullmatch(r'snap-[a-z0-9][a-z0-9._-]{0,122}', snapshot_id):
            raise BackupArchiveError('backup_snapshot_invalid')
        self.authorize(request)
        snapshot = safe_backup_path(self.snapshots / snapshot_id)
        catalog = read_backup_catalog(snapshot)
        if catalog is None:
            raise BackupArchiveError('backup_snapshot_unavailable')
        if verify_vault_backup(snapshot).source_fingerprint != catalog['vault_fingerprint']:
            raise BackupArchiveError('backup_verification_failed')
        archive = pack_backup_archive(snapshot_root=snapshot, destination=stage / 'archive.zip',
            scope_kind='personal-runtime', limits=self.limits, consume=consume)
        self.authorize(request)
        return archive

    def release_export(self, path):
        path = safe_backup_path(path)
        if path.parent.parent != safe_backup_path(self.area / 'exports'):
            raise BackupArchiveError('backup_archive_path_unsafe')
        if path.parent.exists():
            shutil.rmtree(backup_io_path(path.parent))
