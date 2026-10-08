import shutil
import sqlite3
import threading
import time

import pytest

from core.storage_provider import create_vault_backup, restore_vault_backup, fingerprint_vault_root, VaultBackupRestoreError


def test_online_snapshot_with_continuous_writes_and_open_wal_reader(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    db = root / 'memory.sqlite3'
    writer = sqlite3.connect(db)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('CREATE TABLE pairs (n INTEGER, doubled INTEGER)')
    writer.execute('INSERT INTO pairs VALUES (0,0)')
    writer.commit()
    reader = sqlite3.connect(db)
    reader.execute('BEGIN')
    reader.execute('SELECT * FROM pairs').fetchall()
    stop = threading.Event()
    ready = threading.Event()
    def write():
        with sqlite3.connect(db) as connection:
            n = 1
            while not stop.is_set():
                connection.execute('INSERT INTO pairs VALUES (?, ?)', (n, 2*n))
                connection.commit()
                ready.set()
                n += 1
                time.sleep(.001)
    thread = threading.Thread(target=write)
    thread.start()
    try:
        assert ready.wait(2)
        snapshot = create_vault_backup(source_root=root, backups_root=tmp_path/'backups', snapshot_id='live', sqlite_online=True)
        restore_vault_backup(snapshot_root=snapshot.snapshot_root, target_root=tmp_path/'restored')
        with sqlite3.connect(tmp_path/'restored'/'memory.sqlite3') as restored:
            assert restored.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            assert restored.execute('SELECT count(*) FROM pairs').fetchone()[0] >= 2
            assert restored.execute('SELECT count(*) FROM pairs WHERE doubled != 2*n').fetchone()[0] == 0
        assert not list((snapshot.snapshot_root/'payload').glob('*-wal'))
    finally:
        stop.set()
        thread.join(3)
        reader.close()
        writer.close()
    assert not thread.is_alive()


def test_sqlite_replacement_during_other_file_copy_still_fails(tmp_path):
    root = tmp_path/'source'
    root.mkdir()
    db = root/'a.sqlite3'
    connection = sqlite3.connect(db)
    try:
        connection.execute('CREATE TABLE original (n INTEGER)')
    finally:
        connection.close()
    replacement = tmp_path/'replacement.sqlite3'
    connection = sqlite3.connect(replacement)
    try:
        connection.execute('CREATE TABLE replacement (n INTEGER)')
    finally:
        connection.close()
    (root/'z.txt').write_text('stable')
    def copy(origin, destination):
        shutil.copyfile(origin, destination)
        if origin.name == 'z.txt':
            replacement.replace(db)
    with pytest.raises(VaultBackupRestoreError) as error:
        create_vault_backup(source_root=root, backups_root=tmp_path/'backups', snapshot_id='replaced', copy_file=copy, sqlite_online=True)
    assert error.value.reason_code == 'backup_source_changed'
    assert (tmp_path/'backups'/'replaced'/'vault-backup-incomplete.json').is_file()


def test_offline_migration_backup_keeps_the_source_fingerprint_contract(tmp_path):
    root = tmp_path/'source'
    root.mkdir()
    connection = sqlite3.connect(root/'memory.sqlite3')
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE stable (n INTEGER)')
        connection.commit()
        snapshot = create_vault_backup(source_root=root, backups_root=tmp_path/'backups', snapshot_id='offline')
        assert fingerprint_vault_root(root) == snapshot.source_fingerprint
    finally:
        connection.close()
