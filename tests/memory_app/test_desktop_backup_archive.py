import io
import json
import os
from pathlib import Path
import secrets
import shutil
import time
from datetime import datetime, timedelta, timezone
import zipfile

from fastapi.testclient import TestClient


def test_actual_authenticated_desktop_backup_download_import_and_adoption(tmp_path, monkeypatch):
    root = tmp_path / 'd'
    (root / 'config').mkdir(parents=True)
    shutil.copyfile('config/settings.toml.example', root / 'config/settings.toml')
    (root / 'workspace').mkdir()
    original = root / 'workspace' / 'original.md'
    original.write_text('original at backup')
    credential = secrets.token_urlsafe(40)
    for name, value in {
        'CHRIPTMAS_APP_ROOT': str(root.resolve()),
        'CHRIPTMAS_DESKTOP_SESSION_MODE': 'desktop_production',
        'CHRIPTMAS_DESKTOP_SESSION_SECRET': credential,
        'CHRIPTMAS_DESKTOP_INSTANCE_ID': 'desktop-backup-archive-check',
        'CHRIPTMAS_DESKTOP_NONCE': secrets.token_urlsafe(40),
        'CHRIPTMAS_DESKTOP_PROTOCOL_VERSION': 'desktop-loopback/1',
        'CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        'CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN': 'http://127.0.0.1:18101',
    }.items():
        monkeypatch.setenv(name, value)
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve() == root.resolve()
    from backend.memory_app.app import create_app
    from backend.memory_app.runtime_restore_host import RuntimeRestoreHost
    host = RuntimeRestoreHost(root=root, factory=lambda: create_app(runtime_root=root))
    headers = {'X-Chriptmas-Desktop-Session': credential}
    with TestClient(host, base_url='http://127.0.0.1:18101') as client:
        devices = client.get('/api/v2/devices', headers=headers)
        assert devices.status_code == 200 and devices.json()['mode'] == 'desktop'
        assert (root / 'server/devices.sqlite3').is_file()
        snapshot = client.post('/api/rebuild/memory-snapshots', headers=headers, json={})
        assert snapshot.status_code == 200 and snapshot.json()['verified'] is True, snapshot.text
        snapshot_id = snapshot.json()['snapshot_id']
        download_endpoint = '/api/v2/backups/snapshots/' + snapshot_id + '/download'
        assert client.get(download_endpoint).status_code == 403
        download = client.get(download_endpoint, headers=headers)
        assert download.status_code == 200, download.text[:120] if download.status_code != 200 else ''
        assert int(download.headers['content-length']) == len(download.content)
        assert download.headers['content-disposition'] == f'attachment; filename="chriptmas-backup-{snapshot_id}.zip"'
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            assert 'snapshot/payload/server/devices.sqlite3' in archive.namelist()
        original.write_text('current source before explicit restore')
        upload_endpoint = '/api/v2/backups/imports/desktop-download-import-0001'
        imported = client.put(upload_endpoint, headers={**headers, 'Content-Type': 'application/zip'}, content=download.content)
        assert imported.status_code == 200 and imported.json()['state'] == 'published', imported.text
        imported_id = imported.json()['snapshot_id']
        assert client.get(upload_endpoint, headers=headers).json()['snapshot_id'] == imported_id
        assert original.read_text() == 'current source before explicit restore'
        endpoint = '/api/rebuild/memory-snapshots/' + imported_id
        preview = client.post(endpoint + '/rollback-plan', headers=headers, json={})
        assert preview.status_code == 200, preview.text
        plan = preview.json()
        accepted = client.post(endpoint + '/rollback', headers=headers, json={
            'rollback_id': plan['rollback_id'], 'operation_id': plan['operation_id'], 'confirm': True, 'execute': True})
        assert accepted.status_code == 202, accepted.text
        deadline = time.monotonic() + 30
        while True:
            state = client.get('/api/rebuild/memory-restores/' + plan['operation_id'], headers=headers)
            assert state.status_code == 200, state.text
            status = state.json()
            if status['status'] in {'restored', 'failed'}:
                break
            assert time.monotonic() < deadline, status
            time.sleep(.05)
        assert status['status'] == 'restored' and status['runtime_ready'] is True, status
        assert original.read_text() == 'original at backup'
        assert client.get('/api/v2/backups/snapshots/' + imported_id + '/download', headers=headers).status_code == 200
        (root.parent / 'desktop-archive-result.json').write_text(json.dumps({
            'created_verified': True, 'download_size': len(download.content),
            'filename': download.headers['content-disposition'], 'unauthenticated_status': 403,
            'import_state': imported.json()['state'], 'imported_snapshot_id': imported_id,
            'restore': status, 'original_restored': True}), encoding='utf8')
