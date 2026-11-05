import asyncio
import io
import os
from pathlib import Path
import socket
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import zipfile

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
import pytest
import uvicorn

from backend.shared.deployment import DeploymentLayout, runtime_backup_roots
from core.storage_provider.vault_backup_restore import verify_vault_backup
from backend.memory_app.v2.policies import get
from backend.memory_app.backup_archive import pack_backup_archive


class _Server:
    def __init__(self, app, root, credential):
        self.app, self.root, self._credential = app, root, credential

    def headers(self, target=None):
        return {'Authorization': 'Bearer ' + self._credential,
                **({'X-Chriptmas-Target-User': target} if target else {})}


@pytest.fixture
def server(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp('s') / 'server'
    root.mkdir()
    assigned = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    assert root.is_absolute() and root.is_relative_to(assigned)
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
    monkeypatch.setenv('CHRIPTMAS_SERVER_MASTER_KEY', Fernet.generate_key().decode('ascii'))
    from backend.memory_app.app import create_server_app
    app = create_server_app(layout=DeploymentLayout('server', root / 'users' / 'local-user', root), port=18112)
    registry = app.state.device_registry
    pair = registry.issue_pairing(user_id='local-user', actor='install')
    device = registry.exchange(pair['code'], name='synthetic backup management')
    return _Server(app, root, device['key'])


def _client(server):
    return TestClient(server.app, base_url='http://127.0.0.1:18112', client=('127.0.0.1', 1234))


def _snapshot(client, server, *, target=None):
    headers = server.headers(target)
    assert client.get('/api/v2/settings', headers=headers).status_code == 200
    root = server.app.state.server_context.users.root_for(target or 'local-user')
    note = root / 'workspace' / 'original.md'
    note.parent.mkdir(exist_ok=True)
    note.write_text('synthetic snapshot original')
    (note.parent / 'payload.bin').write_bytes(os.urandom(256 * 1024))
    result = client.post('/api/rebuild/memory-snapshots', headers=headers, json={})
    assert result.status_code == 200 and result.json()['verified'] is True, result.text
    return root, note, result.json()['snapshot_id']


def test_actual_factory_download_import_readback_and_explicit_g01_restore(server):
    with _client(server) as client:
        root, note, snapshot_id = _snapshot(client, server)
        assert client.get('/api/v2/backups/snapshots/' + snapshot_id + '/download').status_code == 401
        response = client.get('/api/v2/backups/snapshots/' + snapshot_id + '/download', headers=server.headers())
        assert response.status_code == 200, response.text[:120] if response.status_code != 200 else ''
        archive = response.content
        assert len(archive) > 128 * 1024 and int(response.headers['content-length']) == len(archive)
        assert response.headers['content-disposition'] == f'attachment; filename="chriptmas-backup-{snapshot_id}.zip"'
        with zipfile.ZipFile(io.BytesIO(archive)) as opened:
            assert 'snapshot/vault-backup-manifest.json' in opened.namelist()
        exports = root / '..rebuild-data-recovery' / 'transfers' / 'exports'
        assert not list(exports.iterdir())
        note.write_text('current source stays until explicit restore')
        identity = 'actual-import-command-0001'
        endpoint = '/api/v2/backups/imports/' + identity
        uploaded = client.put(endpoint, headers={**server.headers(), 'Content-Type': 'application/zip'}, content=archive)
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()['state'] == 'published'
        imported = uploaded.json()['snapshot_id']
        assert imported != snapshot_id
        assert note.read_text() == 'current source stays until explicit restore'
        replayed = client.get(endpoint, headers=server.headers())
        assert replayed.status_code == 200 and replayed.json()['snapshot_id'] == imported
        assert client.put(endpoint, headers={**server.headers(), 'Content-Type': 'application/zip'}, content=archive).json()['snapshot_id'] == imported
        rows = client.get('/api/rebuild/memory-snapshots', headers=server.headers()).json()['snapshots']
        row = next(value for value in rows if value['snapshot_id'] == imported)
        assert row['restorable'] is True and row['verified'] is True
        snapshots = runtime_backup_roots(root)[1]
        assert len(list(snapshots.glob('snap-import-*'))) == 1
        assert verify_vault_backup(snapshots / imported).snapshot_id == imported
        assert not (root / '..rebuild-data-recovery' / 'transfers' / 'imports' / identity).exists()
        rollback = '/api/rebuild/memory-snapshots/' + imported
        plan = client.post(rollback + '/rollback-plan', headers=server.headers(), json={})
        assert plan.status_code == 200, plan.text
        plan = plan.json()
        accepted = client.post(rollback + '/rollback', headers=server.headers(), json={
            'rollback_id': plan['rollback_id'], 'operation_id': plan['operation_id'], 'confirm': True, 'execute': True})
        assert accepted.status_code == 202, accepted.text
        for _attempt in range(1200):
            status = client.get('/api/rebuild/memory-restores/' + plan['operation_id'], headers=server.headers())
            assert status.status_code == 200, status.text
            if status.json()['status'] in {'restored', 'failed'}:
                break
            time.sleep(.05)
        assert status.json()['runtime_ready'] is True, status.text
        assert note.read_text() == 'synthetic snapshot original'
        assert client.get(endpoint, headers=server.headers()).json()['state'] == 'published'


def test_download_obeys_real_zero_and_finite_user_quota_and_counts_zip_extent_once(server):
    with _client(server) as client:
        root, _note, snapshot_id = _snapshot(client, server)
        context = server.app.state.server_context
        actor = context.registry.authenticate(server._credential)
        snapshots = runtime_backup_roots(root)[1]
        before = verify_vault_backup(snapshots / snapshot_id).source_fingerprint
        endpoint = '/api/v2/backups/snapshots/' + snapshot_id + '/download'
        exports = root / '..rebuild-data-recovery/transfers/exports'
        user = context.users.get('local-user')
        context.users.update(actor, 'local-user', expected_revision=user['revision'], storage_limit_mb=0)
        rejected = client.get(endpoint, headers=server.headers())
        assert rejected.status_code == 429 and rejected.json()['detail'] == 'storage_quota_exceeded'
        assert not exports.exists() or not list(exports.iterdir())

        baseline = context.jobs.storage_bytes('local-user')
        limit_mb = (baseline + 1024 * 1024 - 1) // (1024 * 1024) + 1
        user = context.users.get('local-user')
        context.users.update(actor, 'local-user', expected_revision=user['revision'], storage_limit_mb=limit_mb)
        padding = root / 'quota-padding.bin'
        with padding.open('wb') as stream:
            stream.truncate(limit_mb * 1024 * 1024 - baseline - 4096)
        rejected = client.get(endpoint, headers=server.headers())
        assert rejected.status_code == 429 and rejected.json()['detail'] == 'intake_phase_budget_exceeded', rejected.text
        assert not exports.exists() or not list(exports.iterdir())
        assert verify_vault_backup(snapshots / snapshot_id).source_fingerprint == before

        user = context.users.get('local-user')
        context.users.update(actor, 'local-user', expected_revision=user['revision'], storage_limit_mb=None)
        response = client.get(endpoint, headers=server.headers())
        assert response.status_code == 200
        destination = root / 'extent-check.zip'
        async def count_extent():
            async with context.jobs.intake('local-user') as budget:
                cap = budget.reserve_up_to(len(response.content))
                try:
                    archive = pack_backup_archive(snapshot_root=snapshots / snapshot_id, destination=destination,
                        scope_kind='personal-runtime', limits=get('backup_archive', version='@1')({'operation': 'limits'}),
                        consume=cap.consume)
                    assert archive.size_bytes == len(response.content) and cap.remaining == 0
                finally:
                    destination.unlink(missing_ok=True)
                    cap.close()
        client.portal.call(count_extent)
        assert not list(exports.iterdir())


def test_real_tcp_concurrent_downloads_hold_intake_until_disconnect_cleanup(server):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 18112))
    listener.listen()
    host = uvicorn.Server(uvicorn.Config(server.app, host='127.0.0.1', port=18112, log_level='warning'))
    thread = threading.Thread(target=host.run, kwargs={'sockets': [listener]})
    thread.start()
    first = None
    try:
        deadline = time.monotonic() + 20
        while not host.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.025)
        assert host.started and thread.is_alive()
        with httpx.Client(base_url='http://127.0.0.1:18112', timeout=40, trust_env=False) as client:
            headers = server.headers()
            assert client.get('/api/v2/settings', headers=headers).status_code == 200
            initial = client.get('/api/v2/backups/schedule', headers=headers).json()
            setting = {key: initial[key] for key in ('frequency', 'time', 'timezone', 'weekday', 'month_day')}
            setting.update(frequency='off', command_id='tcp-stream-schedule-command', expected_revision=initial['revision'])
            assert client.patch('/api/v2/backups/schedule', headers=headers, json=setting).status_code == 200
            context = server.app.state.server_context
            root = context.users.root_for('local-user')
            workspace = root / 'workspace'
            workspace.mkdir(exist_ok=True)
            (workspace / 'tcp-payload.bin').write_bytes(os.urandom(32 * 1024 * 1024))
            response = client.post('/api/rebuild/memory-snapshots', headers=headers, json={})
            assert response.status_code == 200 and response.json()['verified'] is True, response.text
            snapshot_id = response.json()['snapshot_id']
            snapshot = runtime_backup_roots(root)[1] / snapshot_id
            fingerprint = verify_vault_backup(snapshot).source_fingerprint
            actor = context.registry.authenticate(server._credential)
            user = context.users.get('local-user')
            baseline = context.jobs.storage_bytes('local-user')
            limit_mb = (baseline + 1024 * 1024 - 1) // (1024 * 1024) + 40
            context.users.update(actor, 'local-user', expected_revision=user['revision'], storage_limit_mb=limit_mb)
            endpoint = '/api/v2/backups/snapshots/' + snapshot_id + '/download'
            first = client.send(client.build_request('GET', endpoint, headers=headers), stream=True)
            assert first.status_code == 200
            assert int(first.headers['content-length']) > 32 * 1024 * 1024
            exports = root / '..rebuild-data-recovery/transfers/exports'
            first_stages = list(exports.iterdir())
            assert len(first_stages) == 1
            with ThreadPoolExecutor(max_workers=1) as executor:
                def second_download():
                    with httpx.Client(base_url='http://127.0.0.1:18112', timeout=40, trust_env=False) as second:
                        return second.get(endpoint, headers=headers)
                second = executor.submit(second_download)
                deadline = time.monotonic() + 10
                while context.pool._children['local-user'].active < 2 and not second.done() and time.monotonic() < deadline:
                    time.sleep(.025)
                assert context.pool._children['local-user'].active >= 2
                assert not second.done()
                assert context.jobs._intake_locks['local-user'].locked()
                assert list(exports.iterdir()) == first_stages
                assert context.jobs.storage_bytes('local-user') <= limit_mb * 1024 * 1024
                first.close()
                first = None
                completed = second.result(timeout=35)
                assert completed.status_code == 200, completed.text[:120] if completed.status_code != 200 else ''
                assert len(completed.content) == int(completed.headers['content-length'])
                with zipfile.ZipFile(io.BytesIO(completed.content)) as archive:
                    assert 'snapshot/payload/workspace/tcp-payload.bin' in archive.namelist()
            deadline = time.monotonic() + 10
            while list(exports.iterdir()) and time.monotonic() < deadline:
                time.sleep(.025)
            assert not list(exports.iterdir())
            assert not context.jobs._intake_locks['local-user'].locked()
            assert verify_vault_backup(snapshot).source_fingerprint == fingerprint
    finally:
        if first is not None:
            first.close()
        host.should_exit = True
        thread.join(timeout=20)
        listener.close()
        assert not thread.is_alive()


