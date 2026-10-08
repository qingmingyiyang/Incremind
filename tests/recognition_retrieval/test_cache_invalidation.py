from types import SimpleNamespace
import sqlite3
import pytest

from backend.recognition_retrieval import SQLiteEmbeddingCache


def entry(project, identity='same', revision=1):
    return SimpleNamespace(project_id=project, id=identity, revision=revision)


def test_cache_read_requires_exact_project_id_revision_and_model_after_restart(tmp_path):
    path = tmp_path / 'vectors.sqlite3'
    cache = SQLiteEmbeddingCache(str(path))
    cache.write(model_id='model-a', entry=entry('alpha'), vector=(1., 0.))
    cache.close()
    cache = SQLiteEmbeddingCache(str(path))
    try:
        assert cache.read(model_id='model-a', entry=entry('alpha')) == (1., 0.)
        assert cache.read(model_id='model-a', entry=entry('beta')) is None
        assert cache.read(model_id='model-a', entry=entry('alpha', 'other')) is None
        assert cache.read(model_id='model-a', entry=entry('alpha', revision=2)) is None
        assert cache.read(model_id='model-b', entry=entry('alpha')) is None
    finally:
        cache.close()


def test_namespace_deletion_is_exact_and_preserves_other_models_and_parents(tmp_path):
    cache = SQLiteEmbeddingCache(str(tmp_path / 'vectors.sqlite3'))
    target = '["contextual-chunks-v1","alpha","document","d1"]'
    other = '["contextual-chunks-v1","alpha","document","d10"]'
    try:
        for model in ('model-a', 'model-b'):
            for project in (target, other, 'alpha', 'beta'):
                cache.write(model_id=model, entry=entry(project), vector=(1., 0.))
        assert cache.delete_namespace(project_id=target) == 2
        for model in ('model-a', 'model-b'):
            assert cache.read(model_id=model, entry=entry(target)) is None
            for project in (other, 'alpha', 'beta'):
                assert cache.read(model_id=model, entry=entry(project)) == (1., 0.)
        assert cache.delete_recognition(project_id='alpha', recognition_id='same') == 2
        assert cache.read(model_id='model-a', entry=entry('beta')) == (1., 0.)
    finally:
        cache.close()


def test_write_invalidator_targets_only_explicit_material_and_recognition(tmp_path):
    from backend.recognition_retrieval.cache_invalidation import VectorCacheInvalidator, chunk_cache_namespace
    path = tmp_path / 'vectors.sqlite3'
    target = chunk_cache_namespace('alpha', {'kind':'document', 'id':'d1'})
    other = chunk_cache_namespace('alpha', {'kind':'document', 'id':'d2'})
    cache = SQLiteEmbeddingCache(str(path))
    for project in (target, other, 'alpha', 'beta'):
        cache.write(model_id='model-a', entry=entry(project), vector=(1., 0.))
    cache.close()
    invalidator = VectorCacheInvalidator(path)
    assert invalidator.material('alpha', 'document', 'd1') == 1
    assert invalidator.recognitions('alpha', ['same']) == 1
    cache = SQLiteEmbeddingCache(str(path))
    try:
        assert cache.read(model_id='model-a', entry=entry(other)) == (1., 0.)
        assert cache.read(model_id='model-a', entry=entry('beta')) == (1., 0.)
    finally:
        cache.close()


def test_write_invalidator_without_cache_creates_no_file(tmp_path):
    from backend.recognition_retrieval.cache_invalidation import VectorCacheInvalidator
    path = tmp_path / 'vectors.sqlite3'
    invalidator = VectorCacheInvalidator(path)
    assert invalidator.material('alpha', 'source', 's1') == 0
    assert invalidator.recognitions('alpha', ['r1']) == 0
    assert invalidator.project('alpha') == 0
    assert not path.exists()


