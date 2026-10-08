import json
import sqlite3
import subprocess
import sys
import os
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from core.storage_provider import SQLiteStructuredRecordStore


def seed(root):
    root.mkdir(parents=True, exist_ok=True)
    records = SQLiteStructuredRecordStore(root / 'records.sqlite3')
    with records.begin() as tx:
        tx.put('synthetic', 'one', {'value': 1}, expected_revision=0)
        tx.put('synthetic', 'one', {'value': 2}, expected_revision=1)
        tx.commit()
    with sqlite3.connect(root / 'records.sqlite3') as db:
        db.execute('CREATE TABLE additional (revision INTEGER, value TEXT)')
        db.execute("INSERT INTO additional VALUES (9, 'synthetic')")
    return records


def test_backup_restores_and_checks_every_sqlite_table_and_excludes_only_logs(tmp_path):
    from backend.memory_app.backup import backup_runtime, verification_metadata, verify_runtime_backup
    root = tmp_path / 'source'
    seed(root)
    (root / 'logs').mkdir()
    (root / 'logs/app-2026-10-06.log').write_text('synthetic log')
    (root / 'original.txt').write_text('synthetic original')
    snapshot = backup_runtime(root, tmp_path / 'backups')
    meta = verification_metadata(snapshot.snapshot_root)
    assert meta['verified'] is True and meta['reason_code'] is None
    assert meta['tables']['records.sqlite3']['additional'] == {'count': 1, 'max_revision': 9}
    assert not (snapshot.snapshot_root / 'payload/logs').exists()
    assert (snapshot.snapshot_root / 'payload/original.txt').read_text() == 'synthetic original'
    assert verify_runtime_backup(snapshot.snapshot_root)['verified'] is True
    (snapshot.snapshot_root / 'payload/original.txt').write_text('broken')
    failed = verify_runtime_backup(snapshot.snapshot_root)
    assert failed['verified'] is False and failed['reason_code'] == 'backup_verification_failed'
    assert verification_metadata(snapshot.snapshot_root)['verified'] is False


def test_verify_cli_reports_success_and_nonzero_corruption(tmp_path):
    from backend.memory_app.backup import backup_runtime
    root = tmp_path / 'source'
    seed(root)
    snapshot = backup_runtime(root, tmp_path / 'backups')
    command = [sys.executable, 'tools/backup.py', '--verify', str(snapshot.snapshot_root)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['verified'] is True
    (snapshot.snapshot_root / 'payload/records.sqlite3').write_bytes(b'corrupt')
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1 and json.loads(result.stdout)['verified'] is False


def test_unreadable_verification_metadata_reports_safe_failure_without_pruning_unknown(tmp_path):
    from backend.memory_app.backup import backup_runtime, verification_metadata, verify_runtime_backup, prune_automatic_backups
    root = tmp_path / 'source'
    seed(root)
    snapshot = backup_runtime(root, tmp_path / 'backups', snapshot_id='snap-auto-unknown')
    (snapshot.snapshot_root / 'backup-verification.json').write_text('{broken')
    result = verify_runtime_backup(snapshot.snapshot_root)
    assert result['verified'] is False and result['reason_code'] == 'backup_verification_failed'
    assert verification_metadata(snapshot.snapshot_root)['verified'] is False
    prune_automatic_backups(snapshot.snapshot_root.parent, keep=0)
    assert snapshot.snapshot_root.exists()


def test_daily_backup_once_across_instances_and_retains_seven_auto_not_manual(tmp_path):
    from backend.memory_app.v2.daily import DailyBackup
    from backend.memory_app.backup import backup_runtime
    root = tmp_path / 'runtime'
    records = seed(root / '.rebuild-data')
    backups = root / '..rebuild-data-recovery/snapshots'
    manual = backup_runtime(root / '.rebuild-data', backups, snapshot_id='snap-manual')
    day = datetime(2026, 10, 1, tzinfo=timezone.utc)
    instances = [DailyBackup(root, SQLiteStructuredRecordStore(records.database_path), now=lambda: day) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda owner: owner.run(), instances))
    assert len(list(backups.glob('snap-auto-*'))) == 1
    instances[0].run()
    assert len(list(backups.glob('snap-auto-*'))) == 1
    for offset in range(1, 9):
        day = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(days=offset)
        DailyBackup(root, records, now=lambda: day).run()
    assert len(list(backups.glob('snap-auto-*'))) == 7
    assert manual.snapshot_root.is_dir()
    assert not records.list_matching('v2_backup_jobs', status='failed')
    owner = instances[0]
    assert owner.failures() == {}
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.memory_app.v2.jobs import install_job_routes
    from backend.recognition import RecognitionService
    app = FastAPI()
    app.state.memory_backup = owner
    install_job_routes(app, records=records, service=RecognitionService(records))
    with TestClient(app) as client:
        assert not [row for row in client.get('/api/v2/jobs').json()['items'] if row['target']['type'] == 'backup']


