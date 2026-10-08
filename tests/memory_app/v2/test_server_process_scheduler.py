"""Actual intake processing and daily owner callbacks share the same scheduler."""
import asyncio
import pytest
from threading import Event,RLock
from contextlib import asynccontextmanager
from fastapi import FastAPI
from core.storage_provider import SQLiteStructuredRecordStore
from backend.memory_app.workspace_intake import WorkspaceIntake
from backend.memory_app.workspace_items import WorkspaceItems
from backend.memory_app.processing_lease import ProcessingLease
from backend.memory_app.server_jobs import ServerJobScheduler
from backend.memory_app.server_runtime import UserApplicationPool
from backend.memory_app.v2.daily import DailyJobs
from tests.memory_app.v2.test_server_jobs import setup
from tests.memory_app.test_workspace import Model


def test_actual_processing_and_daily_rotate_and_hold_child_lifetime_until_provider_finishes(tmp_path):
    users,_,a,b=setup(tmp_path);gate,entered=Event(),Event();order=[];now=[0.0]
    class Provider(Model):
        def complete(self,messages,**kwargs):
            text=messages[-1]['content']
            name='a1' if '第一份' in text else 'a2'
            order.append(name)
            if name=='a1': entered.set();assert gate.wait(3)
            return super().complete(messages,**kwargs)
    apps={}
    def child(root,user_id):
        app=FastAPI();app.state.workbench_tasks=set();apps[user_id]=app
        return app
    async def scenario():
        pool=UserApplicationPool(users,factory=child,clock=lambda:now[0])
        scheduler=ServerJobScheduler(users,execution_lease=pool.job_lease)
        root=users.root_for(a['user_id']);(root/'workspace').mkdir()
        records=SQLiteStructuredRecordStore(root/'records.sqlite3')
        items=WorkspaceItems(records,ProcessingLease(records,'workspace_items','instance-one'),RLock())
        owner=WorkspaceIntake(root,items,Provider(),job_submitter=lambda kind,callback:scheduler.submit(a['user_id'],kind,callback))
        assert owner.with_items(items,owner.models).job_submitter is owner.job_submitter
        one=await owner.add_text({'text':'第一份合成资料','project_id':'default'})
        two=await owner.add_text({'text':'第二份合成资料','project_id':'default'})
        daily=DailyJobs(server_jobs=scheduler,user_id=b['user_id'])
        daily.register('test-real-daily-callback',lambda:order.append('b-daily'))
        first=asyncio.create_task(owner.process(one['id'],{'project_id':'default'}))
        try:
            assert await asyncio.to_thread(entered.wait,2)
            apps[a['user_id']].state.workbench_tasks.add(first)
            more=asyncio.create_task(owner.process(two['id'],{'project_id':'default'}))
            maintenance=asyncio.create_task(daily.run_once())
            await asyncio.sleep(0)
            now[0]=2000
            assert await pool.collect_idle()==[] and pool.loaded_user_ids==(a['user_id'],)
            gate.set()
            results=await asyncio.wait_for(asyncio.gather(first,more,maintenance),3)
            assert results[0]['status']==results[1]['status']=='ready'
            assert order==['a1','b-daily','a2']
            assert scheduler.active_for(a['user_id'])==scheduler.active_for(b['user_id'])==0
        finally:
            gate.set();await scheduler.close();await pool.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('scheduled',[False,True])
