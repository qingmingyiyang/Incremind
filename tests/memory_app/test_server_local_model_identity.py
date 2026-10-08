"""Run the real gateway and ASGI route, replacing only wire/inference boundaries."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


def local_application(tmp_path, monkeypatch, port=8001, *, provider=True):
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.local_model import install_local_model_routes
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.security.device_auth import ServerDeviceAuth, install_device_authentication
    from backend.security.secrets import InMemorySecretStore
    from backend.shared.deployment import DeploymentLayout
    from core.storage_provider import SQLiteStructuredRecordStore
    root = tmp_path / 'user'
    model = root / 'data/models/qwen2.5-1.5b-instruct/model.safetensors'
    model.parent.mkdir(parents=True); model.write_bytes(b'fake inference asset')
    devices = DeviceRegistry(tmp_path / 'server')
    auth = ServerDeviceAuth(devices, port=port)
    config = ModelConfiguration(SQLiteStructuredRecordStore(root / 'models.sqlite3'), root, InMemorySecretStore(),
        internal_local_key_provider=auth.internal_key_for if provider else None)
    config.update_generation_mode(mode='local', local_enabled=True,
        local_base_url=f'http://127.0.0.1:{port}/local-model/v1', expected_revision=0)
    app = FastAPI()
    app.state.deployment = DeploymentLayout('server' if provider else 'desktop', root, tmp_path if provider else None)
    app.state.device_registry = devices
    @app.get('/api/private')
    def private():
        return {'ok': True}
    install_local_model_routes(app, runtime_root=root, generation_allowed=config.local_generation_allowed)
    install_device_authentication(app, registry=devices, auth=auth)
    engine_calls = []
    def engine(directory, messages, max_tokens):
        engine_calls.append((directory, messages, max_tokens))
        return 'done', 12, 2, True
    monkeypatch.setattr('backend.memory_app.local_model._generate', engine)
    client = TestClient(app, base_url=f'http://127.0.0.1:{port}', client=('127.0.0.1', 1234))
    observed = []
    def wire(**request):
        observed.append(request['api_key'])
        response = client.post('/local-model/v1/chat/completions', headers={'Authorization': 'Bearer ' + request['api_key']},
            json={'model': request['model'].removeprefix('openai/'), 'messages': request['messages'], 'max_tokens': request['max_tokens']})
        assert response.status_code == 200
        return response.json()
    config._completion_fn = wire
    return config, auth, client, engine_calls, observed


@pytest.mark.parametrize('port', [8001, 8765])
def test_selected_local_gateway_has_internal_identity_at_real_wire_and_original_frozen_snapshot(tmp_path, monkeypatch, port):
    config, auth, client, engine_calls, observed = local_application(tmp_path, monkeypatch, port)
    assert config.snapshot('generation')['api_key'] == 'local-model'
    text, _ = config.complete([{'role': 'user', 'content': 'read locally'}])
    assert text == 'done' and len(engine_calls) == 1
    assert observed[0] != 'local-model'
    assert client.get('/api/private', headers={'Authorization': 'Bearer ' + observed[0]}).status_code == 401
    assert client.get('/local-model/v1/models', headers={'Authorization': 'Bearer ' + observed[0]}).status_code == 401
    assert auth.internal_key_for(f'http://127.0.0.1:{port}/local-model/v1') is not None


def test_internal_key_never_follows_other_endpoint_userinfo_or_redirect(tmp_path, monkeypatch):
    config, auth, client, engine_calls, observed = local_application(tmp_path, monkeypatch)
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
    config.update_generation_mode(mode='local', local_enabled=True,
        local_base_url='http://127.0.0.1:8002/local-model/v1', expected_revision=1)
    received = []
    config._completion_fn = lambda **request: (received.append(request['api_key']) or {
        'choices': [{'message': {'content': 'done'}, 'finish_reason': 'stop'}], 'usage': {}})
    config.complete([{'role': 'user', 'content': 'elsewhere'}])
    assert received == ['local-model']
    assert observed == [] and engine_calls == []


def test_desktop_without_internal_provider_keeps_original_local_wire_key(tmp_path, monkeypatch):
    config, _, _, engine_calls, observed = local_application(tmp_path, monkeypatch, provider=False)
    assert config.complete([{'role': 'user', 'content': 'desktop'}])[0] == 'done'
    assert observed == ['local-model'] and len(engine_calls) == 1
