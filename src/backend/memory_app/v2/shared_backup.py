import asyncio
from contextlib import asynccontextmanager, contextmanager
import shutil
import json
from uuid import uuid4

from backend.memory_app.backup import backup_runtime
from backend.memory_app.backup_archive import BackupArchive, backup_io_path, pack_backup_archive, safe_backup_path, unpack_backup_archive, verify_backup_archive
from backend.memory_app.server_jobs import QuotaError, retain_until_finished
from backend.security.user_context import USER_ACCESS, authorize_user, user_context
from backend.shared.deployment import runtime_backup_roots
from backend.shared.interprocess_lock import interprocess_file_lock
from core.storage_provider.persistence_budget import persistence_budget
from core.storage_provider.vault_backup_restore import verify_runtime_backup, verify_vault_backup, verification_metadata, _write_verification
from .backup_transfers import command_id
from .material_origins import SharedProjectLease
from .policies import get
from .shared_extraction import PersonalDestination
from .shared_projects import PROJECTS, SharedProjectError
from .shared_backup_qualification import FIELD, capture_sources


COMMANDS = 'v2_shared_backup_commands'


class SharedBackups:
    def __init__(self, registry, context):
        self.registry, self.context = registry, context

    @property
    def limits(self):
        return get('backup_archive', version='@1')({'operation': 'limits'})

    def _current(self, actor, destination):
        context = self.context
        access = authorize_user(context.users, actor, destination.owner_user_id)
        child = context.pool._children.get(destination.owner_user_id)
        if (not context.registry.is_current(actor)
                or child is None or child.active < 1 or child.retiring is not None
                or child.application.state.recognition_records is not destination.records
                or child.application.state.server_context is not context):
            raise SharedProjectError('shared_backup_owner_changed', status=403)
        destination.validate_application(self.registry, child.application)
        return access

    def _access(self, actor, owner_user_id=None):
        bound = USER_ACCESS.get()
        target = owner_user_id if owner_user_id is not None else bound.target_user_id if bound is not None else actor.user_id
        access = authorize_user(self.context.users, actor, target)
        if bound is not None and bound != access:
            raise SharedProjectError('shared_backup_owner_changed', status=403)
        return access

    @staticmethod
    def _owner_id(payload):
        return payload.get('owner_user_id', payload.get('actor_user_id'))

    def _area(self, destination, identity):
        operations = runtime_backup_roots(destination.root)[2]
        return safe_backup_path(operations.parent / 'shared-project-backups' / command_id(identity))

    def _row(self, actor, destination, identity, *, require_shared=False):
        self._current(actor, destination)
        row = destination.records.read(COMMANDS, command_id(identity))
        if row is None:
            raise SharedProjectError('shared_backup_unavailable', status=404)
        if self._owner_id(row.payload) != destination.owner_user_id:
            raise SharedProjectError('shared_backup_forbidden', status=403)
        lease = None
        if require_shared:
            lease = SharedProjectLease.acquire(self.registry, actor, row.payload['project_id'])
            if (lease.namespace != row.payload['source_namespace']
                    or list(lease.root_identity) != row.payload['source_root_identity']):
                raise SharedProjectError('shared_backup_source_changed', status=409)
        return row, lease

    def _save(self, destination, identity, value, *, cap):
        with persistence_budget(root=destination.root,
                databases=(destination.records.database_path,), consume=cap.consume):
            with destination.records.begin() as tx:
                previous = tx.read(COMMANDS, identity)
                result = tx.put(COMMANDS, identity, value, expected_revision=previous.revision if previous else 0)
                tx.commit()
        return result

    def _view(self, row):
        fields = ('command_id', 'project_id', 'project_name', 'created_at', 'state', 'snapshot_id',
                  'size_bytes', 'reason_code')
        return {**{key: row.payload[key] for key in fields if key in row.payload}, 'revision': row.revision,
                'scope': 'shared-project'}

    def read(self, actor, destination, identity):
        row, _lease = self._row(actor, destination, identity)
        return self._view(row)

    def list(self, actor, destination, project_id=None):
        self._current(actor, destination)
        rows = [row for row in destination.records.list(COMMANDS) if self._owner_id(row.payload) == destination.owner_user_id
                and (project_id is None or row.payload['project_id'] == project_id)]
        rows.sort(key=lambda row: (row.payload['created_at'], row.object_id), reverse=True)
        return {'items': [self._view(row) for row in rows]}

    def _archive(self, actor, destination, identity, *, _content=True):
        row, _lease = self._row(actor, destination, identity)
        if row.payload['state'] != 'ready':
            raise SharedProjectError('shared_backup_not_ready', status=409)
        path = self._area(destination, identity) / 'archive.zip'
        information = backup_io_path(safe_backup_path(path)).stat()
        observed = [information.st_dev, information.st_ino, information.st_size, information.st_mtime_ns]
        if observed != row.payload['archive_identity']:
            raise SharedProjectError('shared_backup_archive_changed', status=409)
        archive = (self._verified_archive(destination, row) if _content else
            BackupArchive(path, 'chriptmas-backup-' + row.payload['snapshot_id'] + '.zip',
                          information.st_size, row.payload['snapshot_id']))
        self._current(actor, destination)
        return archive

    def _verified_archive(self, destination, row):
        area = self._area(destination, row.object_id)
        if (row.payload.get('snapshot_id') != 'shared-' + row.object_id
                or not isinstance(row.payload.get('source_fingerprint'), str)):
            raise SharedProjectError('shared_backup_content_binding_missing', status=409)
        information = backup_io_path(safe_backup_path(area / 'archive.zip')).stat()
        if [information.st_dev, information.st_ino, information.st_size, information.st_mtime_ns] != row.payload['archive_identity']:
            raise SharedProjectError('shared_backup_archive_changed', status=409)
        return verify_backup_archive(archive_path=area / 'archive.zip',
            snapshot_root=area / row.payload['snapshot_id'], expected_scope='shared-project',
            expected_fingerprint=row.payload['source_fingerprint'], limits=self.limits)

    @contextmanager
    def _activity(self, actor, destination, row):
        payload = row.payload
        _row, lease = self._row(actor, destination, row.object_id, require_shared=True)
        with self.registry.records.begin_read() as central:
            access = self.registry.authorize(central, actor, payload['project_id'], authority={
                'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
                'account_revision': lease.account_revision})
            self.registry.roots.project_activity(payload['project_id'], {
                'id': 'backup-' + self._owner_id(payload) + '-' + row.object_id,
                'project_id': payload['project_id'], 'actor_user_id': payload['actor_user_id'],
                'by': payload.get('by', access.by if payload['actor_user_id'] == actor.user_id else None),
                'at': payload['created_at'], 'action': 'backup',
                'object_kind': 'backup', 'object_id': row.object_id, 'object_title': payload['project_name']})
            yield central

    def _finish(self, actor, destination, row, check):
        self._verified_archive(destination, row)
        cap = check.reserve_up_to(self.limits['envelope_bytes'])
        try:
            with self._activity(actor, destination, row) as central, cap.using_reader(central):
                self._current(actor, destination)
                result = self._save(destination, row.object_id, {**row.payload, 'state': 'ready'}, cap=cap)
            return self._view(result)
        finally:
            cap.close()

    def _required_bytes(self, data, area, identity):
        source_bytes, metadata_bytes, source_count = 0, 0, 0
        for path in data.root.rglob('*'):
            path = safe_backup_path(path)
            if path.is_file():
                source_bytes += path.stat().st_size
                metadata_bytes += len(path.relative_to(data.root).as_posix().encode('utf-8')) + 512
                source_count += 1
        if source_bytes > self.limits['unpacked_bytes'] or source_count + 3 > self.limits['entries']:
            raise SharedProjectError('shared_backup_limit_exceeded', status=413)
        snapshot_path = area / ('shared-' + identity)
        retained_bytes = sum(safe_backup_path(path).stat().st_size
            for path in snapshot_path.rglob('*') if path.is_file()) if snapshot_path.exists() else 0
        return 4 * max(source_bytes, retained_bytes) + 8 * metadata_bytes + self.limits['envelope_bytes']

    @contextmanager
    def _reserve(self, actor, project_id, lease, area, identity, check):
        with self.registry.records.begin_read() as central:
            self.registry.authorize(central, actor, project_id, authority={
                'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
                'account_revision': lease.account_revision})
            with self.registry.roots.lease(project_id):
                data = self.registry.roots._data(project_id)
                if data.namespace != lease.data_namespace or tuple(data.root_identity) != lease.root_identity:
                    raise SharedProjectError('shared_backup_source_changed', status=409)
                required = self._required_bytes(data, area, identity)
        # 全部本人资料的容量统计在项目根租约之外完成，再由原范围复验实际备份上界。
        cap = check.reserve_up_to(required)
        try:
            if cap.remaining < required:
                raise QuotaError('storage_quota_exceeded')
            yield cap
        finally:
            cap.close()

    def create(self, actor, destination, project_id, identity, check):
        identity = command_id(identity)
        access = self._current(actor, destination)
        area = self._area(destination, identity)
        with interprocess_file_lock(area, timeout_seconds=0):
            existing = destination.records.read(COMMANDS, identity)
            if existing is not None:
                row, _lease = self._row(actor, destination, identity)
                if row.payload['project_id'] != project_id:
                    raise SharedProjectError('shared_backup_command_conflict', status=409)
                if row.payload['state'] == 'ready':
                    self._archive(actor, destination, identity)
                    return self._view(row)
                if row.payload['state'] == 'archived':
                    return self._finish(actor, destination, row, check)
                if row.payload['state'] == 'failed':
                    return self._view(row)
            lease = SharedProjectLease.acquire(self.registry, actor, project_id)
            with self._reserve(actor, project_id, lease, area, identity, check) as cap, self.registry.records.begin_read() as central:
                self.registry.authorize(central, actor, project_id, authority={
                    'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
                    'account_revision': lease.account_revision})
                project = central.read(PROJECTS, project_id)
                with self.registry.roots.lease(project_id):
                    data = self.registry.roots._data(project_id)
                    if data.namespace != lease.data_namespace or tuple(data.root_identity) != lease.root_identity:
                        raise SharedProjectError('shared_backup_source_changed', status=409)
                    required = self._required_bytes(data, area, identity)
                    with cap.using_reader(central):
                        if cap.remaining < required:
                            raise QuotaError('storage_quota_exceeded')
                        self._current(actor, destination)
                        payload = dict(existing.payload) if existing else {
                            'command_id': identity, 'project_id': project_id, 'actor_user_id': actor.user_id,
                            'owner_user_id': destination.owner_user_id, 'by': access.by,
                            'source_namespace': lease.namespace, 'source_root_identity': list(lease.root_identity),
                            'project_name': project.payload['name'], 'created_at': self.registry.now(),
                            'snapshot_id': 'shared-' + identity, 'state': 'preparing'}
                        self._save(destination, identity, payload, cap=cap)
                        try:
                            snapshot_path = area / payload['snapshot_id']
                            if snapshot_path.exists():
                                snapshot = verify_vault_backup(snapshot_path)
                                if not verify_runtime_backup(snapshot_path)['verified']:
                                    raise SharedProjectError('shared_backup_verification_failed', status=409)
                            else:
                                snapshot = backup_runtime(data.root, area, snapshot_id=payload['snapshot_id'])
                            if payload.get('source_fingerprint', snapshot.source_fingerprint) != snapshot.source_fingerprint:
                                raise SharedProjectError('shared_backup_source_changed', status=409)
                            payload['source_fingerprint'] = snapshot.source_fingerprint
                            archive_path = area / 'archive.zip'
                            metadata = verification_metadata(snapshot.snapshot_root)
                            if not archive_path.exists():
                                qualification = capture_sources(self.registry, data, project_id, lease.namespace, snapshot)
                                if FIELD in metadata and metadata[FIELD] != qualification:
                                    raise SharedProjectError('shared_backup_source_qualification_changed', status=409)
                                if FIELD not in metadata:
                                    metadata[FIELD] = qualification
                                    cap.consume(len(json.dumps(metadata, sort_keys=True).encode('utf-8')))
                                    _write_verification(snapshot.snapshot_root, metadata)
                                if FIELD in payload and payload[FIELD] != qualification:
                                    raise SharedProjectError('shared_backup_source_qualification_changed', status=409)
                                payload[FIELD] = qualification
                            self._save(destination, identity, payload, cap=cap)
                            if archive_path.exists():
                                # 未确认终态的 ZIP 必须沿原解包和恢复验证，目录元数据不足以证明正文完整。
                                verification = area / ('verify-' + uuid4().hex)
                                try:
                                    checked = unpack_backup_archive(archive_path=archive_path,
                                        stage_root=verification, expected_scope='shared-project', limits=self.limits)
                                    if checked.snapshot.source_fingerprint != snapshot.source_fingerprint:
                                        raise SharedProjectError('shared_backup_archive_changed', status=409)
                                finally:
                                    if verification.exists():
                                        for path in verification.rglob('*'):
                                            safe_backup_path(path)
                                        shutil.rmtree(backup_io_path(safe_backup_path(verification)))
                                archive = BackupArchive(archive_path, '', archive_path.stat().st_size, snapshot.snapshot_id)
                            else:
                                archive = pack_backup_archive(snapshot_root=snapshot.snapshot_root,
                                    destination=archive_path, scope_kind='shared-project', limits=self.limits, consume=cap.consume)
                            self._current(actor, destination)
                            cap.validate()
                            info = backup_io_path(archive.path).stat()
                            payload.update(state='archived', size_bytes=archive.size_bytes,
                                archive_identity=[info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])
                        except Exception:
                            payload.update(state='failed', reason_code='shared_backup_incomplete')
                            self._save(destination, identity, payload, cap=cap)
                            raise
                        row = self._save(destination, identity, payload, cap=cap)
            # 本人备份命令先持久保存，跨库活动按稳定命令编号去重后才报告完成。
            return self._finish(actor, destination, row, check)

    async def run(self, operation, actor, *args, owner_user_id=None):
        if operation != 'create':
            raise SharedProjectError('shared_backup_operation_invalid')
        context = self.context
        access = self._access(actor, owner_user_id)
        with user_context(access):
            async with context.pool.lease(access.target_user_id) as application:
                destination = PersonalDestination.from_application(self.registry, actor, application, target_user_id=access.target_user_id)
                async with context.jobs.intake(access.target_user_id) as check:
                    async def execute():
                        return await retain_until_finished(asyncio.to_thread(
                            getattr(self, operation), actor, destination, *args, check))
                    return await retain_until_finished(context.jobs.submit(
                        access.target_user_id, 'shared-backup', execute, retain_running=True))

    @asynccontextmanager
    async def opened(self, actor, identity, *, owner_user_id=None):
        context = self.context
        access = self._access(actor, owner_user_id)
        async with context.pool.lease(access.target_user_id) as application:
            destination = PersonalDestination.from_application(self.registry, actor, application, target_user_id=access.target_user_id)
            archive = await retain_until_finished(asyncio.to_thread(self._archive, actor, destination, identity))
            yield archive, lambda: self._archive(actor, destination, identity, _content=False)

    async def query(self, operation, actor, *args, owner_user_id=None):
        context = self.context
        access = self._access(actor, owner_user_id)
        with user_context(access):
            async with context.pool.lease(access.target_user_id) as application:
                destination = PersonalDestination.from_application(self.registry, actor, application, target_user_id=access.target_user_id)
                return await retain_until_finished(asyncio.to_thread(
                    getattr(self, operation), actor, destination, *args))
