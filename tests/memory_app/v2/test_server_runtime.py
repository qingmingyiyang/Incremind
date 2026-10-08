"""User lifetime covers ASGI bodies, streams and real background execution."""
import asyncio
import pytest
from contextlib import asynccontextmanager

from fastapi import FastAPI


def setup(tmp_path):
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    devices = DeviceRegistry(tmp_path / 'server')
    pair = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='管理电脑')
    admin = devices.authenticate(pair['key'])
    users = ServerUsers(tmp_path, records=devices.records)
    return users, admin


def test_user_children_are_lazy_once_and_idle_thirty_minutes_unloads_with_lifecycle(tmp_path):
    from backend.memory_app.server_runtime import UserApplicationPool
    users, admin = setup(tmp_path)
    a, b = users.create(admin, name='甲'), users.create(admin, name='乙')
    now, created, lifecycle = [0.0], [], []
    def factory(root, user_id):
        created.append((root, user_id))
        @asynccontextmanager
        async def lifetime(app):
            lifecycle.append(('start', user_id))
            yield
            lifecycle.append(('stop', user_id))
        return FastAPI(lifespan=lifetime)
    async def scenario():
        pool = UserApplicationPool(users, factory=factory, clock=lambda: now[0])
        assert pool.loaded_user_ids == () and created == []
        async with pool.lease(a['user_id']) as first:
            async with pool.lease(a['user_id']) as same:
                assert same is first
            now[0] = 2000
            assert await pool.collect_idle() == []
        now[0] = 3799
        assert await pool.collect_idle() == []
        now[0] = 3800
        assert await pool.collect_idle() == [a['user_id']]
        assert lifecycle == [('start', a['user_id']), ('stop', a['user_id'])]
        async with pool.lease(b['user_id']):
            pass
        assert created == [(users.root_for(a['user_id']), a['user_id']), (users.root_for(b['user_id']), b['user_id'])]
        await pool.close()
        assert pool.loaded_user_ids == ()
    asyncio.run(scenario())


def test_failed_real_lifespan_close_keeps_failed_owner_and_never_recreates_root(tmp_path):
    from backend.memory_app.server_runtime import UserApplicationPool
    users,admin=setup(tmp_path);user=users.create(admin,name='close failure');now=[0.0];loads=[]
    @asynccontextmanager
    async def lifetime(app):
        yield
        raise OSError('synthetic_close_failure')
    def factory(root,user_id):loads.append(user_id);return FastAPI(lifespan=lifetime)
    async def scenario():
        pool=UserApplicationPool(users,factory=factory,clock=lambda:now[0])
        async with pool.lease(user['user_id']) as original:pass
        now[0]=2000
        with pytest.raises(OSError,match='synthetic_close_failure'):await pool.collect_idle()
        assert pool.loaded_user_ids==(user['user_id'],)
        with pytest.raises(ValueError,match='user_unavailable'):
            async with pool.lease(user['user_id']):pytest.fail('failed root reopened')
        assert loads==[user['user_id']] and pool._children[user['user_id']].application is original
        with pytest.raises(ValueError,match='user_unavailable'):await pool.close()
    asyncio.run(scenario())


def test_actual_daily_waiting_for_retirement_is_cancelled_without_new_child_or_old_callback(tmp_path):
    from backend.memory_app.server_runtime import UserApplicationPool
    from backend.memory_app.server_jobs import ServerJobScheduler
    from backend.memory_app.v2.daily import DailyJobs
    users,admin=setup(tmp_path);user=users.create(admin,name='daily retirement')
    now=[0.0];loads=[];calls=[]
    async def scenario():
        stopping,release=asyncio.Event(),asyncio.Event();jobs=ServerJobScheduler(users)
        owners=[]
        def factory(root,user_id):
            loads.append(user_id)
            daily=DailyJobs(server_jobs=jobs,user_id=user_id)
            daily.register('actual-old-owner',lambda:calls.append(root))
            owners.append(daily)
            @asynccontextmanager
            async def lifetime(app):
                try:yield
                finally:
                    stopping.set();await release.wait();await daily.stop()
            return FastAPI(lifespan=lifetime)
        pool=UserApplicationPool(users,factory=factory,clock=lambda:now[0])
        jobs.execution_lease=pool.job_lease
        async with pool.lease(user['user_id']):pass
        now[0]=2000;retiring=asyncio.create_task(pool.collect_idle())
        await asyncio.wait_for(stopping.wait(),2)
        owners[0].task=asyncio.create_task(owners[0].run_once())
        for _ in range(5):await asyncio.sleep(0)
        assert jobs.active_for(user['user_id'])==1 and calls==[]
        release.set();await asyncio.wait_for(retiring,2)
        assert calls==[] and loads==[user['user_id']]
        try:
            assert await asyncio.wait_for(jobs.submit(user['user_id'],'new-valid-job',lambda:'valid'),2)=='valid'
            assert loads==[user['user_id'],user['user_id']] and calls==[]
        finally:
            await jobs.close();await pool.close()
    asyncio.run(scenario())


def test_concurrent_first_access_creates_once_and_background_task_blocks_unload(tmp_path):
    from backend.memory_app.server_runtime import UserApplicationPool
    users, admin = setup(tmp_path)
    user = users.create(admin, name='甲')
    now, loads = [0.0], []
    def factory(root, user_id):
        loads.append(user_id)
        app = FastAPI()
        app.state.workbench_tasks = set()
        return app
    async def scenario():
        pool = UserApplicationPool(users, factory=factory, clock=lambda: now[0])
        gate, started = asyncio.Event(), asyncio.Event()
        async def request():
            async with pool.lease(user['user_id']) as app:
                async def background():
                    started.set()
                    await gate.wait()
                task = asyncio.create_task(background())
                app.state.workbench_tasks.add(task)
                return app, task
        pairs = await asyncio.gather(request(), request())
        assert len(loads) == 1 and pairs[0][0] is pairs[1][0]
        await started.wait()
        now[0] = 2000
        assert await pool.collect_idle() == []
        gate.set()
        await asyncio.gather(*(pair[1] for pair in pairs))
        assert await pool.collect_idle() == [user['user_id']]
        await pool.close()
    asyncio.run(scenario())
