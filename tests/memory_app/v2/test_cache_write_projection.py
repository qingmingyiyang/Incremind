import sqlite3
from types import SimpleNamespace

import pytest

from backend.recognition_retrieval import SQLiteEmbeddingCache
from backend.recognition_retrieval.cache_invalidation import chunk_cache_namespace
from core.document_engine.retrieval_index import COLLECTION as DOCUMENT_INDEX, project_document
from core.storage_provider import JsonObjectStore
from core.storage_provider.source_retrieval_index import COLLECTION as SOURCE_INDEX, index_store, namespace_projection
from tests.memory_app.v2.test_workbench_ask import env as env, add_document


def seed(path, project, kind, identity, revision=1):
    namespace = chunk_cache_namespace(project, {'kind':kind, 'id':identity})
    cache = SQLiteEmbeddingCache(str(path))
    try:
        cache.write(model_id='model-a', entry=SimpleNamespace(project_id=namespace, id='chunk', revision=revision), vector=(1., 0.))
    finally:
        cache.close()
    return namespace


def namespaces(path):
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute('SELECT DISTINCT project_id FROM recognition_embedding_cache')}


def source(store, project='alpha', text='oldneedle', revision=0):
    return store.write('sources', 'sample', {'id':'sample', 'project_id':project, 'title':'Synthetic',
                       'metadata':{'content':text}}, expected_revision=revision)


def test_bare_document_edit_invalidates_before_first_query(env):
    doc, _ = add_document(env, body='oldneedle')
    path = env.root / 'recognition-vectors.sqlite3'
    stale = seed(path, 'alpha', 'document', doc, 2)
    other = seed(path, 'alpha', 'document', 'unrelated')
    env.documents.save_user_edit(doc, expected_revision=2, markdown='# Changed\n\n## 正文\nnewneedle')
    assert namespaces(path) == {other}
    assert stale not in namespaces(path)
    rows = env.domains.query.collect_candidates('alpha', 'newneedle')['candidates']
    assert any(row['entry']['id'] == doc for row in rows)
    assert not any(row['entry']['id'] == doc for row in env.domains.query.collect_candidates('alpha', 'oldneedle')['candidates'])


def test_document_projection_clears_old_and_new_project_namespace(env):
    doc, _ = add_document(env)
    path = env.root / 'recognition-vectors.sqlite3'
    seed(path, 'alpha', 'document', doc, 2)
    seed(path, 'beta', 'document', doc, 2)
    other = seed(path, 'alpha', 'document', 'unrelated')
    current = env.documents.read(doc)
    with env.records.begin() as tx:
        project_document(tx, {**current, 'project_id':'beta'}, env.documents.markdown(doc))
        tx.commit()
    assert namespaces(path) == {other}


def test_bare_source_edit_and_delete_clear_only_its_old_and_new_namespaces(tmp_path):
    store = JsonObjectStore(tmp_path / 'objects')
    source(store)
    path = store.root / 'recognition-vectors.sqlite3'
    seed(path, 'alpha', 'source', 'sample')
    seed(path, 'beta', 'source', 'sample')
    other = seed(path, 'alpha', 'source', 'unrelated')
    source(store, 'beta', 'newneedle', 1)
    assert namespaces(path) == {other}
    seed(path, 'beta', 'source', 'sample', 2)
    assert store.delete('sources', 'sample')
    assert namespaces(path) == {other}


@pytest.mark.parametrize('owner', ['document', 'source'])
def test_cache_delete_failure_leaves_owner_and_index_unchanged(env, tmp_path, owner):
    if owner == 'document':
        identity, _ = add_document(env)
        records = env.records
        collection = DOCUMENT_INDEX
        before_owner = env.documents.read(identity)
        before_body = env.documents.markdown(identity)
        path = env.root / 'recognition-vectors.sqlite3'
        seed(path, 'alpha', 'document', identity, 2)
        mutate = lambda: env.documents.save_user_edit(identity, expected_revision=2, markdown='replacement')
    else:
        store = JsonObjectStore(tmp_path / 'objects')
        source(store)
        identity = 'sample'
        records, collection = index_store(store), SOURCE_INDEX
        before_owner = store.read('sources', identity)
        path = store.root / 'recognition-vectors.sqlite3'
        seed(path, 'alpha', 'source', identity)
        mutate = lambda: source(store, text='replacement', revision=1)
    before_index = records.read(collection, identity)
    lock = sqlite3.connect(path, timeout=0)
    try:
        lock.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError):
            mutate()
    finally:
        lock.rollback()
        lock.close()
    assert records.read(collection, identity) == before_index
    if owner == 'document':
        assert env.documents.read(identity) == before_owner
        assert env.documents.markdown(identity) == before_body
    else:
        assert store.read('sources', identity) == before_owner
        assert store.revision('sources', identity) == 1


def test_document_and_source_writes_without_vectors_create_no_cache(env, tmp_path):
    doc, _ = add_document(env)
    env.documents.save_user_edit(doc, expected_revision=2, markdown='new')
    store = JsonObjectStore(tmp_path / 'objects')
    source(store)
    source(store, text='new', revision=1)
    store.delete('sources', 'sample')
    assert not (env.root / 'recognition-vectors.sqlite3').exists()
    assert not (store.root / 'recognition-vectors.sqlite3').exists()


@pytest.mark.parametrize('legacy', [False, True])
def test_source_authority_binds_actual_record_cache_for_edit_delete_and_refresh(tmp_path, legacy):
    from backend.memory_app.original_sources import source_store
    from core.storage_provider import SQLiteStructuredRecordStore
    database = tmp_path / 'recognition.sqlite3' if legacy else tmp_path / '.rebuild-data' / 'structured-records.sqlite3'
    records = SQLiteStructuredRecordStore(database)
    store = source_store(records)
    source(store)
    path = records.database_path.parent / 'recognition-vectors.sqlite3'
    seed(path, 'alpha', 'source', 'sample')
    seed(path, 'beta', 'source', 'sample')
    other = seed(path, 'alpha', 'source', 'unrelated')
    if legacy:
        default_path = store.root / 'recognition-vectors.sqlite3'
        default = seed(default_path, 'alpha', 'source', 'sample')
    source(store, 'beta', 'replacement', 1)
    assert namespaces(path) == {other}
    projected = namespace_projection(index_store(store).read(SOURCE_INDEX, 'sample'), store.namespace_id)
    assert projected['state'] == 'ready'
    assert projected['source_revision'] == 2
    assert projected['chunks'][0]['text'] == 'replacement'
    if legacy:
        assert namespaces(default_path) == {default}
    seed(path, 'beta', 'source', 'sample', 2)
    assert store.delete('sources', 'sample')
    assert namespaces(path) == {other}


def test_explicit_source_cache_path_is_normalized_and_not_saved_in_payload(tmp_path):
    store = JsonObjectStore(tmp_path / 'objects', vector_cache_path=tmp_path / 'cache' / '..' / 'recognition-vectors.sqlite3')
    assert store.vector_cache_path == (tmp_path / 'recognition-vectors.sqlite3').resolve()
    source(store)
    assert 'vector_cache_path' not in store.read('sources', 'sample')