def test_real_schedule_http_cas_and_command_replay_preserve_the_current_revision(server):
    with _client(server) as client:
        response = client.get('/api/v2/backups/schedule', headers=server.headers())
        assert response.status_code == 200, response.text
        value = response.json()
        body = {key: value[key] for key in ('frequency', 'time', 'timezone', 'weekday', 'month_day')}
        body.update(command_id='schedule-http-command-first', expected_revision=value['revision'], frequency='monthly', month_day=31)
        saved = client.patch('/api/v2/backups/schedule', headers=server.headers(), json=body)
        assert saved.status_code == 200 and saved.json()['state'] == 'saved', saved.text
        assert client.get('/api/v2/backups/schedule/commands/' + body['command_id'], headers=server.headers()).json() == saved.json()
        assert client.patch('/api/v2/backups/schedule', headers=server.headers(), json=body).json() == saved.json()
        rejected = client.patch('/api/v2/backups/schedule', headers=server.headers(), json={**body, 'command_id': 'schedule-http-stale-command'})
        assert rejected.status_code == 409
        current = client.get('/api/v2/backups/schedule', headers=server.headers()).json()
        assert current['revision'] == value['revision'] + 1 and current['month_day'] == 31
        assert client.get('/api/v2/backups/schedule/commands/schedule-http-stale-command', headers=server.headers()).status_code == 404


