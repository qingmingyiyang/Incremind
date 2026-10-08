"""Real Core recovery thread must finish before a user client or root unloads."""
import asyncio
from contextlib import asynccontextmanager
from threading import Event
import time

import httpx
import pytest
from fastapi import FastAPI
from core.effect_log.runtime import build_effect_runtime,EffectRecoveryCoordinator,EffectRecoveryService,CoordinationTaskRegistration
from backend.memory_app.server_runtime import UserApplicationPool
from tests.memory_app.v2.test_server_runtime import setup


@pytest.mark.parametrize('operation',['idle','close'])
def test_actual_recovery_thread_keeps_child_and_client_until_real_finish(tmp_path,operation):
    users,admin=setup(tmp_path);user=users.create(admin,name='recovering')
    entered,release=Event(),Event();clock=[0.0];stopped=[];loads=[]
    runtime=build_effect_runtime(users.root_for(user['user_id'])/'effects.sqlite3',owner_id='server-test')
    coordinator=EffectRecoveryCoordinator(runtime)
    def recover():entered.set();assert release.wait(6)
    coordinator.register_coordination(CoordinationTaskRegistration(kind='synthetic-recovery-job',run=recover))
    service=EffectRecoveryService(coordinator,interval_seconds=.01,clock=time.time)
    client=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200)))
    @asynccontextmanager
    async def lifetime(app):
        service.start()
        try:yield
        finally:
            service.shutdown(timeout_seconds=2.0)
            client.close();stopped.append('closed')
    def factory(root,user_id):
        loads.append(user_id)
        if len(loads)>1:return FastAPI()
        app=FastAPI(lifespan=lifetime)
        app.state.effect_recovery_service=service
        return app
    async def scenario():
        pool=UserApplicationPool(users,factory=factory,clock=lambda:clock[0])
        async with pool.lease(user['user_id']):pass
        assert await asyncio.to_thread(entered.wait,2)
        clock[0]=2000
        unloading=asyncio.create_task(pool.collect_idle() if operation=='idle' else pool.close())
        reenter=None
        try:
            assert await asyncio.to_thread(service._stop.wait,3)
            assert not unloading.done() and service._thread.is_alive()
            assert not client.is_closed and stopped==[]
            if operation=='idle':
                async def request():
                    async with pool.lease(user['user_id']):return 'loaded'
                reenter=asyncio.create_task(request());await asyncio.sleep(0)
                assert not reenter.done() and len(loads)==1
            release.set()
            await asyncio.wait_for(unloading,3)
            assert not service._thread.is_alive() and client.is_closed
            if reenter:
                assert await asyncio.wait_for(reenter,3)=='loaded'
        finally:
            release.set()
            await asyncio.gather(unloading,return_exceptions=True)
            if reenter:await asyncio.gather(reenter,return_exceptions=True)
            await pool.close()
    asyncio.run(scenario())
