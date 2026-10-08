"""Shared round-robin heavy jobs use fake work/clocks and real quota records."""
import asyncio
from datetime import datetime, timezone

import pytest


def setup(tmp_path):
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    devices = DeviceRegistry(tmp_path / 'server')
    paired = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='管理电脑')
    admin = devices.authenticate(paired['key'])
    users = ServerUsers(tmp_path, records=devices.records)
    a, b = users.create(admin, name='甲'), users.create(admin, name='乙')
    return users, admin, a, b


@pytest.mark.parametrize('change,code',[('role','user_forbidden'),('device','user_unauthorized'),('target','user_disabled')])
def test_queued_admin_context_is_revalidated_before_any_real_callback(tmp_path,change,code):
    from backend.memory_app.server_jobs import ServerJobScheduler
    from backend.security.user_context import UserAccess,user_context,UserError
    users,admin,a,b=setup(tmp_path)
    async def scenario():
        scheduler=ServerJobScheduler(users)
        gate,started=asyncio.Event(),asyncio.Event();calls=[]
        async def blocking(): started.set();await gate.wait()
        first=asyncio.create_task(scheduler.submit(a['user_id'],'image',blocking))
        await started.wait()
        with user_context(UserAccess(admin,b['user_id'],'admin')):
            queued=asyncio.create_task(scheduler.submit(b['user_id'],'transcribe',lambda:calls.append('executed')))
        await asyncio.sleep(0)
        with users.records.begin() as tx:
            collection,identity,patch=('server_users','local-user',{'role':'user'}) if change=='role' else (
                ('server_devices',admin.device_id,{'revoked_at':'2026-10-05T00:00:00Z'}) if change=='device' else
                ('server_users',b['user_id'],{'disabled_at':'2026-10-05T00:00:00Z'}))
            row=tx.read(collection,identity)
            tx.put(collection,identity,{**row.payload,**patch},expected_revision=row.revision);tx.commit()
        gate.set();await first
        with pytest.raises(ValueError,match=code): await queued
        assert calls==[]
        await scheduler.close()
    asyncio.run(scenario())


def test_jobs_rotate_between_users_and_global_default_is_one(tmp_path):
    from backend.memory_app.server_jobs import ServerJobScheduler
    users, _, a, b = setup(tmp_path)
    async def scenario():
        scheduler = ServerJobScheduler(users)
        gate, started = asyncio.Event(), asyncio.Event()
        order, live, peak = [], 0, 0
        async def job(name):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            order.append(name)
            if name == 'a1':
                started.set()
                await gate.wait()
            live -= 1
        first = asyncio.create_task(scheduler.submit(a['user_id'], 'image', lambda: job('a1')))
        await started.wait()
        pending = [asyncio.create_task(scheduler.submit(owner['user_id'], 'daily', lambda name=name: job(name)))
                   for owner, name in ((a, 'a2'), (a, 'a3'), (b, 'b1'), (b, 'b2'))]
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(first, *pending)
        assert order == ['a1', 'b1', 'a2', 'b2', 'a3']
        assert peak == 1
        await scheduler.close()
    asyncio.run(scenario())


def test_configured_concurrency_limit_is_real_and_close_settles_queued_tasks(tmp_path):
    from backend.memory_app.server_jobs import ServerJobScheduler
    users, _, a, b = setup(tmp_path)
    async def scenario():
        scheduler = ServerJobScheduler(users, concurrency=2)
        gates, two_started = asyncio.Event(), asyncio.Event()
        live = peak = 0
        async def job():
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            if live == 2:
                two_started.set()
            try:
                await gates.wait()
            finally:
                live -= 1
        running = [asyncio.create_task(scheduler.submit(owner['user_id'], 'transcribe', job)) for owner in (a, b)]
        await two_started.wait()
        assert peak == 2
        waiting = asyncio.create_task(scheduler.submit(a['user_id'], 'image', job))
        await asyncio.sleep(0)
        await scheduler.close()
        outcomes = await asyncio.gather(*running, waiting, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in outcomes)
        assert live == 0
    asyncio.run(scenario())


