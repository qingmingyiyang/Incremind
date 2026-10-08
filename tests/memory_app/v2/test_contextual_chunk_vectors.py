import pytest

from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.retrieval_models import ConfiguredTransport
from backend.memory_app.v2.contextual_chunk_vectors import chunk_vector_scores
from backend.memory_app.v2.layers import summary_of
from backend.memory_app.v2.privacy import set_private_project
from backend.recognition import WorkScope
from backend.recognition_retrieval import SQLiteEmbeddingCache
from core.search_and_recall.evidence_windows import split_evidence_chunks
from tests.memory_app.v2.test_workbench_ask import env as env, add_document
from tests.memory_app.v2.test_insight_links import VectorModel
from tests.memory_app.v2.kernel_receipts import wire_receipts, requests


@pytest.fixture
def vectors(env, monkeypatch):
    calls = []

    def wire(transport, *, endpoint, payload):
        transport._check_current()
        calls.append(payload['input'])
        return {'data': [{'index': i, 'embedding': [1.0, 0.0]}
                         for i, _ in enumerate(payload['input'])], 'usage': {'prompt_tokens': 5}}

    monkeypatch.setattr(ConfiguredTransport, 'post_json', wire)
    return calls


def prepared(env, doc):
    query = env.domains.query
    query.models = VectorModel()
    entry = next(e for e in query.query_entries('alpha') if e['id'] == doc)
    scope = WorkScope('local-user', 'alpha')
    snapshot = query.original_snapshot(scope, entry, SourceEgressService(env.records))
    markdown = env.documents.markdown(doc)
    summary, _, start = summary_of(markdown)
    chunks = split_evidence_chunks(markdown[start:])
    return query, 'alpha', entry, scope, snapshot, chunks, start, summary


def score(data, question='尾部是什么？', **overrides):
    query, project, entry, scope, snapshot, chunks, start, summary = data
    return chunk_vector_scores(query, project, entry, scope, snapshot, chunks,
        layer='L1', span_start=start, summary=summary, question=question, **overrides)


def test_vector_cache_reuses_chunks_and_records_kernel_wire(env, vectors):
    doc, _ = add_document(env, summary='本资料摘要', body='第一事实。' * 220)
    data = prepared(env, doc)
    assert score(data) == {i: pytest.approx(1) for i in range(len(data[5]))}
    assert len(vectors[0]) == len(data[5]) + 1
    assert all('本资料摘要' in s for s in vectors[0][1:])
    assert score(data)
    assert [len(v) for v in vectors] == [len(data[5]) + 1, 1]
    assert all(r['status'] == 'succeeded' for r in wire_receipts(env.records))
    assert len(wire_receipts(env.records)) == 2
    assert all(r['execution_policy']['purpose'] == 'aux' for r in requests(env.records))


@pytest.fixture
def dedup_vectors(monkeypatch):
    calls = []
    def wire(transport, *, endpoint, payload):
        transport._check_current()
        calls.append(payload['input'])
        return {'data': [{'index': i, 'embedding': [1.0 if '尾部' in text else -1.0, 0.0]}
                         for i, text in enumerate(payload['input'])], 'usage': {'prompt_tokens': 5}}
    monkeypatch.setattr(ConfiguredTransport, 'post_json', wire)
    return calls


def test_dedup_reads_actual_chunk_cache_without_another_wire(env, dedup_vectors):
    from backend.memory_app.v2.recall_dedup import cached_candidate_vectors, identity
    doc, _ = add_document(env, summary='简介', body='尾部事实。' * 220)
    query = env.domains.query
    query.models = VectorModel()
    candidates = query.collect_candidates('alpha', '尾部')['candidates']
    body = next(c for c in candidates if c['layer'] == 'L1' and c['entry']['id'] == doc)
    assert len(body['windows']) == 1
    count = len(dedup_vectors)
    assert cached_candidate_vectors(query, [body])[identity(body)] == (1., 0.)
    assert len(dedup_vectors) == count
    env.documents.save_user_edit(doc, expected_revision=2,
        markdown='# Synthetic\n\n## 摘要\n新简介\n\n## 正文\n' + '尾部事实。' * 220)
    assert cached_candidate_vectors(query, [body]) == {}


