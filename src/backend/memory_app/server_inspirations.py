import asyncio
import logging
import sqlite3
from contextvars import Context
from datetime import datetime, timezone
from pathlib import Path

from backend.security.user_context import user_context
from backend.shared.rebuild_storage import resolve_rebuild_repository_root
from backend.shared.server_resources import resource_context
from core.aggregate_repository_factory import (
    AggregateRepositoryFactory, AUTHORITY_DATABASE_NAME, TARGET_IDENTITY,
)
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import (
    JsonObjectStore, RebuildStorageSettings, SQLiteAggregateAuthorityStore, SQLiteStructuredRecordStore,
)
from core.storage_provider.sqlite_uow import SQLiteUnitOfWorkError
from .server_jobs import retain_until_finished
from .v2.policies import get
from .v2.server_users import USERS


_LOGGER = logging.getLogger(__name__)


def _readonly_connection(path):
    if not path.is_file():
        raise ValueError('automatic_inspiration_database_unavailable')
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
        timeout=1, check_same_thread=False)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        return connection
    except BaseException:
        connection.close()
        raise


class _ReadonlyAuthority(SQLiteAggregateAuthorityStore):
    def _connect(self):
        return _readonly_connection(self._database_path)


class _ReadonlyRecords(SQLiteStructuredRecordStore):
    def _connect(self):
        return _readonly_connection(self.database_path)

    def begin(self):
        raise SQLiteUnitOfWorkError('automatic_inspiration_probe_is_read_only')


def _existing_records(root, namespace):
    root = Path(root)
    authority_path = root / '.rebuild-data' / AUTHORITY_DATABASE_NAME
    legacy_path = root / 'recognition.sqlite3'
    if not root.is_dir() or not (authority_path.is_file() or legacy_path.is_file()):
        return None
    # 只读适配原权威工厂，不运行空库激活，也不猜测备用业务库。
    factory = AggregateRepositoryFactory(runtime_root=root, namespace_id=namespace,
        json_store=JsonObjectStore(root / '.rebuild-data', legacy_root=root / 'library', namespace_id=namespace),
        authority_store_factory=_ReadonlyAuthority, record_store_factory=_ReadonlyRecords)
    resolution = factory.document_repository_resolution()
    if isinstance(resolution.repository, SQLiteDocumentRepository):
        authority = _ReadonlyAuthority(authority_path).get(namespace, 'documents')
        if (resolution.authority_identity != TARGET_IDENTITY or authority is None
                or authority.evidence is None or authority.evidence.verification_method != 'exact_records'):
            raise ValueError('automatic_inspiration_document_authority_unavailable')
        return resolution.repository.records
    if resolution.repository.list(include_archived=True):
        raise ValueError('automatic_inspiration_document_cutover_required')
    return _ReadonlyRecords(legacy_path) if legacy_path.is_file() else None


def _due_in_existing_space(root, namespace, now):
    from .v2.automatic_inspirations import SETTINGS_COLLECTION, SETTINGS_KEY, JOBS_COLLECTION
    policy = get('inspiration_schedule', version='@1')
    records = _existing_records(root, namespace)
    if records is None:
        return False
    date = policy({'operation': 'date', 'now': now})
    identity = policy({'operation': 'job_id', 'date': date})
    with records.begin_read() as reader:
        settings = reader.read(SETTINGS_COLLECTION, SETTINGS_KEY)
        job = reader.read(JOBS_COLLECTION, identity)
        # 新建但尚无文稿的空间不需要启动完整应用和模型资源。
        present = reader.connection.execute(
            'SELECT 1 FROM crp_structured_records WHERE collection=? LIMIT 1', ('documents',)).fetchone()
        return present is not None and policy({'operation': 'due',
            'settings': settings.payload if settings is not None else None,
            'job': job.payload if job is not None else None, 'now': now})


def _backup_due_in_existing_space(root, namespace, now):
    from .v2.backup_schedule import BackupSchedule
    records = _existing_records(root, namespace)
    instant = datetime.fromisoformat(now.replace('Z', '+00:00')) if isinstance(now, str) else now
    return records is not None and BackupSchedule(records, now=lambda: instant).due()


