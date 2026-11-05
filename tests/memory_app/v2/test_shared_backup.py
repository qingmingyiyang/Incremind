import asyncio
import os
from pathlib import Path
import shutil
import sqlite3
from threading import RLock
import threading
from uuid import uuid4
import zipfile

import pytest

def test_strict_archive_checks_payload_against_fixed_snapshot():
    allocated = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    assert not any(path.name == 'runtime' for path in (allocated, *allocated.parents))
    root = allocated / ('strict-' + uuid4().hex[:8])
    root.mkdir()
    from backend.memory_app.backup import backup_runtime
    from backend.memory_app.backup_archive import BackupArchiveError, pack_backup_archive, verify_backup_archive
    from backend.memory_app.v2.policies import get
    from backend.memory_app.v2.shared_runtime import SharedDataRoots

    roots = SharedDataRoots(root)
    project_id = 'shared-' + uuid4().hex
    roots.provision(project_id, '严格归档正文检查')
    source = roots.path(project_id)
    (source / 'workspace').mkdir()
    (source / 'workspace/original.txt').write_bytes(b'original00')
    snapshot = backup_runtime(source, root / 'snapshots', snapshot_id='strict-snapshot')
    limits = get('backup_archive', version='@1')({'operation': 'limits'})
    archive = pack_backup_archive(snapshot_root=snapshot.snapshot_root, destination=root / 'archive.zip',
        scope_kind='shared-project', limits=limits)
    assert verify_backup_archive(archive_path=archive.path, snapshot_root=snapshot.snapshot_root,
        expected_scope='shared-project', expected_fingerprint=snapshot.source_fingerprint, limits=limits) == archive
    with pytest.raises(BackupArchiveError, match='backup_archive_source_changed'):
        verify_backup_archive(archive_path=archive.path, snapshot_root=snapshot.snapshot_root,
            expected_scope='shared-project', expected_fingerprint='0' * 64, limits=limits)
    altered = root / 'altered.zip'
    with zipfile.ZipFile(archive.path) as original, zipfile.ZipFile(altered, 'w') as target:
        for entry in original.infolist():
            body = original.read(entry)
            if entry.filename == 'snapshot/payload/workspace/original.txt':
                body = b'changed000'
            target.writestr(entry, body)
    with zipfile.ZipFile(altered) as archive_check:
        assert archive_check.testzip() is None
    with pytest.raises(BackupArchiveError, match='backup_archive_source_changed'):
        verify_backup_archive(archive_path=altered, snapshot_root=snapshot.snapshot_root,
            expected_scope='shared-project', expected_fingerprint=snapshot.source_fingerprint, limits=limits)


def test_shared_viewer_backup_real_zip_cold_read_and_revocation():
    allocated = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    assert not any(path.name == 'runtime' for path in (allocated, *allocated.parents))
    root = allocated / ('backup-' + uuid4().hex[:8])
    (root / 'config').mkdir(parents=True)
    root = root.resolve(strict=True)
    assert root.is_relative_to(allocated)
    repo = Path(__file__).resolve().parents[3]
    shutil.copyfile(repo / 'config/settings.toml.example', root / 'config/settings.toml')
    previous = {key: os.environ.get(key) for key in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ['CHRIPTMAS_APP_ROOT'], os.environ['CHRIPTMAS_DEPLOY'] = str(root), 'server'
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) == root
    try:
        _check_server(root)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _rows(path):
    with sqlite3.connect(path) as connection:
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: connection.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid').fetchall()
                for name in names}


def _corrupt_body_same_metadata(path):
    information = path.stat()
    original = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        entry = next(entry for entry in archive.infolist() if entry.filename.startswith('snapshot/payload/')
                     and entry.compress_size > 2)
        offset = entry.header_offset + len(entry.FileHeader(zip64=True)) + entry.compress_size // 2
    with path.open('r+b') as stream:
        stream.seek(offset)
        stream.write(bytes([original[offset] ^ 1]))
    os.utime(path, ns=(information.st_atime_ns, information.st_mtime_ns))
    changed = path.stat()
    assert (changed.st_dev, changed.st_ino, changed.st_size, changed.st_mtime_ns) == (
        information.st_dev, information.st_ino, information.st_size, information.st_mtime_ns)
    return original, information


