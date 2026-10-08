from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from core.storage_provider import (
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
    SQLiteUnitOfWorkError,
    create_vault_backup,
    restore_vault_backup,
)


def _store(tmp_path):
    return SQLiteStructuredRecordStore(tmp_path / "sqlite-uow" / "records.sqlite3")


def test_committed_reads_remain_available_during_another_writer(tmp_path):
    store = _store(tmp_path)
    with store.begin() as tx:
        committed = tx.put("items", "one", {"status": "ready"}, expected_revision=0)
        tx.commit()
    token = store.generation_token(("items",))
    with store.begin() as writer:
        writer.put("items", "one", {"status": "pending"}, expected_revision=1)
        # A fresh store must work too; readiness cannot rely on instance state.
        reader = _store(tmp_path)
        assert reader.read("items", "one") == committed
        assert reader.list("items") == (committed,)
        assert reader.list_matching("items", status="ready") == (committed,)
        assert reader.list_all() == (committed,)
        assert reader.generation_token(("items",)) == token
        writer.commit()
    assert reader.read("items", "one").revision == 2
    assert reader.generation_token(("items",)) != token


def test_ready_connections_execute_no_schema_writes_or_backfill(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.list_all()
    statements = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    assert store.read("items", "missing") is None
    assert not any(sql.lstrip().upper().startswith(
        ("INSERT", "CREATE", "BEGIN", "COMMIT", "UPDATE", "DELETE")
    ) for sql in statements)
    assert not any("GROUP BY" in sql.upper() for sql in statements)


def test_old_schema_backfills_once_and_keeps_generation_history(tmp_path):
    store = _store(tmp_path)
    store.database_path.parent.mkdir(parents=True)
    with sqlite3.connect(store.database_path) as connection:
        connection.executescript("""
            CREATE TABLE crp_structured_records (
                collection TEXT NOT NULL, object_id TEXT NOT NULL,
                payload_json TEXT NOT NULL, revision INTEGER NOT NULL,
                PRIMARY KEY (collection, object_id));
            INSERT INTO crp_structured_records VALUES ('items', 'one', '{}', 3);
            INSERT INTO crp_structured_records VALUES ('items', 'two', '{}', 5);
        """)
    assert len(store.list("items")) == 2
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT generation FROM crp_collection_generations").fetchone() == (10,)
    with store.begin() as tx:
        tx.put("items", "one", {}, expected_revision=3)
        tx.delete("items", "two", expected_revision=5)
        tx.commit()
    # Repair a missing trigger without resetting existing generations.
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DROP TRIGGER crp_generation_after_insert")
    assert len(store.list("items")) == 1
    with store.begin() as tx:
        tx.put("items", "three", {}, expected_revision=0)
        tx.commit()
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT generation FROM crp_collection_generations").fetchone() == (13,)


def test_simultaneous_first_openers_initialize_safely(tmp_path):
    barrier = Barrier(6)

    def open_store(index):
        store = _store(tmp_path)
        barrier.wait(timeout=10)
        with store.begin() as tx:
            tx.put("items", str(index), {}, expected_revision=0)
            tx.commit()

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(open_store, range(6)))
    assert len(_store(tmp_path).list("items")) == 6
    with sqlite3.connect(_store(tmp_path).database_path) as connection:
        assert connection.execute("SELECT generation FROM crp_collection_generations").fetchone() == (6,)


def test_same_store_rechecks_replaced_database_and_propagates_corruption(tmp_path):
    store = _store(tmp_path)
    with store.begin() as tx:
        tx.put("items", "old", {}, expected_revision=0)
        tx.commit()
    store.database_path.unlink()
    assert store.read("items", "old") is None
    store.database_path.write_bytes(b"not a SQLite database")
    with pytest.raises(sqlite3.DatabaseError, match="not a database"):
        store.list_all()


def test_malformed_generation_schema_is_not_treated_as_ready(tmp_path):
    store = _store(tmp_path)
    store.list_all()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DROP TABLE crp_collection_generations")
        connection.execute("CREATE TABLE crp_collection_generations (collection TEXT PRIMARY KEY)")
    with pytest.raises(sqlite3.OperationalError, match="generation"):
        store.list_all()


