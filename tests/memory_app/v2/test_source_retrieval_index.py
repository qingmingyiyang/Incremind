"""Real JSON/SQLite crash boundaries; only filesystem faults are injected."""
from pathlib import Path
import pytest

from core.storage_provider import JsonObjectStore
from core.storage_provider import runtime
from core.storage_provider.source_retrieval_index import COLLECTION, index_store, namespace_projection
from types import SimpleNamespace
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from backend.recognition import RecognitionService
from backend.memory_app.workspace_query import WorkspaceQuery


def source(store, text, revision=0):
    return store.write('sources', 'sample', {'id':'sample', 'project_id':'alpha',
        'title':'Synthetic', 'metadata':{'content':text}}, expected_revision=revision)


def test_payload_changed_before_meta_cannot_reuse_old_index_after_reopen(tmp_path, monkeypatch):
    store = JsonObjectStore(tmp_path / 'objects')
    source(store, 'oldneedle')
    previous_revision = store.revision('sources', 'sample')
    original = runtime._write_json_atomic
    def fail_meta(path, payload):
        if path.name.endswith('.meta.json'):
            raise OSError('synthetic metadata interruption')
        return original(path, payload)
    monkeypatch.setattr(runtime, '_write_json_atomic', fail_meta)
    with pytest.raises(OSError, match='synthetic metadata interruption'):
        source(store, 'newneedle', 1)
    reopened = JsonObjectStore(store.root)
    assert reopened.read('sources', 'sample')['metadata']['content'] == 'newneedle'
    assert reopened.revision('sources', 'sample') == previous_revision
    assert namespace_projection(index_store(reopened).read(COLLECTION, 'sample'), reopened.namespace_id)['state'] == 'invalid'


def test_delete_interruption_cannot_reuse_old_index_after_reopen(tmp_path, monkeypatch):
    store = JsonObjectStore(tmp_path / 'objects')
    source(store, 'oldneedle')
    original = Path.unlink
    def fail_meta(path, *args, **kwargs):
        if path.name.endswith('.meta.json'):
            raise OSError('synthetic delete interruption')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail_meta)
    with pytest.raises(OSError, match='synthetic delete interruption'):
        store.delete('sources', 'sample')
    reopened = JsonObjectStore(store.root)
    assert reopened.read('sources', 'sample') is None
    assert reopened.revision('sources', 'sample') == 1
    assert namespace_projection(index_store(reopened).read(COLLECTION, 'sample'), reopened.namespace_id)['state'] == 'invalid'


def test_source_index_keeps_same_identity_in_two_namespaces_separate(tmp_path):
    first = JsonObjectStore(tmp_path / 'objects')
    second = JsonObjectStore(first.root, namespace_id='secondary')
    source(first, 'firstnamespace')
    source(second, 'secondnamespace')
    row = index_store(first).read(COLLECTION, 'sample')
    assert namespace_projection(row, first.namespace_id)['chunks'][0]['text'] == 'firstnamespace'
    assert namespace_projection(row, second.namespace_id)['chunks'][0]['text'] == 'secondnamespace'


def query_for(root, store):
    records = SQLiteStructuredRecordStore(root / 'records.sqlite3')
    return WorkspaceQuery(records, SQLiteDocumentRepository(records), store,
                          SimpleNamespace(), RecognitionService(records))


def test_source_only_question_reads_index_then_only_hit_body_and_first_edit(tmp_path, monkeypatch):
    store = JsonObjectStore(tmp_path / '.rebuild-data')
    source(store, 'zygomorphic')
    store.write('sources', 'miss', {'id':'miss', 'project_id':'alpha', 'title':'Synthetic',
        'metadata':{'content':'orchard'}}, expected_revision=0)
    query = query_for(tmp_path, store)
    reads, original = [], store.read
    def observed(collection, identity):
        reads.append((collection, identity))
        return original(collection, identity)
    monkeypatch.setattr(store.__class__, 'read', lambda own, c, i: observed(c, i)
        if own is store else original.__func__(own, c, i))
    rows = query.collect_candidates('alpha', 'zygomorphic')['candidates']
    assert {row['entry']['id'] for row in rows} == {'sample'}
    assert reads == [('sources', 'sample')]
    source(store, 'newneedle', 1)
    assert query.collect_candidates('alpha', 'zygomorphic')['candidates'] == []
    assert {row['entry']['id'] for row in query.collect_candidates('alpha', 'newneedle')['candidates']} == {'sample'}


def test_interrupted_source_write_stays_excluded_in_real_reopened_query(tmp_path, monkeypatch):
    store = JsonObjectStore(tmp_path / '.rebuild-data')
    source(store, 'oldneedle')
    query = query_for(tmp_path, store)
    assert query.collect_candidates('alpha', 'oldneedle')['candidates']
    original = runtime._write_json_atomic
    def fail_meta(path, payload):
        if path.name.endswith('.meta.json'):
            raise OSError('synthetic metadata interruption')
        return original(path, payload)
    monkeypatch.setattr(runtime, '_write_json_atomic', fail_meta)
    with pytest.raises(OSError):
        source(store, 'newneedle', 1)
    reopened = query_for(tmp_path, JsonObjectStore(store.root))
    assert reopened.collect_candidates('alpha', 'oldneedle')['candidates'] == []
    assert reopened.collect_candidates('alpha', 'newneedle')['candidates'] == []
    reopened.retrieval_index.wait_for_repairs()
    assert reopened.collect_candidates('alpha', 'newneedle')['candidates'] == []
    monkeypatch.setattr(runtime, '_write_json_atomic', original)
    source(store, 'newneedle', 1)
    assert reopened.collect_candidates('alpha', 'newneedle')['candidates']


