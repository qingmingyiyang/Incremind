import gc
import json
import os
import secrets
import shutil
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.memory_app.backup import backup_runtime, fingerprint_runtime_restore_source
from backend.memory_app.runtime_recovery import (
    _capture_entries,
    adopt_prepared_runtime,
    load_runtime_recovery_operation,
    prepare_runtime_recovery,
)
from backend.shared.deployment import runtime_backup_roots
from backend.vault_recovery_cli import main as recovery_cli_main
from core.storage_provider.vault_backup_restore import create_vault_backup, fingerprint_vault_root
from core.storage_provider.vault_operational_recovery import (
    VaultOperationalRecoveryError,
    _write_operation,
    load_vault_recovery_operation,
    prepare_vault_recovery,
)


def _runtime(root):
    vault = root / '.rebuild-data'
    vault.mkdir(parents=True)
    with closing(sqlite3.connect(vault / 'records.sqlite3')) as database:
        database.executescript('CREATE TABLE notes(value); INSERT INTO notes VALUES (1);')
    (root / 'workspace').mkdir()
    (root / 'workspace' / '原件.md').write_text('备份正文', encoding='utf8')
    (root / 'settings.json').write_text('{"synthetic":1}', encoding='utf8')
    (root / 'logs').mkdir()
    (root / 'logs' / 'trace.log').write_text('initial log', encoding='utf8')
    return root


def _prepare(root, *, operation_id='restore-012345abcdef', snapshot_id='snap-runtime'):
    _working, backups, operations = runtime_backup_roots(root)
    snapshot = backup_runtime(root, backups, snapshot_id=snapshot_id)
    (root / 'workspace' / '原件.md').write_text('当前正文', encoding='utf8')
    (root / 'workspace' / 'later.md').write_text('当前新增原件', encoding='utf8')
    prepared = prepare_runtime_recovery(
        snapshot_root=snapshot.snapshot_root, active_root=root,
        operations_root=operations, operation_id=operation_id,
        expected_source_fingerprint=fingerprint_runtime_restore_source(root),
    )
    return prepared, operations


@pytest.fixture
def ui_factory(tmp_path_factory, monkeypatch):
    area = tmp_path_factory.mktemp('g')
    import_root = area / 'i'
    _configure_root(import_root)
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(import_root))
    from backend.memory_app.app import create_app
    return create_app, area


def _configure_root(root):
    (root / 'config').mkdir(parents=True)
    shutil.copyfile('config/settings.toml.example', root / 'config' / 'settings.toml')


