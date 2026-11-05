from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import shutil

import pytest

from backend.memory_app.backup import restore_runtime
from backend.memory_app.v2.backup_schedule import BackupSchedule
from backend.memory_app.v2.daily import DailyBackup
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / 'runtime'
    root.mkdir()
    records, _namespace = AggregateRepositoryFactory(runtime_root=root, namespace_id='default',
        json_store=JsonObjectStore(root / '.rebuild-data', legacy_root=root / 'library', namespace_id='default')).recognition_record_store_resolution()
    (root / 'workspace').mkdir()
    (root / 'workspace' / 'note.md').write_text('synthetic schedule original')
    return root, records


def _body(schedule, identity, **settings):
    current = schedule.settings()
    fields = {key: current[key] for key in ('frequency', 'time', 'timezone', 'weekday', 'month_day')}
    return {**fields, **settings, 'command_id': identity, 'expected_revision': current['revision']}


def test_schedule_cas_and_replayed_command_keep_one_revision_in_real_sqlite(runtime):
    _root, records = runtime
    schedule = BackupSchedule(records)
    body = _body(schedule, 'schedule-command-first', frequency='weekly')
    first = schedule.save(body)
    assert first['state'] == 'saved' and first['revision'] == 1
    assert schedule.save(body) == first
    assert schedule.settings()['revision'] == 1
    assert schedule.command(body['command_id']) == first
    contenders = [_body(schedule, 'schedule-command-' + str(index), frequency=value)
                  for index, value in enumerate(('monthly', 'off'))]
    def save(value):
        try:
            return BackupSchedule(SQLiteStructuredRecordStore(records.database_path)).save(value)['state']
        except SQLiteUnitOfWorkConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, contenders)) == ['conflict', 'saved']
    assert schedule.settings()['revision'] == 2


def test_actual_backup_obeys_weekly_month_end_off_and_explicit_manual_run(runtime):
    root, records = runtime
    clock = [datetime(2026, 11, 3, 2, tzinfo=timezone.utc)]
    schedule = BackupSchedule(records, now=lambda: clock[0])
    owner = DailyBackup(root, records, now=lambda: clock[0])
    schedule.save(_body(schedule, 'schedule-weekly-command', frequency='weekly', weekday=1, time='03:00'))
    assert owner.run()['status'] == 'not_due'
    clock[0] = datetime(2026, 11, 3, 3, tzinfo=timezone.utc)
    first = owner.run()
    assert first['status'] == 'done'
    assert owner.run()['snapshot_id'] == first['snapshot_id']
    assert records.read('v2_backup_jobs', 'week-2026-11-02').payload['status'] == 'done'
    schedule.save(_body(schedule, 'schedule-off-command', frequency='off'))
    assert owner.run()['status'] == 'not_due'
    manual = owner.run(identity='manual-explicit', automatic=False)
    assert manual['status'] == 'done' and manual['snapshot_id'] != first['snapshot_id']
    schedule.save(_body(schedule, 'schedule-monthly-command', frequency='monthly', month_day=31))
    clock[0] = datetime(2028, 2, 29, 2, 59, 59, tzinfo=timezone.utc)
    assert owner.run()['status'] == 'not_due'
    clock[0] = datetime(2028, 2, 29, 3, tzinfo=timezone.utc)
    monthly = owner.run()
    assert monthly['status'] == 'done'
    assert records.read('v2_backup_jobs', 'month-2028-02').payload['snapshot_id'] == monthly['snapshot_id']
    assert not schedule.due()
    assert (root / 'workspace' / 'note.md').read_text() == 'synthetic schedule original'


def test_restored_running_checkpoint_reconciles_its_actual_completed_snapshot(runtime, tmp_path):
    root, records = runtime
    day = datetime(2026, 11, 3, 3, tzinfo=timezone.utc)
    result = DailyBackup(root, records, now=lambda: day).run()
    snapshot = root / '..rebuild-data-recovery' / 'snapshots' / result['snapshot_id']
    restored = tmp_path / 'restored'
    restore_runtime(snapshot, restored)
    database = restored / records.database_path.relative_to(root)
    replay = SQLiteStructuredRecordStore(database)
    assert replay.read('v2_backup_jobs', '2026-11-03').payload['status'] == 'running'
    retained = restored / '..rebuild-data-recovery' / 'snapshots' / result['snapshot_id']
    assert retained.is_relative_to(restored) and restored.is_relative_to(tmp_path)
    shutil.copytree(snapshot, retained)
    settled = DailyBackup(restored, replay, now=lambda: day).run()
    assert settled['status'] == 'done' and settled['snapshot_id'] == result['snapshot_id']
    assert len(list(retained.parent.iterdir())) == 1
    assert (restored / 'workspace' / 'note.md').read_text() == 'synthetic schedule original'
