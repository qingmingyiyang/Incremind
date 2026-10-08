import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_deployment_defaults_desktop_and_preserves_existing_root(tmp_path):
    from backend.shared.deployment import resolve_deployment
    layout = resolve_deployment(tmp_path, environment={})
    assert layout.mode == 'desktop'
    assert layout.user_root == tmp_path.resolve()
    assert layout.server_root is None


def test_server_has_one_existing_user_layout_and_requires_explicit_root(tmp_path):
    from backend.shared.deployment import resolve_deployment
    layout = resolve_deployment(tmp_path / 'fallback', environment={
        'CHRIPTMAS_DEPLOY': 'server', 'CHRIPTMAS_APP_ROOT': str(tmp_path / 'server')})
    assert layout.server_root == tmp_path / 'server'
    assert layout.user_root == tmp_path / 'server/users/local-user'
    assert not layout.user_root.exists()
    with pytest.raises(ValueError, match='server_root_required'):
        resolve_deployment(tmp_path, environment={'CHRIPTMAS_DEPLOY': 'server'})


@pytest.mark.parametrize('mode', ['invalid', '', 'SERVER'])
def test_invalid_deployment_never_creates_runtime(tmp_path, mode):
    from backend.shared.deployment import resolve_deployment
    with pytest.raises(ValueError, match='deployment_invalid'):
        resolve_deployment(tmp_path / 'absent', environment={'CHRIPTMAS_DEPLOY': mode})
    assert not (tmp_path / 'absent').exists()


def test_server_rejects_desktop_root_contract_before_any_root_initialization(tmp_path, monkeypatch):
    from backend.api.runtime_root_config import resolve_application_runtime_root
    root = tmp_path / 'server-absent'
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
    monkeypatch.setenv('CHRIPTMAS_RUNTIME_ROOT_VERSION', '1')
    with pytest.raises(ValueError, match='server_runtime_contract_conflict'):
        resolve_application_runtime_root(tmp_path / 'fallback')
    assert not root.exists()
    assert not (tmp_path / 'fallback').exists()


@pytest.mark.parametrize('mixed_contract', [False, True])
def test_factory_preflight_refuses_server_environment_before_app_import(tmp_path, mixed_contract):
    import os
    from pathlib import Path
    import subprocess
    import sys
    root = tmp_path / 'absent'
    environment = {key: os.environ[key] for key in ['PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP']
                   if key in os.environ}
    environment.update(CHRIPTMAS_DEPLOY='server', PYTHONPATH=str(Path('src').resolve()))
    reason = 'server_root_required'
    if mixed_contract:
        environment.update(CHRIPTMAS_APP_ROOT=str(root), CHRIPTMAS_RUNTIME_ROOT_VERSION='1')
        reason = 'server_runtime_contract_conflict'
    script = """
import sys
from backend.memory_app.serve import create_application
try:
    create_application()
except ValueError as error:
    assert str(error) == sys.argv[1]
else:
    raise AssertionError('invalid server environment was accepted')
assert 'backend.memory_app.app' not in sys.modules
assert 'backend.api.app' not in sys.modules
"""
    result = subprocess.run([sys.executable, '-c', script, reason], env=environment,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not root.exists()


def test_common_runtime_resolver_creates_only_server_metadata_and_user_root(tmp_path, monkeypatch):
    from backend.api.runtime_root_config import resolve_application_runtime_root
    root = tmp_path / 'server'
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
    assert resolve_application_runtime_root(tmp_path / 'fallback') == root / 'users/local-user'
    assert (root / 'server').is_dir()
    assert not (root / 'recognition.sqlite3').exists()
    assert not (tmp_path / 'fallback').exists()


def test_server_frontend_is_same_origin_and_does_not_catch_api_or_escape(tmp_path):
    from backend.memory_app.serve import mount_server_frontend
    dist = tmp_path / 'src/frontend/dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('<main>synthetic server UI</main>', encoding='utf8')
    (dist / 'assets/app.js').write_text('window.synthetic=true', encoding='utf8')
    (tmp_path / 'private.txt').write_text('not a public asset', encoding='utf8')
    app = FastAPI()
    @app.get('/api/health')
    def health():
        return {'ok': True}
    mount_server_frontend(app, tmp_path)
    client = TestClient(app)
    assert client.get('/').text == '<main>synthetic server UI</main>'
    assert client.get('/settings').text == '<main>synthetic server UI</main>'
    assert client.get('/assets/app.js').text == 'window.synthetic=true'
    assert client.get('/api/health').json() == {'ok': True}
    assert client.get('/api/unknown').status_code == 404
    assert 'not a public asset' not in client.get('/%2e%2e/%2e%2e/private.txt').text


def test_server_refuses_missing_frontend_before_starting(tmp_path):
    from backend.memory_app.serve import mount_server_frontend
    with pytest.raises(ValueError, match='server_frontend_missing'):
        mount_server_frontend(FastAPI(), tmp_path)


def test_cli_always_binds_loopback_and_accepts_only_valid_port(monkeypatch):
    from backend.memory_app import serve
    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, 'run', lambda *args, **kwargs: calls.append((args, kwargs)))
    serve.main(['--port', '8123'])
    assert calls[0][1]['host'] == '127.0.0.1'
    assert calls[0][1]['port'] == 8123
    assert calls[0][0] == ('backend.memory_app.serve:create_application',)
    assert calls[0][1]['factory'] is True
    with pytest.raises(SystemExit):
        serve.main(['--host', '0.0.0.0'])
    with pytest.raises(SystemExit):
        serve.main(['--port', '0'])
    assert len(calls) == 1