def test_real_turn_future_blocks_idle_unload_and_heavy_slot_after_callback_returns(tmp_path,scheduled):
    from backend.api.ai_turn_runner import AITurnRunner
    from core.ai_kernel import SynchronousAIRuntime,SQLiteAITurnStore,ScopedCapabilityRegistry
    from tests.backend.unit.api.test_ai_turn_runner import _request
    users,_,a,b=setup(tmp_path);entered,release=Event(),Event();now=[0.0];closed=[]
    class Planner:
        def plan(self,*args,**kwargs):
            entered.set();assert release.wait(5)
            return {'type':'complete','summary':'synthetic','evidence_refs':[]}
    store=SQLiteAITurnStore(users.root_for(a['user_id'])/'ai-turns.sqlite3')
    runtime=SynchronousAIRuntime(planner=Planner(),registry=ScopedCapabilityRegistry(),events=store,payloads=store,state=store)
    runner=AITurnRunner(runtime,max_workers=1)
    @asynccontextmanager
    async def lifetime(app):
        yield
        closed.append('closed');runner.shutdown()
    def factory(root,user):
        child=FastAPI(lifespan=lifetime)
        child.state.ai_turn_runner=runner
        return child
    async def scenario():
        pool=UserApplicationPool(users,factory=factory,clock=lambda:now[0])
        scheduler=ServerJobScheduler(users,execution_lease=pool.job_lease)
        waiter=queued=closing=None;calls=[]
        def accepted(): return runner.accept_and_submit(_request())
        try:
            if scheduled:
                waiter=asyncio.create_task(scheduler.submit(a['user_id'],'intake',accepted,retain_running=True))
            else:
                async with pool.lease(a['user_id']): accepted()
            assert await asyncio.to_thread(entered.wait,2)
            now[0]=2000
            assert await pool.collect_idle()==[] and closed==[]
            if scheduled:
                assert not waiter.done() and scheduler.active_for(a['user_id'])==1
                waiter.cancel();await asyncio.gather(waiter,return_exceptions=True)
                queued=asyncio.create_task(scheduler.submit(b['user_id'],'daily',lambda:calls.append('other')))
                await asyncio.sleep(0)
                assert calls==[] and scheduler.active_for(a['user_id'])==1
                closing=asyncio.create_task(scheduler.close());await asyncio.sleep(0)
                assert not closing.done() and closed==[]
            release.set()
            if closing: await asyncio.wait_for(closing,3)
            if queued: await asyncio.gather(queued,return_exceptions=True)
            await scheduler.close();await pool.close()
            assert closed==['closed']
        finally:
            release.set();await scheduler.close();await pool.close();runner.shutdown()
    asyncio.run(scenario())


def test_actual_intake_retains_async_provider_thread_on_waiter_cancel_and_repeated_close(tmp_path):
    users,_,a,b=setup(tmp_path)
    entered,release=Event(),Event()
    class BlockingProvider(Model):
        def complete(self,messages,**kwargs):
            entered.set()
            assert release.wait(5)
            return super().complete(messages,**kwargs)
    async def scenario():
        now=[0.0]
        pool=UserApplicationPool(users,factory=lambda root,user:FastAPI(),clock=lambda:now[0])
        scheduler=ServerJobScheduler(users,execution_lease=pool.job_lease)
        root=users.root_for(a['user_id']);(root/'workspace').mkdir()
        records=SQLiteStructuredRecordStore(root/'records.sqlite3')
        items=WorkspaceItems(records,ProcessingLease(records,'workspace_items','instance-cancel'),RLock())
        owner=WorkspaceIntake(root,items,BlockingProvider(),job_submitter=lambda kind,callback:
            scheduler.submit(a['user_id'],kind,callback,retain_running=True))
        item=await owner.add_text({'text':'取消等待仍须等真实线程结束','project_id':'default'})
        running=asyncio.create_task(owner.process(item['id'],{'project_id':'default'}))
        closers=[]
        try:
            if not await asyncio.to_thread(entered.wait,2):
                await running
                pytest.fail('provider did not start')
            running.cancel();await asyncio.gather(running,return_exceptions=True)
            calls=[]
            queued=asyncio.create_task(scheduler.submit(b['user_id'],'daily',lambda:calls.append('ran')))
            await asyncio.sleep(0)
            queued.cancel();await asyncio.gather(queued,return_exceptions=True)
            for _ in range(2):
                closers.append(asyncio.create_task(scheduler.close()))
                await asyncio.sleep(0)
            now[0]=2000
            assert scheduler.active_for(a['user_id'])==1
            assert await pool.collect_idle()==[]
            assert all(not task.done() for task in closers) and calls==[]
            release.set()
            await asyncio.wait_for(asyncio.gather(*closers),3)
            assert scheduler.active_for(a['user_id'])==0
            assert items.item_for(item['id'],'default').payload['status']=='ready'
            assert calls==[]
        finally:
            release.set()
            await scheduler.close();await pool.close()
    asyncio.run(scenario())


