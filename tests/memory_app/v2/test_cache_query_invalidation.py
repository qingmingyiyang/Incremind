from backend.memory_app.retrieval_models import configured_retrieve
from backend.recognition_retrieval import SQLiteEmbeddingCache
from tests.memory_app.test_retrieval_source_policy import Models, setup_sources, fake_transport
from tests.memory_app.v2.test_contextual_chunk_vectors import vectors as vectors, prepared, score
from tests.memory_app.v2.test_workbench_ask import env as env, add_document


def observe_purge(monkeypatch):
    calls = []
    original = SQLiteEmbeddingCache.purge_invalid

    def observed(cache, **kwargs):
        calls.append(kwargs['project_id'])
        return original(cache, **kwargs)

    monkeypatch.setattr(SQLiteEmbeddingCache, 'purge_invalid', observed)
    return calls


def test_configured_query_never_purges_real_cache(tmp_path, monkeypatch):
    service, authority, scope, _ = setup_sources(tmp_path)
    fake_transport(monkeypatch)
    calls = observe_purge(monkeypatch)
    for entries in (service.retrieval_entries(scope=scope), ()):
        configured_retrieve(Models(), tmp_path, 'project', 'alpha', entries,
                            source_egress=authority, source_scope=scope)
    assert calls == []


def test_chunk_query_never_purges_real_cache(env, vectors, monkeypatch):
    doc, _ = add_document(env, summary='摘要', body='尾部事实。' * 100)
    calls = observe_purge(monkeypatch)
    data = prepared(env, doc)
    assert score(data)
    assert score(data)
    assert len(vectors) == 2
    assert calls == []


def test_revision_after_real_write_cannot_hit_old_cached_vector(tmp_path, monkeypatch):
    service, authority, scope, sources = setup_sources(tmp_path)
    calls = fake_transport(monkeypatch)
    configured_retrieve(Models(), tmp_path, 'project', 'alpha', service.retrieval_entries(scope=scope),
                        source_egress=authority, source_scope=scope)
    changed = service.revise(scope=scope, recognition_id=sources[0][1], expected_revision=1,
                             content='alpha revised material')
    calls.clear()
    result = configured_retrieve(Models(), tmp_path, 'project', 'alpha', service.retrieval_entries(scope=scope),
                                 source_egress=authority, source_scope=scope)
    embedding = next(payload['input'] for endpoint, payload in calls if endpoint.endswith('embeddings'))
    assert embedding == ['alpha', 'alpha revised material']
    assert next(hit for hit in result.hits if hit.id == changed.id).revision == changed.revision
