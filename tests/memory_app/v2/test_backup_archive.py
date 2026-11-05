from pathlib import Path
import json
import stat
import sqlite3
import zipfile
from contextlib import closing

import pytest

from backend.memory_app.backup import backup_runtime, restore_runtime
from backend.memory_app.backup_archive import BackupArchiveError, pack_backup_archive, unpack_backup_archive
from backend.memory_app.v2.policies import get
from core.storage_provider import SQLiteStructuredRecordStore
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.storage_provider import JsonObjectStore


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    factory = AggregateRepositoryFactory(runtime_root=root, namespace_id='default',
        json_store=JsonObjectStore(root / '.rebuild-data', legacy_root=root / 'library', namespace_id='default'))
    records, _namespace = factory.recognition_record_store_resolution()
    with records.begin() as tx:
        tx.put('v2_projects', 'daily', {'name': 'synthetic archive check'}, expected_revision=0)
        tx.commit()
    (root / 'workspace').mkdir()
    (root / 'workspace' / '原件.md').write_text('synthetic original at backup', encoding='utf8')
    return root, records


def _limits():
    return get('backup_archive', version='@1')({'operation': 'limits'})


def _archive(source, tmp_path, *, scope='personal-runtime'):
    snapshot = backup_runtime(source, tmp_path / 'backups', snapshot_id='snap-archive-check')
    return pack_backup_archive(snapshot_root=snapshot.snapshot_root, destination=tmp_path / 'backup.zip',
        scope_kind=scope, limits=_limits())


def _copy_zip(source, destination, entries):
    with zipfile.ZipFile(source) as origin, zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as target:
        for info in origin.infolist():
            target.writestr(info.filename, origin.read(info))
        for name, body in entries:
            if isinstance(name, str) and '\\' in name:
                info = zipfile.ZipInfo()
                info.filename = info.orig_filename = name
                name = info
            target.writestr(name, body)


def test_real_archive_roundtrip_rekeys_only_stage_and_restores_committed_sqlite(source, tmp_path):
    root, original_records = source
    archive = _archive(root, tmp_path)
    before = archive.path.read_bytes()
    assert archive.size_bytes == len(before)
    assert archive.filename == 'chriptmas-backup-snap-archive-check.zip'
    (root / 'workspace' / '原件.md').write_text('current original remains', encoding='utf8')
    imported = unpack_backup_archive(archive_path=archive.path, stage_root=tmp_path / 'stage',
        expected_scope='personal-runtime', limits=_limits(), snapshot_id='snap-import-check')
    assert imported.original_snapshot_id == 'snap-archive-check'
    assert imported.snapshot.snapshot_id == 'snap-import-check'
    assert not (imported.snapshot.snapshot_root / 'recovery-point.json').exists()
    restored = restore_runtime(imported.snapshot.snapshot_root, tmp_path / 'restored')
    records = SQLiteStructuredRecordStore(restored.target_root / original_records.database_path.relative_to(root))
    assert records.read('v2_projects', 'daily').payload['name'] == 'synthetic archive check'
    assert (restored.target_root / 'workspace' / '原件.md').read_text(encoding='utf8') == 'synthetic original at backup'
    assert (root / 'workspace' / '原件.md').read_text(encoding='utf8') == 'current original remains'
    assert archive.path.read_bytes() == before


@pytest.mark.parametrize('name', ['../escaped', '/absolute', 'C:/drive', 'snapshot/payload/file:ads',
    'snapshot/payload/CON.txt', 'snapshot/payload/a\\escape', 'snapshot/payload/a./file',
    'snapshot/payload/../file', 'snapshot/payload/WORKSPACE/原件.md'])
def test_real_zip_refuses_unsafe_and_case_colliding_entries_before_extraction(source, tmp_path, name):
    archive = _archive(source[0], tmp_path)
    invalid = tmp_path / 'invalid.zip'
    _copy_zip(archive.path, invalid, [(name, b'unsafe')])
    with pytest.raises(BackupArchiveError):
        unpack_backup_archive(archive_path=invalid, stage_root=tmp_path / 'stage',
            expected_scope='personal-runtime', limits=_limits())
    assert not (tmp_path / 'stage').exists()
    assert not (tmp_path / 'escaped').exists()


