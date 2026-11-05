from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


def local_application(tmp_path, port=8001, *, provider=True, resources=None):
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.local_model import install_local_model_routes
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.security.device_auth import ServerDeviceAuth, install_device_authentication
    from backend.security.secrets import InMemorySecretStore
    from backend.shared.deployment import DeploymentLayout
    from backend.shared.server_resources import resource_context
    from core.storage_provider import SQLiteStructuredRecordStore
    root = tmp_path / 'user'
    devices = DeviceRegistry(tmp_path / 'server')
    auth = ServerDeviceAuth(devices, port=port)
    config = ModelConfiguration(SQLiteStructuredRecordStore(root / 'models.sqlite3'), root, InMemorySecretStore(),
        local_models_root=resources.model_root if resources else None,
        internal_local_key_provider=auth.internal_key_for if provider else None)
    app = FastAPI()
    app.state.deployment = DeploymentLayout('server' if provider else 'desktop', root, tmp_path if provider else None)
    app.state.device_registry = devices
    @app.get('/api/private')
    def private():
        return {'ok': True}
    with resource_context(resources):
        install_local_model_routes(app, runtime_root=root, generation_allowed=config.local_generation_allowed,
            generation_selected=config.local_generation_selected)
    install_device_authentication(app, registry=devices, auth=auth)
    client = TestClient(app, base_url=f'http://127.0.0.1:{port}', client=('127.0.0.1', 1234))
    observed = []
    def wire(**request):
        observed.append(request['api_key'])
        response = client.post('/local-model/v1/chat/completions', headers={'Authorization': 'Bearer ' + request['api_key']},
            json={'model': request['model'].removeprefix('openai/'), 'messages': request['messages'], 'max_tokens': request['max_tokens']})
        assert response.status_code == 200, response.text
        return response.json()
    config._completion_fn = wire
    return config, auth, client, observed


@pytest.mark.parametrize('port', [8001, 8765])
def test_unselected_local_route_keeps_internal_identity_at_exact_endpoint(tmp_path, port):
    config, auth, client, observed = local_application(tmp_path, port)
    key = auth.internal_key_for(f'http://127.0.0.1:{port}/local-model/v1')
    assert key is not None and key != 'local-model'
    assert client.get('/api/private', headers={'Authorization': 'Bearer ' + key}).status_code == 401
    assert client.get('/local-model/v1/models', headers={'Authorization': 'Bearer ' + key}).status_code == 401
    response = client.post('/local-model/v1/chat/completions', headers={'Authorization': 'Bearer ' + key},
        json={'model': 'qwen2.5-1.5b-instruct', 'messages': [{'role': 'user', 'content': 'local'}]})
    assert response.status_code == 403 and response.json()['detail'] == 'local_model_disabled'
    assert not config.local_generation_allowed() and observed == []


def test_internal_key_never_follows_other_endpoint_userinfo_or_redirect(tmp_path):
    _, auth, client, observed = local_application(tmp_path)
    for url in ['http://localhost:8001/local-model/v1', 'http://127.0.0.1:8002/local-model/v1',
                'http://127.0.0.1:8001/api/private', 'http://user@127.0.0.1:8001/local-model/v1',
                'http://127.0.0.1:8001/local-model/v1?query=1', 'http://127.0.0.1:8001/local-model/v1#fragment']:
        assert auth.internal_key_for(url) is None
    @client.app.post('/local-model/v1/redirect')
    def redirect():
        from fastapi.responses import RedirectResponse
        return RedirectResponse('https://outside.example', status_code=307)
    secret = auth.internal_key_for('http://127.0.0.1:8001/local-model/v1')
    assert client.post('/local-model/v1/redirect', headers={'Authorization': 'Bearer ' + secret}).status_code == 401
    assert observed == []


def test_desktop_unselected_local_route_requires_loopback_and_explicit_selection(tmp_path):
    config, _, client, observed = local_application(tmp_path, provider=False)
    body = {'model': 'qwen2.5-1.5b-instruct', 'messages': [{'role': 'user', 'content': 'desktop'}]}
    assert client.post('/local-model/v1/chat/completions', json=body).status_code == 403
    external = TestClient(client.app, client=('192.0.2.1', 1234))
    response = external.post('/local-model/v1/chat/completions', json=body)
    assert response.status_code == 403 and response.json()['detail'] == 'local_model_loopback_only'
    assert not config.local_generation_allowed() and observed == []
