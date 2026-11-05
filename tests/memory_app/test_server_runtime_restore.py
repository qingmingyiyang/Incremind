import asyncio
from pathlib import Path
from dataclasses import replace
import json
import os
import signal
import subprocess
import sys
import threading
import time

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from backend.shared.deployment import DeploymentLayout, runtime_backup_roots
from backend.security.user_context import authorize_user
from backend.memory_app.runtime_recovery import (
    load_runtime_recovery_operation, prepare_runtime_recovery, queue_runtime_recovery,
)
from backend.vault_recovery_cli import _reconcile_dead_lease
from core.storage_provider.vault_operational_recovery import VaultOperationalRecoveryError, _write_operation


class _ServerFixture:
    def __init__(self, app, factory, root, credential):
        self.app, self.factory, self.root, self._credential = app, factory, root, credential

    def headers(self, target=None):
        return {'Authorization': 'Bearer ' + self._credential,
                **({'X-Chriptmas-Target-User': target} if target else {})}


@pytest.fixture
def server(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp('s')
    assert root.is_absolute() and root.is_dir()
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
    monkeypatch.setenv('CHRIPTMAS_SERVER_MASTER_KEY', Fernet.generate_key().decode('ascii'))
    from backend.memory_app.app import create_server_app
    layout = DeploymentLayout('server', root / 'users' / 'local-user', root)
    def factory():
        return create_server_app(layout=layout, port=18101)
    app = factory()
    registry = app.state.device_registry
    pairing = registry.issue_pairing(user_id='local-user', actor='install')
    device = registry.exchange(pairing['code'], name='synthetic restore admin')
    return _ServerFixture(app, factory, root, device['key'])


def _client(app):
    return TestClient(app, base_url='http://127.0.0.1:18101', client=('127.0.0.1', 1234))


def _wait(client, operation_id, headers):
    for _attempt in range(600):
        response = client.get('/api/rebuild/memory-restores/' + operation_id, headers=headers)
        assert response.status_code == 200, response.text
        value = response.json()
        if value['status'] in {'restored', 'failed'}:
            return value
        time.sleep(.05)
    raise AssertionError('server restore did not finish')


def test_actual_server_restores_selected_user_and_keeps_other_user_and_audit(server):
    context = server.app.state.server_context
    with _client(server.app) as client:
        created = client.post('/api/v2/server/users', headers=server.headers(), json={'name': 'restore target'})
        assert created.status_code == 201, created.text
        user_id = created.json()['user_id']
        selected = server.headers(user_id)
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        assert client.get('/api/v2/settings', headers=selected).status_code == 200
        own_root, target_root = context.users.root_for('local-user'), context.users.root_for(user_id)
        own = own_root / 'workspace' / 'control.md'
        own.parent.mkdir(exist_ok=True)
        own.write_text('other user is unchanged')
        original = target_root / 'workspace' / 'original.md'
        original.parent.mkdir(exist_ok=True)
        original.write_text('snapshot target original')
        own_child = context.pool._children['local-user'].application
        old_target = context.pool._children[user_id].application
        backup = client.post('/api/rebuild/memory-snapshots', headers=selected, json={})
        assert backup.status_code == 200 and backup.json()['verified'] is True, backup.text
        snapshot_id = backup.json()['snapshot_id']
        original.write_text('current target original')
        endpoint = '/api/rebuild/memory-snapshots/' + snapshot_id
        plan = client.post(endpoint + '/rollback-plan', headers=selected, json={})
        assert plan.status_code == 200, plan.text
        plan = plan.json()
        accepted = client.post(endpoint + '/rollback', headers=selected, json={
            'rollback_id': plan['rollback_id'], 'operation_id': plan['operation_id'], 'confirm': True, 'execute': True})
        assert accepted.status_code == 202, accepted.text
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        ready = _wait(client, plan['operation_id'], selected)
        assert ready['status'] == 'restored' and ready['runtime_ready'] is True, ready
        assert original.read_text() == 'snapshot target original'
        assert own.read_text() == 'other user is unchanged'
        assert context.pool._children['local-user'].application is own_child
        assert context.pool._children[user_id].application is not old_target
        operation = load_runtime_recovery_operation(operations_root=runtime_backup_roots(target_root)[2], operation_id=plan['operation_id'])
        assert (Path(operation.rollback_root) / 'workspace' / 'original.md').read_text() == 'current target original'
        assert operation.server_identity['target_user_id'] == user_id
        intents = [row for row in context.users.records.list('admin_audit') if row.payload.get('method') == 'RESTORE']
        assert len(intents) == 1 and intents[0].payload['target_user_id'] == user_id
        outcomes = [row for row in context.users.records.list('admin_audit') if row.payload.get('intent_id') == intents[0].object_id]
        assert len(outcomes) == 1 and outcomes[0].payload['status'] == 200
        issued = context.registry.issue_pairing(user_id=user_id, actor=operation.server_identity['actor_device_id'])
        ordinary = context.registry.exchange(issued['code'], name='synthetic target device')
        denied = client.get('/api/rebuild/memory-restores/' + plan['operation_id'], headers={
            'Authorization': 'Bearer ' + ordinary['key'], 'X-Chriptmas-Target-User': 'local-user'})
        assert denied.status_code == 403


def test_server_preview_pauses_only_target_real_coordinator_and_keeps_other_maintenance(server):
    from functools import partial
    from backend.memory_app.backup import fingerprint_runtime_restore_source
    from backend.memory_app.server_inspirations import ServerInspirationCoordinator
    context = server.app.state.server_context
    with _client(server.app) as client:
        created = client.post('/api/v2/server/users', headers=server.headers(), json={'name': 'preview target'})
        assert created.status_code == 201, created.text
        user_id = created.json()['user_id']
        selected = server.headers(user_id)
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        assert client.get('/api/v2/settings', headers=selected).status_code == 200
        backup = client.post('/api/rebuild/memory-snapshots', headers=selected, json={})
        assert backup.status_code == 200, backup.text
        plan = client.post('/api/rebuild/memory-snapshots/' + backup.json()['snapshot_id'] + '/rollback-plan',
            headers=selected, json={})
        assert plan.status_code == 200, plan.text
        target = context.pool._children[user_id].application
        other = context.pool._children['local-user'].application
        assert target.state.memory_daily_jobs.maintenance_paused
        assert not other.state.memory_daily_jobs.maintenance_paused
        coordinator = ServerInspirationCoordinator(context.users, context.pool, context.jobs, context.resources)
        assert coordinator._due(user_id, coordinator.now().isoformat()) is False
        before = fingerprint_runtime_restore_source(context.users.root_for(user_id))
        client.portal.call(coordinator._submit, user_id)
        assert fingerprint_runtime_restore_source(context.users.root_for(user_id)) == before
        maintained = client.portal.call(other.state.memory_daily_jobs._run_callback,
            partial(other.state.memory_backup.run, identity='actual-other-maintenance', automatic=False))
        assert maintained['status'] == 'done'
        assert (runtime_backup_roots(context.users.root_for('local-user'))[1] / maintained['snapshot_id']).is_dir()
        endpoint = '/api/v2/backups/restore-previews/' + plan.json()['preview_id'] + '/release'
        assert client.post(endpoint, headers=server.headers()).status_code == 200
        assert target.state.memory_daily_jobs.maintenance_paused
        released = client.post(endpoint, headers=selected)
        assert released.status_code == 200 and released.json()['status'] == 'released'
        assert not target.state.memory_daily_jobs.maintenance_paused


def test_server_startup_refuses_queued_restore_after_real_device_revocation(server):
    context = server.app.state.server_context
    with _client(server.app) as client:
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        root = context.users.root_for('local-user')
        note = root / 'workspace' / 'original.md'
        note.parent.mkdir(exist_ok=True)
        note.write_text('snapshot original')
        snapshot = client.post('/api/rebuild/memory-snapshots', headers=server.headers(), json={})
        assert snapshot.status_code == 200, snapshot.text
        snapshot_id = snapshot.json()['snapshot_id']
        note.write_text('current original')
        endpoint = '/api/rebuild/memory-snapshots/' + snapshot_id
        plan = client.post(endpoint + '/rollback-plan', headers=server.headers(), json={}).json()
        prepared = client.post(endpoint + '/rollback', headers=server.headers(), json={
            'rollback_id': plan['rollback_id'], 'operation_id': plan['operation_id'], 'confirm': True})
        assert prepared.status_code == 202, prepared.text
        actor = context.registry.authenticate(server._credential)
        authority = context.restores._authority(authorize_user(context.users, actor))
        queue_runtime_recovery(operations_root=runtime_backup_roots(root)[2], operation_id=plan['operation_id'], server_identity=authority)
        context.registry.revoke(actor.user_id, actor.device_id, expected_revision=actor.revision)
    restarted = server.factory()
    operation = load_runtime_recovery_operation(operations_root=runtime_backup_roots(root)[2], operation_id=plan['operation_id'])
    assert operation.state == 'failed' and operation.last_error == 'runtime_restore_authority_changed'
    assert not Path(operation.rollback_root).exists()
    assert note.read_text() == 'current original'
    assert restarted.state.server_user_pool.loaded_user_ids == ()


def test_server_startup_adopts_authorized_queue_before_loading_a_real_child(server):
    context = server.app.state.server_context
    with _client(server.app) as client:
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        root = context.users.root_for('local-user')
        note = root / 'workspace' / 'original.md'
        note.parent.mkdir(exist_ok=True)
        note.write_text('snapshot original')
        snapshot_id = client.post('/api/rebuild/memory-snapshots', headers=server.headers(), json={}).json()['snapshot_id']
        note.write_text('current original')
        endpoint = '/api/rebuild/memory-snapshots/' + snapshot_id
        plan = client.post(endpoint + '/rollback-plan', headers=server.headers(), json={}).json()
        prepared = client.post(endpoint + '/rollback', headers=server.headers(), json={
            'rollback_id': plan['rollback_id'], 'operation_id': plan['operation_id'], 'confirm': True})
        assert prepared.status_code == 202, prepared.text
        actor = context.registry.authenticate(server._credential)
        authority = context.restores._authority(authorize_user(context.users, actor))
        queue_runtime_recovery(operations_root=runtime_backup_roots(root)[2], operation_id=plan['operation_id'], server_identity=authority)
    restarted = server.factory()
    assert restarted.state.server_user_pool.loaded_user_ids == ()
    assert note.read_text() == 'snapshot original'
    with _client(restarted) as client:
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        status = client.get('/api/rebuild/memory-restores/' + plan['operation_id'], headers=server.headers())
        assert status.status_code == 200 and status.json()['status'] == 'restored'
        assert status.json()['runtime_ready'] is True


def test_server_restore_drains_actual_accepted_job_and_reopens_after_source_drift(server):
    from backend.memory_app.server_jobs import QuotaError
    context = server.app.state.server_context
    with _client(server.app) as client:
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        root = context.users.root_for('local-user')
        note = root / 'workspace' / 'original.md'
        note.parent.mkdir(exist_ok=True)
        note.write_text('snapshot original')
        snapshot_id = client.post('/api/rebuild/memory-snapshots', headers=server.headers(), json={}).json()['snapshot_id']
        note.write_text('current original')
        entered, release = threading.Event(), threading.Event()
        def actual_job():
            entered.set()
            if not release.wait(10):
                raise TimeoutError('accepted job was not released')
            note.write_text('accepted job finished')
        future = client.portal.start_task_soon(context.jobs.submit, 'local-user', 'maintenance', actual_job)
        try:
            assert entered.wait(5)
            endpoint = '/api/rebuild/memory-snapshots/' + snapshot_id
            plan = client.post(endpoint + '/rollback-plan', headers=server.headers(), json={}).json()
            accepted = client.post(endpoint + '/rollback', headers=server.headers(), json={
                'rollback_id': plan['rollback_id'], 'operation_id': plan['operation_id'], 'confirm': True, 'execute': True})
            assert accepted.status_code == 202, accepted.text
            status = client.get('/api/rebuild/memory-restores/' + plan['operation_id'], headers=server.headers())
            assert status.status_code == 200 and status.json()['status'] == 'restoring'
            assert status.json()['restore_executed'] is False
            assert client.get('/api/v2/settings', headers=server.headers()).status_code == 503
            with pytest.raises(QuotaError, match='runtime_restore_in_progress'):
                client.portal.call(context.jobs.submit, 'local-user', 'maintenance', lambda: note.write_text('unaccepted job'))
        finally:
            release.set()
            future.result(10)
        failed = _wait(client, plan['operation_id'], server.headers())
        assert failed['status'] == 'failed' and failed['restore_executed'] is False
        assert note.read_text() == 'accepted job finished'
        resumed = client.post('/api/rebuild/memory-restores/' + plan['operation_id'] + '/resume', headers=server.headers(), json={'confirm': True})
        assert resumed.status_code == 202, resumed.text
        checked = _wait(client, plan['operation_id'], server.headers())
        assert checked['status'] == 'failed' and checked['requires_restart'] is False
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        preview = client.post(endpoint + '/rollback-plan', headers=server.headers(), json={})
        assert preview.status_code == 200 and preview.json()['operation_id'] != plan['operation_id']


def test_server_confirmation_is_bound_to_current_account_revision_without_rewinding_quota(server):
    context = server.app.state.server_context
    with _client(server.app) as client:
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        root = context.users.root_for('local-user')
        note = root / 'workspace' / 'original.md'
        note.parent.mkdir(exist_ok=True)
        note.write_text('snapshot original')
        snapshot_id = client.post('/api/rebuild/memory-snapshots', headers=server.headers(), json={}).json()['snapshot_id']
        note.write_text('current original')
        endpoint = '/api/rebuild/memory-snapshots/' + snapshot_id
        first = client.post(endpoint + '/rollback-plan', headers=server.headers(), json={}).json()
        actor = context.registry.authenticate(server._credential)
        user = context.users.get('local-user')
        context.users.update(actor, 'local-user', expected_revision=user['revision'], storage_limit_mb=128)
        refused = client.post(endpoint + '/rollback', headers=server.headers(), json={
            'rollback_id': first['rollback_id'], 'operation_id': first['operation_id'], 'confirm': True, 'execute': True})
        assert refused.status_code == 400 and refused.json()['detail'] == 'restore_plan_changed'
        assert client.get('/api/rebuild/memory-restores/' + first['operation_id'], headers=server.headers()).status_code == 404
        assert note.read_text() == 'current original'
        second = client.post(endpoint + '/rollback-plan', headers=server.headers(), json={}).json()
        assert second['rollback_id'] == first['rollback_id']
        assert second['operation_id'] != first['operation_id']
        accepted = client.post(endpoint + '/rollback', headers=server.headers(), json={
            'rollback_id': second['rollback_id'], 'operation_id': second['operation_id'], 'confirm': True, 'execute': True})
        assert accepted.status_code == 202, accepted.text
        ready = _wait(client, second['operation_id'], server.headers())
        assert ready['status'] == 'restored'
        assert note.read_text() == 'snapshot original'
        assert context.users.get('local-user')['storage_limit_mb'] == 128


@pytest.mark.parametrize('change', ['account', 'device', 'cutover', 'legacy'])
def test_prepared_server_restore_rejects_changed_authority_and_keeps_source(server, change):
    context = server.app.state.server_context
    with _client(server.app) as client:
        created = client.post('/api/v2/server/users', headers=server.headers(), json={'name': 'restore target'})
        assert created.status_code == 201, created.text
        user_id = created.json()['user_id']
        selected = server.headers(user_id)
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        assert client.get('/api/v2/settings', headers=selected).status_code == 200
        own = context.users.root_for('local-user') / 'workspace' / 'control.md'
        own.parent.mkdir(exist_ok=True)
        own.write_text('other user is unchanged')
        root = context.users.root_for(user_id)
        note = root / 'workspace' / 'original.md'
        note.parent.mkdir(exist_ok=True)
        note.write_text('snapshot original')
        backup = client.post('/api/rebuild/memory-snapshots', headers=selected, json={})
        assert backup.status_code == 200 and backup.json()['verified'] is True, backup.text
        endpoint = '/api/rebuild/memory-snapshots/' + backup.json()['snapshot_id']
        note.write_text('current original')
        preview = client.post(endpoint + '/rollback-plan', headers=selected, json={})
        assert preview.status_code == 200, preview.text
        plan = preview.json()
        _working, snapshots, operations = runtime_backup_roots(root)
        if change == 'legacy':
            old = prepare_runtime_recovery(snapshot_root=snapshots / backup.json()['snapshot_id'],
                active_root=root, operations_root=operations, operation_id=plan['operation_id'],
                expected_source_fingerprint=plan['current_vault_fingerprint'])
            assert old.server_identity is None and old.state == 'prepared'
        else:
            prepared = client.post(endpoint + '/rollback', headers=selected, json={
                'rollback_id': plan['rollback_id'], 'operation_id': plan['operation_id'], 'confirm': True})
            assert prepared.status_code == 202, prepared.text
        actor = context.registry.authenticate(server._credential)
        status_url = '/api/rebuild/memory-restores/' + plan['operation_id']
        if change == 'account':
            target = context.users.get(user_id)
            context.users.update(actor, user_id, expected_revision=target['revision'], storage_limit_mb=128)
        else:
            paired = context.registry.issue_pairing(user_id='local-user', actor=actor.device_id)
            replacement = context.registry.exchange(paired['code'], name='synthetic replacement admin')
            replacement_headers = {'Authorization': 'Bearer ' + replacement['key']}
            other_device = {**replacement_headers, 'X-Chriptmas-Target-User': user_id}
            denied = client.post(status_url + '/resume', headers=other_device, json={'confirm': True})
            assert denied.status_code == 409, denied.text
            assert client.get(status_url, headers=selected).json()['state'] == 'prepared'
            if change == 'legacy':
                resumed = client.post(status_url + '/resume', headers=selected, json={'confirm': True})
                assert resumed.status_code == 202 and resumed.json()['operation_id'] == plan['operation_id'], resumed.text
                assert _wait(client, plan['operation_id'], selected)['runtime_ready'] is True
                assert note.read_text() == 'snapshot original'
                assert own.read_text() == 'other user is unchanged'
                return
            entered, release = threading.Event(), threading.Event()

            async def after_retirement_begins():
                entered.set()
                assert await asyncio.to_thread(release.wait, 20)

            try:
                if change == 'cutover':
                    child = context.pool._children[user_id].application
                    child.router.on_shutdown.append(after_retirement_begins)
                    resumed = client.post(status_url + '/resume', headers=selected, json={'confirm': True})
                    assert resumed.status_code == 202, resumed.text
                    assert entered.wait(20)
                revoked = client.post(f'/api/v2/devices/{actor.device_id}/revoke', headers=replacement_headers,
                                      json={'expected_revision': actor.revision})
                assert revoked.status_code == 200, revoked.text
            finally:
                release.set()
            selected = other_device
        if change != 'cutover':
            refused = client.post(status_url + '/resume', headers=selected, json={'confirm': True})
            assert refused.status_code == 409, refused.text
        assert _wait(client, plan['operation_id'], selected)['status'] == 'failed'
        assert client.get(status_url, headers=selected).json()['state'] == 'failed'
        old = load_runtime_recovery_operation(operations_root=operations, operation_id=plan['operation_id'])
        assert old.last_error == 'runtime_restore_authority_changed'
        assert not Path(old.rollback_root).exists()
        assert note.read_text() == 'current original'
        assert own.read_text() == 'other user is unchanged'
        if change == 'cutover':
            return

        preview = client.post(endpoint + '/rollback-plan', headers=selected, json={})
        assert preview.status_code == 200, preview.text
        fresh = preview.json()
        assert fresh['operation_id'] != plan['operation_id']
        prepared = client.post(endpoint + '/rollback', headers=selected, json={
            'rollback_id': fresh['rollback_id'], 'operation_id': fresh['operation_id'], 'confirm': True})
        assert prepared.status_code == 202, prepared.text
        resumed = client.post('/api/rebuild/memory-restores/' + fresh['operation_id'] + '/resume',
                              headers=selected, json={'confirm': True})
        assert resumed.status_code == 202 and resumed.json()['operation_id'] == fresh['operation_id'], resumed.text
        assert _wait(client, fresh['operation_id'], selected)['runtime_ready'] is True
        assert note.read_text() == 'snapshot original'
        assert own.read_text() == 'other user is unchanged'


def test_bad_legacy_restore_archive_releases_the_next_real_confirmation(server):
    context = server.app.state.server_context
    with _client(server.app) as client:
        headers = server.headers()
        assert client.get('/api/v2/settings', headers=headers).status_code == 200
        root = context.users.root_for('local-user')
        note = root / 'workspace' / 'original.md'
        note.parent.mkdir(exist_ok=True)
        note.write_text('snapshot original')
        backup = client.post('/api/rebuild/memory-snapshots', headers=headers, json={})
        assert backup.status_code == 200 and backup.json()['verified'] is True, backup.text
        snapshot_id = backup.json()['snapshot_id']
        note.write_text('current original')
        endpoint = '/api/rebuild/memory-snapshots/' + snapshot_id
        preview = client.post(endpoint + '/rollback-plan', headers=headers, json={})
        assert preview.status_code == 200, preview.text
        plan = preview.json()
        _working, snapshots, operations = runtime_backup_roots(root)
        old = prepare_runtime_recovery(snapshot_root=snapshots / snapshot_id, active_root=root,
            operations_root=operations, operation_id=plan['operation_id'],
            expected_source_fingerprint=plan['current_vault_fingerprint'])
        assert old.server_identity is None and old.state == 'prepared'
        handed_off = client.post('/api/v2/backups/restore-previews/' + plan['preview_id'] + '/release',
                                 headers=headers)
        assert handed_off.status_code == 200 and handed_off.json()['status'] == 'restore_accepted', handed_off.text
        payload = snapshots / snapshot_id / 'payload' / 'workspace' / 'original.md'
        payload.write_text('changed archived material')

        status_url = '/api/rebuild/memory-restores/' + plan['operation_id']
        refused = client.post(status_url + '/resume', headers=headers, json={'confirm': True})
        assert refused.status_code == 409, refused.text
        assert client.get(status_url, headers=headers).json()['state'] == 'failed'
        settled = load_runtime_recovery_operation(operations_root=operations, operation_id=old.operation_id)
        assert settled.last_error == 'runtime_restore_archive_invalid'
        assert not Path(old.rollback_root).exists()
        assert note.read_text() == 'current original'

        healthy = client.post('/api/rebuild/memory-snapshots', headers=headers, json={})
        assert healthy.status_code == 200 and healthy.json()['verified'] is True, healthy.text
        fresh_endpoint = '/api/rebuild/memory-snapshots/' + healthy.json()['snapshot_id']
        fresh_preview = client.post(fresh_endpoint + '/rollback-plan', headers=headers, json={})
        assert fresh_preview.status_code == 200, fresh_preview.text
        fresh = fresh_preview.json()
        accepted = client.post(fresh_endpoint + '/rollback', headers=headers, json={
            'rollback_id': fresh['rollback_id'], 'operation_id': fresh['operation_id'], 'confirm': True})
        assert accepted.status_code == 202 and accepted.json()['operation_id'] == fresh['operation_id'], accepted.text
        assert note.read_text() == 'current original'


def test_server_reconciles_interrupted_preparation_and_rechecks_changed_startup_source(server):
    context = server.app.state.server_context
    with _client(server.app) as client:
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        root = context.users.root_for('local-user')
        note = root / 'workspace' / 'original.md'
        note.parent.mkdir(exist_ok=True)
        note.write_text('snapshot original')
        snapshot_id = client.post('/api/rebuild/memory-snapshots', headers=server.headers(), json={}).json()['snapshot_id']
        note.write_text('current original')
        endpoint = '/api/rebuild/memory-snapshots/' + snapshot_id
        plan = client.post(endpoint + '/rollback-plan', headers=server.headers(), json={}).json()
        prepared = client.post(endpoint + '/rollback', headers=server.headers(), json={
            'rollback_id': plan['rollback_id'], 'operation_id': plan['operation_id'], 'confirm': True})
        assert prepared.status_code == 202, prepared.text
        operations = runtime_backup_roots(root)[2]
        operation = load_runtime_recovery_operation(operations_root=operations, operation_id=plan['operation_id'])
        _write_operation(operations, replace(operation, state='preparing', target_entries=()))
        actor = context.registry.authenticate(server._credential)
        pairing = context.registry.issue_pairing(user_id='local-user', actor=actor.device_id)
        device = context.registry.exchange(pairing['code'], name='synthetic second admin device')
    restarted = server.factory()
    operation = load_runtime_recovery_operation(operations_root=operations, operation_id=plan['operation_id'])
    assert operation.state == 'prepared'
    assert note.read_text() == 'current original'
    with _client(restarted) as client:
        headers = {'Authorization': 'Bearer ' + device['key']}
        assert client.get('/api/v2/settings', headers=headers).status_code == 200
        resumed = client.post('/api/rebuild/memory-restores/' + plan['operation_id'] + '/resume', headers=headers, json={'confirm': True})
        assert resumed.status_code == 409
        refused = client.get('/api/rebuild/memory-restores/' + plan['operation_id'], headers=headers)
        assert refused.status_code == 200 and refused.json()['state'] == 'failed'
        assert refused.json()['restore_executed'] is False and refused.json()['requires_restart'] is False
        assert note.read_text() == 'current original'
        fresh = client.post(endpoint + '/rollback-plan', headers=headers, json={})
        assert fresh.status_code == 200, fresh.text
        fresh = fresh.json()
        assert fresh['operation_id'] != plan['operation_id']
        accepted = client.post(endpoint + '/rollback', headers=headers, json={
            'rollback_id': fresh['rollback_id'], 'operation_id': fresh['operation_id'], 'confirm': True, 'execute': True})
        assert accepted.status_code == 202, accepted.text
        ready = _wait(client, fresh['operation_id'], headers)
        assert ready['status'] == 'restored', ready
        assert note.read_text() == 'snapshot original'
        operation = load_runtime_recovery_operation(operations_root=operations, operation_id=fresh['operation_id'])
        assert operation.server_identity['actor_device_id'] == restarted.state.device_registry.authenticate(device['key']).device_id


def test_corrupt_user_restore_journal_closes_only_that_space_and_keeps_both_trees(server):
    context = server.app.state.server_context
    with _client(server.app) as client:
        created = client.post('/api/v2/server/users', headers=server.headers(), json={'name': 'corrupt journal target'})
        assert created.status_code == 201, created.text
        user_id = created.json()['user_id']
        selected = server.headers(user_id)
        assert client.get('/api/v2/settings', headers=selected).status_code == 200
        root = context.users.root_for(user_id)
        note = root / 'workspace' / 'original.md'
        note.parent.mkdir(exist_ok=True)
        note.write_text('snapshot original')
        snapshot_id = client.post('/api/rebuild/memory-snapshots', headers=selected, json={}).json()['snapshot_id']
        note.write_text('current original')
        endpoint = '/api/rebuild/memory-snapshots/' + snapshot_id
        plan = client.post(endpoint + '/rollback-plan', headers=selected, json={}).json()
        prepared = client.post(endpoint + '/rollback', headers=selected, json={
            'rollback_id': plan['rollback_id'], 'operation_id': plan['operation_id'], 'confirm': True})
        assert prepared.status_code == 202, prepared.text
        operations = runtime_backup_roots(root)[2]
        operation = load_runtime_recovery_operation(operations_root=operations, operation_id=plan['operation_id'])
    (operations / (plan['operation_id'] + '.json')).write_text('{"kind":"runtime"', encoding='utf8')
    restarted = server.factory()
    with _client(restarted) as client:
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        assert client.get('/api/v2/settings', headers=selected).status_code == 503
        status = client.get('/api/rebuild/memory-restores/' + plan['operation_id'], headers=selected)
        assert status.status_code == 400 and status.json()['detail'] == 'restore_status_unavailable'
        assert user_id not in restarted.state.server_user_pool.loaded_user_ids
        assert note.read_text() == 'current original'
        assert Path(operation.staging_root).is_dir() and Path(operation.snapshot_root).is_dir()


@pytest.mark.parametrize('terminal', ['adopted', 'failed', 'before-journal'])
def test_server_clears_a_real_dead_terminal_lease_before_loading_and_restores_a_new_operation(server, terminal):
    context = server.app.state.server_context
    with _client(server.app) as client:
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        root = context.users.root_for('local-user')
        note = root / 'workspace' / 'original.md'
        note.parent.mkdir(exist_ok=True)
        note.write_text('snapshot original')
        snapshot = client.post('/api/rebuild/memory-snapshots', headers=server.headers(), json={})
        assert snapshot.status_code == 200, snapshot.text
        endpoint = '/api/rebuild/memory-snapshots/' + snapshot.json()['snapshot_id']
        note.write_text('current original')
        response = client.post(endpoint + '/rollback-plan', headers=server.headers(), json={})
        assert response.status_code == 200, response.text
        plan = response.json()
        prepared = client.post(endpoint + '/rollback', headers=server.headers(), json={
            'rollback_id': plan['rollback_id'], 'operation_id': plan['operation_id'], 'confirm': True})
        assert prepared.status_code == 202, prepared.text
        if terminal == 'failed':
            note.write_text('source changed after preparation')
        resumed = client.post('/api/rebuild/memory-restores/' + plan['operation_id'] + '/resume',
            headers=server.headers(), json={'confirm': True})
        assert resumed.status_code == (409 if terminal == 'failed' else 202), resumed.text
        settled = _wait(client, plan['operation_id'], server.headers())
        assert settled['state'] == ('failed' if terminal == 'failed' else 'adopted')
        assert settled['runtime_ready'] is (terminal != 'failed')
        operations = runtime_backup_roots(root)[2]
    operation = load_runtime_recovery_operation(operations_root=operations, operation_id=plan['operation_id'])
    lease_id = 'before-journal-' + operation.operation_id if terminal == 'before-journal' else operation.operation_id
    lease = root / '.chriptmas-vault-recovery.lock'
    ready = server.root / 'lease-ready'
    environment = dict(os.environ, CHRIPTMAS_APP_ROOT=str(root), PYTHONPATH=str(Path('src').resolve()))
    script = Path(__file__).with_name('runtime_restore_lease_child.py')
    arguments = [sys.executable, str(script), str(operations), operation.operation_id, str(ready)]
    if terminal == 'before-journal':
        arguments.append(lease_id)
    with subprocess.Popen(arguments,
            env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        for _attempt in range(500):
            if ready.is_file():
                break
            assert child.poll() is None, child.stderr.read().decode('utf8', errors='replace')
            time.sleep(.02)
        else:
            child.terminate()
            raise AssertionError('real recovery lease child did not become ready')
        holder = json.loads(ready.read_text(encoding='utf8'))
        assert holder['pid'] == child.pid or holder['parent_pid'] == child.pid
        assert json.loads(lease.read_text(encoding='utf8')) == {'operation_id': lease_id, 'pid': holder['pid']}
        with pytest.raises(VaultOperationalRecoveryError, match='live or invalid owner'):
            context.restores.recover_before_open('local-user')
        assert lease.is_file()
        blocked = server.factory()
        with _client(blocked) as client:
            assert client.get('/api/v2/settings', headers=server.headers()).status_code == 503
            assert blocked.state.server_user_pool.loaded_user_ids == ()
        os.kill(holder['pid'], signal.SIGTERM)
        assert child.wait(timeout=10) != 0
    if terminal == 'before-journal':
        assert not (operations / f'{lease_id}.json').exists()
        working = root / '.rebuild-data'
        for wrong_operations, wrong_working in ((operations.parent / 'unknown', working), (operations, root / 'unknown')):
            with pytest.raises(VaultOperationalRecoveryError, match='fixed runtime root'):
                _reconcile_dead_lease(wrong_operations, wrong_working)
            assert lease.is_file()
        traces = [operations.parent / name / lease_id for name in ('restored', 'rollback')]
        traces.extend(root / f'.{working.name}.{name}-{lease_id}' for name in ('restore', 'rollback', 'displaced'))
        for trace in traces:
            assert trace.is_relative_to(root)
            trace.mkdir(parents=True)
            with pytest.raises(VaultOperationalRecoveryError, match='restore artifacts'):
                context.restores.recover_before_open('local-user')
            assert lease.is_file()
            trace.rmdir()
    else:
        _write_operation(operations, replace(operation, kind='unknown'))
        with pytest.raises(VaultOperationalRecoveryError, match='kind is invalid'):
            context.restores.recover_before_open('local-user')
        assert lease.is_file()
        _write_operation(operations, replace(operation, active_root=str(server.root)))
        with pytest.raises(VaultOperationalRecoveryError, match='paths are invalid'):
            context.restores.recover_before_open('local-user')
        assert lease.is_file()
        _write_operation(operations, operation)
    restarted = server.factory()
    assert not lease.exists()
    assert restarted.state.server_user_pool.loaded_user_ids == ()
    with _client(restarted) as client:
        assert client.get('/api/v2/settings', headers=server.headers()).status_code == 200
        note.write_text('new source after restart')
        fresh = client.post(endpoint + '/rollback-plan', headers=server.headers(), json={})
        assert fresh.status_code == 200, fresh.text
        fresh = fresh.json()
        assert fresh['operation_id'] != operation.operation_id
        accepted = client.post(endpoint + '/rollback', headers=server.headers(), json={
            'rollback_id': fresh['rollback_id'], 'operation_id': fresh['operation_id'], 'confirm': True, 'execute': True})
        assert accepted.status_code == 202, accepted.text
        assert _wait(client, fresh['operation_id'], server.headers())['runtime_ready'] is True
        assert note.read_text() == 'snapshot original'
        new_operation = load_runtime_recovery_operation(operations_root=operations, operation_id=fresh['operation_id'])
        assert (Path(new_operation.rollback_root) / 'workspace' / 'original.md').read_text() == 'new source after restart'
