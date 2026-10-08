"""One application-owned scheduler shared by all memory maintenance jobs."""
import asyncio
import logging
import time
from functools import partial
from uuid import uuid4
from datetime import datetime, timezone
from .policies import get, version
from .policies.types import TriggerInput
from backend.security.user_context import user_context


_LOGGER = logging.getLogger(__name__)


class DailyJobs:
    def __init__(self, *, interval=86400, initial_delay=60, records=None, clock=time.monotonic,
                 check_interval=60, server_jobs=None, user_id=None):
        self.jobs, self.interval, self.task = {}, interval, None
        self.initial_delay = initial_delay
        self.records, self.clock, self.check_interval = records, clock, check_interval
        self.server_jobs, self.user_id = server_jobs, user_id
        self._clock_session = uuid4().hex
        self._last_clock = None
        self._fallback_elapsed = 0

    def completion_clock(self):
        return {'session': self._clock_session, 'cursor': self.clock()}

    def checkpoint(self):
        from .learning_events import checkpoint
        sample = self.completion_clock()
        now, previous = sample['cursor'], self._last_clock
        elapsed = get('trigger')(None, operation='elapsed', elapsed=0 if previous is None else now - previous,
                                 check_interval=self.check_interval)
        self._last_clock = now
        self._fallback_elapsed += max(0, now - self._last_fallback_clock) if hasattr(self, '_last_fallback_clock') else 0
        self._last_fallback_clock = now
        return checkpoint(self.records, elapsed, clock_sample=sample, previous=previous,
                          check_interval=self.check_interval)

    def completed(self, project):
        from .learning_events import completed
        completed(self.records, project, completion_clock=self.completion_clock)

    def register(self, name, callback):
        if name in self.jobs:
            raise ValueError('daily_job_already_registered')
        self.jobs[name] = callback

    def run(self, *, exclude=()):
        for name, callback in tuple(self.jobs.items()):
            if name in exclude:
                continue
            try:
                callback()
            except Exception as error:
                _LOGGER.warning('daily_job_failed job=%s exception_type=%s', name, type(error).__name__)

    async def start(self):
        self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        # 自动维护沿用用户空间，不继承创建应用时的管理员写入归属。
        with user_context(None):
            if self.records is not None and version('trigger') == '@2':
                self.checkpoint()
                await asyncio.sleep(get('trigger')(TriggerInput(True, self.initial_delay, self.interval)))
                await self._run_automatic(exclude=('consolidation',))
                while True:
                    await asyncio.sleep(get('trigger')(TriggerInput(False, self.initial_delay, self.interval),
                        operation='check', check_interval=self.check_interval))
                    states = await asyncio.to_thread(self.checkpoint)
                    callback = self.jobs.get('nudges')
                    if callback is not None:
                        try:
                            await self._run_callback(callback)
                        except Exception as error:
                            _LOGGER.warning('daily_job_failed job=nudges exception_type=%s', type(error).__name__)
                    callback = self.jobs.get('consolidation')
                    for project, state in states.items():
                        if callback and get('trigger')(None, operation='due', score=state['score'], run_seconds=state['run_seconds']):
                            try:
                                await self._run_callback(partial(callback, project))
                            except Exception as error:
                                _LOGGER.warning('daily_job_failed job=consolidation exception_type=%s', type(error).__name__)
                    if self._fallback_elapsed >= self.interval:
                        await self._run_automatic()
                        self._fallback_elapsed = 0
                return
            await asyncio.sleep(get('trigger')(TriggerInput(True, self.initial_delay, self.interval)))
            while True:
                await self._run_automatic()
                await asyncio.sleep(get('trigger')(TriggerInput(False, self.initial_delay, self.interval)))

    async def _run_callback(self, callback):
        if self.server_jobs is not None:
            return await self.server_jobs.submit(self.user_id, 'daily', callback)
        return await asyncio.to_thread(callback)

    async def run_once(self, *, exclude=()):
        return await self._run_callback(partial(self.run, exclude=exclude))

    async def _run_automatic(self, *, exclude=()):
        try:
            await self.run_once(exclude=exclude)
        except Exception as error:
            _LOGGER.warning('daily_job_paused exception_type=%s', type(error).__name__)

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


