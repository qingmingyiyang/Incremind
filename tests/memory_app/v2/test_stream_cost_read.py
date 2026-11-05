import pytest

from backend.memory_app.model_costs import attempt_cost
from backend.memory_app.v2.turn_frames import read_stream_records
from core.storage_provider import SQLiteStructuredRecordStore


def test_stream_reader_keeps_unreported_attempt_cost_unknown_without_writing(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'read.sqlite3')
    with records.begin() as tx:
        tx.commit()
    attempt = {'attempt_id': 'unreported-attempt'}
    assert attempt_cost(records, attempt) is None
    with read_stream_records(records) as reader:
        assert reader.connection.execute('PRAGMA query_only').fetchone()[0] == 1
        assert attempt_cost(reader, attempt) is None
        assert reader.connection.total_changes == 0
        with pytest.raises(ValueError, match='unsupported_stream_record'):
            reader.read('unrelated-secret-records', 'unreported-attempt')
