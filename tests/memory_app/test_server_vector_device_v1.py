"""向量路由沿原真实 DeviceAuthenticationMiddleware；只替换编码器。"""
from backend.memory_app.v2.embedding_settings import vector_policy
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.memory_app import local_vectors
from backend.memory_app.local_model import install_local_model_routes
from backend.memory_app.v2.devices import DeviceRegistry
from backend.security.device_auth import ServerDeviceAuth, install_device_authentication
from backend.shared.deployment import DeploymentLayout
from tests.memory_app.v2.test_local_vector_consumers_v1 import marker_assets


@pytest.fixture
def server_vectors(tmp_path, monkeypatch):
    registry = DeviceRegistry(tmp_path / 'server')
    paired = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='向量测试设备')
    auth = ServerDeviceAuth(registry, port=8020)
    root = tmp_path / 'user'
    directory = marker_assets(root)
    app = FastAPI()
    app.state.deployment = DeploymentLayout('server', root, tmp_path)
    app.state.device_registry = registry
    install_local_model_routes(app, runtime_root=root, generation_allowed=lambda: False, embedding_policy_reader=vector_policy)
    install_device_authentication(app, registry=registry, auth=auth)
    calls = []
    class Encoder:
        def __init__(self, directory):
            pass
        def encode(self, texts, input_type, *, policy):
            calls.append((tuple(texts), input_type))
            return [[1.0] + [0.0] * 255 for _ in texts], len(texts)
    monkeypatch.setattr(local_vectors, 'SentenceEncoder', Encoder)
    monkeypatch.setattr(local_vectors, 'dependencies_available', lambda: True)
    with TestClient(app, base_url='http://127.0.0.1:8020', client=('192.0.2.20', 1234)) as client:
        try:
            yield client, registry, paired, auth, calls
        finally:
            local_vectors.worker_for(directory, policy=vector_policy()).close()


def request(client, key=None):
    return client.post('/local-model/v1/embeddings',
        headers={'Authorization': 'Bearer ' + key} if key else {},
        json={'model': 'google/embeddinggemma-2', 'input': '合成查询', 'input_type': 'query'})


def test_server_vector_no_device_key_is_401_before_encoding(server_vectors):
    client, _, _, _, calls = server_vectors
    assert request(client).status_code == 401
    assert calls == []


def test_server_vector_generation_internal_key_does_not_grant_embedding_access(server_vectors):
    client, _, _, auth, calls = server_vectors
    assert request(client, auth.internal_key_for('http://127.0.0.1:8020/local-model/v1')).status_code == 401
    assert calls == []


def test_server_vector_revoked_real_device_is_401_before_encoding(server_vectors):
    client, registry, paired, _, calls = server_vectors
    device = paired['device']
    registry.revoke('local-user', device['device_id'], expected_revision=device['revision'])
    assert request(client, paired['key']).status_code == 401
    assert calls == []


def test_server_vector_current_real_device_gets_256_dimensions_and_reported_usage(server_vectors):
    client, _, paired, _, calls = server_vectors
    response = request(client, paired['key'])
    assert response.status_code == 200
    assert len(response.json()['data']) == 1
    assert len(response.json()['data'][0]['embedding']) == 256
    assert response.json()['usage'] == {'prompt_tokens': 1, 'total_tokens': 1}
    assert calls == [(('合成查询',), 'query')]