class ServerInspirationCoordinator:
    def __init__(self, users, pool, jobs, resources, *, now=lambda: datetime.now(timezone.utc), shared_daily=None):
        repository = resolve_rebuild_repository_root(Path(__file__))
        self.namespace = RebuildStorageSettings.from_toml(repository / 'config' / 'rebuild.toml.example',
            repository_root=repository).namespace_id
        self.users, self.pool, self.jobs, self.resources, self.now = users, pool, jobs, resources, now
        self.shared_daily = shared_daily
        self._tasks, self._probe_errors = {}, {}
        self._cursor, self._closed = None, False

    def _users(self):
        return sorted(row.object_id for row in self.users.records.list(USERS)
            if row.payload.get('disabled_at') is None and not self.users.deletion_pending(row.object_id))

    def _due(self, identity, now):
        child = self.pool._children.get(identity)
        daily = getattr(child.application.state, 'memory_daily_jobs', None) if child is not None else None
        if daily is not None and daily.maintenance_paused:
            return False
        user = self.users.get(identity)
        if user['disabled_at'] is not None or self.users.deletion_pending(identity):
            return False
        root = self.users.root_for(identity)
        result = (_backup_due_in_existing_space(root, self.namespace, now)
                  or _due_in_existing_space(root, self.namespace, now)
                  or self.shared_daily is not None and self.shared_daily.due(identity))
        # 只读巡检与入队之间仍须尊重账号停用和删除事实。
        return result and self.users.get(identity)['disabled_at'] is None and not self.users.deletion_pending(identity)

    async def tick(self):
        if self._closed or len(self._tasks) >= self.jobs.concurrency:
            return
        identities = await retain_until_finished(asyncio.to_thread(self._users))
        if self._cursor in identities:
            split = identities.index(self._cursor) + 1
            identities = identities[split:] + identities[:split]
        now = self.now().astimezone(timezone.utc).isoformat()
        for identity in identities:
            if self._closed or len(self._tasks) >= self.jobs.concurrency:
                break
            self._cursor = identity
            if identity in self._tasks or self.jobs.pending_for(identity) or self.jobs.active_for(identity):
                continue
            try:
                due = await retain_until_finished(asyncio.to_thread(self._due, identity, now))
            except Exception as error:
                if self._probe_errors.get(identity) is not type(error):
                    _LOGGER.warning('automatic_inspiration_probe_unavailable exception_type=%s', type(error).__name__)
                self._probe_errors[identity] = type(error)
                continue
            self._probe_errors.pop(identity, None)
            if due:
                task = asyncio.create_task(self._submit(identity), context=Context())
                self._tasks[identity] = task
                task.add_done_callback(lambda completed, owner=identity: self._settled(owner, completed))

    async def _submit(self, identity):
        async def execute():
            # 原调度器已取得 job_lease；这里仅取得同一用户应用，不创建替代金额资格。
            async with self.pool.lease(identity) as application:
                daily = application.state.memory_daily_jobs
                async with daily.maintenance_lease(wait=False) as admitted:
                    if not admitted:
                        return
                    if self.shared_daily is not None:
                        await retain_until_finished(asyncio.to_thread(self.shared_daily.run_due, application, identity))
                    backup = getattr(application.state, 'memory_backup', None)
                    if backup is None:
                        raise RuntimeError('automatic_backup_service_unavailable')
                    await retain_until_finished(asyncio.to_thread(backup.run))
                    now = self.now().astimezone(timezone.utc).isoformat()
                    due = await retain_until_finished(asyncio.to_thread(_due_in_existing_space,
                        self.users.root_for(identity), self.namespace, now))
                    if not due:
                        return
                    service = getattr(application.state, 'memory_automatic_inspirations', None)
                    if service is None:
                        raise RuntimeError('automatic_inspiration_service_unavailable')
                    return await retain_until_finished(asyncio.to_thread(service.run_due))
        with user_context(None), resource_context(self.resources):
            return await self.jobs.submit(identity, 'daily', execute, retain_running=True)

    def _settled(self, identity, task):
        if self._tasks.get(identity) is task:
            self._tasks.pop(identity)
        if not task.cancelled():
            try:
                task.result()
            except Exception as error:
                _LOGGER.warning('automatic_inspiration_job_unavailable exception_type=%s', type(error).__name__)

    async def close(self):
        self._closed = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