def test_real_zip_refuses_links_duplicates_bombs_and_actual_limits(source, tmp_path):
    archive = _archive(source[0], tmp_path)
    link = zipfile.ZipInfo('snapshot/payload/link')
    link.create_system, link.external_attr = 3, (stat.S_IFLNK | 0o777) << 16
    for name, entries in [('link', [(link, b'../escape')]),
            ('duplicate', [('chriptmas-backup.json', b'{}')]),
            ('bomb', [('snapshot/payload/bomb', b'0' * (2 * 1024 ** 2))])]:
        path = tmp_path / (name + '.zip')
        _copy_zip(archive.path, path, entries)
        with pytest.raises(BackupArchiveError):
            unpack_backup_archive(archive_path=path, stage_root=tmp_path / name,
                expected_scope='personal-runtime', limits=_limits())
        assert not (tmp_path / name).exists()
    for field, value in [('archive_bytes', 64), ('unpacked_bytes', 32), ('entries', 2), ('entry_bytes', 32)]:
        with pytest.raises(BackupArchiveError, match='limit_exceeded'):
            unpack_backup_archive(archive_path=archive.path, stage_root=tmp_path / field,
                expected_scope='personal-runtime', limits={**_limits(), field: value})
        assert not (tmp_path / field).exists()


def test_shared_binding_cannot_be_relabelled_into_a_personal_runtime(source, tmp_path):
    root, records = source
    with records.begin() as tx:
        tx.put('v2_shared_root', 'identity', {'project_id': 'shared-synthetic'}, expected_revision=0)
        tx.commit()
    archive = _archive(root, tmp_path, scope='shared-project')
    forged = tmp_path / 'forged.zip'
    with zipfile.ZipFile(archive.path) as origin, zipfile.ZipFile(forged, 'w') as target:
        for info in origin.infolist():
            body = origin.read(info)
            if info.filename == 'chriptmas-backup.json':
                value = json.loads(body)
                value['scope'] = 'personal-runtime'
                body = json.dumps(value).encode('utf8')
            target.writestr(info.filename, body)
    with pytest.raises(BackupArchiveError, match='scope_invalid'):
        unpack_backup_archive(archive_path=forged, stage_root=tmp_path / 'stage',
            expected_scope='personal-runtime', limits=_limits())
    imported = unpack_backup_archive(archive_path=archive.path, stage_root=tmp_path / 'shared-stage',
        expected_scope='shared-project', limits=_limits())
    assert imported.scope_kind == 'shared-project'


def test_personal_archive_rejects_an_actual_server_layout(source, tmp_path):
    root, _records = source
    (root / 'server').mkdir()
    (root / 'server' / 'central.txt').write_text('synthetic central authority')
    with pytest.raises(BackupArchiveError, match='scope_invalid'):
        _archive(root, tmp_path)


def test_personal_archive_preserves_original_empty_desktop_device_owner(source, tmp_path):
    from backend.memory_app.v2.devices import DeviceRegistry
    root, _records = source
    registry = DeviceRegistry(root / 'server')
    assert not list(registry.records.list('server_devices'))
    archive = _archive(root, tmp_path)
    imported = unpack_backup_archive(archive_path=archive.path, stage_root=tmp_path / 'stage',
        expected_scope='personal-runtime', limits=_limits(), snapshot_id='snap-import-desktop')
    restored = restore_runtime(imported.snapshot.snapshot_root, tmp_path / 'restored')
    device_records = SQLiteStructuredRecordStore(restored.target_root / 'server/devices.sqlite3')
    assert not list(device_records.list('server_devices'))
    assert (restored.target_root / 'workspace/原件.md').read_text(encoding='utf8') == 'synthetic original at backup'
    assert imported.scope_kind == 'personal-runtime'


@pytest.mark.parametrize('change', ['records', 'generations', 'metadata', 'table', 'view', 'trigger', 'application', 'file'])
def test_desktop_initialization_exception_rejects_all_nonempty_or_changed_owners(source, tmp_path, change):
    from backend.memory_app.v2.devices import DeviceRegistry
    root, _records = source
    registry = DeviceRegistry(root / 'server')
    assert not list(registry.records.list('server_devices'))
    if change == 'records':
        registry.issue_pairing(user_id='local-user', actor='install')
    elif change == 'file':
        (root / 'server' / 'extra.txt').write_text('additional server entry')
    else:
        statements = {
            'generations': "INSERT INTO crp_collection_generations VALUES ('server_devices', 1)",
            'metadata': "INSERT INTO crp_uow_meta VALUES ('extra', 'present')",
            'table': 'CREATE TABLE extra(value TEXT)',
            'view': 'CREATE VIEW extra AS SELECT revision FROM crp_structured_records',
            'trigger': 'CREATE TRIGGER extra AFTER INSERT ON crp_uow_meta BEGIN SELECT 1; END',
            'application': 'PRAGMA application_id=7',
        }
        with closing(sqlite3.connect(registry.records.database_path)) as connection:
            connection.execute(statements[change])
            connection.commit()
    with pytest.raises(BackupArchiveError, match='scope_invalid'):
        _archive(root, tmp_path)
    assert not (tmp_path / 'backup.zip').exists()


