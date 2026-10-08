import os
from pathlib import Path
import subprocess
import sys


def _subprocess_python_path():
    # 本树源码优先，只追加调度者分配的只读二维码依赖目录。
    source_root = str(Path('src').resolve())
    overlay = os.environ.get('CHRIPTMAS_TEST_QRCODE_OVERLAY')
    return os.pathsep.join([source_root, overlay]) if overlay else source_root


def test_real_server_factory_injects_actual_port_and_keeps_only_bootstrap_public(tmp_path, tmp_path_factory):
    root = tmp_path_factory.mktemp('d')
    dist = tmp_path / 'code/src/frontend/dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('<main>pairing</main>', encoding='utf8')
    (dist / 'assets/app.js').write_text('window.synthetic=true', encoding='utf8')
    environment = {key: os.environ[key] for key in ['PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'] if key in os.environ}
    environment.update(CHRIPTMAS_DEPLOY='server', CHRIPTMAS_APP_ROOT=str(root),
        LOCALAPPDATA=str(tmp_path / 'localappdata'), PYTHONPATH=_subprocess_python_path(), PYTHONDONTWRITEBYTECODE='1',
        USERPROFILE=str(tmp_path / 'profile'), HOME=str(tmp_path / 'profile'), CHRIPTMAS_COMPANION_MODE='development')
    script = """
import sys
from pathlib import Path
from fastapi.testclient import TestClient
from backend.memory_app.serve import create_application
app = create_application(frontend_root=Path(sys.argv[1]), port=8765)
assert app.state.server_device_auth.internal_key_for('http://127.0.0.1:8765/local-model/v1') is not None
assert app.state.server_device_auth.internal_key_for('http://127.0.0.1:8001/local-model/v1') is None
with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 1234)) as client:
    for path in ['/', '/pair', '/assets/app.js']:
        assert client.get(path).status_code == 200
    for path in ['/settings', '/assets/missing.js', '/api/v2/settings', '/docs', '/openapi.json']:
        assert client.get(path).status_code == 401
    assert client.get('/api/health').json() == {'status':'ok'}
    registry = app.state.device_registry
    issued = registry.issue_pairing(user_id='local-user', actor='install')
    pair = client.post('/api/v2/devices/exchange', json={'code':issued['code'],'name':'computer'})
    assert pair.status_code == 201
    assert client.get('/api/v2/settings', headers={'Authorization':'Bearer '+pair.json()['key']}).status_code == 200
    headers = {'Authorization':'Bearer '+pair.json()['key'], 'Origin':'http://127.0.0.1:8765'}
    assert client.post('/api/v2/devices/pair', json={}, headers=headers).status_code == 200
    headers['Origin'] = 'https://foreign.example'
    assert client.post('/api/v2/devices/pair', json={}, headers=headers).status_code == 403
    with client.portal.wrap_async_context_manager(app.state.server_user_pool.lease('local-user')) as child:
        assert child.state.server_user_id == 'local-user'
        assert child.state.recognition_runtime_root == app.state.server_users.root_for('local-user')
        models = child.state.recognition_models
        from backend.memory_app.app import create_app
        provider = models._internal_local_key_provider
        try:
            create_app(runtime_root=child.state.recognition_runtime_root, model_configuration=models)
        except ValueError as error:
            assert str(error) == 'server_model_identity_required'
        else:
            raise AssertionError('injected model identity was silently overwritten')
        assert models._internal_local_key_provider is provider
        from backend.api.app import create_worker_auth_middleware
        from fastapi import FastAPI
        legacy = FastAPI()
        @legacy.get('/api/private')
        def private():
            return {'should_not_be_public':True}
        create_worker_auth_middleware(legacy)
        assert TestClient(legacy).get('/api/private').status_code == 401
        assert TestClient(legacy).get('/api/private', headers={'Authorization':'Bearer '+pair.json()['key']}).status_code == 401
        model = app.state.server_context.resources.model_path('qwen2.5-1.5b-instruct')/'model.safetensors'
        model.parent.mkdir(parents=True); model.write_bytes(b'fake inference asset')
        models.update_generation_mode(mode='local', local_enabled=True, local_base_url='http://127.0.0.1:8765/local-model/v1', expected_revision=0)
        from backend.memory_app import local_model
        calls = []
        def engine(directory, messages, max_tokens):
            calls.append(messages)
            return 'done', 12, 2, True
        local_model._generate = engine
        def wire(**request):
            response = client.post('/local-model/v1/chat/completions', headers={'Authorization':'Bearer '+request['api_key']},
                json={'model': request['model'].removeprefix('openai/'), 'messages':request['messages'], 'max_tokens':request['max_tokens']})
            assert response.status_code == 200
            return response.json()
        models._completion_fn = wire
        assert models.complete([{'role':'user','content':'background without device'}])[0] == 'done'
        assert len(calls) == 1
        assert models.snapshot('generation')['api_key'] == 'local-model'
        assert not (Path(sys.argv[2])/'recognition.sqlite3').exists()
print('server-device-composition-ok')
"""
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'code'), str(root)],
        env=environment, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == 'server-device-composition-ok'


def test_real_module_cli_port_survives_uvicorn_import_string_module_identity(tmp_path, tmp_path_factory):
    root = tmp_path_factory.mktemp('d')
    dist = tmp_path / 'code/src/frontend/dist'
    dist.mkdir(parents=True)
    (dist / 'index.html').write_text('<main>pairing</main>', encoding='utf8')
    environment = {key: os.environ[key] for key in ['PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'] if key in os.environ}
    environment.update(CHRIPTMAS_DEPLOY='server', CHRIPTMAS_APP_ROOT=str(root),
        LOCALAPPDATA=str(tmp_path / 'localappdata'), PYTHONPATH=_subprocess_python_path(), PYTHONDONTWRITEBYTECODE='1',
        USERPROFILE=str(tmp_path / 'profile'), HOME=str(tmp_path / 'profile'), CHRIPTMAS_COMPANION_MODE='development')
    script = """
import importlib, runpy, sys
from pathlib import Path
import uvicorn
fixture = Path(sys.argv[1])
observed = []
def wire(target, *, factory, host, port):
    assert factory is True and host == '127.0.0.1' and port == 8765
    if isinstance(target,str):
        module, name = target.split(':')
        target = getattr(importlib.import_module(module),name)
    app = target(frontend_root=fixture)
    assert app.state.server_device_auth.internal_key_for('http://127.0.0.1:8765/local-model/v1') is not None
    assert app.state.server_device_auth.internal_key_for('http://127.0.0.1:8001/local-model/v1') is None
    observed.append(app)
uvicorn.run = wire
sys.argv = ['serve','--port','8765']
runpy.run_module('backend.memory_app.serve',run_name='__main__')
assert len(observed)==1
print('server-cli-port-ok')
"""
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'code')],
        env=environment, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == 'server-cli-port-ok'