class DailyBackup:
    """Date leases and failure projection use independent revisioned records."""
    def __init__(self, runtime_root, records, *, now=lambda: datetime.now(timezone.utc)):
        self.runtime_root, self.records, self.now = runtime_root, records, now

    def fail(self, identity, *, automatic, reason_code='backup_failed'):
        with self.records.begin() as tx:
            prior = tx.read('v2_backup_jobs', identity)
            tx.put('v2_backup_jobs', identity, {'status': 'failed', 'automatic': automatic,
                'snapshot_id': identity, 'reason_code': reason_code, 'updated_at': self.now().isoformat()},
                expected_revision=prior.revision if prior else 0)
            tx.commit()

    def failures(self):
        from backend.memory_app.backup import runtime_backup_roots, verification_metadata, read_backup_catalog
        rows = {row.object_id: row.payload for row in self.records.list('v2_backup_jobs')}
        failed = {identity: value for identity, value in rows.items() if value.get('status') == 'failed'}
        _, backups, _ = runtime_backup_roots(self.runtime_root)
        current_snapshots = set()
        for identity, value in rows.items():
            snapshot_id = value.get('snapshot_id')
            if not isinstance(snapshot_id, str):
                continue
            current_snapshots.add(snapshot_id)
            if value.get('status') != 'done':
                continue
            # Retention removes old successful snapshots without changing their
            # date checkpoint. Absence alone is not a failed self-check.
            if not (backups / snapshot_id).exists():
                continue
            try:
                meta = verification_metadata(backups / snapshot_id)
            except (OSError, ValueError):
                meta = {}
            if meta.get('verified') is not True:
                failed[identity] = {**value, 'status': 'failed', 'reason_code': 'backup_verification_failed'}
        if backups.is_dir() and not backups.is_symlink():
            for snapshot in backups.glob('snap-*'):
                if not snapshot.is_dir() or snapshot.is_symlink() or snapshot.name in current_snapshots:
                    continue
                try:
                    meta = verification_metadata(snapshot)
                except (OSError, ValueError):
                    continue
                # Old automatic files never become new jobs, nor do superseded
                # failed files recreate their already completed logical job.
                if meta.get('automatic') is True or meta.get('job_id') is not None:
                    continue
                if read_backup_catalog(snapshot) is None:
                    continue
                if meta.get('verified') is False and meta.get('reason_code') != 'backup_verification_pending':
                    if rows.get(snapshot.name, {}).get('status') in {'done', 'running'}:
                        continue
                    failed.setdefault(snapshot.name, {'automatic': False, 'snapshot_id': snapshot.name,
                        'updated_at': meta.get('created_at'), 'reason_code': 'backup_verification_failed'})
        return failed

    def retry(self, identity):
        failures = self.failures()
        if identity not in failures:
            row = self.records.read('v2_backup_jobs', identity)
            return row.payload if row and row.payload.get('status') in {'done', 'running'} else None
        return self.run(identity=identity, automatic=failures[identity].get('automatic') is True,
            retry=True, expected_snapshot_id=failures[identity].get('snapshot_id'))

    def run(self, *, identity=None, automatic=True, retry=False, expected_snapshot_id=None):
        from backend.shared.interprocess_lock import interprocess_file_lock
        from backend.memory_app.backup import runtime_backup_roots
        _, backups, _ = runtime_backup_roots(self.runtime_root)
        try:
            with interprocess_file_lock(backups.parent / 'daily-backup', timeout_seconds=0):
                return self._run_locked(identity=identity, automatic=automatic, retry=retry,
                    expected_snapshot_id=expected_snapshot_id)
        except TimeoutError:
            return {'status': 'running'}

    def _run_locked(self, *, identity=None, automatic=True, retry=False, expected_snapshot_id=None):
        from backend.memory_app.backup import (backup_runtime, runtime_backup_roots, write_backup_catalog,
            prune_automatic_backups, verification_metadata)
        now = self.now()
        identity = identity or now.date().isoformat()
        active, backups, _ = runtime_backup_roots(self.runtime_root)
        snapshot_id = f"snap-{'auto-' if automatic else ''}{now.strftime('%Y%m%dt%H%M%Sz')}-{uuid4().hex[:8]}"
        with self.records.begin() as tx:
            prior = tx.read('v2_backup_jobs', identity)
            if prior and prior.payload.get('status') == 'done':
                if not retry or expected_snapshot_id != prior.payload.get('snapshot_id'):
                    return prior.payload
                try:
                    current = verification_metadata(backups / expected_snapshot_id)
                except (OSError, ValueError):
                    current = {}
                if current.get('verified') is True:
                    return prior.payload
            reservation = tx.put('v2_backup_jobs', identity, {'status': 'running', 'automatic': automatic,
                'snapshot_id': snapshot_id, 'updated_at': now.isoformat()}, expected_revision=prior.revision if prior else 0)
            tx.commit()
        try:
            snapshot = backup_runtime(active, backups,
                snapshot_id=snapshot_id, automatic=automatic, job_id=identity if automatic else None)
            write_backup_catalog(snapshot.snapshot_root, {'schema_version': '2.0.0', 'id': snapshot.snapshot_id,
                'created_at': now.isoformat(), 'label': '', 'namespace_id': 'default', 'layer_fingerprints': {},
                'layer_counts': {}, 'notes': '', 'restorable': True,
                'vault_fingerprint': snapshot.source_fingerprint, 'backup_file_count': snapshot.file_count})
            payload = {'status': 'done', 'automatic': automatic, 'updated_at': now.isoformat(), 'snapshot_id': snapshot.snapshot_id}
        except Exception as error:
            payload = {'status': 'failed', 'automatic': automatic, 'updated_at': now.isoformat(),
                'snapshot_id': snapshot_id, 'reason_code': getattr(error, 'reason_code', 'backup_failed')}
        try:
            prune_automatic_backups(backups)
        except OSError:
            payload = {'status': 'failed', 'automatic': automatic, 'updated_at': now.isoformat(),
                'snapshot_id': snapshot_id, 'reason_code': 'backup_prune_failed'}
        with self.records.begin() as tx:
            tx.put('v2_backup_jobs', identity, payload, expected_revision=reservation.revision)
            tx.commit()
        return payload


def install_daily_jobs(application, *, records=None, runtime_root=None):
    jobs = DailyJobs(records=records, server_jobs=getattr(application.state, 'server_jobs', None),
        user_id=getattr(application.state, 'server_user_id', None))
    application.state.memory_daily_jobs = jobs
    if records is not None:
        from .signals import SignalService
        signals = getattr(application.state, 'memory_signals', None) or SignalService(records)
        jobs.register('signals_rollup', signals.rollup)
        gaps = getattr(application.state, 'gaps', None)
        if gaps is not None:
            jobs.register('gaps', gaps.refresh)
    if records is not None and runtime_root is not None:
        backup = DailyBackup(runtime_root, records)
        application.state.memory_backup = backup
        jobs.register('backup', backup.run)
    application.router.on_startup.append(jobs.start)
    application.router.on_shutdown.append(jobs.stop)
    return jobs