def test_failed_initialization_rolls_back_and_next_open_can_retry(tmp_path, monkeypatch):
    store = _store(tmp_path)
    connect = sqlite3.connect

    def denied_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_authorizer(lambda action, *rest:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TRIGGER
            else sqlite3.SQLITE_OK)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", denied_connect)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            store.list_all()
    with connect(store.database_path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    assert store.list_all() == ()


@pytest.mark.parametrize("error_code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_ERROR])
def test_wal_setup_retries_only_busy_errors(tmp_path, monkeypatch, error_code):
    attempts = []
    connect = sqlite3.connect

    class ContendedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA journal_mode=WAL":
                attempts.append(sql)
                if len(attempts) == 1:
                    error = sqlite3.OperationalError("synthetic WAL setup failure")
                    error.sqlite_errorcode = error_code
                    raise error
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs:
        connect(*args, **kwargs, factory=ContendedConnection))
    if error_code == sqlite3.SQLITE_BUSY:
        assert _store(tmp_path).list_all() == ()
        assert len(attempts) == 2
    else:
        with pytest.raises(sqlite3.OperationalError, match="synthetic WAL setup failure"):
            _store(tmp_path).list_all()
        assert len(attempts) == 1


def test_matching_filters_are_exact_parameterized_and_collection_scoped(tmp_path):
    store = _store(tmp_path)
    with store.begin() as tx:
        for key, payload in (("b", {"status": "processing", "project_id": "alpha"}),
                             ("a", {"status": "processing", "project_id": "beta"}),
                             ("c", {"status": "ready", "project_id": "alpha"})):
            tx.put("items", key, payload, expected_revision=0)
        tx.put("other", "b", {"status": "processing", "project_id": "alpha"}, expected_revision=0)
        tx.commit()
    assert [r.object_id for r in store.list_matching("items", status="processing")] == ["a", "b"]
    assert [r.object_id for r in store.list_matching("items", status="processing", project_id="alpha")] == ["b"]
    assert store.list_matching("items", status="processing' OR 1=1 --") == ()
    assert store.list_matching("items", missing="value") == ()
    assert store.list_matching("items") == store.list("items")
    with pytest.raises(SQLiteUnitOfWorkError):
        store.list_matching("items", status=1)


def test_uow_commits_multiple_records_atomically_with_deterministic_revisions(tmp_path) -> None:
    store = _store(tmp_path)

    with store.begin() as uow:
        source = uow.put(
            "sources",
            "source-uow-1",
            {"id": "source-uow-1", "title": "UoW source"},
            expected_revision=0,
        )
        asset = uow.put(
            "assets",
            "asset-uow-1",
            {"id": "asset-uow-1", "source_id": "source-uow-1"},
            expected_revision=0,
        )
        committed = uow.commit()

    assert [record.revision for record in committed] == [1, 1]
    assert source.revision == 1
    assert asset.revision == 1

    reloaded = _store(tmp_path)
    assert reloaded.read("sources", "source-uow-1") == source
    assert reloaded.read("assets", "asset-uow-1") == asset
    assert reloaded.list("sources") == (source,)
    assert reloaded.list_all() == (asset, source)


def test_revision_conflict_rolls_back_all_prior_mutations_in_the_same_uow(tmp_path) -> None:
    store = _store(tmp_path)
    with store.begin() as initial:
        initial.put(
            "sources",
            "source-uow-conflict",
            {"id": "source-uow-conflict", "title": "first"},
            expected_revision=0,
        )
        initial.commit()

    uow = store.begin()
    uow.put(
        "assets",
        "asset-uow-rolled-back",
        {"id": "asset-uow-rolled-back", "source_id": "source-uow-conflict"},
        expected_revision=0,
    )
    with pytest.raises(SQLiteUnitOfWorkConflict, match="expected revision 0, found 1"):
        uow.put(
            "sources",
            "source-uow-conflict",
            {"id": "source-uow-conflict", "title": "stale"},
            expected_revision=0,
        )

    assert uow.closed is True
    assert store.read("sources", "source-uow-conflict").payload["title"] == "first"
    assert store.read("assets", "asset-uow-rolled-back") is None
    with pytest.raises(SQLiteUnitOfWorkError, match="closed"):
        uow.commit()


def test_uow_rejects_blind_or_stale_writes_and_invalid_structured_records(tmp_path) -> None:
    store = _store(tmp_path)

    uow = store.begin()
    with pytest.raises(SQLiteUnitOfWorkError, match="expected_revision"):
        uow.put("sources", "source-uow-invalid", {"id": "source-uow-invalid"}, expected_revision=None)  # type: ignore[arg-type]
    assert uow.closed is True

    with store.begin() as initial:
        initial.put("sources", "source-uow-2", {"id": "source-uow-2"}, expected_revision=0)
        initial.commit()

    duplicate = store.begin()
    with pytest.raises(SQLiteUnitOfWorkConflict, match="expected revision 0, found 1"):
        duplicate.put("sources", "source-uow-2", {"id": "source-uow-2"}, expected_revision=0)

    invalid = store.begin()
    with pytest.raises(SQLiteUnitOfWorkError, match="JSON object"):
        invalid.put("sources", "source-uow-3", {"invalid": {"set"}}, expected_revision=0)
    assert invalid.closed is True

    unsafe = store.begin()
    with pytest.raises(SQLiteUnitOfWorkError, match="safe repository segment"):
        unsafe.put("sources", "../source-uow-unsafe", {"id": "unsafe"}, expected_revision=0)
    assert unsafe.closed is True


def test_uow_sqlite_settings_are_wal_foreign_keys_full_sync_and_reopenable(tmp_path) -> None:
    store = _store(tmp_path)

    assert store.journal_mode() == "wal"
    assert store.foreign_keys_enabled() is True
    assert store.synchronous_mode() == "full"

    with store.begin() as uow:
        uow.put("records", "record-uow-1", {"id": "record-uow-1"}, expected_revision=0)
        uow.commit()

    assert _store(tmp_path).read("records", "record-uow-1").revision == 1


def test_uow_reads_and_deletes_in_the_same_transaction_with_mandatory_cas(tmp_path) -> None:
    store = _store(tmp_path)
    with store.begin() as uow:
        uow.put("records", "record-uow-delete", {"id": "record-uow-delete"}, expected_revision=0)
        uow.commit()

    with store.begin() as uow:
        current = uow.read("records", "record-uow-delete")
        assert current is not None and current.revision == 1
        removed = uow.delete("records", "record-uow-delete", expected_revision=current.revision)
        assert removed == current
        assert uow.read("records", "record-uow-delete") is None
        uow.commit()

    assert store.read("records", "record-uow-delete") is None


def test_generation_token_advances_only_after_committed_target_mutations(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    collections = ("memory_atoms", "memory_scenarios")
    initial = store.generation_token(collections)

    rolled_back = store.begin()
    rolled_back.put(
        "memory_atoms",
        "atom-rollback",
        {"id": "atom-rollback"},
        expected_revision=0,
    )
    rolled_back.rollback()
    assert store.generation_token(collections) == initial

    with store.begin() as created:
        created.put(
            "memory_atoms",
            "atom-current",
            {"id": "atom-current", "revision": 1},
            expected_revision=0,
        )
        created.commit()
    after_create = store.generation_token(collections)
    assert after_create != initial
    assert _store(tmp_path).generation_token(collections) == after_create

    with store.begin() as unrelated:
        unrelated.put(
            "documents",
            "document-unrelated",
            {"id": "document-unrelated"},
            expected_revision=0,
        )
        unrelated.commit()
    assert store.generation_token(collections) == after_create

    with store.begin() as updated:
        updated.put(
            "memory_atoms",
            "atom-current",
            {"id": "atom-current", "revision": 2},
            expected_revision=1,
        )
        updated.commit()
    after_update = store.generation_token(collections)
    assert after_update != after_create

    with store.begin() as deleted:
        deleted.delete(
            "memory_atoms",
            "atom-current",
            expected_revision=2,
        )
        deleted.commit()
    assert store.generation_token(collections) != after_update


def test_generation_token_rejects_ambiguous_collection_scope(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SQLiteUnitOfWorkError, match="unique collections"):
        store.generation_token(())
    with pytest.raises(SQLiteUnitOfWorkError, match="unique collections"):
        store.generation_token(("memory_atoms", "memory_atoms"))


def test_generation_token_survives_verified_vault_backup_and_restore(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    with store.begin() as transaction:
        transaction.put(
            "memory_atoms",
            "atom-backed-up",
            {"id": "atom-backed-up"},
            expected_revision=0,
        )
        transaction.commit()
    expected = store.generation_token(("memory_atoms",))
    snapshot = create_vault_backup(
        source_root=tmp_path / "sqlite-uow",
        backups_root=tmp_path / "backups",
        snapshot_id="generation-token-backup",
    )
    restored_root = tmp_path / "restored"
    restore_vault_backup(
        snapshot_root=snapshot.snapshot_root,
        target_root=restored_root,
    )

    restored = SQLiteStructuredRecordStore(restored_root / "records.sqlite3")
    assert restored.generation_token(("memory_atoms",)) == expected
    assert restored.read("memory_atoms", "atom-backed-up") is not None