def test_slow_backup_keeps_cross_process_exclusion_even_after_old_lease_expiry(tmp_path, monkeypatch):
    from backend.memory_app.v2.daily import DailyBackup
    from core.storage_provider import vault_backup_restore
    root = tmp_path / 'runtime'
    records = seed(root / '.rebuild-data')
    day = datetime(2026, 10, 1, tzinfo=timezone.utc)
    entered, release = threading.Event(), threading.Event()
    original = vault_backup_restore._backup_sqlite_file
    def observe(*args):
        entered.set()
        assert release.wait(20)
        return original(*args)
    monkeypatch.setattr(vault_backup_restore, '_backup_sqlite_file', observe)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(DailyBackup(root, records, now=lambda: day).run)
        try:
            assert entered.wait(5)
            script = '''import sys
from pathlib import Path
from datetime import datetime, timezone
from backend.memory_app.v2.daily import DailyBackup
from core.storage_provider import SQLiteStructuredRecordStore
root=Path(sys.argv[1]); records=SQLiteStructuredRecordStore(root/'.rebuild-data/records.sqlite3')
print(DailyBackup(root,records,now=lambda:datetime(2026,10,1,1,tzinfo=timezone.utc)).run())
'''
            env = dict(os.environ, PYTHONPATH=str(Path('src').resolve()))
            second = subprocess.run([sys.executable, '-c', script, str(root)], env=env,
                                    capture_output=True, text=True, timeout=15)
            assert second.returncode == 0, second.stderr
            assert "'status': 'running'" in second.stdout
            assert len(list((root / '..rebuild-data-recovery/snapshots').glob('snap-auto-*'))) == 1
        finally:
            release.set()
        assert first.result(timeout=10)['status'] == 'done'


def test_failed_automatic_restore_checks_keep_seven_and_retain_failure_jobs(tmp_path, monkeypatch):
    from backend.memory_app.backup import backup_runtime, verification_metadata
    from backend.memory_app.v2.daily import DailyBackup
    from core.storage_provider import vault_backup_restore
    root = tmp_path / 'runtime'
    records = seed(root / '.rebuild-data')
    backups = root / '..rebuild-data-recovery/snapshots'
    manual = backup_runtime(root / '.rebuild-data', backups, snapshot_id='snap-manual')
    unknown = backups / 'snap-auto-unknown'
    unknown.mkdir()
    (unknown / 'operator-note').write_text('synthetic unrelated operator data')
    original = vault_backup_restore._copy_file
    def corrupt_temporary_restore(origin, destination):
        original(origin, destination)
        if 'chriptmas-backup-check-' in str(destination) and destination.name == 'records.sqlite3':
            destination.write_bytes(b'synthetic restore corruption')
    monkeypatch.setattr(vault_backup_restore, '_copy_file', corrupt_temporary_restore)
    for offset in range(9):
        day = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(days=offset)
        result = DailyBackup(root, records, now=lambda: day).run()
        assert result['status'] == 'failed' and result['reason_code'] == 'backup_verification_failed'
    owned = [path for path in backups.glob('snap-auto-*') if verification_metadata(path).get('automatic') is True]
    assert len(owned) == 7
    assert all(verification_metadata(path)['verified'] is False for path in owned)
    assert len(records.list_matching('v2_backup_jobs', status='failed')) == 9
    assert manual.snapshot_root.exists() and unknown.joinpath('operator-note').is_file()


