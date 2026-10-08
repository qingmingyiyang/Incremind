"""依赖倒置的真实装配、旧独立领域与显式冻结输入合同。"""
from threading import Event
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.memory_app.app import create_app
from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.memory_app import local_vectors
from backend.memory_app.v2.embedding_settings import EmbeddingSettings
from backend.memory_app.v2.policies import get
from backend.security.secrets import InMemorySecretStore
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.mark.parametrize('configured', [False, True], ids=['empty', 'remote'])
def test_unbound_model_configuration_keeps_original_embedding_identity(tmp_path, configured):
    records = SQLiteStructuredRecordStore(tmp_path / 'models.sqlite3')
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore())
    try:
        if configured:
            models.update('embedding', {'base_url': 'https://synthetic.invalid/v1', 'model': 'synthetic-embedding',
                'api_key': 'test-synthetic-embedding', 'allow_remote': True, 'enabled': True, 'expected_revision': 0})
        public = models.public()['embedding']
        assert public['provider'] == 'openai' and public['configured'] is configured
        assert not {'mode', 'mode_revision', 'local', 'model_key'} & public.keys()
        if configured:
            frozen = models.snapshot('embedding')
            assert frozen['revision'] == public['revision'] == 1
            assert frozen['model'] == public['model'] == 'synthetic-embedding'
            assert not {'mode', 'mode_revision', 'local', 'model_key'} & frozen.keys()
        else:
            with pytest.raises(ModelConfigurationError, match='^model_not_configured$'):
                models.snapshot('embedding')
        assert records.list('v2_embedding_mode') == () and records.list('v2_embedding_install') == ()
    finally:
        models.close()


def test_app_binds_one_real_embedding_owner_before_first_public_read(tmp_path, monkeypatch):
    instances, closed = [], []
    original_init, original_close = EmbeddingSettings.__init__, EmbeddingSettings.close
    def observed_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        instances.append(self)
    def observed_close(self):
        original_close(self)
        closed.append(self)
    # 只记录真实构造和关闭，两个观察包装均执行原方法，不替换领域行为。
    monkeypatch.setattr(EmbeddingSettings, '__init__', observed_init)
    monkeypatch.setattr(EmbeddingSettings, 'close', observed_close)
    app = create_app(runtime_root=tmp_path, legacy_app=FastAPI())
    models = app.state.recognition_models
    try:
        with TestClient(app, base_url='http://127.0.0.1:8020') as client:
            owner = getattr(app.state, 'memory_embedding_settings', None)
            assert isinstance(owner, EmbeddingSettings)
            assert models._embedding_projection.__self__ is owner
            assert owner.records is models.records and owner.models_root == models._local_models_root
            public = models.public()['embedding']
            assert public['mode'] == 'local' and public['mode_revision'] == 0
            assert public['provider'] == 'local' and public['configured'] is False
            assert public['local']['status'] == 'missing'
            response = client.patch('/api/v2/settings/embedding-mode', json={'mode': 'remote', 'expected_revision': 0})
            assert response.status_code == 200
            assert response.json()['mode'] == 'remote' and response.json()['mode_revision'] == 1
            assert app.state.memory_embedding_settings is owner
            assert models._embedding_projection.__self__ is owner
            assert owner.mode({}) == {'mode': 'remote', 'revision': 1}
            assert [value for value in instances if value.records is models.records] == [owner]
        assert closed == [owner]
    finally:
        models.close()