def test_storage_and_daily_time_quotas_pause_new_work_but_not_other_user(tmp_path):
    from backend.memory_app.server_jobs import ServerJobScheduler, QuotaError
    users, admin, a, b = setup(tmp_path)
    elapsed = [0.0]
    date = [datetime(2026, 10, 5, tzinfo=timezone.utc)]
    users.update(admin, a['user_id'], expected_revision=1, job_minutes_per_day=1)
    scheduler = ServerJobScheduler(users, monotonic=lambda: elapsed[0], clock=lambda: date[0])
    async def scenario():
        called = []
        async def first():
            called.append('a')
            elapsed[0] += 60
        await scheduler.submit(a['user_id'], 'image', first)
        with pytest.raises(QuotaError, match='job_quota_exceeded'):
            await scheduler.submit(a['user_id'], 'daily', first)
        await scheduler.submit(b['user_id'], 'daily', lambda: called.append('b'))
        assert called == ['a', 'b']
        date[0] = datetime(2026, 10, 6, tzinfo=timezone.utc)
        await scheduler.submit(a['user_id'], 'image', first)
        users.update(admin, a['user_id'], expected_revision=2, storage_limit_mb=0)
        with pytest.raises(QuotaError, match='storage_quota_exceeded'):
            scheduler.check_intake(a['user_id'])
        assert users.get(a['user_id'])['name'] == '甲'
        assert users.get(b['user_id'])['name'] == '乙'
        await scheduler.close()
    asyncio.run(scenario())
    assert len(users.records.list('server_job_usage')) == 3


def test_queued_job_checks_disabled_and_quota_again_before_start(tmp_path):
    from backend.memory_app.server_jobs import ServerJobScheduler, QuotaError
    users, admin, a, b = setup(tmp_path)
    async def scenario():
        scheduler = ServerJobScheduler(users)
        gate, started = asyncio.Event(), asyncio.Event()
        called = []
        async def first():
            started.set()
            await gate.wait()
        running = asyncio.create_task(scheduler.submit(a['user_id'], 'image', first))
        await started.wait()
        queued = asyncio.create_task(scheduler.submit(b['user_id'], 'daily', lambda: called.append('forbidden')))
        await asyncio.sleep(0)
        users.update(admin, b['user_id'], expected_revision=1, disabled=True)
        gate.set()
        await running
        with pytest.raises(QuotaError, match='user_disabled'):
            await queued
        assert called == []
        await scheduler.close()
    asyncio.run(scenario())


def test_actual_usage_write_failure_settles_and_fail_closes_without_killing_worker(tmp_path):
    from backend.memory_app.server_jobs import ServerJobScheduler, QuotaError
    users, _, a, b = setup(tmp_path)
    with users.records.begin() as tx:
        tx.connection.execute("CREATE TRIGGER reject_usage BEFORE INSERT ON crp_structured_records WHEN NEW.collection='server_job_usage' BEGIN SELECT RAISE(ABORT,'ledger_refused'); END")
        tx.commit()
    async def scenario():
        scheduler = ServerJobScheduler(users)
        called = []
        with pytest.raises(QuotaError, match='job_usage_unavailable'):
            await asyncio.wait_for(scheduler.submit(a['user_id'], 'image', lambda: called.append('a')), 3)
        with pytest.raises(QuotaError, match='job_usage_unavailable'):
            await asyncio.wait_for(scheduler.submit(b['user_id'], 'daily', lambda: called.append('b')), 3)
        assert called == ['a']
        assert all(not worker.done() for worker in scheduler._workers)
        await scheduler.close()
    asyncio.run(scenario())


def test_blocking_thread_retains_execution_lease_until_real_finish_on_cancel_and_close(tmp_path):
    from threading import Event
    from backend.memory_app.server_jobs import ServerJobScheduler
    users, _, a, b = setup(tmp_path)
    started, release = Event(), Event()
    async def scenario():
        scheduler = ServerJobScheduler(users)
        called = []
        def blocking():
            called.append('a')
            started.set()
            assert release.wait(5)
        running = asyncio.create_task(scheduler.submit(a['user_id'], 'image', blocking))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            assert scheduler.active_for(a['user_id']) == 1
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            waiting = asyncio.create_task(scheduler.submit(b['user_id'], 'daily', lambda: called.append('b')))
            await asyncio.sleep(0)
            closing = asyncio.create_task(scheduler.close())
            await asyncio.sleep(0)
            assert not closing.done() and scheduler.active_for(a['user_id']) == 1
            assert called == ['a']
            release.set()
            await asyncio.wait_for(closing, 3)
            await asyncio.gather(waiting, return_exceptions=True)
            assert scheduler.active_for(a['user_id']) == 0
            assert called == ['a']
        finally:
            release.set()
            await scheduler.close()
    asyncio.run(scenario())


