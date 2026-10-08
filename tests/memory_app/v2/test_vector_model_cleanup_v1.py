from contextlib import closing
"""原 daily callback 的模型域清理合同；编码器仅外围替身。"""
from types import SimpleNamespace
import json
import sqlite3
import pytest
from backend.memory_app import local_vectors
from backend.memory_app.retrieval_models import configured_adapter
from backend.memory_app.v2.embedding_index import EmbeddingIndex
from backend.memory_app.v2.embedding_settings import EmbeddingSettings
from backend.recognition_retrieval import SQLiteEmbeddingCache
from tests.memory_app.v2.test_local_vector_consumers_v1 import env, local_query
from tests.memory_app.v2.test_workbench_ask import add_document
from tests.memory_app.v2.kernel_receipts import wire_receipts

@pytest.mark.parametrize('state', ['ready', 'missing', 'remote', 'disabled'])
def test_existing_daily_job_only_purges_ready_local_managed_model_keys(env, local_query, monkeypatch, state):
    query, calls = local_query
    add_document(env, summary='合成摘要', body='合成事实。' * 180)
    assert EmbeddingIndex(query).run() > 0
    provider = configured_adapter(query.models, 'embedding')
    path = env.records.database_path.parent / 'recognition-vectors.sqlite3'
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute('SELECT * FROM recognition_embedding_cache LIMIT 1').fetchone()
    entry = SimpleNamespace(project_id=row['project_id'], id=row['recognition_id'], revision=row['revision'])
    previous = provider.endpoint + '|synthetic/old-model:128:vector@0|0:0'
    cache = SQLiteEmbeddingCache(str(path))
    try:
        cache.write(model_id=previous, entry=entry, vector=json.loads(row['vector_json']))
        cache.write(model_id='model-b', entry=entry, vector=(0.0, 1.0))
    finally:
        cache.close()
    if state == 'missing':
        monkeypatch.setattr('backend.memory_app.v2.embedding_settings.dependencies_available', lambda: False)
    elif state in {'remote', 'disabled'}:
        query.models.update('embedding', {'base_url': 'https://synthetic.invalid/v1', 'model': 'synthetic-remote',
            'api_key': 'test-synthetic-vector', 'allow_remote': state == 'remote', 'enabled': state == 'remote',
            'expected_revision': 0})
        EmbeddingSettings(env.records, query.models._local_models_root).update_mode(mode='remote', expected_revision=0)
    before_calls = list(calls)
    before_receipts = wire_receipts(env.records)
    facts = {name: env.records.list(name) for name in ('documents', 'workspace_items', 'recognitions')}
    callback = env.http.app.state.memory_daily_jobs.jobs['embedding_cache']
    assert callback.__self__.query is query
    assert callback() == (1 if state == 'ready' else 0)
    cache = SQLiteEmbeddingCache(str(path))
    try:
        assert cache.read(model_id=provider.cache_identity, entry=entry) == tuple(json.loads(row['vector_json']))
        assert cache.read(model_id='model-b', entry=entry) == (0.0, 1.0)
        old = cache.read(model_id=previous, entry=entry)
        assert old is None if state == 'ready' else old == tuple(json.loads(row['vector_json']))
    finally:
        cache.close()
    assert calls == before_calls and wire_receipts(env.records) == before_receipts
    assert {name: env.records.list(name) for name in facts} == facts
    assert query._v1_network_calls == []


def test_daily_keeps_same_endpoint_remote_identity_with_local_shaped_model(env, local_query):
    query, calls = local_query
    add_document(env, summary='合成摘要', body='合成事实。' * 180)
    base_url = query.models.public()['embedding']['base_url']
    shaped_model = 'google/embeddinggemma-2:256:vector@1'
    query.models.update('embedding', {'base_url': base_url, 'model': shaped_model,
        'api_key': 'test-synthetic-vector', 'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    settings = EmbeddingSettings(env.records, query.models._local_models_root)
    settings.update_mode(mode='remote', expected_revision=0)
    remote = configured_adapter(query.models, 'embedding')
    assert remote.config_revision.isdecimal() and ':' not in remote.config_revision
    settings.update_mode(mode='local', expected_revision=1)
    assert EmbeddingIndex(query).run() > 0
    local = configured_adapter(query.models, 'embedding')
    assert remote.endpoint == local.endpoint and local.config_revision.count(':') == 1
    path = env.records.database_path.parent / 'recognition-vectors.sqlite3'
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute('SELECT * FROM recognition_embedding_cache LIMIT 1').fetchone()
    entry = SimpleNamespace(project_id=row['project_id'], id=row['recognition_id'], revision=row['revision'])
    old_local = local.endpoint + '|google/embeddinggemma-2:128:vector@1|0:0'
    vector = tuple(json.loads(row['vector_json']))
    cache = SQLiteEmbeddingCache(str(path))
    try:
        cache.write(model_id=old_local, entry=entry, vector=vector)
        cache.write(model_id=remote.cache_identity, entry=entry, vector=(0.0, 1.0))
    finally:
        cache.close()
    before_calls, before_receipts = list(calls), wire_receipts(env.records)
    facts = {name: env.records.list(name) for name in ('documents', 'workspace_items', 'recognitions')}
    callback = env.http.app.state.memory_daily_jobs.jobs['embedding_cache']
    assert callback.__self__.query is query
    assert callback() == 1
    cache = SQLiteEmbeddingCache(str(path))
    try:
        assert cache.read(model_id=old_local, entry=entry) is None
        assert cache.read(model_id=local.cache_identity, entry=entry) == vector
        assert cache.read(model_id=remote.cache_identity, entry=entry) == (0.0, 1.0)
    finally:
        cache.close()
    assert calls == before_calls and wire_receipts(env.records) == before_receipts
    assert {name: env.records.list(name) for name in facts} == facts
    assert query._v1_network_calls == []