def test_empty_desktop_device_database_does_not_admit_a_shared_or_central_scope(source, tmp_path):
    from backend.memory_app.v2.devices import DeviceRegistry
    root, records = source
    DeviceRegistry(root / 'server').records.list('server_devices')
    with records.begin() as tx:
        tx.put('v2_shared_root', 'identity', {'project_id': 'shared-actual'}, expected_revision=0)
        tx.commit()
    with pytest.raises(BackupArchiveError, match='scope_invalid'):
        _archive(root, tmp_path)
    with pytest.raises(BackupArchiveError, match='scope_invalid'):
        _archive(root, tmp_path / 'shared-scope', scope='shared-project')
    central = tmp_path / 'central'
    DeviceRegistry(central / 'server').records.list('server_devices')
    snapshot = backup_runtime(central, tmp_path / 'central-backups', snapshot_id='snap-central-only')
    with pytest.raises(BackupArchiveError, match='scope_invalid'):
        pack_backup_archive(snapshot_root=snapshot.snapshot_root, destination=tmp_path / 'central.zip',
            scope_kind='personal-runtime', limits=_limits())


def test_personal_attachment_sqlite_and_nonidentity_rows_do_not_define_runtime_scope(source, tmp_path):
    root, records = source
    attachment = SQLiteStructuredRecordStore(root / 'workspace' / 'devices.sqlite3')
    with attachment.begin() as tx:
        tx.put('v2_shared_root', 'identity', {'project_id': 'shared-attachment'}, expected_revision=0)
        tx.commit()
    with records.begin() as tx:
        tx.put('v2_shared_root', 'other', {'project_id': 'shared-other-key'}, expected_revision=0)
        tx.commit()
    archive = _archive(root, tmp_path)
    imported = unpack_backup_archive(archive_path=archive.path, stage_root=tmp_path / 'stage',
        expected_scope='personal-runtime', limits=_limits())
    assert imported.scope_kind == 'personal-runtime'
    assert (imported.snapshot.snapshot_root / 'payload' / 'workspace' / 'devices.sqlite3').is_file()


@pytest.mark.parametrize('project_id', ['daily', '../shared-invalid', 123])
def test_actual_owner_identity_requires_a_legal_shared_project(source, tmp_path, project_id):
    root, records = source
    with records.begin() as tx:
        tx.put('v2_shared_root', 'identity', {'project_id': project_id}, expected_revision=0)
        tx.commit()
    with pytest.raises(ValueError):
        _archive(root, tmp_path, scope='shared-project')
    assert not (tmp_path / 'backup.zip').exists()


def test_archive_output_cannot_modify_its_input_snapshot_or_manifest(source, tmp_path):
    snapshot = backup_runtime(source[0], tmp_path / 'backups', snapshot_id='snap-input-check')
    root = snapshot.snapshot_root
    files = sorted(path.relative_to(root).as_posix() for path in root.rglob('*'))
    manifest = (root / 'vault-backup-manifest.json').read_bytes()
    for destination in (root, root / 'export.zip', root / 'payload' / 'export.zip'):
        with pytest.raises(BackupArchiveError, match='overlaps_snapshot'):
            pack_backup_archive(snapshot_root=root, destination=destination,
                scope_kind='personal-runtime', limits=_limits())
        assert sorted(path.relative_to(root).as_posix() for path in root.rglob('*')) == files
        assert (root / 'vault-backup-manifest.json').read_bytes() == manifest
    pack_backup_archive(snapshot_root=root, destination=tmp_path / 'outside.zip',
        scope_kind='personal-runtime', limits=_limits())
    assert sorted(path.relative_to(root).as_posix() for path in root.rglob('*')) == files
    assert (root / 'vault-backup-manifest.json').read_bytes() == manifest