def test_scheduler_owns_sqlite_scope_across_background_queue_and_thread_completion(tmp_path):
    import sqlite3
    from threading import Event
    from backend.memory_app.server_jobs import ServerJobScheduler
    from backend.security.user_context import USER_ACCESS, authorize_user, user_context
    from backend.shared.server_resources import RESOURCE_POOL, SharedResources, resource_context
    from core.storage_provider.connection_scope import _SCOPE, connection_scope
    from core.storage_provider.observability import Observation, observation_scope
    from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore

    users, admin, a, b = setup(tmp_path)
    resources = SharedResources(tmp_path)
    stores = {}
    accesses = {}
    for user in (a, b):
        identity = user['user_id']
        stores[identity] = SQLiteStructuredRecordStore(users.root_for(identity) / 'scope-evidence.sqlite3')
        accesses[identity] = authorize_user(users, admin, identity)
        with stores[identity].begin() as tx:
            tx.put('checks', 'own', {'user_id': identity}, expected_revision=0)
            tx.commit()

    def read(user, label):
        identity = user['user_id']
        assert USER_ACCESS.get() == accesses[identity]
        assert RESOURCE_POOL.get() is resources
        observation = Observation('scheduler', label)
        # 只计回调内真实读取，夹具写入与服务器配额事务不进入连接预算。
        with observation_scope(observation):
            one = stores[identity].read('checks', 'own')
            two = stores[identity].read('checks', 'own')
            lease = stores[identity]._connect()
            raw = getattr(lease, 'connection', lease)
            lease.close()
        assert one.payload == two.payload == {'user_id': identity}
        return raw, _SCOPE.get(), observation.snapshot()['connection_count']

    def warm(user):
        with stores[user['user_id']]._connect() as lease:
            return lease.connection

    def closed(raw):
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            raw.execute('SELECT 1')

    async def background_forms():
        scheduler = ServerJobScheduler(users)
        results = []
        try:
            async def asynchronous():
                await asyncio.sleep(0)
                return read(a, 'async')
            def synchronous():
                return read(b, 'thread')
            def awaitable():
                first = read(a, 'awaitable-thread')
                async def finish():
                    await asyncio.sleep(0)
                    return first, read(a, 'awaitable-async')
                return finish()
            for user, callback in ((a, asynchronous), (b, synchronous), (a, awaitable)):
                with user_context(accesses[user['user_id']]), resource_context(resources):
                    results.append(await scheduler.submit(user['user_id'], 'daily', callback))
            first, second, (thread, asynchronous_result) = results
            assert first[2] == second[2] == thread[2] == 1
            assert asynchronous_result[2] == 0
            assert thread[0] is asynchronous_result[0] and thread[1] is asynchronous_result[1]
            assert first[0] is not second[0]
            for raw, unit, count in (first, second, thread, asynchronous_result):
                assert unit is not None and unit.ended
                closed(raw)
            assert USER_ACCESS.get() is RESOURCE_POOL.get() is _SCOPE.get() is None
        finally:
            await scheduler.close()

    async def queued_after_request():
        scheduler = ServerJobScheduler(users)
        gate, started = asyncio.Event(), asyncio.Event()
        async def blocking():
            started.set()
            await gate.wait()
        first = asyncio.create_task(scheduler.submit(a['user_id'], 'image', blocking))
        queued = None
        try:
            await started.wait()
            with connection_scope() as unit, user_context(accesses[b['user_id']]), resource_context(resources):
                raw = warm(b)
                queued = asyncio.create_task(scheduler.submit(b['user_id'], 'daily', lambda: read(b, 'queued')))
                await asyncio.sleep(0)
                assert scheduler.pending_for(b['user_id']) == 1
            prematurely_closed = unit.ended
            gate.set()
            outcomes = await asyncio.gather(first, queued, return_exceptions=True)
            assert not prematurely_closed, '排队任务没有保留请求工作单元'
            assert outcomes[0] is None and not isinstance(outcomes[1], BaseException), outcomes
            result_raw, result_unit, count = outcomes[1]
            assert result_raw is raw and result_unit is unit and count == 0
            assert unit.ended
            closed(raw)
        finally:
            gate.set()
            await scheduler.close()
            await asyncio.gather(first, *([queued] if queued is not None else []), return_exceptions=True)

    async def worker_after_first_request():
        scheduler = ServerJobScheduler(users)
        try:
            with connection_scope() as first_unit, user_context(accesses[a['user_id']]), resource_context(resources):
                first = await scheduler.submit(a['user_id'], 'daily', lambda: read(a, 'first-request'))
                assert first[1] is first_unit and not first_unit.ended
            assert first_unit.ended
            closed(first[0])
            with user_context(accesses[b['user_id']]), resource_context(resources):
                second = await scheduler.submit(b['user_id'], 'daily', lambda: read(b, 'next-background'))
            assert second[1] is not first_unit and second[1].ended and second[2] == 1
            closed(second[0])
            assert all(not worker.done() for worker in scheduler._workers)
        finally:
            await scheduler.close()

    async def cancelled_queue_and_retained_thread():
        scheduler = ServerJobScheduler(users)
        entered, release = Event(), Event()
        calls, completed, running, queued, closing = [], [], None, [], None
        def blocking():
            first = read(a, 'retained-before')
            entered.set()
            # 三秒仅作防挂死等待，不是性能验收；使用事件确定交接顺序。
            assert release.wait(3)
            second = read(a, 'retained-after')
            assert first[0] is second[0] and first[1] is second[1]
            assert not second[1].ended
            completed.append(second)
            return second
        try:
            with connection_scope() as running_unit, user_context(accesses[a['user_id']]), resource_context(resources):
                running_raw = warm(a)
                running = asyncio.create_task(scheduler.submit(a['user_id'], 'image', blocking))
                assert await asyncio.to_thread(entered.wait, 3)
            assert not running_unit.ended
            running.cancel()
            assert isinstance((await asyncio.gather(running, return_exceptions=True))[0], asyncio.CancelledError)
            with connection_scope() as cancelled_unit, user_context(accesses[b['user_id']]), resource_context(resources):
                cancelled_raw = warm(b)
                cancelled = asyncio.create_task(scheduler.submit(b['user_id'], 'daily', lambda: calls.append('cancelled')))
                queued.append(cancelled)
                await asyncio.sleep(0)
                assert scheduler.pending_for(b['user_id']) == 1
            assert not cancelled_unit.ended
            cancelled.cancel()
            assert isinstance((await asyncio.gather(cancelled, return_exceptions=True))[0], asyncio.CancelledError)
            assert cancelled_unit.ended
            closed(cancelled_raw)
            with connection_scope() as closing_unit, user_context(accesses[b['user_id']]), resource_context(resources):
                closing_raw = warm(b)
                waiting = asyncio.create_task(scheduler.submit(b['user_id'], 'daily', lambda: calls.append('closing')))
                queued.append(waiting)
                await asyncio.sleep(0)
                assert scheduler.pending_for(b['user_id']) == 1
            assert not closing_unit.ended
            closing = asyncio.create_task(scheduler.close())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not closing.done() and scheduler.active_for(a['user_id']) == 1
            assert not running_unit.ended and calls == []
            release.set()
            await asyncio.wait_for(closing, 3)
            assert isinstance((await asyncio.gather(waiting, return_exceptions=True))[0], asyncio.CancelledError)
            assert running_unit.ended and closing_unit.ended
            closed(running_raw)
            closed(closing_raw)
            assert calls == [] and scheduler.active_for(a['user_id']) == 0
            assert completed == [(running_raw, running_unit, 0)]
        finally:
            release.set()
            await scheduler.close()
            if closing is not None:
                await closing
            await asyncio.gather(*([running] if running is not None else []), *queued, return_exceptions=True)

    async def scenario():
        failures = []
        for phase in (background_forms, queued_after_request, worker_after_first_request,
                      cancelled_queue_and_retained_thread):
            try:
                await phase()
            except Exception as error:
                failures.append(f'{phase.__name__}: {type(error).__name__}: {error}')
        assert not failures, '\n'.join(failures)
    asyncio.run(scenario())