def test_import_absence_cas_tombstone_rejects_late_put_and_preserves_an_accepted_stream(server):
    with _client(server) as client:
        root, note, snapshot_id = _snapshot(client, server)
        archive = client.get('/api/v2/backups/snapshots/' + snapshot_id + '/download', headers=server.headers()).content
        first = '/api/v2/backups/imports/cancel-unreceived-first'
        assert client.post(first + '/cancel-unreceived', json={'expected_revision': 0}).status_code == 401
        assert client.get(first, headers=server.headers()).status_code == 404
        invalid = client.post(first + '/cancel-unreceived', headers=server.headers(), json={'expected_revision': 1})
        assert invalid.status_code == 400
        assert client.get(first, headers=server.headers()).status_code == 404
        cancelled = client.post(first + '/cancel-unreceived', headers=server.headers(), json={'expected_revision': 0})
        assert cancelled.status_code == 200 and cancelled.json()['state'] == 'cancelled', cancelled.text
        assert client.get(first, headers=server.headers()).json() == cancelled.json()
        late = client.put(first, headers={**server.headers(), 'Content-Type': 'application/zip'}, content=archive)
        assert late.status_code == 200 and late.json() == cancelled.json()
        assert client.post(first + '/cancel-unreceived', headers=server.headers(), json={'expected_revision': 0}).json() == cancelled.json()
        assert not (runtime_backup_roots(root)[1] / 'snap-import-cancel-unreceived-first').exists()
        assert not (root / '..rebuild-data-recovery/transfers/imports/cancel-unreceived-first').exists()
        second = '/api/v2/backups/imports/accepted-stream-second'
        async def accepted_stream():
            waiting, release = asyncio.Event(), asyncio.Event()
            async def body():
                yield archive[:65536]
                waiting.set()
                await release.wait()
                yield archive[65536:]
            transport = httpx.ASGITransport(app=server.app, client=('127.0.0.1', 1234))
            async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:18112') as wire:
                uploading = asyncio.create_task(wire.put(second,
                    headers={**server.headers(), 'Content-Type': 'application/zip'}, content=body()))
                try:
                    await asyncio.wait_for(waiting.wait(), timeout=10)
                    accepted = await wire.get(second, headers=server.headers())
                    assert accepted.status_code == 200 and accepted.json()['state'] == 'receiving'
                    result = await wire.post(second + '/cancel-unreceived', headers=server.headers(), json={'expected_revision': 0})
                    assert result.status_code == 200 and result.json()['state'] == 'receiving', result.text
                finally:
                    release.set()
                return await asyncio.wait_for(uploading, timeout=30)
        published = client.portal.call(accepted_stream)
        assert published.status_code == 200 and published.json()['state'] == 'published', published.text
        again = client.post(second + '/cancel-unreceived', headers=server.headers(), json={'expected_revision': 0})
        assert again.status_code == 200 and again.json()['state'] == 'published'
        assert client.get(second, headers=server.headers()).json()['snapshot_id'] == published.json()['snapshot_id']
        assert note.read_text() == 'synthetic snapshot original'


