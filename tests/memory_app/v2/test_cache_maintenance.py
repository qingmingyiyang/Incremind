from types import SimpleNamespace

from backend.recognition_retrieval import SQLiteEmbeddingCache
from tests.memory_app.v2.test_workbench_ask import env as env, add_document, publish
from tests.memory_app.v2.test_contextual_chunk_vectors import vectors as vectors, prepared, score


def test_daily_composition_registers_cache_maintenance_in_existing_scheduler(env):
    from backend.memory_app.v2.cache_maintenance import CacheMaintenance
    jobs = env.http.app.state.memory_daily_jobs
    assert jobs.initial_delay == 60
    assert jobs.interval == 86400
    callback = jobs.jobs['embedding_cache']
    assert isinstance(callback.__self__, CacheMaintenance)
    assert set(jobs.jobs) == {'auto_forget', 'candidate_fade', 'insight_links', 'consolidation', 'embedding_cache',
                             'local_embedding_index', 'signals_rollup', 'gaps', 'backup'}
    assert jobs.jobs['local_embedding_index'].__self__ is env.http.app.state.memory_embedding_index


def test_daily_sweep_removes_stale_chunk_revision_without_touching_current_parent(env, vectors):
    from backend.memory_app.v2.cache_maintenance import CacheMaintenance
    from backend.memory_app.v2.contextual_chunk_vectors import chunk_cache_namespace
    doc, _ = add_document(env, body='尾部事实。' * 100)
    other, _ = add_document(env, body='另一资料。' * 100)
    query = prepared(env, doc)[0]
    assert query.collect_candidates('alpha', '尾部')['candidates']
    old_namespace = chunk_cache_namespace('alpha', {'kind':'document', 'id':doc})
    other_namespace = chunk_cache_namespace('alpha', {'kind':'document', 'id':other})
    cache = SQLiteEmbeddingCache(str(env.root / 'recognition-vectors.sqlite3'))
    stale_rows = cache._connection.execute('SELECT * FROM recognition_embedding_cache WHERE project_id=?', (old_namespace,)).fetchall()
    before = cache._connection.execute('SELECT * FROM recognition_embedding_cache WHERE project_id=?', (other_namespace,)).fetchall()
    cache.close()
    env.documents.save_user_edit(doc, expected_revision=2, markdown='# Changed\n\n## 正文\n新的事实')
    cache = SQLiteEmbeddingCache(str(env.root / 'recognition-vectors.sqlite3'))
    try:
        assert cache._connection.execute('SELECT count(*) FROM recognition_embedding_cache WHERE project_id=?', (old_namespace,)).fetchone()[0] == 0
        # Recreate real pre-upgrade/interrupted rows for the daily fallback.
        for namespace, identity, revision, model, vector_json in stale_rows:
            import json
            cache.write(model_id=model, entry=SimpleNamespace(project_id=namespace, id=identity, revision=revision), vector=json.loads(vector_json))
    finally:
        cache.close()
    calls = len(vectors)
    assert CacheMaintenance(env.domains.query).run() > 0
    assert len(vectors) == calls
    cache = SQLiteEmbeddingCache(str(env.root / 'recognition-vectors.sqlite3'))
    try:
        assert cache._connection.execute('SELECT count(*) FROM recognition_embedding_cache WHERE project_id=?', (old_namespace,)).fetchone()[0] == 0
        assert cache._connection.execute('SELECT * FROM recognition_embedding_cache WHERE project_id=?', (other_namespace,)).fetchall() == before
    finally:
        cache.close()


def test_explicit_daily_sweep_cleans_removed_recognition_and_preserves_other_project(env):
    from backend.memory_app.v2.cache_maintenance import CacheMaintenance
    recognition, _ = publish(env)
    other, _ = publish(env, project='beta')
    env.service.revoke(scope=recognition.scope, recognition_id=recognition.id, expected_revision=recognition.revision, reason='test removal')
    cache = SQLiteEmbeddingCache(str(env.root / 'recognition-vectors.sqlite3'))
    try:
        cache.write(model_id='model-a', entry=SimpleNamespace(project_id='alpha', id=recognition.id, revision=recognition.revision), vector=(1., 0.))
        cache.write(model_id='model-b', entry=SimpleNamespace(project_id='beta', id=other.id, revision=other.revision), vector=(0., 1.))
    finally:
        cache.close()
    assert CacheMaintenance(env.domains.query).run() == 1
    cache = SQLiteEmbeddingCache(str(env.root / 'recognition-vectors.sqlite3'))
    try:
        assert cache.read(model_id='model-a', entry=SimpleNamespace(project_id='alpha', id=recognition.id, revision=recognition.revision)) is None
        assert cache.read(model_id='model-b', entry=SimpleNamespace(project_id='beta', id=other.id, revision=other.revision)) == (0., 1.)
    finally:
        cache.close()


def test_no_cache_daily_job_does_not_create_database_or_call_model(env):
    from backend.memory_app.v2.cache_maintenance import CacheMaintenance
    before = env.model.calls
    assert CacheMaintenance(env.domains.query).run() == 0
    assert env.model.calls == before
    assert not (env.root / 'recognition-vectors.sqlite3').exists()
