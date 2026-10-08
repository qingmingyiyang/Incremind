"""Actual settings routes use fake download I/O, never fake permission owners."""
from types import SimpleNamespace
from contextlib import asynccontextmanager
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.api.routes import settings
from backend.shared.deployment import DeploymentLayout
from backend.memory_app.server_runtime import create_server_application
from backend.video_summary.infrastructure.faster_whisper_models import FasterWhisperModelManager
from backend.video_summary.infrastructure.rag_models import RagModelManager
from backend.video_summary.infrastructure.in_memory_progress_tracker import InMemoryProgressTracker


@pytest.mark.parametrize('path', ['/api/asr/faster-whisper/models/small/download','/api/rag/models/embedding/download'])
def test_shared_weight_downloads_require_admin_and_ordinary_model_lists_remain_readable(tmp_path, monkeypatch, path):
    downloads, completed = [], threading.Event()
    def whisper_download(manager, model_id, *, progress_reporter=None):
        downloads.append(('whisper',model_id))
        completed.set()
    monkeypatch.setattr(FasterWhisperModelManager,'download',whisper_download)
    def rag_download(spec, reporter):
        downloads.append(('rag',spec.key)); completed.set()
    def child(root,user_id,context):
        app = FastAPI()
        app.state.container = SimpleNamespace(root_dir=root,config_path=root/'config/settings.toml',
            faster_whisper_model_manager=FasterWhisperModelManager(context.resources.model_path('faster-whisper')),
            model_download_progress_tracker=InMemoryProgressTracker(),
            rag_model_manager=RagModelManager(root_dir=root,models_root=context.resources.model_path('fastembed'),
                progress_tracker=InMemoryProgressTracker(),downloader=rag_download))
        app.include_router(settings.router)
        return app
    app = create_server_application(DeploymentLayout('server',tmp_path/'users/local-user',tmp_path),child_factory=child)
    registry,users = app.state.device_registry,app.state.server_users
    paired = registry.exchange(registry.issue_pairing(user_id='local-user',actor='install')['code'],name='admin')
    admin = registry.authenticate(paired['key'])
    user = users.create(admin,name='乙')
    device = registry.exchange(registry.issue_pairing(user_id=user['user_id'],actor=admin.device_id)['code'],name='user')
    with TestClient(app) as client:
        own = {'Authorization':'Bearer '+device['key']}
        result = client.post(path,headers=own)
        assert result.status_code == 403
        assert downloads == []
        listing = '/api/asr/faster-whisper/models' if '/asr/' in path else '/api/rag/models'
        assert client.get(listing,headers=own).status_code == 200
        changed = client.post(path,headers={'Authorization':'Bearer '+paired['key'],'X-Chriptmas-Target-User':user['user_id']})
        assert changed.status_code == 200
        assert completed.wait(3) and len(downloads)==1


@pytest.mark.parametrize('path',['/api/asr/faster-whisper/models/small/download','/api/rag/models/embedding/download'])
def test_actual_download_thread_pins_only_requesting_child_until_its_finally(tmp_path,monkeypatch,path):
    entered,release=threading.Event(),threading.Event();stops=[];now=[0.0]
    def downloader(*args,**kwargs):
        entered.set();assert release.wait(5)
    monkeypatch.setattr(FasterWhisperModelManager,'download',downloader)
    def factory(root,user_id,context):
        @asynccontextmanager
        async def lifetime(app):
            yield
            stops.append(user_id)
        child=FastAPI(lifespan=lifetime)
        child.state.container=SimpleNamespace(root_dir=root,config_path=root/'config/settings.toml',
            faster_whisper_model_manager=FasterWhisperModelManager(context.resources.model_path('faster-whisper')),
            model_download_progress_tracker=InMemoryProgressTracker(),
            rag_model_manager=RagModelManager(root_dir=root,models_root=context.resources.model_path('fastembed'),
                progress_tracker=InMemoryProgressTracker(),downloader=downloader))
        child.include_router(settings.router)
        return child
    app=create_server_application(DeploymentLayout('server',tmp_path/'users/local-user',tmp_path),child_factory=factory,clock=lambda:now[0])
    registry,users=app.state.device_registry,app.state.server_users
    pair=registry.exchange(registry.issue_pairing(user_id='local-user',actor='install')['code'],name='admin')
    admin=registry.authenticate(pair['key']);user=users.create(admin,name='busy space')
    admin_headers={'Authorization':'Bearer '+pair['key']}
    with TestClient(app) as client:
        target={**admin_headers,'X-Chriptmas-Target-User':user['user_id']}
        assert client.get('/api/rag/models',headers=admin_headers).status_code==200
        assert client.post(path,headers=target).status_code==200
        try:
            assert entered.wait(2)
            pool=app.state.server_user_pool;now[0]=2000
            assert client.portal.call(pool.collect_idle)==['local-user']
            assert pool.loaded_user_ids==(user['user_id'],) and stops==['local-user']
        finally:
            release.set()
        child=pool._children[user['user_id']].application
        client.portal.call(pool._settle_work,child)
        assert client.portal.call(pool.collect_idle)==[user['user_id']]
    assert stops==['local-user',user['user_id']]
