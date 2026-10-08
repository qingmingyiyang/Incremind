"""真实本机路由的 V1 合同；编码器替身只用于之后的编码边界。"""
from backend.memory_app.v2.embedding_settings import vector_policy
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


def local_client(root, *, host='127.0.0.1'):
    from backend.memory_app.local_model import install_local_model_routes
    application = FastAPI()
    install_local_model_routes(application, runtime_root=root, generation_allowed=lambda: False, embedding_policy_reader=vector_policy)
    return TestClient(application, client=(host, 1234))


def test_embedding_route_rejects_desktop_non_loopback_before_loading(tmp_path):
    with local_client(tmp_path, host='192.0.2.5') as client:
        response = client.post('/local-model/v1/embeddings', json={
            'model': 'google/embeddinggemma-2', 'input': '合成查询'})
    assert response.status_code == 403
    assert response.json()['detail'] == 'local_model_loopback_only'


def test_embedding_route_missing_assets_is_explicit_and_not_generation_disabled(tmp_path):
    with local_client(tmp_path) as client:
        response = client.post('/local-model/v1/embeddings', json={
            'model': 'google/embeddinggemma-2', 'input': '合成查询', 'input_type': 'query'})
    assert response.status_code == 503
    assert response.json()['detail'] == 'local_vector_not_installed'


@pytest.mark.parametrize('body', [
    None,
    [],
    {'model': 'other-model', 'input': '合成输入'},
    {'model': 'google/embeddinggemma-2', 'input': []},
    {'model': 'google/embeddinggemma-2', 'input': 5},
    {'model': 'google/embeddinggemma-2', 'input': ['合成输入', 5]},
    {'model': 'google/embeddinggemma-2', 'input': '合成输入', 'input_type': 'image'},
])
def test_embedding_route_validates_request_before_asset_loading(tmp_path, body):
    with local_client(tmp_path) as client:
        response = client.post('/local-model/v1/embeddings', json=body)
    assert response.status_code == 400
    assert response.json()['detail'] == 'invalid_local_vector_request'


@pytest.mark.parametrize('inputs', ['合' * 24001, ['合成'] * 129], ids=['characters', 'items'])
def test_embedding_route_reports_resource_limits_before_loading(tmp_path, inputs):
    with local_client(tmp_path) as client:
        response = client.post('/local-model/v1/embeddings', json={
            'model': 'google/embeddinggemma-2', 'input': inputs})
    assert response.status_code == 413
    assert response.json()['detail'] == 'local_vector_input_too_large'


def test_embedding_route_malformed_json_is_400(tmp_path):
    with local_client(tmp_path) as client:
        response = client.post('/local-model/v1/embeddings', content='{',
            headers={'content-type': 'application/json'})
    assert response.status_code == 400


def test_embedding_route_real_worker_capacity_is_429_and_releases(tmp_path, monkeypatch):
    from threading import Event
    from backend.memory_app import local_vectors
    from tests.memory_app.v2.test_local_vector_consumers_v1 import marker_assets
    directory = marker_assets(tmp_path)
    started, release = Event(), Event()
    class Encoder:
        def __init__(self, directory):
            pass
        def encode(self, texts, input_type, *, policy):
            started.set()
            assert release.wait(5)
            return [[1.0] + [0.0] * 255 for _ in texts], len(texts)
    monkeypatch.setattr(local_vectors, 'SentenceEncoder', Encoder)
    monkeypatch.setattr(local_vectors, 'dependencies_available', lambda: True)
    worker = local_vectors.worker_for(directory, policy=vector_policy())
    try:
        worker.submit(['合成'], 'document')
        assert started.wait(5)
        for _ in range(vector_policy()['queue_limit'] - 1):
            worker.submit(['合成'], 'document')
        with local_client(tmp_path) as client:
            response = client.post('/local-model/v1/embeddings', json={
                'model': 'google/embeddinggemma-2', 'input': '合成查询'})
        assert response.status_code == 429
        assert response.json()['detail'] == 'local_vector_queue_full'
    finally:
        release.set()
        worker.close()
    assert worker.closed and not worker.thread.is_alive()