@pytest.mark.parametrize('routed', [False, True])
def test_source_writer_does_not_hold_file_lock_while_waiting_for_authority_transaction(tmp_path, monkeypatch, routed):
    from threading import Event, Thread, current_thread
    from core.storage_provider.source_asset_runtime import SourceAssetRuntimeStore

    json_store = JsonObjectStore(tmp_path / '.rebuild-data')
    source(json_store, 'oldneedle')
    store = (SourceAssetRuntimeStore(json_store=json_store, sqlite_records=None,
             library_root=tmp_path / 'library', authority_identity='json')
             if routed else json_store)
    records = index_store(json_store)
    waiting, errors = Event(), []
    original = SQLiteStructuredRecordStore.begin

    def observed_begin(own):
        if current_thread().name == 'synthetic-source-writer' and own.database_path == records.database_path:
            waiting.set()
        return original(own)

    monkeypatch.setattr(SQLiteStructuredRecordStore, 'begin', observed_begin)

    def write():
        try:
            source(store, 'newneedle', 1)
        except BaseException as error:
            errors.append(error)

    writer = Thread(target=write, name='synthetic-source-writer')
    with records.begin() as tx:
        writer.start()
        assert waiting.wait(10), 'writer did not enter the real SQLite transaction'
        # Source authority already takes SQLite -> file. The writer must not
        # hold the opposite lock while waiting for this transaction to finish.
        with json_store.locked('sources', 'sample'):
            assert json_store.read('sources', 'sample')['metadata']['content'] == 'oldneedle'
        tx.commit()
    writer.join(10)
    assert not writer.is_alive()
    assert errors == []
    assert json_store.revision('sources', 'sample') == 2
    assert namespace_projection(records.read(COLLECTION, 'sample'), json_store.namespace_id)['chunks'][0]['text'] == 'newneedle'


def test_rejected_source_cas_keeps_current_projection_ready(tmp_path):
    store = JsonObjectStore(tmp_path / '.rebuild-data')
    source(store, 'oldneedle')
    before = index_store(store).read(COLLECTION, 'sample')
    with pytest.raises(runtime.ObjectStoreRevisionError, match='expected revision 0, found 1'):
        source(store, 'newneedle', 0)
    assert index_store(store).read(COLLECTION, 'sample') == before
    assert store.read('sources', 'sample')['metadata']['content'] == 'oldneedle'


def test_source_index_contains_entry_and_chunks_without_whole_body_copy(tmp_path):
    store = JsonObjectStore(tmp_path / '.rebuild-data')
    source(store, 'oldneedle')
    value = namespace_projection(index_store(store).read(COLLECTION, 'sample'), store.namespace_id)
    assert not {'content', 'search_text', 'markdown'} & value.keys()
    assert value['entry']['id'] == 'sample'
    assert value['chunks'][0]['text'] == 'oldneedle'


def test_failed_invalidation_does_not_mutate_source_body_or_metadata(tmp_path, monkeypatch):
    store = JsonObjectStore(tmp_path / '.rebuild-data')
    source(store, 'oldneedle')
    before = index_store(store).read(COLLECTION, 'sample')
    def unavailable(own):
        raise OSError('synthetic index interruption')
    monkeypatch.setattr(SQLiteStructuredRecordStore, 'begin', unavailable)
    with pytest.raises(OSError, match='synthetic index interruption'):
        source(store, 'newneedle', 1)
    assert store.read('sources', 'sample')['metadata']['content'] == 'oldneedle'
    assert store.revision('sources', 'sample') == 1
    assert index_store(store).read(COLLECTION, 'sample') == before


def test_late_refresh_cannot_bless_a_newer_interrupted_body(tmp_path, monkeypatch):
    from core.storage_provider import source_retrieval_index as owner
    store = JsonObjectStore(tmp_path / '.rebuild-data')
    source(store, 'before')
    original_refresh, original_write = owner.refresh_source, runtime._write_json_atomic
    def interrupted_meta(path, value):
        if path.name.endswith('.meta.json'):
            raise OSError('synthetic metadata interruption')
        return original_write(path, value)
    def delayed_refresh(own, identity, revision, incarnation, token):
        with monkeypatch.context() as patch:
            patch.setattr(runtime, '_write_json_atomic', interrupted_meta)
            with pytest.raises(OSError, match='synthetic metadata interruption'):
                source(store, 'ambiguous', revision)
        return original_refresh(own, identity, revision, incarnation, token)
    monkeypatch.setattr(owner, 'refresh_source', delayed_refresh)
    source(store, 'committed', 1)
    assert store.revision('sources', 'sample') == 2
    assert store.read('sources', 'sample')['metadata']['content'] == 'ambiguous'
    assert namespace_projection(index_store(store).read(COLLECTION, 'sample'), store.namespace_id)['state'] == 'invalid'
    query = query_for(tmp_path, JsonObjectStore(store.root))
    assert query.collect_candidates('alpha', 'committed')['candidates'] == []
    assert query.collect_candidates('alpha', 'ambiguous')['candidates'] == []
