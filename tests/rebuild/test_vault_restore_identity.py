from contextlib import closing
import sqlite3

import pytest

from core.storage_provider.vault_backup_restore import fingerprint_vault_restore_source, VaultBackupRestoreError


def _database(tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    database = root / 'notes.sqlite3'
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript('CREATE TABLE notes(value); INSERT INTO notes VALUES (1); CREATE TABLE counters(id INTEGER PRIMARY KEY AUTOINCREMENT); INSERT INTO counters DEFAULT VALUES;')
    return root, database


@pytest.mark.parametrize('statement', [
    "UPDATE notes SET value='1'", "UPDATE notes SET value=x'31'",
    "UPDATE notes SET value=NULL", "UPDATE notes SET value=1.0",
    'UPDATE notes SET rowid=2', 'INSERT INTO notes VALUES(1)',
    'CREATE INDEX note_value ON notes(value)',
    'CREATE VIEW readable AS SELECT value FROM notes',
    'CREATE TRIGGER keep_notes AFTER INSERT ON notes BEGIN SELECT 1; END',
    "UPDATE sqlite_sequence SET seq=10 WHERE name='counters'",
    'PRAGMA user_version=7', 'PRAGMA application_id=123',
])
def test_restore_identity_covers_sqlite_content_and_metadata(tmp_path, statement):
    root, database = _database(tmp_path)
    before = fingerprint_vault_restore_source(root)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(statement)
        connection.commit()
    assert fingerprint_vault_restore_source(root) != before


def test_restore_identity_reads_uncheckpointed_wal(tmp_path):
    root, database = _database(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        before = fingerprint_vault_restore_source(root)
        connection.execute('UPDATE notes SET value=2')
        connection.commit()
        assert database.with_name(database.name + '-wal').stat().st_size > 0
        assert fingerprint_vault_restore_source(root) != before


def test_restore_identity_ignores_storage_layout_and_schema_counter(tmp_path):
    root, database = _database(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('CREATE INDEX note_value ON notes(value)')
        before = fingerprint_vault_restore_source(root)
        connection.executescript('DROP INDEX note_value; CREATE INDEX note_value ON notes(value); PRAGMA page_size=8192; VACUUM;')
    assert fingerprint_vault_restore_source(root) == before


@pytest.mark.parametrize('operation', ['edit', 'add', 'delete'])
def test_restore_identity_preserves_ordinary_file_changes(tmp_path, operation):
    root, _ = _database(tmp_path)
    note = root / 'note.txt'
    note.write_text('one')
    before = fingerprint_vault_restore_source(root)
    if operation == 'edit':
        note.write_text('two')
    elif operation == 'add':
        (root / 'new.txt').write_text('one')
    else:
        note.unlink()
    assert fingerprint_vault_restore_source(root) != before


def test_restore_identity_supports_without_rowid(tmp_path):
    root, database = _database(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript('CREATE TABLE keyed(id TEXT PRIMARY KEY, body BLOB) WITHOUT ROWID; INSERT INTO keyed VALUES ("key", x\'00\');')
        before = fingerprint_vault_restore_source(root)
        connection.execute("UPDATE keyed SET body=x'01'")
        connection.commit()
    assert fingerprint_vault_restore_source(root) != before


def test_restore_identity_covers_fts_shadow_tables(tmp_path):
    root, database = _database(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('CREATE VIRTUAL TABLE search USING fts5(body)')
        connection.execute("INSERT INTO search VALUES('before')")
        connection.commit()
        before = fingerprint_vault_restore_source(root)
        connection.execute("UPDATE search SET body='after'")
        connection.commit()
    assert fingerprint_vault_restore_source(root) != before


def test_restore_identity_rejects_unreadable_hidden_rowid(tmp_path):
    root, database = _database(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('CREATE TABLE shadowed(rowid TEXT, _rowid_ TEXT, oid TEXT)')
    with pytest.raises(VaultBackupRestoreError, match='every row identity'):
        fingerprint_vault_restore_source(root)
