import io
import json
import os
from pathlib import Path
import shutil
from threading import RLock
from uuid import uuid4
import zipfile


def test_actual_admin_target_backup_download_and_personal_copy():
    allocated = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    assert not any(path.name == 'runtime' for path in (allocated, *allocated.parents))
    root = allocated / ('admin-' + uuid4().hex[:8])
    (root / 'config').mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', root / 'config/settings.toml')
    old = {key: os.environ.get(key) for key in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ['CHRIPTMAS_APP_ROOT'], os.environ['CHRIPTMAS_DEPLOY'] = str(root.resolve(strict=True)), 'server'
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) == root.resolve(strict=True)
    try:
        _check(root)
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _check(root):
    from fastapi.testclient import TestClient
    from backend.memory_app.app import create_server_app
    from backend.memory_app.workspace_items import WorkspaceItems
    from backend.memory_app.workspace_generation import _draft
    from backend.memory_app.workspace_confirmation import WorkspaceConfirmation
    from backend.shared.deployment import DeploymentLayout
    from backend.memory_app.v2.shared_backup import SharedBackups

    server = create_server_app(layout=DeploymentLayout('server', root / 'users/local-user', root), port=8012)
    users, devices, shared = server.state.server_users, server.state.device_registry, server.state.shared_projects
    paired = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='代办验收管理员')
    admin = devices.authenticate(paired['key'])
    target = users.create(admin, name='代办目标用户')
    target_pair = devices.exchange(devices.issue_pairing(user_id=target['user_id'], actor=admin.device_id)['code'], name='目标用户设备')
    member = devices.authenticate(target_pair['key'])
    other = users.create(admin, name='普通跨空间验收用户')
    other_pair = devices.exchange(devices.issue_pairing(user_id=other['user_id'], actor=admin.device_id)['code'], name='普通用户设备')
    project = shared.create(admin, project_name='管理员目标共享资料', operation_id='create-' + uuid4().hex)['project']
    shared.invite(admin, project['id'], user_id=member.user_id, role='viewer', authority=project['authority'],
        operation_id='invite-' + uuid4().hex)
    invite = shared.invitations(member)[0]
    shared.accept(member, invite['id'], expected_revision=invite['revision'], operation_id='accept-' + uuid4().hex)
    data = shared.roots._data(project['id'])
    text = '合成采购资料，管理员代目标用户保留数量和金额。'
    draft = _draft({'title': '合成采购资料', 'summary': text, 'topics': [], 'facts': [], 'todos': [],
        'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}, text)
    staged = WorkspaceItems(data.records, None, RLock()).create_upload({'id': 'workspace-' + uuid4().hex,
        'project_id': project['id'], 'input_kind': 'text', 'title': draft['title'], 'source_text': text,
        'status': 'ready', 'draft': draft, 'error': None, 'document_id': None, 'created_at': shared.now()})
    original = WorkspaceConfirmation(data.root, data.records, data.documents).confirm(
        staged['id'], project['id'], data.records.read('workspace_items', staged['id']).revision, lambda _draft: text)
    base = '/api/v2/server/shared-projects'
    admin_headers = {'Authorization': 'Bearer ' + paired['key']}
    target_headers = {**admin_headers, 'X-Chriptmas-Target-User': member.user_id}
    member_headers = {'Authorization': 'Bearer ' + target_pair['key']}
    options = {'base_url': 'http://127.0.0.1:8012', 'client': ('127.0.0.1', 16001)}
    backup_key, restore_key = 'admin-backup-' + uuid4().hex[:8], 'admin-copy-' + uuid4().hex[:8]
    with TestClient(server, **options) as client:
        result = client.post(base + '/' + project['id'] + '/backups', json={},
            headers={**target_headers, 'Idempotency-Key': backup_key})
        assert result.status_code == 200, result.text
        backup = result.json()
        assert backup['state'] == 'ready'
        assert client.get(base + '/backups/owned', headers=target_headers).json()['items'] == [backup]
        assert client.get(base + '/backups/owned', headers=member_headers).json()['items'] == [backup]
        assert client.get(base + '/backups/owned', headers=admin_headers).json()['items'] == []
        archive = client.get(base + '/backups/' + backup_key + '/download', headers=target_headers)
        assert archive.status_code == 200
        with zipfile.ZipFile(io.BytesIO(archive.content)) as opened:
            assert opened.testzip() is None
            envelope = json.loads(opened.read('chriptmas-backup.json'))
            assert envelope['scope'] == 'shared-project'
        body = {'name': '目标用户采购副本', 'approved': True, 'batch_size': 8}
        copied = client.post(base + '/backups/' + backup_key + '/projects', json=body,
            headers={**target_headers, 'Idempotency-Key': restore_key})
        assert copied.status_code == 200, copied.text
        progress = copied.json()
        assert progress['state'] == 'completed' and progress['completed'] == progress['total']
        target_app = server.state.server_user_pool._children[member.user_id].application
        records = target_app.state.recognition_records
        owned = records.read('v2_shared_backup_commands', backup_key)
        assert owned.payload['owner_user_id'] == member.user_id
        assert owned.payload['actor_user_id'] == admin.user_id and owned.payload['by'] == 'admin'
        command = records.read('v2_shared_backup_project_imports', restore_key)
        assert command.payload['owner_user_id'] == member.user_id
        assert command.payload['actor_user_id'] == admin.user_id and command.payload['by'] == 'admin'
        assert SharedBackups._owner_id({'actor_user_id': member.user_id}) == member.user_id
        assert records.read('v2_projects', progress['project_id']).payload['name'] == body['name']
        assert any(target_app.state.recognition_documents.markdown(row['id'], revision=row['revision']) == text
            for row in target_app.state.recognition_documents.list() if row['project_id'] == progress['project_id'])
        admin_records = server.state.server_user_pool._children[admin.user_id].application.state.recognition_records
        assert admin_records.read('v2_shared_backup_commands', backup_key) is None
        assert admin_records.read('v2_projects', progress['project_id']) is None
        assert client.get(base + '/backup-import-commands/' + restore_key, headers=member_headers).json() == progress
        assert client.post(base + '/backups/' + backup_key + '/projects', json=body,
            headers={**member_headers, 'Idempotency-Key': restore_key}).json() == progress
        writes = [row.payload for row in records.list('server_admin_writes')]
        assert any(row['target_user_id'] == member.user_id and row['actor_user_id'] == admin.user_id
            and row['by'] == 'admin' and row['collection'] == 'v2_projects' for row in writes)
        events = shared.activity(admin, project['id'])
        assert any(row['object_id'] == backup_key and row['actor_user_id'] == admin.user_id
            and row['by'] == 'admin' for row in events)
        outsider = {'Authorization': 'Bearer ' + other_pair['key'], 'X-Chriptmas-Target-User': member.user_id}
        assert client.get(base + '/backups/owned', headers=outsider).status_code == 403
        assert client.post(base + '/backups/' + backup_key + '/projects', json=body,
            headers={**outsider, 'Idempotency-Key': 'cross-target-denied'}).status_code == 403
        assert data.documents.read(original['document_id'])['revision'] == 1