def test_damaged_backup_enters_original_tray_and_retry_is_idempotent(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.memory_app.v2.daily import install_daily_jobs
    from backend.memory_app.v2.jobs import install_job_routes
    from backend.memory_app.backup import verify_runtime_backup, verification_metadata
    from backend.recognition import RecognitionService
    root = tmp_path / 'runtime'
    records = seed(root / '.rebuild-data')
    app = FastAPI()
    jobs = install_daily_jobs(app, records=records, runtime_root=root)
    install_job_routes(app, records=records, service=RecognitionService(records))
    jobs.run()
    assert 'backup' in jobs.jobs
    result = records.list('v2_backup_jobs')[0]
    snapshot = root / '..rebuild-data-recovery/snapshots' / result.payload['snapshot_id']
    (snapshot / 'payload/records.sqlite3').write_bytes(b'corruption')
    assert verify_runtime_backup(snapshot)['verified'] is False
    with TestClient(app) as client:
        row, = client.get('/api/v2/jobs').json()['items']
        assert row['state'] == 'failed' and row['title'] == '备份失败'
        assert row['target'] == {'type': 'backup', 'id': row['id']}
        response = client.post(f"/api/v2/jobs/{row['id']}/retry")
        assert response.status_code == 200, response.text
        assert client.get('/api/v2/jobs').json()['items'] == []
        count = len(list(snapshot.parent.glob('snap-auto-*')))
        assert client.post(f"/api/v2/jobs/{row['id']}/retry").status_code == 200
        assert len(list(snapshot.parent.glob('snap-auto-*'))) == count


def backup_app(root):
    from fastapi import FastAPI
    from backend.memory_app.v2.daily import install_daily_jobs
    from backend.memory_app.v2.jobs import install_job_routes
    from backend.recognition import RecognitionService
    records = seed(root / '.rebuild-data')
    app = FastAPI()
    install_daily_jobs(app, records=records, runtime_root=root)
    install_job_routes(app, records=records, service=RecognitionService(records))
    app.state.memory_backup.now = lambda: datetime(2026, 10, 1, tzinfo=timezone.utc)
    return app, records


def test_automatic_failure_retry_and_later_corruption_keep_one_logical_tray_job(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from core.storage_provider import vault_backup_restore
    from backend.memory_app.backup import verification_metadata, verify_runtime_backup
    root = tmp_path / 'runtime'
    app, records = backup_app(root)
    owner = app.state.memory_backup
    original = vault_backup_restore._copy_file
    corrupt = [True]
    def observe(origin, destination):
        original(origin, destination)
        if corrupt[0] and 'chriptmas-backup-check-' in str(destination) and destination.name == 'records.sqlite3':
            destination.write_bytes(b'synthetic temporary corruption')
    monkeypatch.setattr(vault_backup_restore, '_copy_file', observe)
    assert owner.run()['status'] == 'failed'
    failed = records.read('v2_backup_jobs', '2026-10-01')
    old = root / '..rebuild-data-recovery/snapshots' / failed.payload['snapshot_id']
    meta = verification_metadata(old)
    assert meta['job_id'] == '2026-10-01' and meta['snapshot_id'] == old.name
    assert meta['schema_version'] == '1.0.0'
    with TestClient(app) as client:
        row, = client.get('/api/v2/jobs').json()['items']
        assert row['id'] == '2026-10-01'
        corrupt[0] = False
        assert client.post('/api/v2/jobs/2026-10-01/retry').status_code == 200
        assert client.get('/api/v2/jobs').json()['items'] == []
        (old / 'payload/records.sqlite3').write_bytes(b'synthetic superseded corruption')
        assert verify_runtime_backup(old)['verified'] is False
        assert client.get('/api/v2/jobs').json()['items'] == []
        saved = records.read('v2_backup_jobs', row['id'])
        current = root / '..rebuild-data-recovery/snapshots' / saved.payload['snapshot_id']
        (current / 'payload/records.sqlite3').write_bytes(b'synthetic later corruption')
        assert verify_runtime_backup(current)['verified'] is False
        row, = client.get('/api/v2/jobs').json()['items']
        assert row['id'] == '2026-10-01'
        assert client.post('/api/v2/jobs/2026-10-01/retry').status_code == 200
        assert client.get('/api/v2/jobs').json()['items'] == []


def test_two_real_http_retries_with_stale_failure_capture_create_only_one_success(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from core.storage_provider import vault_backup_restore
    root = tmp_path / 'runtime'
    app, records = backup_app(root)
    owner = app.state.memory_backup
    original_copy = vault_backup_restore._copy_file
    def corrupt(origin, destination):
        original_copy(origin, destination)
        if 'chriptmas-backup-check-' in str(destination) and destination.name == 'records.sqlite3':
            destination.write_bytes(b'synthetic temporary corruption')
    monkeypatch.setattr(vault_backup_restore, '_copy_file', corrupt)
    assert owner.run()['status'] == 'failed'
    monkeypatch.setattr(vault_backup_restore, '_copy_file', original_copy)
    original_failures = owner.failures
    both_captured, first_completed = threading.Event(), threading.Event()
    guard, calls = threading.Lock(), []
    def observe_failures():
        captured = original_failures()
        with guard:
            calls.append(True)
            number = len(calls)
        if number == 1:
            assert both_captured.wait(10)
        else:
            both_captured.set()
            assert first_completed.wait(10)
        return captured
    monkeypatch.setattr(owner, 'failures', observe_failures)
    before = len(list((root / '..rebuild-data-recovery/snapshots').glob('snap-auto-*')))
    def request_first():
        result = client.post('/api/v2/jobs/2026-10-01/retry')
        first_completed.set()
        return result
    def request_second():
        return client.post('/api/v2/jobs/2026-10-01/retry')
    with TestClient(app) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(request_first)
            # First must be the first captured request, not just the first submitted.
            while not calls:
                threading.Event().wait(0.005)
            second = pool.submit(request_second)
            responses = [first.result(timeout=15), second.result(timeout=15)]
    assert [result.status_code for result in responses] == [200, 200]
    assert len(list((root / '..rebuild-data-recovery/snapshots').glob('snap-auto-*'))) == before + 1
    assert records.read('v2_backup_jobs', '2026-10-01').payload['status'] == 'done'


def test_real_http_retry_during_running_backup_is_idempotent(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from core.storage_provider import vault_backup_restore
    root = tmp_path / 'runtime'
    app, records = backup_app(root)
    owner = app.state.memory_backup
    original_copy = vault_backup_restore._copy_file
    def corrupt(origin, destination):
        original_copy(origin, destination)
        if 'chriptmas-backup-check-' in str(destination) and destination.name == 'records.sqlite3':
            destination.write_bytes(b'synthetic temporary corruption')
    monkeypatch.setattr(vault_backup_restore, '_copy_file', corrupt)
    assert owner.run()['status'] == 'failed'
    entered, release = threading.Event(), threading.Event()
    def block_after_real_copy(origin, destination):
        original_copy(origin, destination)
        if 'chriptmas-backup-check-' in str(destination) and destination.name == 'records.sqlite3':
            entered.set()
            assert release.wait(15)
    monkeypatch.setattr(vault_backup_restore, '_copy_file', block_after_real_copy)
    backups = root / '..rebuild-data-recovery/snapshots'
    before = len(list(backups.glob('snap-auto-*')))
    with TestClient(app) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(client.post, '/api/v2/jobs/2026-10-01/retry')
            try:
                assert entered.wait(10)
                running = records.read('v2_backup_jobs', '2026-10-01')
                assert running.payload['status'] == 'running'
                second = client.post('/api/v2/jobs/2026-10-01/retry')
                unknown = client.post('/api/v2/jobs/unknown-id/retry')
            finally:
                release.set()
            completed = first.result(timeout=15)
    assert second.status_code == 200 and second.json()['status'] == 'running'
    assert unknown.status_code == 404
    assert completed.status_code == 200 and completed.json()['status'] == 'done'
    assert records.read('v2_backup_jobs', '2026-10-01').payload['status'] == 'done'
    assert len(list(backups.glob('snap-auto-*'))) == before + 1


def test_actual_windows_readonly_prune_failure_commits_one_failed_job(tmp_path):
    import stat
    from fastapi.testclient import TestClient
    root = tmp_path / 'runtime'
    app, records = backup_app(root)
    owner = app.state.memory_backup
    for offset in range(7):
        day = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(days=offset)
        owner.now = lambda: day
        result = owner.run()
        assert result['status'] == 'done'
        assert owner.run() == result
    oldest = records.read('v2_backup_jobs', '2026-10-01').payload['snapshot_id']
    blocked = root / '..rebuild-data-recovery/snapshots' / oldest / 'payload/records.sqlite3'
    blocked.chmod(stat.S_IREAD)
    try:
        day = datetime(2026, 10, 8, tzinfo=timezone.utc)
        owner.now = lambda: day
        result = owner.run()
        saved = records.read('v2_backup_jobs', '2026-10-08')
        if os.name == 'nt':
            assert result['status'] == 'failed' and result['reason_code'] == 'backup_prune_failed'
            assert saved.payload['status'] == 'failed' and saved.payload['snapshot_id']
            with TestClient(app) as client:
                row, = client.get('/api/v2/jobs').json()['items']
                assert row['id'] == '2026-10-08' and row['state'] == 'failed'
        else:
            assert result['status'] == 'done'
            assert saved.payload['status'] == 'done' and saved.payload['snapshot_id']
            assert len(list((root / '..rebuild-data-recovery/snapshots').glob('snap-auto-*'))) == 7
            assert owner.failures() == {}
            with TestClient(app) as client:
                assert client.get('/api/v2/jobs').json()['items'] == []
    finally:
        if blocked.exists():
            blocked.chmod(stat.S_IWRITE | stat.S_IREAD)


def test_prune_preserves_parseable_partial_identity_and_invalid_date_metadata(tmp_path):
    from backend.memory_app.backup import backup_runtime, prune_automatic_backups
    root = tmp_path / 'source'
    seed(root)
    backups = tmp_path / 'backups'
    manual = backup_runtime(root, backups, snapshot_id='snap-manual')
    for name, meta in [
        ('snap-auto-partial', {'automatic': True, 'created_at': '2000-01-01T00:00:00+00:00'}),
        ('snap-auto-bad-date', {'schema_version': '1.0.0', 'snapshot_id': 'snap-auto-bad-date', 'job_id': '2026-10-01',
                              'automatic': True, 'created_at': 'not-a-date', 'verified': True, 'reason_code': None, 'tables': {}}),
    ]:
        unknown = backups / name
        (unknown / 'payload').mkdir(parents=True)
        (unknown / 'payload/operator.txt').write_text('synthetic operator data')
        manifest = json.loads((manual.snapshot_root / 'vault-backup-manifest.json').read_text())
        manifest['snapshot_id'] = name
        if name.endswith('partial'):
            manifest = {}
        (unknown / 'vault-backup-manifest.json').write_text(json.dumps(manifest))
        (unknown / 'backup-verification.json').write_text(json.dumps(meta))
    prune_automatic_backups(backups, keep=0)
    assert (backups / 'snap-auto-partial/payload/operator.txt').is_file()
    assert (backups / 'snap-auto-bad-date/payload/operator.txt').is_file()
    assert manual.snapshot_root.is_dir()


def test_backup_roots_preserve_original_home_expansion():
    from backend.shared.deployment import runtime_backup_roots
    active, backups, operations = runtime_backup_roots('~')
    expected = (Path('~') / '.rebuild-data').expanduser().absolute().resolve(strict=False)
    assert active == expected
    assert backups == expected.parent / '..rebuild-data-recovery/snapshots'
    assert operations == expected.parent / '..rebuild-data-recovery/operations'