def test_project_invalidation_keeps_other_project_and_identical_parent_ids(tmp_path):
    from backend.recognition_retrieval.cache_invalidation import VectorCacheInvalidator, chunk_cache_namespace
    path = tmp_path / 'vectors.sqlite3'
    cache = SQLiteEmbeddingCache(str(path))
    scopes = ['alpha', 'beta'] + [chunk_cache_namespace(project, {'kind':kind, 'id':'same'})
                                  for project in ('alpha', 'beta') for kind in ('document', 'source')]
    for scope in scopes:
        for model in ('model-a', 'model-b'):
            cache.write(model_id=model, entry=entry(scope), vector=(1., 0.))
    cache.close()
    assert VectorCacheInvalidator(path).project('alpha') == 6
    cache = SQLiteEmbeddingCache(str(path))
    try:
        for scope in scopes:
            for model in ('model-a', 'model-b'):
                expected = None if scope == 'alpha' or '"alpha"' in scope else (1., 0.)
                assert cache.read(model_id=model, entry=entry(scope)) == expected
    finally:
        cache.close()


def test_in_memory_owner_has_no_disk_cache_path_for_raw_or_enlisted_transaction():
    import sqlite3
    from backend.memory_app.transaction_records import TransactionRecords
    from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork
    from core.search_and_recall.vector_cache_invalidation import vector_cache_path

    connection = sqlite3.connect(':memory:')
    try:
        transaction = SQLiteStructuredRecordUnitOfWork(connection)
        assert vector_cache_path(transaction) is None
        assert vector_cache_path(TransactionRecords(transaction)) is None
    finally:
        connection.close()


def test_cached_parent_enumeration_reads_only_distinct_identity_metadata(tmp_path):
    from backend.recognition_retrieval.cache_invalidation import chunk_cache_namespace
    path = tmp_path / 'vectors.sqlite3'
    namespace = chunk_cache_namespace('beta', {'kind': 'source', 'id': 'original'})
    cache = SQLiteEmbeddingCache(str(path))
    try:
        for model in ('model-a', 'model-b'):
            for revision in (1, 2):
                for project, identity in (('alpha', 'same'), ('beta', 'same'), (namespace, 'chunk')):
                    cache.write(model_id=model, entry=entry(project, identity, revision), vector=(1., 0.))
        def metadata_only(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and table == 'recognition_embedding_cache' and column == 'vector_json':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        cache._connection.set_authorizer(metadata_only)
        assert cache.parents() == tuple(sorted((('alpha', 'same'), ('beta', 'same'), (namespace, 'chunk'))))
    finally:
        cache.close()


def test_invalidator_parent_enumeration_does_not_create_missing_cache_or_table(tmp_path):
    from backend.recognition_retrieval.cache_invalidation import VectorCacheInvalidator
    path = tmp_path / 'vectors.sqlite3'
    assert VectorCacheInvalidator(path).parents() == ()
    assert not path.exists()
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE unrelated (identity TEXT)')
        connection.commit()
    assert VectorCacheInvalidator(path).parents() == ()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('unrelated',)]


def test_qualified_multi_parent_delete_rolls_back_all_cache_rows_on_late_sql_failure(tmp_path):
    from backend.recognition_retrieval.cache_invalidation import VectorCacheInvalidator
    path = tmp_path / 'vectors.sqlite3'
    cache = SQLiteEmbeddingCache(str(path))
    try:
        for project in ('alpha', 'beta', 'independent'):
            cache.write(model_id='model-a', entry=entry(project), vector=(1., 0.))
        cache._connection.execute("CREATE TRIGGER fail_late_delete BEFORE DELETE ON recognition_embedding_cache "
            "WHEN OLD.project_id='beta' BEGIN SELECT RAISE(ABORT, 'synthetic late delete failure'); END")
        cache._connection.commit()
        before = cache._connection.execute('SELECT * FROM recognition_embedding_cache ORDER BY project_id').fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            VectorCacheInvalidator(path).targets(recognitions=[('alpha', 'same'), ('beta', 'same')])
        assert cache._connection.execute('SELECT * FROM recognition_embedding_cache ORDER BY project_id').fetchall() == before
    finally:
        cache.close()