def test_dedup_discards_cache_after_privacy_revocation(env, dedup_vectors, monkeypatch):
    from backend.memory_app.v2.recall_dedup import cached_candidate_vectors
    doc, _ = add_document(env, summary='简介', body='尾部事实。' * 220)
    query = env.domains.query
    query.models = VectorModel()
    candidates = query.collect_candidates('alpha', '尾部')['candidates']
    body = next(c for c in candidates if c['layer'] == 'L1' and c['entry']['id'] == doc)
    assert len(body['windows']) == 1
    assert cached_candidate_vectors(query, [body])
    original = SQLiteEmbeddingCache.read
    reads = []
    def revoke(cache, **args):
        result = original(cache, **args)
        reads.append(args['entry'].id)
        set_private_project(env.records, 'alpha', True, expected_revision=0)
        return result
    monkeypatch.setattr(SQLiteEmbeddingCache, 'read', revoke)
    assert cached_candidate_vectors(query, [body]) == {}
    assert len(reads) == 1


def test_revision_and_legacy_purge_do_not_evict_other_parent(env, vectors):
    a, _ = add_document(env, body='甲事实。' * 200)
    b, _ = add_document(env, body='乙事实。' * 200)
    first, second = prepared(env, a), prepared(env, b)
    assert score(first) and score(second)
    cache = SQLiteEmbeddingCache(str(env.root / 'recognition-vectors.sqlite3'))
    try:
        cache.purge_invalid(project_id='alpha', current_revisions={})
    finally:
        cache.close()
    env.documents.save_user_edit(a, expected_revision=2,
        markdown='# Synthetic\n\n## 摘要\nalpha\n\n## 正文\n' + '甲的新事实。' * 200)
    changed = prepared(env, a)
    assert score(changed)
    assert len(vectors[-1]) == len(changed[5]) + 1
    assert score(second, question='换一个问题？')
    assert len(vectors[-1]) == 1


@pytest.mark.parametrize('restriction', ['disabled', 'private', 'missing_snapshot', 'private_item'])
def test_restricted_material_never_reaches_wire(env, vectors, restriction):
    doc, item = add_document(env, body='不可外发正文。' * 100)
    data = list(prepared(env, doc))
    if restriction == 'disabled':
        class Disabled(VectorModel):
            def public(self):
                result = super().public()
                result['embedding']['allow_remote'] = False
                return result
        data[0].models = Disabled()
    elif restriction == 'private':
        set_private_project(env.records, 'alpha', True, expected_revision=0)
    elif restriction == 'private_item':
        row = env.records.read('workspace_items', item)
        SourceEgressService(env.records).set_policy(data[3], 'original_item', item, row.revision, 0, [])
    else:
        data[4] = None
    assert score(data) == {}
    assert vectors == []


def test_revoke_during_wire_discards_vector_result(env, monkeypatch):
    doc, _ = add_document(env, body='合成正文。' * 200)
    data = prepared(env, doc)
    calls = []

    def wire(transport, *, endpoint, payload):
        transport._check_current()
        calls.append(payload)
        set_private_project(env.records, 'alpha', True, expected_revision=0)
        return {'data': [{'index': i, 'embedding': [1.0, 0.0]}
                         for i, _ in enumerate(payload['input'])]}

    monkeypatch.setattr(ConfiguredTransport, 'post_json', wire)
    assert score(data) == {}
    assert len(calls) == 1


def test_forged_prefix_and_stale_parent_are_rejected_before_wire(env, vectors):
    doc, _ = add_document(env, body='合成正文。' * 100)
    data = list(prepared(env, doc))
    data[-1] = '其他私密资料摘要'
    assert score(data) == {}
    data = prepared(env, doc)
    env.documents.save_user_edit(doc, expected_revision=2, markdown='# Changed\nnew text')
    assert score(data) == {}
    assert vectors == []


@pytest.mark.parametrize('kind', ['document_original', 'source'])
def test_original_chunks_freeze_real_materials_and_preserve_parent(env, vectors, kind):
    text = '原件证据。' * 180
    query = env.domains.query
    scope = WorkScope('local-user', 'alpha')
    if kind == 'document_original':
        doc, item = add_document(env, original=text, summary='同资料摘要')
        query.models = VectorModel()
        entry = next(e for e in query.query_entries('alpha') if e['id'] == doc)
        summary = '同资料摘要'
    else:
        query.source_store.write('sources', 'standalone', {'id': 'standalone',
            'project_id': 'alpha', 'title': '独立原件', 'metadata': {'content_snapshot': text}},
            expected_revision=0)
        query.models = VectorModel()
        entry = next(e for e in query.query_entries('alpha') if e['id'] == 'standalone')
        summary = ''
    snapshot = query.original_snapshot(scope, entry, SourceEgressService(env.records))
    chunks = split_evidence_chunks(text)
    assert chunk_vector_scores(query, 'alpha', entry, scope, snapshot, chunks,
        layer='L0', span_start=0, summary=summary, question='原件证据是什么？')
    material_types = {ref['type'] for request in requests(env.records)
                      for ref in request['privacy']['material_refs']}
    assert material_types == ({'document', 'original_item', 'original_source'} if kind == 'document_original'
                              else {'original_source'})
    assert len(vectors) == 1


