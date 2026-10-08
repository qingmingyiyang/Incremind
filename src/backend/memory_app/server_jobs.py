"""One process scheduler for user-scoped heavy jobs and persistent daily quotas."""
from __future__ import annotations

import asyncio
from collections import Counter, deque
from contextvars import Context, copy_context
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
import time

from backend.security.user_context import USER_ACCESS, authorize_user
from core.storage_provider.connection_scope import capture_connection_scope, connection_scope


class QuotaError(ValueError):
    """Fixed resource-paused reason for API/tray projection."""


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
    def __init__(self, users, *, concurrency=1, monotonic=time.monotonic, clock=None, execution_lease=None):
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
        self.execution_lease = execution_lease

    @asynccontextmanager
    async def intake(self, user_id):
        """Serialize user admission through the real owner persistence boundary."""
        lock = self._intake_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            self.check_intake(user_id)
            baseline, reserved = self.storage_bytes(user_id), 0
            def check(bytes_delta):
                nonlocal reserved
                if type(bytes_delta) is not int or bytes_delta < 0:
                    raise ValueError('intake_bytes_invalid')
                user = self._user(user_id)
                limit = user['storage_limit_mb']
                projected = max(baseline + reserved, self.storage_bytes(user_id)) + bytes_delta
                if limit is not None and projected > limit * 1024 * 1024:
                    raise QuotaError('storage_quota_exceeded')
                reserved += bytes_delta
            yield check

    def active_for(self, user_id):
        return self._active[user_id]

    def pending_for(self, user_id):
        return self._active[user_id] + sum(not job.future.cancelled() for job in self._queues.get(user_id, ()))

    def _user(self, user_id):
        user = self.users.get(user_id)
        if user['disabled_at'] is not None:
            raise QuotaError('user_disabled')
        return user

    def storage_bytes(self, user_id):
        root = self.users.root_for(user_id)
        total = 0
        for path in root.rglob('*'):
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise QuotaError('user_storage_invalid')
            if path.is_file():
                total += path.stat().st_size
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