def _started_request(client, server, headers, project_id, key, *, mutate=None, cancel=False):
    from httpx import ASGITransport, AsyncClient

    started, release = threading.Event(), threading.Event()
    previous = threading.getprofile()

    def observe(frame, event, _argument):
        if (event == 'call' and frame.f_code.co_name == '_finish'
                and Path(frame.f_code.co_filename).name == 'shared_backup.py'
                and frame.f_locals['row'].object_id == key):
            started.set()
            if not release.wait(90):
                raise AssertionError('actual_archive_observation_timed_out')

    async def invoke():
        async with AsyncClient(transport=ASGITransport(app=server), base_url='http://127.0.0.1:8012') as http:
            request = asyncio.create_task(http.post('/api/v2/server/shared-projects/' + project_id + '/backups',
                json={}, headers={**headers, 'Idempotency-Key': key}))
            try:
                assert await asyncio.to_thread(started.wait, 90)
                if cancel:
                    request.cancel()
                    await asyncio.sleep(.05)
                    assert not request.done()
                if mutate is not None:
                    mutate()
            finally:
                release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await request
                return None
            return await request

    threading.setprofile_all_threads(observe)
    try:
        return client.portal.call(invoke)
    finally:
        release.set()
        threading.setprofile_all_threads(previous)


