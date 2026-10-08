"""Import-time globals do not create server user containers."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys


def test_lazy_proxy_introspection_does_not_create_a_child_and_asgi_loads_once():
    from backend.shared.lazy_application import LazyApplication
    calls = []
    async def child(scope, receive, send):
        calls.append(scope['type'])
    def factory():
        calls.append('factory')
        return child
    proxy = LazyApplication(factory)
    assert proxy.application is None
    assert getattr(proxy, 'state', None) is None
    assert calls == []
    async def run():
        await proxy({'type': 'http'}, None, None)
        await proxy({'type': 'http'}, None, None)
    asyncio.run(run())
    assert calls == ['factory', 'http', 'http']


def test_real_server_api_module_import_does_not_initialize_default_user(tmp_path):
    task_env = dict(os.environ, CHRIPTMAS_DEPLOY='server', CHRIPTMAS_APP_ROOT=str(tmp_path))
    task_env['PYTHONPATH'] = str(Path(__file__).resolve().parents[3] / 'src')
    config = tmp_path / 'users' / 'local-user' / 'config' / 'settings.toml'
    config.parent.mkdir(parents=True)
    config.write_bytes((Path(__file__).resolve().parents[3] / 'config' / 'settings.toml.example').read_bytes())
    for name in tuple(task_env):
        if name.startswith('CHRIPTMAS_RUNTIME_'):
            del task_env[name]
    code = "from pathlib import Path; import backend.api.app as entry; assert not list(Path(__import__('os').environ['CHRIPTMAS_APP_ROOT']).rglob('*.sqlite3')), 'default_user_initialized'; assert entry.app.application is None"
    result = subprocess.run([sys.executable, '-c', code], env=task_env, capture_output=True, text=True,
        timeout=60)
    assert result.returncode == 0, result.stderr