def test_network_failure_degrades_without_retry_or_cached_vectors(env, monkeypatch):
    doc, _ = add_document(env, body='合成正文。' * 100)
    data = prepared(env, doc)
    calls = []

    def wire(transport, **request):
        calls.append(request)
        raise OSError('synthetic failure')

    monkeypatch.setattr(ConfiguredTransport, 'post_json', wire)
    assert score(data) == {}
    assert len(calls) == 1
    assert all(r['status'] != 'succeeded' for r in wire_receipts(env.records))


def test_actual_candidate_collection_uses_governed_vectors(env, vectors):
    doc, _ = add_document(env, body='合成正文。' * 250)
    data = prepared(env, doc)
    result = data[0].collect_candidates('alpha', 'unseen-vector-query')
    assert vectors
    assert any(c['entry']['id'] == doc and c['layer'] == 'L1' for c in result['candidates'])
    assert wire_receipts(env.records)
    assert all(r['privacy']['material_refs'] for r in requests(env.records))


def test_actual_collection_disabled_embedding_keeps_lexical(env, vectors):
    doc, _ = add_document(env, body='合成正文alpha。' * 100)
    data = prepared(env, doc)
    class Disabled(VectorModel):
        def public(self):
            result = super().public()
            result['embedding']['allow_remote'] = False
            return result
    data[0].models = Disabled()
    result = data[0].collect_candidates('alpha', 'alpha')
    assert vectors == []
    assert any(c['entry']['id'] == doc for c in result['candidates'])


def test_actual_collection_revoked_during_wire_excludes_parent(env, monkeypatch):
    doc, _ = add_document(env, body='合成正文alpha。' * 100)
    data = prepared(env, doc)
    def wire(transport, *, endpoint, payload):
        transport._check_current()
        set_private_project(env.records, 'alpha', True, expected_revision=0)
        return {'data': [{'index': i, 'embedding': [1.0, 0.0]}
                         for i, _ in enumerate(payload['input'])]}
    monkeypatch.setattr(ConfiguredTransport, 'post_json', wire)
    result = data[0].collect_candidates('alpha', 'alpha')
    assert not any(c['entry']['id'] == doc for c in result['candidates'])


def test_full_parent_inventory_reuses_all_spans_and_purges_old_revision(env, vectors):
    import json
    import sqlite3
    doc, _ = add_document(env, summary='初始摘要', body='合成正文。' * 250)
    query = prepared(env, doc)[0]
    query.collect_candidates('alpha', 'alpha')
    count = len(vectors)
    query.collect_candidates('alpha', 'alpha')
    assert all(len(batch) == 1 for batch in vectors[count:])
    env.documents.save_user_edit(doc, expected_revision=2,
        markdown='# Changed\n\n## 摘要\n更长的新摘要内容\n\n## 正文\n' + '新事实。' * 230)
    query.collect_candidates('alpha', 'alpha')
    with sqlite3.connect(env.root / 'recognition-vectors.sqlite3') as connection:
        rows = connection.execute('SELECT project_id, revision FROM recognition_embedding_cache').fetchall()
    owned = [revision for namespace, revision in rows if json.loads(namespace)[-1] == doc]
    assert owned and set(owned) == {3}


def test_twenty_thousand_character_index_reports_time(env, vectors):
    import json
    from time import perf_counter
    body = ('合成资料用于测量分块索引，保留真实坐标。' * 1200)[:20000]
    assert len(body) == 20000
    doc, _ = add_document(env, body=body)
    started = perf_counter()
    data = prepared(env, doc)
    assert score(data)
    elapsed = perf_counter() - started
    assert sum(len(chunk.text) for chunk in data[5]) >= 20000
    assert all(len(chunk.text) <= 800 for chunk in data[5])
    print('T12.1_BENCHMARK=' + json.dumps({'characters': len(body), 'chunks': len(data[5]),
        'seconds': round(elapsed, 6), 'wire_calls': len(vectors),
        'provider': 'synthetic transport; actual governed Turn and SQLite cache'}))