def test_real_application_factory_uses_only_one_user_root(tmp_path):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    from uuid import uuid4
    root = (Path('work') / ('srv-' + uuid4().hex[:6])).resolve()
    dist = tmp_path / 'code/src/frontend/dist'
    dist.mkdir(parents=True)
    (dist / 'index.html').write_text('<main>same origin</main>', encoding='utf8')
    environment = {key: os.environ[key] for key in ['PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP']
                   if key in os.environ}
    environment.update(CHRIPTMAS_DEPLOY='server', CHRIPTMAS_APP_ROOT=str(root),
        LOCALAPPDATA=str(tmp_path / 'localappdata'), PYTHONPATH=str(Path('src').resolve()),
        USERPROFILE=str(tmp_path / 'profile'), HOME=str(tmp_path / 'profile'),
        CHRIPTMAS_COMPANION_MODE='development')
    script = """
import json
from pathlib import Path
from fastapi.testclient import TestClient
from backend.memory_app.serve import create_application
import sys
application = create_application(frontend_root=Path(sys.argv[1]))
assert create_application(frontend_root=Path(sys.argv[1])) is application
client = TestClient(application)
assert client.get('/').text == '<main>same origin</main>'
assert client.get('/api/v2/settings').status_code == 401
pairing = application.state.device_registry.issue_pairing(user_id='local-user', actor='install')
paired = client.post('/api/v2/devices/exchange', json={'code': pairing['code'], 'name': 'test device'})
assert paired.status_code == 201
assert client.get('/api/v2/settings', headers={'Authorization': 'Bearer ' + paired.json()['key']}).status_code == 200
print(json.dumps({'root': str(application.state.recognition_runtime_root),
                  'container': str(application.state.container.root_dir)}))
"""
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'code')],
        env=environment, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.splitlines()[-1])
    assert Path(observed['root']) == root / 'users/local-user'
    assert Path(observed['container']) == root / 'users/local-user'
    assert not (root / 'recognition.sqlite3').exists()


def test_desktop_factory_preserves_electron_contract_priority_over_app_root(tmp_path):
    import os
    from pathlib import Path
    from shutil import copyfile
    import subprocess
    import sys
    from uuid import uuid4
    root = (Path('work') / ('desk-' + uuid4().hex[:6])).resolve()
    (root / 'config').mkdir(parents=True)
    copyfile('config/settings.toml.example', root / 'config/settings.toml')
    ignored = tmp_path / 'ignored-app-root'
    environment = {key: os.environ[key] for key in ['PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP']
                   if key in os.environ}
    environment.update(CHRIPTMAS_DEPLOY='desktop', CHRIPTMAS_APP_ROOT=str(ignored),
        CHRIPTMAS_RUNTIME_ROOT_VERSION='1', CHRIPTMAS_RUNTIME_ROOT_REVISION='synthetic-revision',
        CHRIPTMAS_RUNTIME_VAULT_ROOT=str(root), CHRIPTMAS_RUNTIME_MODEL_ROOT=str(root),
        CHRIPTMAS_RUNTIME_MEDIA_ROOT=str(root), LOCALAPPDATA=str(root / 'localappdata'),
        PYTHONPATH=str(Path('src').resolve()), USERPROFILE=str(root / 'profile'),
        HOME=str(root / 'profile'), CHRIPTMAS_COMPANION_MODE='development')
    script = """
import sys
from pathlib import Path
from backend.memory_app.serve import create_application
application = create_application()
expected = Path(sys.argv[1])
assert application.state.recognition_runtime_root == expected
assert application.state.container.root_dir == expected
assert application.state.deployment.user_root == expected
"""
    result = subprocess.run([sys.executable, '-c', script, str(root)], env=environment,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert not ignored.exists()


def test_deployment_templates_keep_loopback_and_streams_and_no_credentials():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for name in ['Caddyfile.lan', 'Caddyfile.public']:
        text = (root / 'deploy' / name).read_text('utf8')
        assert 'reverse_proxy 127.0.0.1:8001' in text and 'flush_interval -1' in text
        assert 'T15.3' in text and 'response_buffers' not in text
    service = (root / 'deploy/chriptmas.service').read_text('utf8')
    assert 'LoadCredential=chriptmas-master-key:' in service
    assert 'UMask=0077' in service and 'User=chriptmas' in service
    assert 'CHRIPTMAS_DEPLOY=server' in service
    assert 'OnCalendar=daily' in (root / 'deploy/chriptmas-backup.timer').read_text('utf8')