def test_actual_user_and_admin_scope_and_revoked_uploader_never_publish(server):
    with _client(server) as client:
        created = client.post('/api/v2/server/users', headers=server.headers(), json={'name': 'synthetic import target'})
        assert created.status_code == 201, created.text
        user = created.json()['user_id']
        root, note, snapshot_id = _snapshot(client, server, target=user)
        response = client.get('/api/v2/backups/snapshots/' + snapshot_id + '/download', headers=server.headers(user))
        assert response.status_code == 200, response.text[:120] if response.status_code != 200 else ''
        archive = response.content
        context = server.app.state.server_context
        actor = context.registry.authenticate(server._credential)
        issued = context.registry.issue_pairing(user_id=user, actor=actor.device_id)
        device = context.registry.exchange(issued['code'], name='synthetic import user')
        own = {'Authorization': 'Bearer ' + device['key']}
        assert client.get('/api/v2/backups/imports/other-space-command', headers={**own, 'X-Chriptmas-Target-User': 'local-user'}).status_code == 403
        identity = 'ordinary-import-command'
        result = client.put('/api/v2/backups/imports/' + identity,
            headers={**own, 'Content-Type': 'application/zip'}, content=archive)
        assert result.status_code == 200 and result.json()['state'] == 'published', result.text
        assert client.get('/api/v2/backups/imports/' + identity, headers=server.headers()).status_code == 404
        assert client.get('/api/v2/backups/imports/' + identity, headers=server.headers(user)).json()['state'] == 'published'
        note.write_text('source remains after revoked import')
        failed_id = 'revoked-import-command'
        uploader = context.registry.authenticate(device['key'])
        async def send_revoked():
            async def body():
                yield archive[:65536]
                context.registry.revoke(user, uploader.device_id, expected_revision=uploader.revision)
                yield archive[65536:]
            transport = httpx.ASGITransport(app=server.app, client=('127.0.0.1', 1234))
            async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:18112') as wire:
                return await wire.put('/api/v2/backups/imports/' + failed_id,
                    headers={**own, 'Content-Type': 'application/zip'}, content=body())
        rejected = client.portal.call(send_revoked)
        assert rejected.status_code == 403, rejected.text
        status = client.get('/api/v2/backups/imports/' + failed_id, headers=server.headers(user))
        assert status.status_code == 200 and status.json()['state'] == 'failed', status.text
        assert not (runtime_backup_roots(root)[1] / ('snap-import-' + failed_id)).exists()
        assert note.read_text() == 'source remains after revoked import'


