"""One process scheduler for user-scoped heavy jobs and persistent daily quotas."""
from __future__ import annotations

import asyncio
from collections import Counter, deque
from contextvars import Context, copy_context
from contextlib import asynccontextmanager, contextmanager, ExitStack, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
from pathlib import Path
import stat
from threading import Lock
import time

from backend.security.user_context import USER_ACCESS, authorize_user
from core.storage_provider.connection_scope import capture_connection_scope, connection_scope
from core.storage_provider.vault_backup_restore import _native_io_path


class QuotaError(ValueError):
    """Fixed resource-paused reason for API/tray projection."""


class _IntakeBudget:
    def __init__(self, scheduler, user_id):
        self.scheduler, self.user_id = scheduler, user_id
        self.baseline, self.reserved = scheduler.storage_bytes(user_id), 0
        self.active = True

    def _basis(self):
        if not self.active:
            raise QuotaError('intake_admission_expired')
        user = self.scheduler._user(self.user_id)
        return user, max(self.baseline + self.reserved, self.scheduler.storage_bytes(self.user_id))

    def __call__(self, bytes_delta):
        if type(bytes_delta) is not int or bytes_delta < 0:
            raise ValueError('intake_bytes_invalid')
        user, accounted = self._basis()
        limit = user['storage_limit_mb']
        if limit is not None and accounted + bytes_delta > limit * 1024 * 1024:
            raise QuotaError('storage_quota_exceeded')
        self.reserved += bytes_delta

    def reserve_up_to(self, cap_bytes):
        if type(cap_bytes) is not int or cap_bytes < 1:
            raise ValueError('intake_cap_invalid')
        user, accounted = self._basis()
        limit = user['storage_limit_mb']
        amount = cap_bytes if limit is None else min(cap_bytes, max(0, limit * 1024 * 1024 - accounted))
        if amount < 1:
            raise QuotaError('storage_quota_exceeded')
        self.reserved += amount
        return _FiniteIntakeCap(self, amount, user['revision'])


class _FiniteIntakeCap:
    def __init__(self, budget, amount, user_revision):
        self.budget, self.remaining, self.user_revision = budget, amount, user_revision
        self.active, self._reader = True, None

    @contextmanager
    def using_reader(self, reader):
        if self._reader is not None:
            raise QuotaError('intake_cap_reader_conflict')
        self._reader = reader
        try:
            self.validate()
            yield self
        finally:
            self._reader = None

    def validate(self):
        if not self.active or not self.budget.active:
            raise QuotaError('intake_admission_expired')
        reader = self._reader if self._reader is not None else self.budget.scheduler.users.records
        user = reader.read('server_users', self.budget.user_id)
        if (user is None or user.revision != self.user_revision or user.payload.get('disabled_at') is not None
                or reader.read('server_user_deletions', self.budget.user_id) is not None):
            raise QuotaError('intake_account_changed')

    def consume(self, size):
        self.validate()
        if type(size) is not int or size < 0:
            raise ValueError('intake_bytes_invalid')
        if size > self.remaining:
            raise QuotaError('intake_phase_budget_exceeded')
        self.remaining -= size

    def close(self):
        if self.active:
            self.budget.reserved -= self.remaining
            self.active = False