def test_runtime_restore_prepares_a_new_root_then_adopts_all_data_and_preserves_original(tmp_path, capsys):
    root = _runtime(tmp_path / 'runtime')
    operation, operations = _prepare(root)
    assert operation.state == 'prepared'
    assert (Path(operation.staging_root) / 'workspace' / '原件.md').read_text(encoding='utf8') == '备份正文'
    assert (root / 'workspace' / '原件.md').read_text(encoding='utf8') == '当前正文'
    assert not Path(operation.rollback_root).exists()
    assert recovery_cli_main(['--working-root', str(root / '.rebuild-data'), '--operation-id', operation.operation_id]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'adopted'
    adopted = load_runtime_recovery_operation(operations_root=operations, operation_id=operation.operation_id)
    assert adopted.state == 'adopted'
    assert (root / 'workspace' / '原件.md').read_text(encoding='utf8') == '备份正文'
    assert not (root / 'workspace' / 'later.md').exists()
    assert (Path(adopted.rollback_root) / 'workspace' / 'later.md').read_text(encoding='utf8') == '当前新增原件'
    assert (Path(adopted.rollback_root) / 'logs' / 'trace.log').read_text(encoding='utf8') == 'initial log'
    assert Path(adopted.snapshot_root).is_dir()
    assert (operations / f'{adopted.operation_id}.json').is_file()
    with closing(sqlite3.connect(root / '.rebuild-data' / 'records.sqlite3')) as database:
        assert database.execute('SELECT value FROM notes').fetchall() == [(1,)]
    # 完成记录不限制恢复后继续使用，后续启动只继续未完成的操作。
    (root / 'workspace' / 'after.md').write_text('恢复后新增', encoding='utf8')
    assert recovery_cli_main(['--working-root', str(root / '.rebuild-data'), '--recover-pending']) == 0
    assert (root / 'workspace' / 'after.md').read_text(encoding='utf8') == '恢复后新增'


@pytest.mark.parametrize('interruption', ['detach-one', 'detach-all', 'adopt-one'])
def test_startup_reconciles_real_partially_moved_runtime_entries(tmp_path, interruption, capsys):
    root = _runtime(tmp_path / 'runtime')
    operation, operations = _prepare(root)
    rollback, staging = Path(operation.rollback_root), Path(operation.staging_root)
    fingerprint_vault_root(root, exclude_roots=(operations.parent,))
    original = _capture_entries(root, exclude_controls=True)
    rollback.mkdir(parents=True)
    operation = replace(operation, state='adopting', original_entries=original)
    _write_operation(operations, operation)
    for index, entry in enumerate(original):
        operation = replace(operation, intent=f"detach:{entry['name']}")
        _write_operation(operations, operation)
        os.replace(root / entry['name'], rollback / entry['name'])
        if interruption == 'detach-one' and index == 0:
            break
    if interruption == 'adopt-one':
        entry = operation.target_entries[0]
        operation = replace(operation, intent=f"adopt:{entry['name']}")
        _write_operation(operations, operation)
        os.replace(staging / entry['name'], root / entry['name'])
    assert recovery_cli_main(['--working-root', str(root / '.rebuild-data'), '--recover-pending']) == 0
    assert json.loads(capsys.readouterr().out)['operations'][0]['state'] == 'adopted'
    assert (root / 'workspace' / '原件.md').read_text(encoding='utf8') == '备份正文'
    assert (rollback / 'workspace' / 'later.md').read_text(encoding='utf8') == '当前新增原件'


def test_runtime_identity_ignores_management_and_logs_but_rejects_current_data_drift(tmp_path):
    root = _runtime(tmp_path / 'runtime')
    operation, operations = _prepare(root)
    identity = fingerprint_runtime_restore_source(root)
    (operations.parent / 'diagnostic.txt').write_text('control bookkeeping')
    (root / 'logs' / 'trace.log').write_text('new log')
    assert fingerprint_runtime_restore_source(root) == identity
    (root / 'settings.json').write_text('{"synthetic":2}')
    with pytest.raises(VaultOperationalRecoveryError):
        adopt_prepared_runtime(operations_root=operations, operation_id=operation.operation_id,
            working_root=root / '.rebuild-data', application_offline=True)
    assert (root / 'workspace' / '原件.md').read_text(encoding='utf8') == '当前正文'
    assert not Path(operation.rollback_root).exists()
    assert load_runtime_recovery_operation(operations_root=operations, operation_id=operation.operation_id).state == 'failed'


def test_runtime_adoption_rejects_tampered_stage_and_wrong_working_root(tmp_path, capsys):
    root = _runtime(tmp_path / 'runtime')
    operation, operations = _prepare(root)
    with pytest.raises(VaultOperationalRecoveryError, match='working root'):
        adopt_prepared_runtime(operations_root=operations, operation_id=operation.operation_id,
            working_root=tmp_path / 'other' / '.rebuild-data', application_offline=True)
    (Path(operation.staging_root) / 'settings.json').write_text('tampered')
    assert recovery_cli_main(['--working-root', str(root / '.rebuild-data'), '--operation-id', operation.operation_id]) == 2
    assert json.loads(capsys.readouterr().err)['status'] == 'failed'
    assert (root / 'settings.json').read_text() == '{"synthetic":1}'
    assert not Path(operation.rollback_root).exists()


def test_runtime_preparation_is_idempotent_and_blocks_a_second_pending_restore(tmp_path):
    root = _runtime(tmp_path / 'runtime')
    operation, operations = _prepare(root)
    again = prepare_runtime_recovery(snapshot_root=Path(operation.snapshot_root), active_root=root,
        operations_root=operations, operation_id=operation.operation_id,
        expected_source_fingerprint=operation.original_fingerprint)
    assert again == operation
    with pytest.raises(VaultOperationalRecoveryError, match='pending'):
        prepare_runtime_recovery(snapshot_root=Path(operation.snapshot_root), active_root=root,
            operations_root=operations, operation_id='restore-second',
            expected_source_fingerprint=operation.original_fingerprint)


def test_existing_vault_operation_still_uses_its_original_schema_and_root_binding(tmp_path, capsys):
    root = _runtime(tmp_path / 'runtime')
    vault, backups, operations = runtime_backup_roots(root)
    snapshot = create_vault_backup(source_root=vault, backups_root=backups, snapshot_id='snap-legacy')
    operation = prepare_vault_recovery(snapshot_root=snapshot.snapshot_root, active_root=vault,
        operations_root=operations, operation_id='restore-legacy')
    payload = json.loads((operations / f'{operation.operation_id}.json').read_text())
    assert 'kind' not in payload
    assert recovery_cli_main(['--working-root', str(vault), '--operation-id', operation.operation_id]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'adopted'
    assert load_vault_recovery_operation(operations_root=operations, operation_id=operation.operation_id).state == 'adopted'


def test_new_ui_snapshot_can_preview_confirm_restore_and_read_after_restart(tmp_path, capsys, ui_factory):
    create_app, area = ui_factory
    root = area / 'r'
    _configure_root(root)
    (root / 'workspace').mkdir(parents=True)
    note = root / 'workspace' / 'original.md'
    note.write_text('before backup')
    with TestClient(create_app(runtime_root=root)) as client:
        response = client.post('/api/rebuild/memory-snapshots', json={})
        assert response.status_code == 200, response.text
        snapshot_id = response.json()['snapshot_id']
        assert response.json()['verified'] is True
        note.write_text('after backup')
        plan = client.post(f'/api/rebuild/memory-snapshots/{snapshot_id}/rollback-plan', json={})
        assert plan.status_code == 200, plan.text
        assert plan.json()['restore_kind'] == 'runtime'
        prepared = client.post(f'/api/rebuild/memory-snapshots/{snapshot_id}/rollback',
            json={'rollback_id': plan.json()['rollback_id'], 'confirm': True})
        assert prepared.status_code == 202, prepared.text
        assert prepared.json()['operation_id'] == plan.json()['operation_id']
        assert prepared.json()['restore_executed'] is False
        assert str(root) not in prepared.text
        assert client.get('/api/rebuild/memory-snapshots').json()['restore_operations'][-1]['state'] == 'prepared'
        assert note.read_text() == 'after backup'
    gc.collect()
    assert recovery_cli_main(['--working-root', str(root / '.rebuild-data'), '--operation-id', prepared.json()['operation_id']]) == 0
    capsys.readouterr()
    assert note.read_text() == 'before backup'
    with TestClient(create_app(runtime_root=root)) as restarted:
        snapshots = restarted.get('/api/rebuild/memory-snapshots')
        assert snapshots.status_code == 200, snapshots.text
        receipt = snapshots.json()['restore_operations'][-1]
        assert receipt['state'] == 'adopted' and receipt['restore_executed'] is True
        assert snapshots.json()['snapshots'][-1]['snapshot_id'] == snapshot_id


def test_ui_confirmation_rejects_source_changes_without_preparing_restore(tmp_path, ui_factory):
    create_app, area = ui_factory
    root = area / 'r'
    _configure_root(root)
    with TestClient(create_app(runtime_root=root)) as client:
        snapshot_id = client.post('/api/rebuild/memory-snapshots', json={}).json()['snapshot_id']
        plan = client.post(f'/api/rebuild/memory-snapshots/{snapshot_id}/rollback-plan', json={}).json()
        (root / 'new-original.md').write_text('new source after confirmation plan')
        response = client.post(f'/api/rebuild/memory-snapshots/{snapshot_id}/rollback',
            json={'rollback_id': plan['rollback_id'], 'confirm': True})
        assert response.status_code == 200
        assert response.json()['status'] == 'rejected'
        assert response.json()['restore_executed'] is False
        assert client.get('/api/rebuild/memory-snapshots').json()['restore_operations'] == []


def _wait_host_restore(client, operation_id, host=None):
    for _attempt in range(300):
        response = client.get(f'/api/rebuild/memory-restores/{operation_id}')
        if response.status_code != 200:
            raise AssertionError(str(host._status_failure) if host is not None else response.text)
        value = response.json()
        if value['status'] in {'restored', 'failed'}:
            return value
        time.sleep(.05)
    raise AssertionError('runtime host did not finish: ' + json.dumps(value))


def test_browser_confirm_retires_real_application_adopts_and_rebuilds_before_success(ui_factory):
    from backend.memory_app.runtime_restore_host import RuntimeRestoreHost
    create_app, area = ui_factory
    root = area / 'r'
    _configure_root(root)
    (root / 'workspace').mkdir()
    note = root / 'workspace' / 'original.md'
    note.write_text('snapshot original')
    host = RuntimeRestoreHost(root=root, factory=lambda: create_app(runtime_root=root))
    with TestClient(host) as client:
        before = host._current
        created = client.post('/api/rebuild/memory-snapshots', json={})
        assert created.status_code == 200, created.text
        snapshot_id = created.json()['snapshot_id']
        note.write_text('current original')
        plan = client.post(f'/api/rebuild/memory-snapshots/{snapshot_id}/rollback-plan', json={})
        assert plan.status_code == 200, plan.text
        response = client.post(f'/api/rebuild/memory-snapshots/{snapshot_id}/rollback', json={
            'rollback_id': plan.json()['rollback_id'], 'confirm': True, 'execute': True,
        })
        assert response.status_code == 202, response.text
        assert response.json()['status'] == 'restore_queued' and response.json()['restore_executed'] is False
        result = _wait_host_restore(client, response.json()['operation_id'], host)
        if result['status'] != 'restored':
            raise AssertionError(str(host._failure.__cause__) if host._failure else json.dumps(result))
        assert result['restore_executed'] is True and result['runtime_ready'] is True
        assert host._current is not before
        assert note.read_text() == 'snapshot original'
        snapshots = client.get('/api/rebuild/memory-snapshots')
        assert snapshots.status_code == 200, snapshots.text
        assert snapshots.json()['restore_operations'][-1]['runtime_ready'] is True
        operation = load_runtime_recovery_operation(operations_root=host.operations, operation_id=result['operation_id'])
        assert (Path(operation.rollback_root) / 'workspace' / 'original.md').read_text() == 'current original'


def test_browser_restore_reports_real_factory_failure_and_keeps_original_copy(ui_factory):
    from backend.memory_app.runtime_restore_host import RuntimeRestoreHost
    create_app, area = ui_factory
    root = area / 'r'
    _configure_root(root)
    host = RuntimeRestoreHost(root=root, factory=lambda: create_app(runtime_root=root))
    settings = root / 'config' / 'settings.toml'
    valid = settings.read_text(encoding='utf8')
    with TestClient(host) as client:
        settings.write_text('invalid = [', encoding='utf8')
        created = client.post('/api/rebuild/memory-snapshots', json={})
        assert created.status_code == 200, created.text
        snapshot_id = created.json()['snapshot_id']
        settings.write_text(valid, encoding='utf8')
        plan = client.post(f'/api/rebuild/memory-snapshots/{snapshot_id}/rollback-plan', json={})
        assert plan.status_code == 200, plan.text
        response = client.post(f'/api/rebuild/memory-snapshots/{snapshot_id}/rollback', json={
            'rollback_id': plan.json()['rollback_id'], 'confirm': True, 'execute': True,
        })
        assert response.status_code == 202, response.text
        result = _wait_host_restore(client, response.json()['operation_id'])
        assert result['status'] == 'failed' and result['runtime_ready'] is False
        assert client.get('/api/rebuild/memory-snapshots').status_code == 503
        operation = load_runtime_recovery_operation(operations_root=host.operations, operation_id=result['operation_id'])
        assert operation.state == 'adopted' and operation.activation_state == 'failed'
        assert result['requires_restart'] is True
        assert (Path(operation.rollback_root) / 'config' / 'settings.toml').read_text(encoding='utf8') == valid
        assert Path(operation.snapshot_root).is_dir()
        settings.write_text(valid, encoding='utf8')
        resumed = client.post(f"/api/rebuild/memory-restores/{operation.operation_id}/resume", json={'confirm': True})
        assert resumed.status_code == 202, resumed.text
        ready = _wait_host_restore(client, operation.operation_id, host)
        assert ready['status'] == 'restored' and ready['runtime_ready'] is True
        assert ready['requires_restart'] is False
        assert client.get('/api/rebuild/memory-snapshots').status_code == 200
        assert len(list(host.operations.glob('*.json'))) == 1


def test_stable_preview_identity_recovers_an_unobserved_preparation_without_another_operation(ui_factory):
    from backend.memory_app.runtime_restore_host import RuntimeRestoreHost
    create_app, area = ui_factory
    root = area / 'r'
    _configure_root(root)
    host = RuntimeRestoreHost(root=root, factory=lambda: create_app(runtime_root=root))
    with TestClient(host) as client:
        created = client.post('/api/rebuild/memory-snapshots', json={})
        assert created.status_code == 200, created.text
        snapshot_id = created.json()['snapshot_id']
        plan = client.post(f'/api/rebuild/memory-snapshots/{snapshot_id}/rollback-plan', json={}).json()
        operation_id = plan['operation_id']
        missing = client.get(f'/api/rebuild/memory-restores/{operation_id}')
        assert missing.status_code == 404 and missing.json()['detail'] == 'restore_operation_not_found'
        client.post(f'/api/rebuild/memory-snapshots/{snapshot_id}/rollback',
            json={'rollback_id': plan['rollback_id'], 'confirm': True})
        observed = client.get(f'/api/rebuild/memory-restores/{operation_id}')
        assert observed.status_code == 200 and observed.json()['state'] == 'prepared'
        resumed = client.post(f'/api/rebuild/memory-restores/{operation_id}/resume', json={'confirm': True})
        assert resumed.status_code == 202, resumed.text
        ready = _wait_host_restore(client, operation_id, host)
        assert ready['status'] == 'restored'
        assert len(list(host.operations.glob('*.json'))) == 1
        assert client.post(f'/api/rebuild/memory-restores/{operation_id}/resume', json={'confirm': True}).status_code == 409


def test_prepared_source_drift_fails_without_adoption_and_allows_a_new_preview(ui_factory):
    from backend.memory_app.runtime_restore_host import RuntimeRestoreHost
    create_app, area = ui_factory
    root = area / 'r'
    _configure_root(root)
    (root / 'workspace').mkdir()
    note = root / 'workspace' / 'original.md'
    note.write_text('backup original')
    host = RuntimeRestoreHost(root=root, factory=lambda: create_app(runtime_root=root))
    with TestClient(host) as client:
        snapshot_id = client.post('/api/rebuild/memory-snapshots', json={}).json()['snapshot_id']
        endpoint = f'/api/rebuild/memory-snapshots/{snapshot_id}'
        first = client.post(endpoint + '/rollback-plan', json={}).json()
        prepared = client.post(endpoint + '/rollback', json={'rollback_id': first['rollback_id'], 'confirm': True})
        assert prepared.status_code == 202, prepared.text
        original_operation = load_runtime_recovery_operation(operations_root=host.operations, operation_id=first['operation_id'])
        note.write_text('changed after preparation')
        refused = client.post(f"/api/rebuild/memory-restores/{first['operation_id']}/resume", json={'confirm': True})
        assert refused.status_code == 409
        status = client.get(f"/api/rebuild/memory-restores/{first['operation_id']}")
        assert status.status_code == 200, status.text
        assert status.json()['state'] == 'failed' and status.json()['restore_executed'] is False
        assert status.json()['requires_restart'] is False
        assert Path(original_operation.staging_root).is_dir()
        assert not Path(original_operation.rollback_root).exists()
        assert note.read_text() == 'changed after preparation'
        assert client.get('/api/rebuild/memory-snapshots').status_code == 200
        second = client.post(endpoint + '/rollback-plan', json={})
        assert second.status_code == 200, second.text
        second = second.json()
        assert second['operation_id'] != first['operation_id']
        accepted = client.post(endpoint + '/rollback', json={
            'rollback_id': second['rollback_id'], 'confirm': True, 'execute': True})
        assert accepted.status_code == 202, accepted.text
        ready = _wait_host_restore(client, second['operation_id'], host)
        assert ready['status'] == 'restored'
        assert note.read_text() == 'backup original'


def test_host_control_status_keeps_real_desktop_session_authentication_and_cors(ui_factory, monkeypatch):
    from backend.memory_app.runtime_restore_host import RuntimeRestoreHost
    create_app, area = ui_factory
    root = area / 'r'
    _configure_root(root)
    credential = secrets.token_urlsafe(40)
    for name, value in {
        'CHRIPTMAS_DESKTOP_SESSION_MODE': 'desktop_production',
        'CHRIPTMAS_DESKTOP_SESSION_SECRET': credential,
        'CHRIPTMAS_DESKTOP_INSTANCE_ID': 'synthetic-restore-host',
        'CHRIPTMAS_DESKTOP_NONCE': secrets.token_urlsafe(40),
        'CHRIPTMAS_DESKTOP_PROTOCOL_VERSION': 'desktop-loopback/1',
        'CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        'CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN': 'http://127.0.0.1:18101',
    }.items():
        monkeypatch.setenv(name, value)
    headers = {'X-Chriptmas-Desktop-Session': credential}
    host = RuntimeRestoreHost(root=root, factory=lambda: create_app(runtime_root=root))
    with TestClient(host) as client:
        assert client.get('/api/rebuild/memory-snapshots').status_code == 403
        created = client.post('/api/rebuild/memory-snapshots', json={}, headers=headers)
        assert created.status_code == 200, created.text
        snapshot_id = created.json()['snapshot_id']
        plan = client.post(f'/api/rebuild/memory-snapshots/{snapshot_id}/rollback-plan', json={}, headers=headers)
        assert plan.status_code == 200, plan.text
        prepared = client.post(f'/api/rebuild/memory-snapshots/{snapshot_id}/rollback',
            json={'rollback_id': plan.json()['rollback_id'], 'confirm': True}, headers=headers)
        assert prepared.status_code == 202, prepared.text
        endpoint = '/api/rebuild/memory-restores/' + prepared.json()['operation_id']
        assert client.get(endpoint).status_code == 403
        status = client.get(endpoint, headers=headers)
        assert status.status_code == 200 and status.json()['restore_executed'] is False
        cors = client.options(endpoint, headers={
            'Origin': 'http://127.0.0.1:18101', 'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'X-Chriptmas-Desktop-Session',
        })
        assert cors.status_code == 200
        assert cors.headers['access-control-allow-origin'] == 'http://127.0.0.1:18101'
