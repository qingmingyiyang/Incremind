from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore
from core.ai_kernel.sqlite_store import SQLiteAITurnStore


def test_request_reuses_connections_but_observes_new_commits(tmp_path):
    from core.storage_provider.connection_scope import connection_scope
    store = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    kernel = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    with connection_scope():
        first = store._connect()
        raw = first.connection
        first.execute("PRAGMA query_only=ON")
        first.close()
        second = store._connect()
        assert second.connection is raw
        assert second.execute("PRAGMA synchronous").fetchone()[0] == 2
        second.close()
        with SQLiteStructuredRecordStore(tmp_path / "records.sqlite3").begin() as tx:
            tx.put("checks", "fresh", {"value": 1}, expected_revision=0)
            tx.commit()
        assert store.read("checks", "fresh").payload["value"] == 1
        one = kernel._connect()
        underlying = one.connection
        one.close()
        two = kernel._connect()
        assert two.connection is underlying
        two.close()
    import sqlite3
    import pytest
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")


def test_active_transaction_is_not_reused_by_nested_reads(tmp_path):
    from core.storage_provider.connection_scope import connection_scope
    store = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    with connection_scope():
        with store.begin() as tx:
            tx.put("checks", "pending", {}, expected_revision=0)
            assert store.read("checks", "pending") is None
        assert store.read("checks", "pending") is None