@pytest.mark.parametrize('input_type', ['query', 'document'])
def test_explicit_worker_policy_is_frozen_per_submit_not_constructor(tmp_path, input_type):
    initial = get('vector', version='@1')()
    selected = {**initial, 'dims': 128, 'query_prefix': 'explicit-query: ',
        'document_prefix': 'explicit-title: {title} | explicit-text: {content}'}
    entered, release = Event(), Event()
    observations = []
    class ModelBoundary:
        tokenizer = SimpleNamespace(encode=lambda text: [1, 2])
        def encode(self, texts, **options):
            observations.append((tuple(texts), dict(options)))
            entered.set()
            assert release.wait(10)
            return [[1.0] + [0.0] * (options['truncate_dim'] - 1) for _ in texts]
    def loader(directory):
        encoder = object.__new__(local_vectors.SentenceEncoder)
        encoder.model = ModelBoundary()
        return encoder
    worker = local_vectors.VectorWorker(tmp_path, loader=loader, policy=initial)
    try:
        text = 'needle' if input_type == 'query' else 'Title\nBody'
        future = worker.submit([text], input_type, policy=selected)
        assert entered.wait(10)
        selected.update(dims=64, query_prefix='changed: ', document_prefix='changed: {content}')
        release.set()
        result = future.result(timeout=10)
        expected = 'explicit-query: needle' if input_type == 'query' else 'explicit-title: Title | explicit-text: Body'
        assert len(observations) == 1 and observations[0][0] == (expected,)
        assert observations[0][1]['truncate_dim'] == 128 and observations[0][1]['batch_size'] == 8
        assert observations[0][1]['normalize_embeddings'] is True
        assert len(result['data']) == 1 and len(result['data'][0]['embedding']) == 128
        assert result['model'] == initial['model'] and result['usage'] == {'prompt_tokens': 2, 'total_tokens': 2}
    finally:
        release.set()
        worker.close()
        assert worker.thread is None or not worker.thread.is_alive()
        assert worker.pending == 0


def test_independent_settings_installer_binds_real_model_owner_without_fixture_prebind(tmp_path, monkeypatch):
    from backend.memory_app.v2.settings import install_settings_routes
    records = SQLiteStructuredRecordStore(tmp_path / 'settings.sqlite3')
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore())
    app = FastAPI()
    closed, fetched = [], []
    original_close = EmbeddingSettings.close
    def observed_close(self):
        original_close(self)
        closed.append(self)
    def fetch_boundary(models_root, *, model, progress):
        fetched.append((models_root, model))
        progress({'done': 1, 'total': 1})
    # 仅观察原关闭；网络/权重安装是外围边界，真实 owner/job/CAS/thread 保留。
    monkeypatch.setattr(EmbeddingSettings, 'close', observed_close)
    monkeypatch.setattr('backend.memory_app.v2.embedding_settings.install_embedding', fetch_boundary)
    assert models._embedding_projection is None
    install_settings_routes(app, runtime_root=tmp_path, records=records, models=models)
    owner = app.state.memory_embedding_settings
    try:
        with TestClient(app, base_url='http://127.0.0.1:8020') as client:
            response = client.get('/api/v2/settings')
            assert response.status_code == 200
            initial = response.json()['model']['embedding']
            assert initial['mode'] == 'local' and initial['mode_revision'] == 0
            assert initial['provider'] == 'local' and initial['local']['can_install'] is True
            assert models._embedding_projection.__self__ is owner
            assert owner.records is records and owner.models_root == models._local_models_root
            response = client.patch('/api/v2/settings/embedding-mode', json={'mode': 'remote', 'expected_revision': 0})
            assert response.status_code == 200
            assert response.json()['mode'] == 'remote' and response.json()['mode_revision'] == 1
            response = client.patch('/api/v2/settings/embedding-mode', json={'mode': 'local', 'expected_revision': 1})
            assert response.status_code == 200
            assert response.json()['mode'] == 'local' and response.json()['mode_revision'] == 2
            response = client.post('/api/v2/settings/embedding/install', json={'expected_revision': 2})
            assert response.status_code == 202
            job = records.read('v2_embedding_install', 'default')
            assert job.payload['job_id'] == response.json()['job_id']
            assert owner.thread is not None
            owner.thread.join(10)
            assert not owner.thread.is_alive()
            assert fetched == [(owner.models_root, get('vector', version='@1')()['model'])]
            assert records.read('v2_embedding_install', 'default').payload['status'] == 'ready'
            assert app.state.memory_embedding_settings is owner
            assert models._embedding_projection.__self__ is owner
            assert records.list('recognition_model_config') == ()
        assert closed == [owner]
    finally:
        models.close()
