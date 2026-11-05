import os
import base64
from pathlib import Path
import shutil
from threading import RLock
from uuid import uuid4
import pytest


@pytest.mark.parametrize('unverifiable', [False, True])
def test_real_archive_restores_new_personal_project_and_pending_candidates(unverifiable):
    _run(unverifiable=unverifiable)


def test_real_archive_preserves_stopped_history_private():
    _run(unverifiable=False, history=True)


def _run(*, unverifiable, history=False):
    allocated = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    assert not any(path.name == 'runtime' for path in (allocated, *allocated.parents))
    root = allocated / ('import-' + uuid4().hex[:8])
    (root / 'config').mkdir(parents=True)
    repo = Path(__file__).resolve().parents[3]
    shutil.copyfile(repo / 'config/settings.toml.example', root / 'config/settings.toml')
    root = root.resolve(strict=True)
    old = {name: os.environ.get(name) for name in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ['CHRIPTMAS_APP_ROOT'], os.environ['CHRIPTMAS_DEPLOY'] = str(root), 'server'
    assert Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) == root
    try:
        _check(root, unverifiable=unverifiable, history=history)
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _check(root, *, unverifiable, history):
    from fastapi.testclient import TestClient
    from backend.memory_app.app import create_server_app
    from backend.memory_app.v2.projects import assign_scene
    from backend.memory_app.v2.shared_backup_import import COMMANDS, OBJECTS
    from backend.memory_app.workspace_items import WorkspaceItems
    from backend.memory_app.source_egress import SourceEgressService
    from backend.memory_app.workspace_confirmation import WorkspaceConfirmation
    from backend.memory_app.workspace_generation import _draft
    from backend.recognition import RecognitionService, WorkScope
    from backend.recognition import RecognitionError
    from backend.shared.deployment import DeploymentLayout
    from core.document_engine import DocumentDraft
    from core.product_core.workbench_original_asset import StoreWorkbenchOriginalAsset, link_workbench_original_asset_to_source

    layout = DeploymentLayout('server', root / 'users/local-user', root)
    server = create_server_app(layout=layout, port=8012)
    devices, users, shared = server.state.device_registry, server.state.server_users, server.state.shared_projects
    paired = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='副本管理员')
    admin = devices.authenticate(paired['key'])
    user = users.create(admin, name='副本只看成员')
    paired_viewer = devices.exchange(devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)['code'], name='副本成员设备')
    viewer = devices.authenticate(paired_viewer['key'])
    headers = {'Authorization': 'Bearer ' + paired_viewer['key']}
    project = shared.create(admin, project_name='原共享QA', operation_id='create-' + uuid4().hex)['project']
    shared.invite(admin, project['id'], user_id=viewer.user_id, role='viewer', authority=project['authority'],
        operation_id='invite-' + uuid4().hex)
    invitation = shared.invitations(viewer)[0]
    shared.accept(viewer, invitation['id'], expected_revision=invitation['revision'], operation_id='accept-' + uuid4().hex)
    data = shared.roots._data(project['id'])
    original_id = 'workspace-' + uuid4().hex
    original = data.root / 'workspace' / (original_id + '.txt')
    original.parent.mkdir(exist_ok=True)
    original.write_text('采购文件原文，完整保留数量和金额。', encoding='utf-8')
    item = WorkspaceItems(data.records, None, RLock()).create_upload({'id': original_id,
        'project_id': project['id'], 'input_kind': 'file', 'title': '采购原件', 'source_text': '采购文件原文，完整保留数量和金额。',
        'status': 'staged', 'draft': None, 'error': None, 'document_id': None,
        'created_at': shared.now(), 'original_path': str(original), 'original_name': '采购原件.txt'})
    doc = data.documents.create(DocumentDraft('采购整理稿', 'note', '逐项核对采购数量和金额。',
        ({'source_id': original_id, 'locator': 'workspace://' + original_id},), project['id']))
    assign_scene(data.records, 'document', doc['id'], project['id'], '采购核对')
    with data.records.begin() as tx:
        current = tx.read('v2_projects', project['id'])
        tx.put('v2_projects', project['id'], {**current.payload, 'scenes': ['采购核对']}, expected_revision=current.revision)
        tx.commit()
    SourceEgressService(data.records).set_policy(WorkScope('local-user', project['id']), 'original_item', original_id, item['revision'], 0, [])
    source = RecognitionService(data.records)
    scope = WorkScope('local-user', project['id'])
    experience = source.stage_experience(scope=scope, content='核对采购数量和金额。',
        provenance={'kind': 'user_statement', 'actor': admin.user_id, 'source_refs': []})
    proposal = source.propose(scope=scope, content='核对采购数量和金额。', source_experience_ids=[experience])
    insight = source.publish(scope=scope, candidate_id=proposal.id, expected_revision=proposal.revision, reviewer=admin.user_id)
    pending = source.propose(scope=scope, content='采购异常应再次核实。', source_experience_ids=[experience])
    standalone_text = '这是独立原件及附件的完整内容。'
    draft = _draft({'title': '独立原件', 'summary': standalone_text, 'topics': [], 'facts': [], 'todos': [],
        'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}, standalone_text)
    standalone = WorkspaceItems(data.records, None, RLock()).create_upload({
        'id': 'workspace-' + uuid4().hex, 'project_id': project['id'], 'input_kind': 'text', 'title': '独立原件',
        'source_text': standalone_text, 'status': 'ready', 'draft': draft, 'error': None, 'document_id': None,
        'created_at': shared.now()})
    confirmed = WorkspaceConfirmation(data.root, data.records, data.documents).confirm(
        standalone['id'], project['id'], standalone['revision'], lambda _draft: standalone_text)
    attachment = '独立附件真实字节。'.encode('utf-8')
    asset = StoreWorkbenchOriginalAsset(object_store=data.sources, assets_root=data.root / 'library/assets/originals',
        namespace_id=data.namespace).execute(display_name='独立附件.txt', media_type='text/plain', size_bytes=len(attachment),
            content_base64=base64.b64encode(attachment).decode('ascii'))
    link_workbench_original_asset_to_source(object_store=data.sources, namespace_id=data.namespace,
        asset_ref=asset.asset_ref, source_id=confirmed['source_id'],
        source_uri='crp://default/sources/' + confirmed['source_id'], now=shared.now())
    if unverifiable:
        source.stage_experience(scope=scope, content='缺少原任务证明的合成历史条目', provenance={
            'kind': 'model_generated_artifact', 'source_refs': [
                {'type': 'task', 'id': 'missing-task', 'revision': 1},
                {'type': 'document', 'id': 'missing-document', 'revision': 1},
                {'type': 'context_packet', 'id': 'missing-packet', 'revision': 1}]})
    if history:
        historical_experience = source.stage_experience(scope=scope, content='已停止的合成历史认识依据',
            provenance={'kind': 'user_statement', 'source_refs': []})
        historical_candidate = source.propose(scope=scope, content='已停止的合成历史认识', source_experience_ids=[historical_experience])
        historical_insight = source.publish(scope=scope, candidate_id=historical_candidate.id,
            expected_revision=historical_candidate.revision, reviewer='local-user')
        source.revoke_experience(scope=scope, experience_id=historical_experience, expected_revision=1)
        with pytest.raises(RecognitionError):
            SourceEgressService(data.records).snapshot(scope, [
                {'type': 'experience', 'id': historical_experience, 'revision': 2}])
    base = '/api/v2/server/shared-projects'
    options = {'base_url': 'http://127.0.0.1:8012', 'client': ('127.0.0.1', 16001)}
    with TestClient(server, **options) as client:
        settings = client.get('/api/v2/backups/schedule', headers=headers).json()
        assert client.patch('/api/v2/backups/schedule', headers=headers, json={
            **{name: settings[name] for name in ('time', 'timezone', 'weekday', 'month_day')}, 'frequency': 'off',
            'expected_revision': settings['revision'], 'command_id': 'disable-import-schedule'}).status_code == 200
        backup = client.post(base + '/' + project['id'] + '/backups', json={}, headers={
            **headers, 'Idempotency-Key': 'source-archive-' + uuid4().hex[:8]})
        assert backup.status_code == 200, backup.text
        backup_id = backup.json()['command_id']
        backup_records = server.state.server_user_pool._children[viewer.user_id].application.state.recognition_records
        qualification = backup_records.read('v2_shared_backup_commands', backup_id).payload['shared_archive_sources']
        assert qualification['version'] == 2
        assert qualification['namespace'] == 'shared/' + project['id'] + '/' + data.namespace
        assert qualification['objects']
        assert all(set(value) == {'collection', 'id', 'revision', 'lineage', 'status', 'private', 'authority'}
            for value in qualification['objects'])
        if unverifiable:
            assert any(value['status'] == 'unverifiable' and value['authority'] is None
                for value in qualification['objects'])
            key = 'unverifiable-import-' + uuid4().hex[:8]
            denied = client.post(base + '/backups/' + backup_id + '/projects',
                json={'name': '不可恢复的缺证明副本', 'approved': True, 'batch_size': 1},
                headers={**headers, 'Idempotency-Key': key})
            assert denied.status_code == 409 and denied.json()['detail'] == 'shared_backup_import_source_unverifiable'
            command = backup_records.read(COMMANDS, key)
            assert command.payload['state'] == 'preparing'
            assert backup_records.read('v2_projects', command.payload['project_id']) is None
            repeated = client.post(base + '/backups/' + backup_id + '/projects',
                json={'name': '不可恢复的缺证明副本', 'approved': True, 'batch_size': 1},
                headers={**headers, 'Idempotency-Key': key})
            assert repeated.status_code == 409
            assert backup_records.read(COMMANDS, key).payload['input_stage'] == command.payload['input_stage']
            assert client.get(base + '/backups/' + backup_id + '/download', headers=headers).status_code == 200
            return
        assert all(value['status'] in ({'verified', 'historical'} if history else {'verified'})
            and value['authority'] == 'portable' for value in qualification['objects'])
        if history:
            stopped = {(value['collection'], value['id']): value for value in qualification['objects']
                if value['status'] == 'historical'}
            assert stopped['recognition_experiences', historical_experience]['private'] is True
            assert stopped['recognitions', historical_insight.id]['private'] is True
        existing = client.post('/api/v2/projects', json={'name': '我的采购副本'}, headers=headers)
        assert existing.status_code == 200
        command_id = 'restore-project-' + uuid4().hex[:8]
        address = base + '/backups/' + backup_id + '/projects'
        body = {'name': '我的采购副本', 'approved': True, 'batch_size': 1}
        refused = client.post(address, json={**body, 'approved': False}, headers={**headers, 'Idempotency-Key': command_id})
        assert refused.status_code == 400
        restored = client.post(address, json=body, headers={**headers, 'Idempotency-Key': command_id})
        assert restored.status_code == 200, restored.text
        value = restored.json()
        assert value['state'] == 'copying' and value['completed'] == 1
        assert value['name'] == '我的采购副本 (2)' and value['project_id'] != existing.json()['id']
        destination_project = value['project_id']
        assert client.get(base + '/backup-import-commands/' + command_id, headers=headers).json() == value
        detail = shared.detail(admin, project['id'])
        member = next(member for member in detail['members'] if member['user_id'] == viewer.user_id)
        shared.change_member(admin, project['id'], user_id=viewer.user_id, action='remove', role=None,
            expected_revision=member['revision'], authority=detail['project']['authority'], operation_id='remove-' + uuid4().hex)
    server = create_server_app(layout=layout, port=8012)
    with TestClient(server, **options) as client:
        assert client.get(base + '/backup-import-commands/' + command_id, headers=headers).json() == value
        for _phase in range(12):
            if value['state'] == 'completed':
                break
            response = client.post(address, json=body, headers={**headers, 'Idempotency-Key': command_id})
            assert response.status_code == 200, response.text
            value = response.json()
            assert value['project_id'] == destination_project
        assert value['state'] == 'completed' and value['completed'] == value['total']
        child = server.state.server_user_pool._children[viewer.user_id].application
        records = child.state.recognition_records
        mappings = [row.payload for row in records.list(OBJECTS) if row.payload['command_id'] == command_id]
        by_source = {(row['collection'], row['source_id']): row for row in mappings}
        item_map, doc_map = by_source['workspace_items', original_id], by_source['documents', doc['id']]
        copied_item = records.read('workspace_items', item_map['target_id'])
        assert copied_item.payload['status'] == 'staged'
        copied_path = Path(copied_item.payload['original_path'])
        assert copied_path.is_relative_to(users.root_for(viewer.user_id)) and copied_path.read_bytes() == original.read_bytes()
        assert child.state.recognition_documents.markdown(doc_map['target_id'], revision=1) == '逐项核对采购数量和金额。'
        assert records.read('v2_scene_assignments_document', doc_map['target_id']).payload['scene'] == '采购核对'
        assert SourceEgressService(records).policy(WorkScope('local-user', destination_project), 'original_item',
            copied_item.object_id)['allowed_purposes'] == []
        insight_map = by_source['recognitions', insight.id]
        personal = child.state.recognition_service.get_recognition(scope=WorkScope('local-user', destination_project),
            recognition_id=insight_map['target_id'])
        assert personal.authorized and personal.content == insight.content
        assert records.read('recognitions', insight_map['target_id']).payload['published_by'] == viewer.user_id
        pending_map = by_source['recognition_candidates', pending.id]
        assert records.read('recognition_candidates', pending_map['target_id']).payload['state'] == 'pending'
        if history:
            history_exp = by_source['recognition_experiences', historical_experience]
            history_insight = by_source['recognitions', historical_insight.id]
            assert records.read('recognition_experiences', history_exp['target_id']).payload['state'] == 'revoked'
            assert records.read('recognitions', history_insight['target_id']).payload['state'] == 'revoked'
            assert not RecognitionService(records).get_recognition(
                scope=WorkScope('local-user', destination_project), recognition_id=history_insight['target_id']).authorized
            with pytest.raises(RecognitionError):
                SourceEgressService(records).snapshot(WorkScope('local-user', destination_project), [
                    {'type': 'experience', 'id': history_exp['target_id'],
                     'revision': records.read('recognition_experiences', history_exp['target_id']).revision}])
        source_map = by_source['original_sources', confirmed['source_id']]
        assert records.read('workspace_items', source_map['target_id']).payload['source_text'] == standalone_text
        asset_map = by_source['source_assets', asset.asset_id]
        retained_asset = records.read('workspace_items', asset_map['target_id'])
        assert retained_asset.payload['original_name'] == '独立附件.txt'
        assert Path(retained_asset.payload['original_path']).read_bytes() == attachment
        recalled = client.get('/api/workspace/v1/search', params={'project_id': destination_project, 'q': '采购'}, headers=headers)
        assert recalled.status_code == 200
        assert any(entry['id'] == doc_map['target_id'] for entry in recalled.json()['items'])
        assert records.read(COMMANDS, command_id).payload['source_project_id'] == project['id']
        assert data.documents.read(doc['id'])['revision'] == 1 and source.get_recognition(scope=scope, recognition_id=insight.id).revision == 1
        duplicate = client.post(address, json=body, headers={**headers, 'Idempotency-Key': command_id})
        assert duplicate.status_code == 200 and duplicate.json() == value
    restarted = create_server_app(layout=layout, port=8012)
    with TestClient(restarted, **options) as client:
        assert client.get(base + '/backup-import-commands/' + command_id, headers=headers).json() == value
        assert client.get(base + '/backups/' + backup_id + '/download', headers=headers).status_code == 200
        pairs = restarted.state.device_registry
        new_pair = pairs.exchange(pairs.issue_pairing(user_id=viewer.user_id, actor=admin.device_id)['code'], name='副本第二设备')
        second_headers = {'Authorization': 'Bearer ' + new_pair['key']}
        owned = client.get(base + '/backups/owned', headers=second_headers)
        assert owned.status_code == 200
        retained = next(row for row in owned.json()['items'] if row['command_id'] == backup_id)
        assert retained['project_name'] == '原共享QA' and retained['created_at'] == backup.json()['created_at']
        shared = restarted.state.shared_projects
        current = shared.detail(admin, project['id'])['project']
        deleted = shared.delete(admin, project['id'], confirmation=current['name'], authority=current['authority'],
            operation_id='delete-' + uuid4().hex, skip_backup=True)
        assert deleted['state'] == 'completed'
        assert client.get(base + '/backups/owned', headers=second_headers).json()['items'] == [retained]
        assert client.get(base + '/backup-commands/' + backup_id, headers=second_headers).json() == retained
        assert client.get(base + '/backups/' + backup_id + '/download', headers=second_headers).status_code == 200
        assert client.get(base + '/backups/owned', headers={'Authorization': 'Bearer ' + paired['key']}).json() == {'items': []}
