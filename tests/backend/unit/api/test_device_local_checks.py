"""Exercise actual inventory functions through real desktop/server requests."""
from datetime import datetime, timedelta, timezone
import importlib
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest


CHECKS = [
    ('ai', '_local_request'), ('ai', '_local_governance_request'), ('ai', '_external_agent_context_request'),
    ('ai_agents', '_local_governance'), ('context_graph', '_local_authorized'),
    ('expert_catalog', '_local_request'), ('expert_catalog', '_authorized_request'),
    ('external_extensions', '_request_actor_or_403'), ('personal_world_model', '_local_request'),
    ('project_task_cases', '_local_request'), ('recursive_evolution', '_is_local'),
    ('tasks', '_local_request'), ('xiaohongshu_controlled_credentials', '_local_governance_request'),
]


def test_server_device_identity_cannot_become_desktop_file_grant_or_paid_benchmark(tmp_path, monkeypatch):
    app, devices = checked_app(tmp_path, monkeypatch, 'context_graph', '_local_authorized', 'server')
    from backend.api.routes.context_graph import router, _replay_session_id
    from backend.api.desktop_session import DesktopSession
    from backend.security.device_identity import server_identity
    app.include_router(router)
    @app.get('/api/replay-identity')
    def identity(request: Request):
        current = server_identity(request)
        return {'session_id': _replay_session_id(current), 'desktop': isinstance(current, DesktopSession)}
    pair = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='电脑')
    client = TestClient(app, headers={'Authorization': 'Bearer ' + pair['key']})
    result = client.get('/api/replay-identity').json()
    assert result == {'session_id': 'device:' + pair['device']['device_id'], 'desktop': False}
    for path, code in [('/api/rebuild/context-graph-import-selections', 'desktop_file_selection_required'),
        ('/api/rebuild/context-graphs/imports', 'desktop_import_session_required'),
        ('/api/rebuild/context-benchmark-runs', 'benchmark_desktop_session_required')]:
        response = client.post(path, json={})
        assert response.status_code == 403 and response.json()['code'] == code


def checked_app(tmp_path, monkeypatch, module, function, mode):
    # Set only a synthetic root before any legacy module can be imported.
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path / 'isolated'))
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.security.device_auth import install_device_authentication
    from backend.shared.deployment import DeploymentLayout
    check = getattr(importlib.import_module('backend.api.routes.' + module), function)
    app = FastAPI()
    app.state.deployment = DeploymentLayout(mode, tmp_path / 'user', tmp_path if mode == 'server' else None)
    devices = DeviceRegistry(tmp_path / 'server')
    app.state.device_registry = devices
    @app.get('/api/check')
    def call(request: Request):
        try:
            result = check(request)
        except HTTPException:
            result = False
        return {'allowed': bool(result[0]) if isinstance(result, tuple) else bool(result)}
    install_device_authentication(app, registry=devices)
    return app, devices


@pytest.mark.parametrize('module,function', CHECKS)
def test_each_inventory_check_accepts_own_server_device_not_proxy_loopback(tmp_path, monkeypatch, module, function):
    app, devices = checked_app(tmp_path, monkeypatch, module, function, 'server')
    result = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='电脑')
    client = TestClient(app, client=('203.0.113.15', 1234))
    assert client.get('/api/check').status_code == 401
    assert client.get('/api/check', headers={'Authorization': 'Bearer ' + result['key']}).json() == {'allowed': True}
    device = result['device']
    devices.revoke('local-user', device['device_id'], expected_revision=device['revision'])
    assert client.get('/api/check', headers={'Authorization': 'Bearer ' + result['key']}).status_code == 401


@pytest.mark.parametrize('module,function', CHECKS)
def test_each_desktop_inventory_check_keeps_authenticated_local_and_rejects_foreign(tmp_path, monkeypatch, module, function):
    secret = secrets.token_urlsafe(32)
    for key, value in {
        'CHRIPTMAS_DEPLOY': 'desktop', 'CHRIPTMAS_DESKTOP_SESSION_MODE': 'desktop_production',
        'CHRIPTMAS_DESKTOP_SESSION_SECRET': secret, 'CHRIPTMAS_DESKTOP_INSTANCE_ID': 'desktop-a',
        'CHRIPTMAS_DESKTOP_NONCE': secrets.token_urlsafe(32),
        'CHRIPTMAS_DESKTOP_PROTOCOL_VERSION': 'desktop-loopback/1',
        'CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        'CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN': 'http://127.0.0.1:8001',
    }.items():
        monkeypatch.setenv(key, value)
    app, _ = checked_app(tmp_path, monkeypatch, module, function, 'desktop')
    headers = {'X-Chriptmas-Desktop-Session': secret}
    assert TestClient(app, client=('127.0.0.1', 12)).get('/api/check', headers=headers).json() == {'allowed': True}
    assert TestClient(app, client=('203.0.113.15', 12)).get('/api/check', headers=headers).json() == {'allowed': False}