async def retain_until_finished(awaitable):
    """Defer repeated cancellation until the real executor has settled."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


@dataclass
class _Job:
    user_id: str
    kind: str
    callback: object
    future: asyncio.Future
    context: object
    retain_running: bool = False
    execution: object = None
    started: bool = False
    unit: object = None


class ServerJobScheduler:
    def __init__(self, users, *, concurrency=1, monotonic=time.monotonic, clock=None, execution_lease=None,
                 related_storage_roots=None):
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError('server_concurrency_invalid')
        self.users, self.concurrency = users, concurrency
        self.monotonic = monotonic
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._queues, self._rotation = {}, deque()
        self._ready, self._workers, self._closed = asyncio.Event(), [], False
        self._last_user = None
        self._active, self._ledger_failed = Counter(), False
        self._intake_locks = {}
        self._intake_locks_guard = Lock()
        self.related_storage_roots = related_storage_roots
        self.execution_lease = execution_lease
        self.admission = None

    @property
    def closed(self):
        return self._closed

    @asynccontextmanager
    async def intake(self, user_id):
        async with self.intake_many((user_id,)) as budgets:
            yield budgets[user_id]

    def _intake_lock(self, user_id):
        with self._intake_locks_guard:
            return self._intake_locks.setdefault(user_id, Lock())

    @contextmanager
    def storage_guard(self, user_ids):
        # 同步转让与异步入库共用同一组锁，始终按用户编号取得。
        with ExitStack() as stack:
            for user_id in sorted(set(user_ids)):
                lock = self._intake_lock(user_id)
                if not lock.acquire(blocking=False):
                    raise QuotaError('storage_admission_in_progress')
                stack.callback(lock.release)
            yield

    @asynccontextmanager
    async def intake_many(self, user_ids):
        user_ids = sorted(set(user_ids))
        locks, budgets = [], {}
        try:
            for user_id in user_ids:
                lock = self._intake_lock(user_id)
                # 等待容量锁时不占用实际文件和 SQLite 作业使用的线程池。
                while not lock.acquire(blocking=False):
                    await asyncio.sleep(0.05)
                locks.append(lock)
            for user_id in user_ids:
                self.check_intake(user_id)
                budgets[user_id] = _IntakeBudget(self, user_id)
            try:
                yield budgets
            finally:
                for budget in budgets.values():
                    budget.active = False
        finally:
            for lock in reversed(locks):
                lock.release()

    def active_for(self, user_id):
        return self._active[user_id]

    def pending_for(self, user_id):
        return self._active[user_id] + sum(not job.future.cancelled() for job in self._queues.get(user_id, ()))

    def _user(self, user_id):
        user = self.users.get(user_id)
        if self.users.deletion_pending(user_id):
            raise QuotaError('user_deletion_in_progress')
        if user['disabled_at'] is not None:
            raise QuotaError('user_disabled')
        return user

    def storage_bytes(self, user_id, *, reader=None):
        related = (self.related_storage_roots(user_id, reader=reader)
                   if self.related_storage_roots is not None else nullcontext(()))
        with related as roots:
            return self._storage_bytes(user_id, roots)

    def _storage_bytes(self, user_id, related_roots):
        roots = (self.users.root_for(user_id), *related_roots)
        distinct, total = set(), 0
        for root in roots:
            root = Path(root).absolute()
            if root in distinct:
                continue
            distinct.add(root)
            total += self._root_bytes(root)
        return total

    @staticmethod
    def _root_bytes(root):
        # 与备份和删除预检共用扩展路径，避免长文件被 is_file 静默漏算。
        root = Path(_native_io_path(root))
        try:
            information = root.lstat()
        except FileNotFoundError:
            return 0
        if (not stat.S_ISDIR(information.st_mode) or stat.S_ISLNK(information.st_mode)
                or getattr(information, 'st_file_attributes', 0) & 0x400):
            raise QuotaError('user_storage_invalid')
        total = 0
        for path in root.rglob('*'):
            try:
                information = path.lstat()
                if (stat.S_ISLNK(information.st_mode)
                        or getattr(information, 'st_file_attributes', 0) & 0x400
                        or not path.resolve().is_relative_to(root)):
                    raise QuotaError('user_storage_invalid')
                if stat.S_ISREG(information.st_mode):
                    total += information.st_size
            except FileNotFoundError:
                # 统计只读取一次元数据，已枚举后消失的文件不计入本次快照。
                continue
        return total

    def check_intake(self, user_id):
        user = self._user(user_id)
        limit = user['storage_limit_mb']
        if limit is not None and self.storage_bytes(user_id) >= limit * 1024 * 1024:
            raise QuotaError('storage_quota_exceeded')

    def _usage_id(self, user_id, day):
        return 'usage-' + day + '-' + user_id

    def job_seconds(self, user_id):
        day = self.clock().astimezone(timezone.utc).date().isoformat()
        row = self.users.records.read('server_job_usage', self._usage_id(user_id, day))
        return row.payload['seconds'] if row is not None else 0.0

    def _check_job(self, user_id):
        if self._ledger_failed:
            raise QuotaError('job_usage_unavailable')
        user = self._user(user_id)
        self.check_intake(user_id)
        limit = user['job_minutes_per_day']
        if limit is not None and self.job_seconds(user_id) >= limit * 60:
            raise QuotaError('job_quota_exceeded')

    def _record_usage(self, user_id, day, seconds):
        identity = self._usage_id(user_id, day)
        with self.users.records.begin() as tx:
            row = tx.read('server_job_usage', identity)
            tx.put('server_job_usage', identity, {'user_id': user_id, 'day': day,
                'seconds': (row.payload['seconds'] if row else 0.0) + max(0.0, seconds)},
                expected_revision=row.revision if row else 0)
            tx.commit()

    async def submit(self, user_id, kind, callback, *, retain_running=False):
        if self._closed:
            raise QuotaError('server_jobs_closed')
        if self.admission is not None and not self.admission(user_id):
            raise QuotaError('runtime_restore_in_progress')
        access = USER_ACCESS.get()
        if access is not None and access.target_user_id != user_id:
            raise ValueError('job_target_mismatch')
        self._check_job(user_id)
        future = asyncio.get_running_loop().create_future()
        # 排队之前保留请求工作单元，避免请求返回后只剩失效的上下文引用。
        job = _Job(user_id, kind, callback, future, copy_context(), retain_running,
                   unit=capture_connection_scope())
        if user_id not in self._queues:
            self._queues[user_id] = deque()
            self._rotation.append(user_id)
        self._queues[user_id].append(job)
        self._ready.set()
        if not self._workers:
            # 常驻 worker 不继承首请求资源；实际任务仍使用各自捕获的完整上下文。
            self._workers = [asyncio.create_task(self._worker(), context=Context())
                             for _ in range(self.concurrency)]
        try:
            return await future
        except asyncio.CancelledError:
            future.cancel()
            queue = self._queues.get(user_id)
            if queue is not None and job in queue:
                queue.remove(job)
                if not queue:
                    del self._queues[user_id]
                    self._rotation.remove(user_id)
                self._release_unit(job)
            if job.execution is not None and not job.started:
                job.execution.cancel()
            raise

    def _release_unit(self, job):
        # 排队取消、close 与执行完成可能交接同一任务，捕获的所有者只释放一次。
        unit, job.unit = job.unit, None
        if unit is not None:
            unit.close()

    def _next(self):
        if not self._rotation:
            return None
        if len(self._rotation) > 1 and self._rotation[0] == self._last_user:
            self._rotation.rotate(-1)
        user_id = self._rotation.popleft()
        job = self._queues[user_id].popleft()
        if self._queues[user_id]:
            self._rotation.append(user_id)
        else:
            del self._queues[user_id]
        self._last_user = user_id
        return job

    def _validate_access(self, job):
        access = USER_ACCESS.get()
        if access is not None:
            current = authorize_user(self.users, access.caller, job.user_id)
            if current != access:
                raise ValueError('job_identity_changed')

    async def _execute(self, job):
        # 无请求的后台任务在此建立原工作单元，同步线程真正结束后才退出。
        with connection_scope(job.unit):
            self._validate_access(job)
            if self.execution_lease is not None:
                async with self.execution_lease(job.user_id):
                    if job.future.cancelled():
                        raise asyncio.CancelledError
                    self._validate_access(job)
                    self._check_job(job.user_id)
                    job.started = True
                    return await self._invoke_callback(job.callback, retain_running=job.retain_running)
            job.started = True
            return await self._invoke_callback(job.callback, retain_running=job.retain_running)

    async def _invoke_callback(self, callback, *, retain_running=False):
        operation = self._call_callback(callback)
        return await retain_until_finished(operation) if retain_running else await operation

    async def _call_callback(self, callback):
        if inspect.iscoroutinefunction(callback):
            return await callback()
        # Cancelling a waiter cannot stop a Python thread. Retain the worker
        # lease, and therefore concurrency/user lifetime, until real completion.
        result = await retain_until_finished(asyncio.to_thread(callback))
        if inspect.isawaitable(result):
            return await result
        return result

    async def _worker(self):
        while True:
            job = self._next()
            if job is None:
                self._ready.clear()
                await self._ready.wait()
                continue
            if job.future.cancelled():
                self._release_unit(job)
                continue
            started = None
            result, failure, cancelled = None, None, False
            try:
                day = self.clock().astimezone(timezone.utc).date().isoformat()
                self._check_job(job.user_id)
                started = self.monotonic()
                self._active[job.user_id] += 1
                execution = asyncio.create_task(self._execute(job), context=job.context)
                job.execution = execution
                result = await execution
            except asyncio.CancelledError:
                cancelled = True
            except Exception as error:
                failure = error
            finally:
                if started is not None:
                    try:
                        self._record_usage(job.user_id, day, self.monotonic() - started)
                    except Exception:
                        self._ledger_failed = True
                        failure = QuotaError('job_usage_unavailable')
                    finally:
                        self._active[job.user_id] -= 1
                self._release_unit(job)
            if not job.future.done():
                if cancelled:
                    job.future.cancel()
                elif failure is not None:
                    job.future.set_exception(failure)
                else:
                    job.future.set_result(result)
            if cancelled and (self._closed or asyncio.current_task().cancelling()):
                raise asyncio.CancelledError

    async def close(self):
        self._closed = True
        for jobs in self._queues.values():
            for job in jobs:
                job.future.cancel()
                self._release_unit(job)
        self._queues.clear()
        self._rotation.clear()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
