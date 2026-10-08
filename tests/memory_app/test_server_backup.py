from pathlib import Path
import sqlite3

import pytest


def seed_database(path):
    from core.storage_provider import SQLiteStructuredRecordStore
    records = SQLiteStructuredRecordStore(path)
    with records.begin() as tx:
        tx.put('synthetic', 'one', {'text': 'first'}, expected_revision=0)
        tx.put('synthetic', 'one', {'text': 'second'}, expected_revision=1)
        tx.commit()
    return records


def database_rows(path):
    with sqlite3.connect(path) as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: connection.execute('SELECT * FROM "' + table + '" ORDER BY rowid').fetchall()
                for table in tables}


def test_server_backup_restores_all_user_tables_revisions_and_originals(tmp_path):
    from backend.memory_app.backup import backup_runtime, restore_runtime
    source = tmp_path / 'server'
    (source / 'server').mkdir(parents=True)
    (source / 'server/config.json').write_text('{"synthetic":true}', encoding='utf8')
    expected = {}
    for user in ['local-user', 'second-user']:
        root = source / 'users' / user
        (root / 'workspace').mkdir(parents=True)
        records = seed_database(root / 'records.sqlite3')
        (root / 'workspace/original.txt').write_text(user + ' synthetic original', encoding='utf8')
        expected[user] = database_rows(records.database_path)
    snapshot = backup_runtime(source, tmp_path / 'backups')
    assert snapshot.snapshot_id.startswith('backup-')
    target = tmp_path / 'restored'
    restore_runtime(snapshot.snapshot_root, target)
    assert (target / 'server/config.json').read_bytes() == (source / 'server/config.json').read_bytes()
    for user in expected:
        assert database_rows(target / 'users' / user / 'records.sqlite3') == expected[user]
        assert (target / 'users' / user / 'workspace/original.txt').read_bytes() == (
            source / 'users' / user / 'workspace/original.txt').read_bytes()
    with pytest.raises(ValueError, match='already exists'):
        restore_runtime(snapshot.snapshot_root, target)


def test_single_user_backup_excludes_server_and_other_users_and_rejects_existing_empty_target(tmp_path):
    from backend.memory_app.backup import backup_runtime, restore_runtime
    source = tmp_path / 'server'
    for user in ['local-user', 'other']:
        root = source / 'users' / user
        root.mkdir(parents=True)
        seed_database(root / 'records.sqlite3')
    snapshot = backup_runtime(source, tmp_path / 'backups', user='local-user')
    assert sorted(path.name for path in (snapshot.snapshot_root / 'payload').iterdir()) == ['records.sqlite3']
    empty = tmp_path / 'empty'
    empty.mkdir()
    with pytest.raises(ValueError, match='already exists'):
        restore_runtime(snapshot.snapshot_root, empty)
    assert list(empty.iterdir()) == []
    with pytest.raises(ValueError, match='user_invalid'):
        backup_runtime(source, tmp_path / 'backups', user='../other')


def test_backup_cli_runs_and_restore_cli_refuses_existing_target(tmp_path):
    import json
    import subprocess
    import sys
    source = tmp_path / 'user'
    source.mkdir()
    seed_database(source / 'records.sqlite3')
    command = [sys.executable, 'tools/backup.py']
    result = subprocess.run([*command, '--root', str(source), '--output', str(tmp_path / 'backups')],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(result.stdout)['snapshot']
    target = tmp_path / 'restored'
    restored = subprocess.run([*command, '--restore', snapshot, '--to', str(target)],
        capture_output=True, text=True, timeout=30)
    assert restored.returncode == 0, restored.stderr
    assert database_rows(target / 'records.sqlite3') == database_rows(source / 'records.sqlite3')
    refused = subprocess.run([*command, '--restore', snapshot, '--to', str(target)],
        capture_output=True, text=True, timeout=30)
    assert refused.returncode == 1 and 'backup_or_restore_failed' in refused.stderr
