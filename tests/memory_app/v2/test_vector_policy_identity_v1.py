from backend.memory_app.v2.embedding_settings import vector_policy
from contextlib import closing
"""仅草案：真实策略登记、生产身份与四键缓存；尚未运行。"""
from types import SimpleNamespace
from threading import Event
import sqlite3
import pytest
from backend.memory_app import local_vectors
from backend.memory_app.model_config import ModelConfigurationError
from backend.memory_app.retrieval_models import configured_adapter
from backend.memory_app.v2.embedding_index import EmbeddingIndex
from backend.memory_app.v2 import policies
from backend.recognition_retrieval import SQLiteEmbeddingCache, HttpEmbeddingProvider
from tests.memory_app.v2.test_local_vector_consumers_v1 import env, local_query
from tests.memory_app.v2.test_workbench_ask import add_document
from tests.memory_app.v2.kernel_receipts import wire_receipts

@pytest.mark.parametrize('changed', ['model', 'dims', 'version'])
def test_selected_vector_policy_misses_real_previous_cache(changed, env, local_query, monkeypatch):
    query, calls = local_query
    add_document(env, summary='合成摘要', body='合成事实。' * 180)
    facts = {name: env.records.list(name) for name in ('documents', 'workspace_items', 'recognitions')}
    assert EmbeddingIndex(query).run() > 0
    old_provider = configured_adapter(query.models, 'embedding')
    old_receipts = wire_receipts(env.records)
    old_calls = list(calls)
    path = env.records.database_path.parent / 'recognition-vectors.sqlite3'
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute('SELECT * FROM recognition_embedding_cache').fetchall()
    assert rows
    old_policy = dict(policies.get('vector', version='@1')())
    selected = '@71' if changed == 'version' else '@1'
    candidate = {**old_policy, **({'model': 'synthetic/new-embedding'} if changed == 'model' else
                                {'dims': 128} if changed == 'dims' else {})}
    monkeypatch.setitem(policies._REGISTRY, 'vector',
        {key: value for key, value in policies._REGISTRY['vector'].items() if key != selected})
    policies.register('vector', selected)(lambda: dict(candidate))
    with policies.override(vector=selected):
        if changed == 'model':
            frozen = dict(query.models.public()['embedding'])
            assert frozen['configured'] is False and frozen['local']['status'] == 'missing'
            with pytest.raises(ModelConfigurationError, match='^model_not_configured$'):
                query.models.snapshot('embedding')
            plan = query.prepare_ask('alpha', '合成事实')
            assert any(entry['layer'] == 'L1' and '合成事实。' in entry['excerpt'] for entry in plan['chosen'])
            # 只读公开冻结配置，经原提供方身份方法查缓存；未配置资产不进入新模型编码。
            provider = HttpEmbeddingProvider(None, frozen['base_url'].rstrip('/') + '/embeddings',
                frozen['model_key'], config_revision=f"{frozen['revision']}:{frozen['mode_revision']}")
        else:
            provider = configured_adapter(query.models, 'embedding')
        cache = SQLiteEmbeddingCache(str(path))
        try:
            for row in rows:
                entry = SimpleNamespace(project_id=row['project_id'], id=row['recognition_id'], revision=row['revision'])
                assert cache.read(model_id=old_provider.cache_identity, entry=entry) is not None
                assert cache.read(model_id=provider.cache_identity, entry=entry) is None
            assert query.models.public()['embedding']['model_key'] == f"{candidate['model']}:{candidate['dims']}:vector{selected}"
            assert provider.cache_identity != old_provider.cache_identity
        finally:
            cache.close()
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute('SELECT * FROM recognition_embedding_cache').fetchall() == rows
    assert calls == old_calls and wire_receipts(env.records) == old_receipts
    assert {name: env.records.list(name) for name in facts} == facts
    assert query._v1_network_calls == []


@pytest.mark.parametrize('input_type', ['query', 'document'])
def test_worker_freezes_selected_policy_after_caller_override_ends(tmp_path, monkeypatch, input_type):
    selected = '@71'
    candidate = {**policies.get('vector', version='@1')(), 'dims': 128,
        'query_prefix': 'frozen-query: ', 'document_prefix': 'frozen-title: {title} | frozen-text: {content}'}
    monkeypatch.setitem(policies._REGISTRY, 'vector', dict(policies._REGISTRY['vector']))
    policies.register('vector', selected)(lambda: dict(candidate))
    entered, release = Event(), Event()
    observations = []
    class ModelBoundary:
        tokenizer = SimpleNamespace(encode=lambda text: [1, 2])
        def encode(self, inputs, **options):
            observations.append((tuple(inputs), dict(options)))
            entered.set()
            assert release.wait(10)
            return [[1.0] + [0.0] * (options['truncate_dim'] - 1) for _ in inputs]
    def loader(directory):
        encoder = object.__new__(local_vectors.SentenceEncoder)
        encoder.model = ModelBoundary()
        return encoder
    worker = local_vectors.VectorWorker(tmp_path, loader=loader, policy=vector_policy())
    text = 'needle' if input_type == 'query' else 'Title\nBody'
    try:
        with policies.override(vector=selected):
            future = worker.submit([text], input_type, policy=vector_policy())
            assert entered.wait(10)
        release.set()
        response = future.result(timeout=10)
        expected = ('frozen-query: needle',) if input_type == 'query' else ('frozen-title: Title | frozen-text: Body',)
        assert len(observations) == 1 and observations[0][0] == expected
        assert observations[0][1]['truncate_dim'] == 128
        assert observations[0][1]['normalize_embeddings'] is True
        assert observations[0][1]['batch_size'] == candidate['batch_size']
        assert response['model'] == candidate['model']
        assert len(response['data']) == 1 and len(response['data'][0]['embedding']) == 128
        assert response['usage'] == {'prompt_tokens': 2, 'total_tokens': 2}
    finally:
        release.set()
        worker.close()
    assert worker.thread is not None and not worker.thread.is_alive() and worker.pending == 0
