"""Connection metadata comes from the installed desktop, without opening data."""
from pathlib import Path
from shutil import copyfile
import shlex
import sys
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    (tmp_path / 'config').mkdir()
    copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example',
             tmp_path / 'config/settings.toml')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'desktop')
    from backend.memory_app.app import create_app
    return create_app(runtime_root=tmp_path / 'runtime', legacy_app=FastAPI())


def get(app, base='http://127.0.0.1:8765', server=None, **options):
    if server is not None:
        inner = app
        async def app(scope, receive, send):
            # ASGI server address is the external HTTP boundary, not the Host header.
            await inner({**scope, 'server': server}, receive, send)
    return TestClient(app, base_url=base).get(
        '/api/v2/settings/external-agent/connection?project_id=alpha', **options)


def test_installed_metadata_uses_actual_port_and_source_root_not_host(desktop):
    response = get(desktop, headers={'Host': 'foreign.invalid:9999',
        'Forwarded': 'host=foreign.invalid:1234;proto=https', 'X-Forwarded-Port': '3333'})
    assert response.status_code == 200
    value = response.json()
    assert set(value) == {'available', 'version', 'project_id', 'shell', 'commands', 'instructions'}
    assert value['available'] is True and value['version'] == 'mcp-connection@1'
    assert value['project_id'] == 'alpha'
    assert value['shell'] == ('powershell' if sys.platform == 'win32' else 'sh')
    assert set(value['commands']) == {'claude', 'codex'}
    for client, command in value['commands'].items():
        assert command.startswith(client + ' mcp add ')
        assert 'PYTHONPATH=' + str(Path(__file__).resolve().parents[3] / 'src') in command
        assert 'CHRIPTMAS_MCP_BACKEND_URL=http://127.0.0.1:8765' in command
        assert sys.executable in command
        assert command.endswith(' -m backend.memory_app.mcp')
        assert 'foreign.invalid' not in command and ':8001' not in command
    assert '--transport stdio' in value['commands']['claude']
    assert 'mcp-connection@1' in value['instructions']
    assert 'methods' in value['instructions'] and 'recall' in value['instructions']
    assert 'report_use' in value['instructions'] and 'turn_id' in value['instructions']
    assert 'project="alpha"' in value['instructions']


def test_metadata_does_not_read_or_write_records_models_or_configuration(desktop, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('connection_metadata_must_not_access_data')
    records = desktop.state.recognition_records
    for method in ('begin', 'read', 'list'):
        monkeypatch.setattr(records, method, forbidden)
    monkeypatch.setattr(desktop.state.recognition_models, 'public', forbidden)
    assert get(desktop).status_code == 200


def test_server_metadata_never_discloses_local_paths_or_commands(desktop):
    registry = desktop.state.device_registry
    paired = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='合成设备')
    desktop.state.deployment = SimpleNamespace(mode='server')
    response = get(desktop, headers={'Authorization': 'Bearer ' + paired['key']})
    assert response.status_code == 200 and response.json() == {'available': False}


def test_ipv6_actual_loopback_uses_brackets(desktop):
    response = get(desktop, server=('::1', 9123))
    assert response.status_code == 200
    assert all('CHRIPTMAS_MCP_BACKEND_URL=http://[::1]:9123' in command
               for command in response.json()['commands'].values())


@pytest.mark.parametrize('base', ['http://localhost:8765', 'http://192.0.2.5:8765'])
def test_non_numeric_or_non_loopback_address_is_rejected(desktop, base):
    response = get(desktop, base)
    assert response.status_code == 400
    assert response.json() == {'detail': 'mcp_connection_unavailable'}


def test_project_validation_is_original_strict_contract(desktop):
    response = TestClient(desktop, base_url='http://127.0.0.1:8765').get(
        '/api/v2/settings/external-agent/connection', params={'project_id': 'alpha\nrun command'})
    assert response.status_code == 400 and response.json()['detail'] == 'invalid_project_id'


@pytest.mark.parametrize('server', [None, (), ('127.0.0.1', True), ('127.0.0.1', 0),
                                   ('127.0.0.1', 65536), ([], 8765)])
def test_missing_or_invalid_asgi_server_fails_closed(desktop, server):
    from starlette.requests import Request
    from fastapi import HTTPException
    from backend.memory_app.v2.mcp_connection import connection_metadata
    with pytest.raises(HTTPException) as failure:
        connection_metadata(Request({'type': 'http', 'app': desktop, 'server': server}), 'alpha')
    assert failure.value.status_code == 400
    assert failure.value.detail == 'mcp_connection_unavailable'


@pytest.mark.parametrize('shell', ['powershell', 'sh'])
def test_command_quoting_preserves_exact_arguments_without_execution(shell):
    from backend.memory_app.v2.mcp_connection import _command
    executable = "/tmp/a b'$(unsafe)`;python"
    source = "/tmp/source space'$(unsafe)`;src"
    command = _command('codex', executable, source, 'http://127.0.0.1:8765', shell)
    wanted = ['codex', 'mcp', 'add', 'chriptmas-memory', '--env', 'PYTHONPATH=' + source,
        '--env', 'CHRIPTMAS_MCP_BACKEND_URL=http://127.0.0.1:8765', '--', executable,
        '-m', 'backend.memory_app.mcp']
    if shell == 'sh':
        assert shlex.split(command) == wanted
    else:
        # PowerShell single-quoted literals only escape an apostrophe by doubling it.
        assert "'" + executable.replace("'", "''") + "'" in command
        assert "'PYTHONPATH=" + source.replace("'", "''") + "'" in command
        assert command.count('--env ') == 2
