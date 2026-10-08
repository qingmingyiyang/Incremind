"""The real workspace ASR path shares an installed engine across user roots."""
import json
from pathlib import Path
import subprocess
import sys
import asyncio
from threading import Event
from types import ModuleType, SimpleNamespace

from backend.shared.server_resources import SharedResources, resource_context
from backend.security.device_identity import DeviceIdentity
from backend.security.user_context import UserAccess, user_context
import pytest


def installed(pool):
    from backend.video_summary.infrastructure.faster_whisper_models import FasterWhisperModelManager
    from backend.video_summary.infrastructure.huggingface_model_downloader import write_downloaded_model_manifest
    manager = FasterWhisperModelManager(pool.model_path('faster-whisper'))
    path = manager.resolve_model_dir('large-v3-turbo')
    path.mkdir(parents=True)
    (path/'model.bin').write_bytes(b'synthetic-model')
    (path/'config.json').write_text('{}')
    write_downloaded_model_manifest(path,manager.download_spec('large-v3-turbo'))


def test_real_workspace_transcription_uses_one_engine_and_separate_outputs_with_admin_by(tmp_path, monkeypatch):
    from backend.memory_app.workspace_audio import _transcribe_output
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from core.storage_provider import JsonObjectStore
    from core.product_core.audio_asset_transcriber import SaveAudioAssetTranscriberSettings, BUILTIN_FASTER_WHISPER_COMMAND
    pool = SharedResources(tmp_path)
    installed(pool)
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT',str(tmp_path))
    calls=[]
    provider=ModuleType('faster_whisper')
    class Engine:
        def __init__(self,*args,**kwargs): calls.append(('load',args))
        def transcribe(self,path,**kwargs):
            calls.append(('input',path))
            return iter([SimpleNamespace(start=0,end=1,text=Path(path).read_text())]),SimpleNamespace(duration=1,language='zh')
    provider.WhisperModel=Engine
    monkeypatch.setitem(sys.modules,'faster_whisper',provider)
    # Only the GPU-detection process boundary is substituted. Any ASR child
    # process means the actual product path failed to use the shared engine.
    def process(argv,*args,**kwargs):
        if list(argv)==['nvidia-smi']: raise FileNotFoundError('synthetic-cpu-host')
        raise AssertionError('ASR must not launch a second model process')
    monkeypatch.setattr(subprocess,'run',process)
    with resource_context(pool):
        for user,text in [('user-a','甲文字'),('user-b','乙文字')]:
            root=tmp_path/'users'/user;root.mkdir(parents=True)
            store,_=build_rebuild_object_store(root)
            SaveAudioAssetTranscriberSettings(store).execute(enabled=True,command=[BUILTIN_FASTER_WHISPER_COMMAND],model_name='large-v3-turbo',confirm_enable=True)
            audio=root/'sample.wav';audio.write_text(text,encoding='utf8')
            with user_context(UserAccess(DeviceIdentity('device-admin','local-user',1),user,'admin')):
                output=_transcribe_output(audio,root,'default','item-'+user,'run-'+user)
                assert output['status']=='completed' and output['text']==text
                store,settings=build_rebuild_object_store(root)
                assert store.read('workbench_asr_bindings','workbench-asr-run-'+user+'-source-run-'+user)['remote_processing'] is False
                internal=JsonObjectStore(root/'workspace/asr-internal',namespace_id=settings.namespace_id)
                assert internal.attribution('media_processing_outputs',output['id'],internal.revision('media_processing_outputs',output['id']))['target_user_id']==user
    assert sum(kind=='load' for kind,*_ in calls)==1
    assert len({path for kind,path in calls if kind=='input'})==2


