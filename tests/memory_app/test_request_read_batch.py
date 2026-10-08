from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore
from core.storage_provider.connection_scope import connection_scope


def test_batch_reads_selected_records_once_without_caching_live_reads(tmp_path, monkeypatch):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with records.begin() as tx:
        tx.put('documents', 'one', {'project_id': 'alpha'}, expected_revision=0)
        tx.put('documents', 'foreign', {'project_id': 'beta'}, expected_revision=0)
        tx.put('v2_threads', 'thread', {'project_id': 'alpha'}, expected_revision=0)
        tx.commit()
    statements = []
    # The actual SQLite query is captured through the public trace seam.
    import core.storage_provider.sqlite_uow as module
    monkeypatch.setattr(module, 'observe_connection', lambda connection: connection.set_trace_callback(statements.append))
    with connection_scope():
        records.read('documents', 'one')
        statements.clear()
        batch = records.read_batch({'documents': ('one',), 'v2_threads': ('thread',)})
        selected = [sql for sql in statements if 'SELECT collection, object_id, payload_json, revision' in sql]
        assert len(selected) == 1
        assert set(batch) == {'documents', 'v2_threads'}
        assert [row.object_id for row in batch['documents']] == ['one']
        with records.begin() as tx:
            tx.put('documents', 'one', {'project_id': 'alpha', 'changed': True}, expected_revision=1)
            tx.commit()
        assert records.read('documents', 'one').revision == 2
        assert batch['documents'][0].revision == 1


def test_empty_batch_does_not_open_database(tmp_path):
    path = tmp_path / 'unused.sqlite3'
    records = SQLiteStructuredRecordStore(path)
    assert records.read_batch({'documents': ()}) == {'documents': ()}
    assert not path.exists()