@pytest.fixture
def _mixed_short_root():
    from pathlib import Path
    from tempfile import TemporaryDirectory

    # 缩短物理前缀，目录在原调度器与用户池关闭后清理。
    with TemporaryDirectory(prefix='m4-') as temporary:
        yield Path(temporary).resolve()


def test_actual_image_audio_and_daily_entries_share_one_heavy_slot_and_user_outputs(_mixed_short_root,monkeypatch):
    import io
    import subprocess
    import sys
    import wave
    from types import SimpleNamespace
    from pathlib import Path
    from fastapi import UploadFile
    from backend.shared.server_resources import SharedResources,resource_context
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from core.product_core.local_ocr_provider_settings import SaveLocalOcrProviderSettings
    from core.product_core.audio_asset_transcriber import BUILTIN_FASTER_WHISPER_COMMAND
    from core.product_core.local_asr_provider_settings import SaveLocalAsrProviderSettings
    from tests.memory_app.v2.test_server_shared_asr_product import installed
    tmp_path=_mixed_short_root
    users,_,a,b=setup(tmp_path)
    resources=SharedResources(tmp_path);installed(resources)
    entered,release=Event(),Event();order=[];loads=[];models=[];audio_inputs=[]
    def command(argv,**kwargs):
        if list(argv)==['nvidia-smi']:raise FileNotFoundError('synthetic CPU host')
        assert argv[0]==sys.executable and Path(argv[-1]).parent==users.root_for(a['user_id'])/'workspace'
        order.append('image');entered.set();assert release.wait(5)
        return subprocess.CompletedProcess(argv,0,'甲图片原文','')
    monkeypatch.setattr(subprocess,'run',command)
    class Engine:
        def __init__(self,*args,**kwargs):loads.append(args[0])
        def transcribe(self,path,**kwargs):
            audio_inputs.append(path)
            assert Path(path).parent==users.root_for(b['user_id'])/'workspace'
            order.append('audio')
            return iter([SimpleNamespace(start=0,end=1,text='乙录音原文')]),SimpleNamespace(duration=1,language='zh')
    monkeypatch.setitem(sys.modules,'faster_whisper',SimpleNamespace(WhisperModel=Engine))
    class Provider(Model):
        def complete(self,messages,**kwargs):
            models.append(messages[-1]['content'])
            return super().complete(messages,**kwargs)
    async def scenario():
        pool=UserApplicationPool(users,factory=lambda root,user:FastAPI())
        scheduler=ServerJobScheduler(users,execution_lease=pool.job_lease)
        owners={}
        for user in (a,b):
            user_id=user['user_id'];root=users.root_for(user_id);(root/'workspace').mkdir()
            records=SQLiteStructuredRecordStore(root/'records.sqlite3')
            items=WorkspaceItems(records,ProcessingLease(records,'workspace_items','instance-'+user_id),RLock())
            owners[user_id]=WorkspaceIntake(root,items,Provider(),admission=lambda user_id=user_id:scheduler.intake(user_id),
                job_submitter=lambda kind,callback,user_id=user_id:scheduler.submit(user_id,kind,callback,retain_running=True))
            store,_=build_rebuild_object_store(root)
            if user==a:
                SaveLocalOcrProviderSettings(store).execute(enabled=True,command=[sys.executable,'{image_path}'],confirm_enable=True)
            else:
                SaveLocalAsrProviderSettings(store).execute(enabled=True,command=[BUILTIN_FASTER_WHISPER_COMMAND],
                    model_name='large-v3-turbo',confirm_enable=True)
                from backend.api.tokenhub_asr_provider import workbench_local_transcriber_settings
                assert workbench_local_transcriber_settings(store).status=='ready'
        image=await owners[a['user_id']].add_file('default',UploadFile(filename='sample.png',file=io.BytesIO(b'\x89PNG\r\n\x1a\n')))
        wav=io.BytesIO()
        with wave.open(wav,'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000);output.writeframes(b'\x00\x00'*16000)
        audio=await owners[b['user_id']].add_file('default',UploadFile(filename='sample.wav',file=io.BytesIO(wav.getvalue())))
        daily=DailyJobs(server_jobs=scheduler,user_id=a['user_id'])
        daily.register('synthetic daily model boundary',lambda:order.append('daily'))
        first=asyncio.create_task(owners[a['user_id']].process(image['id'],{'project_id':'default'}))
        tasks=[]
        try:
            if not await asyncio.to_thread(entered.wait,2):
                await first
                pytest.fail('OCR command did not start')
            tasks=[asyncio.create_task(daily.run_once()),asyncio.create_task(owners[b['user_id']].process(audio['id'],{'project_id':'default'}))]
            await asyncio.sleep(0)
            assert order==['image'] and scheduler.active_for(a['user_id'])==1 and scheduler.active_for(b['user_id'])==0
            release.set()
            result=await asyncio.wait_for(asyncio.gather(first,*tasks),5)
            assert result[2]['status']=='ready',{'error':result[2].get('error'),'order':order,'loads':len(loads),'audio_inputs':audio_inputs}
            assert result[0]['status']=='ready' and order==['image','audio','daily']
            assert owners[a['user_id']].items.item_for(image['id'],'default').payload['source_text']=='甲图片原文'
            assert owners[b['user_id']].items.item_for(audio['id'],'default').payload['source_text']=='乙录音原文'
            assert len(loads)==1 and len(models)==2
            assert not owners[b['user_id']].items.records.read('workspace_items',image['id'])
            assert not owners[a['user_id']].items.records.read('workspace_items',audio['id'])
        finally:
            release.set();await scheduler.close();await pool.close()
    with resource_context(resources):asyncio.run(scenario())


def test_actual_accumulation_daily_loop_queues_startup_project_and_fallback_in_shared_slot(tmp_path, monkeypatch):
    from backend.memory_app.v2.policies import override
    from backend.security.user_context import USER_ACCESS, UserAccess, user_context
    from backend.shared.server_resources import RESOURCE_POOL, SharedResources, resource_context
    from tests.memory_app.v2.test_accumulation_trigger import facts

    users, admin, a, b = setup(tmp_path)
    records = facts(users.root_for(b['user_id']))
    peer = SQLiteStructuredRecordStore(users.root_for(a['user_id']) / 'daily-peer.sqlite3')
    resources = SharedResources(tmp_path)
    real_sleep = asyncio.sleep

    async def scenario():
        clock, sleeps, observations, order = [0.0], [], [], []
        checks_ready, checks_go = asyncio.Event(), asyncio.Event()
        fallback_ready, fallback_go = asyncio.Event(), asyncio.Event()
        finished, parked = asyncio.Event(), asyncio.Event()
        entered = [asyncio.Event() for _ in range(3)]
        gates = [asyncio.Event() for _ in range(3)]
        pool = UserApplicationPool(users, factory=lambda root, user: FastAPI())
        scheduler = ServerJobScheduler(users, execution_lease=pool.job_lease)
        daily = DailyJobs(records=records, clock=lambda: clock[0], server_jobs=scheduler,
            user_id=b['user_id'])
        fallback_held = False

        async def sleep(delay):
            nonlocal fallback_held
            if delay == 0:
                return await real_sleep(0)
            sleeps.append(delay)
            if len(sleeps) == 2:
                checks_ready.set()
                await checks_go.wait()
            if clock[0] >= 93600:
                finished.set()
                await parked.wait()
            elif clock[0] >= 7200 and not fallback_held:
                fallback_held = True
                fallback_ready.set()
                await fallback_go.wait()
                clock[0] += daily.interval
            else:
                clock[0] += delay

        def observe(name, project=None):
            # 使用真实事实和任务槽观察回调，不替换 Daily 或 scheduler。
            rows = records.list('recognitions')
            with records.begin() as tx:
                identity = 'event-' + str(len(observations))
                tx.put('test_daily_callbacks', identity, {'name': name, 'project': project},
                    expected_revision=0)
                tx.commit()
            observations.append((name, project, clock[0], USER_ACCESS.get(), RESOURCE_POOL.get(),
                scheduler.active_for(a['user_id']), scheduler.active_for(b['user_id']), len(rows)))
            order.append((name, project))
            if project is not None:
                daily.completed(project)

        daily.register('maintenance', lambda: observe('maintenance'))
        daily.register('consolidation', lambda project=None: observe('consolidation', project))

        async def occupy(index):
            with peer.begin() as tx:
                tx.put('test_daily_peer', 'phase-' + str(index), {'phase': index}, expected_revision=0)
                tx.commit()
            order.append(('peer', index))
            entered[index].set()
            await gates[index].wait()

        async def pending_daily():
            while not scheduler.pending_for(b['user_id']):
                await real_sleep(0)

        # 三秒仅为防挂死保护，触发策略使用原 60 秒检查和两小时阈值。
        async def wait(event):
            await asyncio.wait_for(event.wait(), 3)

        running = []
        monkeypatch.setattr(asyncio, 'sleep', sleep)
        try:
            running.append(asyncio.create_task(scheduler.submit(a['user_id'], 'image', lambda: occupy(0))))
            await wait(entered[0])
            with user_context(UserAccess(admin, b['user_id'], 'admin')):
                await daily.start()
            await asyncio.wait_for(pending_daily(), 3)
            assert observations == [] and scheduler.active_for(a['user_id']) == 1
            assert scheduler.active_for(b['user_id']) == 0
            gates[0].set()
            await running[0]
            await wait(checks_ready)
            assert [(row[0], row[1]) for row in observations] == [('maintenance', None)]

            running.append(asyncio.create_task(scheduler.submit(a['user_id'], 'image', lambda: occupy(1))))
            await wait(entered[1])
            # 队列保护只覆盖交接，先在自动循环闸门内通过原 checkpoint 累积真实步进。
            while clock[0] < 7140:
                clock[0] += daily.check_interval
                state = daily.checkpoint()['alpha']
            assert state['score'] == 11 and state['run_seconds'] == 7140
            checks_go.set()
            await asyncio.wait_for(pending_daily(), 3)
            assert [(row[0], row[1]) for row in observations] == [('maintenance', None)]
            assert scheduler.active_for(a['user_id']) == 1 and scheduler.active_for(b['user_id']) == 0
            state = records.read('v2_learning_accumulation', 'alpha').payload
            assert state['score'] == 11 and state['run_seconds'] == 7200
            gates[1].set()
            await running[1]
            await wait(fallback_ready)
            assert [(row[0], row[1]) for row in observations] == [
                ('maintenance', None), ('consolidation', 'alpha')]

            running.append(asyncio.create_task(scheduler.submit(a['user_id'], 'image', lambda: occupy(2))))
            await wait(entered[2])
            fallback_go.set()
            await asyncio.wait_for(pending_daily(), 3)
            assert len(observations) == 2
            assert scheduler.active_for(a['user_id']) == 1 and scheduler.active_for(b['user_id']) == 0
            gates[2].set()
            await running[2]
            await wait(finished)
            assert order == [('peer', 0), ('maintenance', None), ('peer', 1),
                ('consolidation', 'alpha'), ('peer', 2), ('maintenance', None), ('consolidation', None)]
            assert [row[2] for row in observations] == [60, 7200, 93600, 93600]
            assert all(row[3] is None and row[4] is resources and row[5:7] == (0, 1)
                and row[7] == 1 for row in observations)
            state = records.read('v2_learning_accumulation', 'alpha').payload
            assert state['score'] == 0 and state['run_seconds'] == 120
            assert sleeps and all(delay == 60 for delay in sleeps)
            assert len(records.list('test_daily_callbacks')) == 4
            assert not records.list('test_daily_peer') and not peer.list('test_daily_callbacks')
            assert len(peer.list('test_daily_peer')) == 3
        finally:
            for gate in gates:
                gate.set()
            checks_go.set()
            fallback_go.set()
            await daily.stop()
            await scheduler.close()
            await asyncio.gather(*running, return_exceptions=True)
            await pool.close()

    with resource_context(resources), override(trigger='@2'):
        asyncio.run(scenario())