def _check_server(root):
    from fastapi.testclient import TestClient
    from backend.memory_app.app import create_server_app
    from backend.memory_app.backup_archive import BackupArchiveError, unpack_backup_archive
    from backend.memory_app.backup import restore_runtime
    from backend.memory_app.v2.shared_backup import COMMANDS
    from backend.memory_app.v2.privacy import set_private_project
    from backend.memory_app.v2.projects import assign_scene
    from backend.memory_app.workspace_items import WorkspaceItems
    from backend.recognition import RecognitionService, WorkScope
    from backend.shared.deployment import DeploymentLayout, runtime_backup_roots
    from core.document_engine import DocumentDraft

    layout = DeploymentLayout('server', root / 'users/local-user', root)
    server = create_server_app(layout=layout, port=8012)
    devices, users = server.state.device_registry, server.state.server_users
    admin_pair = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='备份检查管理员')
    admin = devices.authenticate(admin_pair['key'])
    actors, headers = [], []
    for name in ('备份所有者', '备份只看成员', '备份无权限账号'):
        user = users.create(admin, name=name)
        pair = devices.exchange(devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)['code'], name=name)
        actors.append(devices.authenticate(pair['key']))
        headers.append({'Authorization': 'Bearer ' + pair['key']})
    owner, viewer, outsider = actors
    shared = server.state.shared_projects
    project = shared.create(owner, project_name='共享完整备份QA', operation_id='create-' + uuid4().hex)['project']
    shared.invite(owner, project['id'], user_id=viewer.user_id, role='viewer', authority=project['authority'],
                  operation_id='invite-' + uuid4().hex)
    invitation = shared.invitations(viewer)[0]
    shared.accept(viewer, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-' + uuid4().hex)
    data = shared.roots._data(project['id'])
    service = RecognitionService(data.records)
    scope = WorkScope('local-user', project['id'])
    experience = service.stage_experience(scope=scope, content='共享合成原件与依据。')
    candidate = service.propose(scope=scope, content='共享备份合成认识。', source_experience_ids=[experience])
    insight = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer=owner.user_id)
    set_private_project(data.records, project['id'], True, 0)
    original = data.root / 'originals' / 'qa-original.txt'
    original.parent.mkdir(parents=True)
    original.write_text('共享合成文件原文。', encoding='utf-8')
    item = WorkspaceItems(data.records, None, RLock()).create(project['id'], 'file', '共享合成文件',
        '共享合成文件原文。', original_path=str(original), original_name=original.name)
    document = data.documents.create(DocumentDraft(title='共享合成整理稿', document_type='note', markdown='共享整理正文。',
        project_id=project['id'], source_refs=({'source_id': item['id'], 'locator': 'workspace://' + item['id']},)))
    assign_scene(data.records, 'document', document['id'], project['id'], '合成核验场景')
    for user in (owner, viewer, outsider):
        private = users.root_for(user.user_id) / 'qa-personal-private.txt'
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_text('不得进入共享备份的个人合成内容。', encoding='utf-8')
    options = {'base_url': 'http://127.0.0.1:8012', 'client': ('127.0.0.1', 14001)}
    base = '/api/v2/server/shared-projects'
    key = 'viewer-backup-' + uuid4().hex[:12]
    with TestClient(server, **options) as client:
        for account_headers in headers:
            current = client.get('/api/v2/backups/schedule', headers=account_headers)
            assert current.status_code == 200
            settings = current.json()
            changed = client.patch('/api/v2/backups/schedule', headers=account_headers, json={
                **{field: settings[field] for field in ('time', 'timezone', 'weekday', 'month_day')},
                'frequency': 'off', 'expected_revision': settings['revision'],
                'command_id': 'qa-schedule-' + uuid4().hex})
            assert changed.status_code == 200
        users.update(admin, viewer.user_id, expected_revision=users.get(viewer.user_id)['revision'], storage_limit_mb=0)
        denied = client.post(base + '/' + project['id'] + '/backups', json={}, headers={
            **headers[1], 'Idempotency-Key': 'quota-denied-backup'})
        assert denied.status_code == 429
        assert client.get(base + '/backup-commands/quota-denied-backup', headers=headers[1]).status_code == 404
        users.update(admin, viewer.user_id, expected_revision=users.get(viewer.user_id)['revision'], storage_limit_mb=None)
        response = client.post(base + '/' + project['id'] + '/backups', json={}, headers={**headers[1], 'Idempotency-Key': key})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['state'] == 'ready' and result['scope'] == 'shared-project'
        assert result['command_id'] == key
        repeated = client.post(base + '/' + project['id'] + '/backups', json={}, headers={**headers[1], 'Idempotency-Key': key})
        assert repeated.status_code == 200 and repeated.json() == result
        assert client.get(base + '/backup-commands/' + key, headers=headers[1]).json() == result
        listing = client.get(base + '/' + project['id'] + '/backups', headers=headers[1])
        assert listing.status_code == 200 and listing.json()['items'] == [result]
        assert client.get(base + '/backup-commands/' + key, headers=headers[0]).status_code == 404
        assert client.post(base + '/' + project['id'] + '/backups', json={}, headers={**headers[2], 'Idempotency-Key': 'outsider-backup'}).status_code == 403
        assert client.post(base + '/' + project['id'] + '/backups', json={}, headers=headers[1]).status_code == 400
        assert client.post(base + '/' + project['id'] + '/backups', json={}, headers={
            'Authorization': 'Bearer ' + admin_pair['key'], 'x-chriptmas-target-user': viewer.user_id,
            'Idempotency-Key': 'admin-target-backup'}).status_code == 403
        download = client.get(base + '/backups/' + key + '/download', headers=headers[1])
        assert download.status_code == 200 and download.headers['content-type'] == 'application/zip'
        archive = root / 'downloaded.zip'
        archive.write_bytes(download.content)
        imported = unpack_backup_archive(archive_path=archive, stage_root=root / 'unpacked',
            expected_scope='shared-project', limits=server.state.shared_backups.limits)
        with pytest.raises(BackupArchiveError, match='backup_archive_scope_invalid'):
            unpack_backup_archive(archive_path=archive, stage_root=root / 'wrong-scope',
                expected_scope='personal-runtime', limits=server.state.shared_backups.limits)
        payload = imported.snapshot.snapshot_root / 'payload'
        assert (payload / 'originals/qa-original.txt').read_text(encoding='utf-8') == original.read_text(encoding='utf-8')
        assert not any(path.name in {'qa-personal-private.txt', 'ai-turns.sqlite3', 'secrets.json', 'users', 'server'}
                       for path in payload.rglob('*'))
        restored = restore_runtime(imported.snapshot.snapshot_root, root / 'restore-check')
        database = Path(data.records.database_path).relative_to(data.root)
        assert _rows(payload / database) == _rows(restored.target_root / database)
        with sqlite3.connect(restored.target_root / database) as connection:
            assert connection.execute('SELECT count(*) FROM crp_structured_records').fetchone()[0] > 0
        assert service.get_recognition(scope=scope, recognition_id=insight.id).revision == insight.revision
        assert data.documents.markdown(document['id'], revision=1) == '共享整理正文。'
        assert len([row for row in shared.roots.activity(project['id']) if row['action'] == 'backup']) == 1
        child = server.state.server_user_pool._children[viewer.user_id].application
        assert len(child.state.recognition_records.list(COMMANDS)) == 1
        assert not (runtime_backup_roots(users.root_for(viewer.user_id))[1] / result['snapshot_id']).exists()
        from backend.memory_app.v2.shared_extraction import PersonalDestination
        destination = PersonalDestination.from_application(shared, viewer, child)
        backup_owner = server.state.shared_backups
        saved_zip = backup_owner._area(destination, key) / 'archive.zip'
        clean, info = _corrupt_body_same_metadata(saved_zip)
        assert client.get(base + '/backups/' + key + '/download', headers=headers[1]).status_code in {400, 409}
        with saved_zip.open('r+b') as stream:
            stream.write(clean)
        os.utime(saved_zip, ns=(info.st_atime_ns, info.st_mtime_ns))
        assert client.get(base + '/backups/' + key + '/download', headers=headers[1]).content == download.content
        for operation in ('missing', 'changed'):
            failed_key = 'archive-' + operation + '-' + uuid4().hex[:8]
            failed_zip = backup_owner._area(destination, failed_key) / 'archive.zip'

            def mutate(path=failed_zip, mode=operation):
                if mode == 'missing':
                    path.unlink()
                else:
                    _corrupt_body_same_metadata(path)

            failed = _started_request(client, server, headers[1], project['id'], failed_key, mutate=mutate)
            assert failed.status_code in {400, 409}
            actual = child.state.recognition_records.read(COMMANDS, failed_key)
            assert actual.payload['state'] == 'archived'
            replay = client.post(base + '/' + project['id'] + '/backups', json={}, headers={
                **headers[1], 'Idempotency-Key': failed_key})
            assert replay.status_code in {400, 409}
            if operation == 'missing':
                assert not failed_zip.exists()
            assert not any(row['object_id'] == failed_key for row in shared.roots.activity(project['id']))
        interrupted_key = 'disconnect-' + uuid4().hex[:8]
        _started_request(client, server, headers[1], project['id'], interrupted_key, cancel=True)
        recovered = client.get(base + '/backup-commands/' + interrupted_key, headers=headers[1])
        assert recovered.status_code == 200 and recovered.json()['state'] == 'ready'
        completed = child.state.recognition_records.read(COMMANDS, interrupted_key)
        recovered_zip = backup_owner._area(destination, interrupted_key) / 'archive.zip'
        recovered_identity = recovered_zip.stat()
        assert completed.payload['archive_identity'] == [recovered_identity.st_dev, recovered_identity.st_ino,
            recovered_identity.st_size, recovered_identity.st_mtime_ns]
        assert client.get(base + '/backups/' + interrupted_key + '/download', headers=headers[1]).status_code == 200
        assert len([row for row in shared.roots.activity(project['id']) if row['object_id'] == interrupted_key]) == 1
    restarted = create_server_app(layout=layout, port=8012)
    with TestClient(restarted, **options) as client:
        assert client.get(base + '/backup-commands/' + key, headers=headers[1]).json() == result
        assert client.get(base + '/backups/' + key + '/download', headers=headers[1]).content == download.content
        shared = restarted.state.shared_projects
        detail = shared.detail(owner, project['id'])
        member = next(member for member in detail['members'] if member['user_id'] == viewer.user_id)
        shared.change_member(owner, project['id'], user_id=viewer.user_id, action='remove', role=None,
            expected_revision=member['revision'], authority=detail['project']['authority'], operation_id='remove-' + uuid4().hex)
        assert client.get(base + '/backup-commands/' + key, headers=headers[1]).json() == result
        assert client.get(base + '/backups/' + key + '/download', headers=headers[1]).content == download.content
        assert result in client.get(base + '/' + project['id'] + '/backups', headers=headers[1]).json()['items']
        assert client.post(base + '/' + project['id'] + '/backups', json={}, headers={
            **headers[1], 'Idempotency-Key': 'revoked-new-backup'}).status_code == 403