def test_shared_runner_checks_revocation_between_provider_segments(tmp_path,monkeypatch):
    from backend.api.server_asr_runner import run_shared_asr
    pool=SharedResources(tmp_path);installed(pool)
    root=tmp_path/'users/user-a';root.mkdir(parents=True)
    audio=root/'sample.wav';audio.write_text('audio')
    cancelled=[False];consumed=[]
    class Revoked(Exception): pass
    def check():
        if cancelled[0]: raise Revoked('device_unauthorized')
    class Engine:
        def __init__(self,*args,**kwargs): pass
        def transcribe(self,*args,**kwargs):
            def parts():
                consumed.append('first');cancelled[0]=True
                yield SimpleNamespace(start=0,end=1,text='first')
                consumed.append('second')
                yield SimpleNamespace(start=1,end=2,text='second')
            return parts(),SimpleNamespace(duration=2,language='zh')
    monkeypatch.setitem(sys.modules,'faster_whisper',SimpleNamespace(WhisperModel=Engine))
    monkeypatch.setattr(subprocess,'run',lambda *args,**kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    argv=(sys.executable,'-m','backend.video_summary.infrastructure.local_asr_cli','--audio',str(audio),'--model','large-v3-turbo','--mode','balanced','--language','zh')
    with resource_context(pool),pytest.raises(Revoked):
        run_shared_asr(argv,root,10,check)
    assert consumed==['first']


def test_real_transcription_timeout_and_cancel_keep_global_lease_until_sdk_returns(tmp_path,monkeypatch):
    from backend.memory_app.server_jobs import ServerJobScheduler
    from tests.memory_app.v2.test_server_jobs import setup
    from backend.memory_app.workspace_audio import _transcribe_output
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from backend.api import server_asr_runner as module
    from core.product_core.audio_asset_transcriber import SaveAudioAssetTranscriberSettings,BUILTIN_FASTER_WHISPER_COMMAND
    from core.storage_provider import JsonObjectStore
    users,_,a,b=setup(tmp_path);pool=SharedResources(tmp_path);installed(pool)
    entered,release=Event(),Event();calls=[];now=[0.0]
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:now[0]))
    class Engine:
        def __init__(self,*args,**kwargs): calls.append('load')
        def transcribe(self,path,**kwargs):
            calls.append(Path(path).parent.name)
            if Path(path).parent.name==a['user_id']:
                entered.set();assert release.wait(3)
            return iter([SimpleNamespace(start=0,end=1,text=Path(path).read_text())]),SimpleNamespace(duration=1,language='zh')
    monkeypatch.setitem(sys.modules,'faster_whisper',SimpleNamespace(WhisperModel=Engine))
    monkeypatch.setattr(subprocess,'run',lambda *args,**kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    with resource_context(pool):
        for user in (a,b):
            root=users.root_for(user['user_id']);(root/'sample.wav').write_text(user['user_id'])
            store,_=build_rebuild_object_store(root)
            SaveAudioAssetTranscriberSettings(store).execute(enabled=True,command=[BUILTIN_FASTER_WHISPER_COMMAND],model_name='large-v3-turbo',timeout_seconds=1,confirm_enable=True)
        async def scenario():
            scheduler=ServerJobScheduler(users)
            def callback(user):
                root=users.root_for(user['user_id'])
                label='a' if user['user_id']==a['user_id'] else 'b'
                return lambda:_transcribe_output(root/'sample.wav',root,'default','item-'+label,'run-'+label)
            first=asyncio.create_task(scheduler.submit(a['user_id'],'transcribe',callback(a)))
            try:
                if not await asyncio.to_thread(entered.wait,2):
                    await first
                    raise AssertionError('provider_not_entered')
                first.cancel()
                with pytest.raises(asyncio.CancelledError): await first
                second=asyncio.create_task(scheduler.submit(b['user_id'],'transcribe',callback(b)))
                await asyncio.sleep(0)
                assert scheduler.active_for(a['user_id'])==1 and calls==['load',a['user_id']]
                now[0]=2;release.set()
                output=await asyncio.wait_for(second,3)
                assert output['text']==b['user_id']
                store=JsonObjectStore(users.root_for(a['user_id'])/'workspace/asr-internal')
                assert store.list('media_processing_outputs')==()
                assert store.list('media_processing_jobs')[0]['status']=='failed'
                assert scheduler.active_for(a['user_id'])==0 and calls.count('load')==1
            finally:
                release.set();await scheduler.close()
        asyncio.run(scenario())
