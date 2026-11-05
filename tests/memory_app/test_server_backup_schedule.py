import asyncio
from datetime import datetime, timezone
from pathlib import Path
import os

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from backend.memory_app.server_inspirations import ServerInspirationCoordinator, _due_in_existing_space
from backend.memory_app.v2.backup_schedule import BackupSchedule
from backend.shared.deployment import DeploymentLayout, runtime_backup_roots


def test_real_server_wakes_a_cold_user_for_backup_without_documents_and_keeps_the_due_id(tmp_path, monkeypatch):
    root = tmp_path / 'server'
    root.mkdir()
    assigned = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    assert root.is_absolute() and root.is_relative_to(assigned)
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
    monkeypatch.setenv('CHRIPTMAS_SERVER_MASTER_KEY', Fernet.generate_key().decode('ascii'))
    from backend.memory_app.app import create_server_app
    layout = DeploymentLayout('server', root / 'users' / 'local-user', root)
    original = create_server_app(layout=layout, port=18112)
    registry = original.state.device_registry
    pair = registry.issue_pairing(user_id='local-user', actor='install')
    device = registry.exchange(pair['code'], name='synthetic backup clock')
    headers = {'Authorization': 'Bearer ' + device['key']}
    options = {'base_url': 'http://127.0.0.1:18112', 'client': ('127.0.0.1', 1234)}
    with TestClient(original, **options) as client:
        assert client.get('/api/v2/settings', headers=headers).status_code == 200
        context = original.state.server_context
        child = context.pool._children['local-user'].application
        schedule = BackupSchedule(child.state.recognition_records)
        now = datetime.now(timezone.utc)
        body = {**{key: value for key, value in schedule.settings().items() if key != 'revision'},
            'frequency': 'weekly', 'weekday': now.weekday(), 'command_id': 'cold-user-schedule', 'expected_revision': 0}
        assert schedule.save(body)['state'] == 'saved'
        user_root = context.users.root_for('local-user')
        assert not _due_in_existing_space(user_root, 'default', now.isoformat())
        assert not runtime_backup_roots(user_root)[1].exists()
    restarted = create_server_app(layout=layout, port=18112)
    with TestClient(restarted, **options) as client:
        context = restarted.state.server_context
        assert context.pool.loaded_user_ids == ()
        owner = ServerInspirationCoordinator(context.users, context.pool, context.jobs, context.resources)
        async def finish():
            for _attempt in range(1200):
                if not owner._tasks and context.jobs.pending_for('local-user') == 0:
                    return
                await asyncio.sleep(.05)
            raise AssertionError('actual scheduled backup did not finish')
        client.portal.call(owner.tick)
        client.portal.call(finish)
        assert context.pool.loaded_user_ids == ('local-user',)
        child = context.pool._children['local-user'].application
        schedule = BackupSchedule(child.state.recognition_records)
        key = schedule.slot()['key']
        result = child.state.recognition_records.read('v2_backup_jobs', key)
        assert result is not None and result.payload['status'] == 'done'
        snapshots = list(runtime_backup_roots(user_root)[1].glob('snap-auto-*'))
        assert len(snapshots) == 1 and snapshots[0].name == result.payload['snapshot_id']
        client.portal.call(owner.tick)
        client.portal.call(finish)
        assert list(runtime_backup_roots(user_root)[1].glob('snap-auto-*')) == snapshots
        assert client.get('/api/v2/settings', headers=headers).status_code == 200
        client.portal.call(owner.close)