def test_actual_stream_cap_preserves_origin_and_other_request_limits(server):
    with _client(server) as client:
        initial = client.get('/api/v2/backups/schedule', headers=server.headers()).json()
        schedule = {key: initial[key] for key in ('frequency', 'time', 'timezone', 'weekday', 'month_day')}
        schedule.update(frequency='off', command_id='stream-cap-schedule-command', expected_revision=initial['revision'])
        assert client.patch('/api/v2/backups/schedule', headers=server.headers(), json=schedule).status_code == 200
        malformed = client.put('/api/v2/backups/imports/malformed-command',
            headers={**server.headers(), 'Content-Type': 'application/zip'}, content=b'not a ZIP')
        assert malformed.status_code == 400
        assert client.get('/api/v2/backups/imports/malformed-command', headers=server.headers()).json()['state'] == 'failed'
        bad_origin = client.put('/api/v2/backups/imports/origin-check-command',
            headers={**server.headers(), 'Origin': 'http://untrusted.invalid', 'Content-Type': 'application/zip'}, content=b'ZIP')
        assert bad_origin.status_code == 403
        assert client.get('/api/v2/backups/imports/origin-check-command', headers=server.headers()).status_code == 404
        oversized_json = b'{"name":"' + b'x' * (128 * 1024) + b'"}'
        assert client.post('/api/v2/projects', headers={**server.headers(), 'Content-Type': 'application/json'}, content=oversized_json).status_code == 413
        assert client.put('/api/v2/backups/imports/invalid.id',
            headers={**server.headers(), 'Content-Type': 'application/zip'}, content=oversized_json).status_code == 413
        cap = get('backup_archive', version='@1')({'operation': 'limits'})['archive_bytes']
        observed = [0]
        async def send_over_cap():
            async def body():
                chunk = b'0' * (1024 * 1024)
                for _index in range(cap // len(chunk)):
                    observed[0] += len(chunk)
                    yield chunk
                observed[0] += 1
                yield b'0'
            transport = httpx.ASGITransport(app=server.app, client=('127.0.0.1', 1234))
            async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:18112', timeout=120) as wire:
                return await wire.put('/api/v2/backups/imports/actual-stream-cap-command',
                    headers={**server.headers(), 'Content-Type': 'application/zip'}, content=body())
        response = client.portal.call(send_over_cap)
        assert observed[0] == cap + 1
        assert response.status_code == 413 and response.json()['detail'] == 'backup_archive_limit_exceeded', response.text
        result = client.get('/api/v2/backups/imports/actual-stream-cap-command', headers=server.headers())
        assert result.status_code == 200 and result.json()['state'] == 'failed'
        root = server.app.state.server_context.users.root_for('local-user')
        assert not (root / '..rebuild-data-recovery/transfers/imports/actual-stream-cap-command').exists()
        assert not list(runtime_backup_roots(root)[1].glob('snap-import-*'))
