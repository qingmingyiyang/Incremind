import base64
import os
import sqlite3
from pathlib import Path
from shutil import copyfile
from uuid import uuid4


def test_shared_document_move_resumes_after_destination_commit():
    allocated = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    assert not any(path.name == 'runtime' for path in (allocated, *allocated.parents))
    root = (allocated / ('shared-filing-' + uuid4().hex[:8])).resolve()
    assert root.is_relative_to(allocated)
    (root / 'config').mkdir(parents=True)
    repo = Path(__file__).resolve().parents[3]
    copyfile(repo / 'config/settings.toml.example', root / 'config/settings.toml')
    old = {name: os.environ.get(name) for name in ('CHRIPTMAS_APP_ROOT', 'CHRIPTMAS_DEPLOY')}
    os.environ['CHRIPTMAS_APP_ROOT'], os.environ['CHRIPTMAS_DEPLOY'] = str(root), 'server'
    try:
        _check(root)
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _check(root):
    from fastapi.testclient import TestClient
    from backend.memory_app.app import create_server_app
    from backend.memory_app.document_recognition import ensure_document_experience
    from backend.memory_app.source_egress import SourceEgressService, recognition_service
    from backend.memory_app.v2.document_filings import SHARED_FILINGS, SHARED_IMPORTS, filing_view
    from backend.memory_app.workspace_confirmation import WorkspaceConfirmation
    from backend.memory_app.workspace_generation import _draft
    from backend.recognition import WorkScope
    from backend.shared.deployment import DeploymentLayout
    from core.product_core.workbench_original_asset import StoreWorkbenchOriginalAsset, link_workbench_original_asset_to_source

    server = create_server_app(layout=DeploymentLayout('server', root / 'users/local-user', root), port=4224)
    devices, users, shared = server.state.device_registry, server.state.server_users, server.state.shared_projects
    paired = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='移入测试管理员')
    admin = devices.authenticate(paired['key'])
    project = shared.create(admin, project_name='共享移入验证', operation_id='shared-filing-create')['project']
    viewer_user = users.create(admin, name='移入测试只看成员')
    viewer_device = devices.exchange(devices.issue_pairing(user_id=viewer_user['user_id'], actor=admin.device_id)['code'],
        name='移入测试只看设备')
    viewer = devices.authenticate(viewer_device['key'])
    shared.invite(admin, project['id'], user_id=viewer.user_id, role='viewer', authority=project['authority'],
        operation_id='shared-filing-invite')
    invitation = shared.invitations(viewer)[0]
    shared.accept(viewer, invitation['id'], expected_revision=invitation['revision'], operation_id='shared-filing-accept')
    headers = {'Authorization': 'Bearer ' + paired['key'], 'Idempotency-Key': 'shared-filing-request'}
    base = '/api/v2/server/shared-projects/' + project['id']
    options = {'base_url': 'http://127.0.0.1:4224', 'client': ('127.0.0.1', 16001)}
    with TestClient(server, **options) as client:
        available = client.get(base + '/filing-target', headers=headers)
        assert available.status_code == 200, available.text
        denied = client.get(base + '/filing-target', headers={'Authorization': 'Bearer ' + viewer_device['key']})
        assert denied.status_code == 403, denied.text
        state = server.state.server_user_pool._children[admin.user_id].application.state
        records, documents = state.recognition_records, state.recognition_documents
        personal = state.recognition_runtime_root
        item_id = 'workspace-' + uuid4().hex
        attachment = personal / 'workspace' / (item_id + '.txt')
        attachment.parent.mkdir(exist_ok=True)
        attachment.write_text('保留原件中的完整采购数量和金额。', encoding='utf-8')
        text = attachment.read_text(encoding='utf-8')
        draft = _draft({'title': '采购原件', 'summary': text, 'topics': [], 'facts': [], 'todos': [],
            'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}, text)
        item = state.workspace_domains.items.create_upload({'id': item_id, 'project_id': 'default',
            'input_kind': 'file', 'title': '采购原件', 'source_text': text, 'status': 'ready', 'draft': draft,
            'error': None, 'document_id': None, 'created_at': shared.now(), 'original_path': str(attachment),
            'original_name': '采购原件.txt'})
        source = WorkspaceConfirmation(personal, records, documents).confirm(
            item_id, 'default', item['revision'], lambda _draft: '确认稿保留条件和结论。')
        extra = '确认时附带的第二份原件。'.encode('utf-8')
        assets = state.workspace_domains.query.source_store
        asset = StoreWorkbenchOriginalAsset(object_store=assets, assets_root=personal / 'library/assets/originals',
            namespace_id=documents.namespace_id).execute(display_name='补充附件.txt', media_type='text/plain',
                size_bytes=len(extra), content_base64=base64.b64encode(extra).decode('ascii'))
        link_workbench_original_asset_to_source(object_store=assets, namespace_id=documents.namespace_id,
            asset_ref=asset.asset_ref, source_id=source['source_id'],
            source_uri=f'crp://{documents.namespace_id}/sources/{source["source_id"]}', now=shared.now())
        document_id = source['document_id']
        url = base + '/filings/' + document_id
        request = {'source_project_id': 'default', 'expected_revision': 1, 'scene': None,
            'target_namespace': available.json()['namespace']}
        # 用真实 SQLite 写入故障中断源库提交，保留先提交的共享数据。
        with sqlite3.connect(records.database_path) as sql:
            sql.execute("""CREATE TRIGGER fail_shared_filing_recall BEFORE INSERT ON crp_structured_records
                WHEN NEW.collection = 'v2_document_recall'
                BEGIN SELECT RAISE(ABORT, 'simulated_source_storage_interruption'); END""")
        client._transport.raise_server_exceptions = False
        try:
            first = client.post(url, headers=headers, json=request)
        finally:
            client._transport.raise_server_exceptions = True
        assert first.status_code == 500, first.text
        data = shared.roots._data(project['id'])
        retained, = data.records.list(SHARED_IMPORTS)
        target_id = retained.payload['document_id']
        assert data.documents.markdown(target_id) == '确认稿保留条件和结论。'
        assert records.read('v2_document_recall', document_id) is None
        assert records.read(SHARED_FILINGS, document_id).payload['state'] == 'pending'
        command = client.get(url + '/commands/shared-filing-request', headers=headers)
        assert command.status_code == 200 and command.json()['state'] == 'pending', command.text
        with sqlite3.connect(records.database_path) as sql:
            sql.execute('DROP TRIGGER fail_shared_filing_recall')
        result = client.post(url, headers=headers, json=request)
        assert result.status_code == 200 and result.json()['target_document_id'] == target_id, result.text
        assert len(data.records.list(SHARED_IMPORTS)) == 1 and len(data.records.list('workspace_items')) == 2
        assert filing_view(records, document_id, 'default')['target_scope_kind'] == 'shared'
        assert documents.markdown(document_id) == '确认稿保留条件和结论。'
        copies = data.records.list('workspace_items')
        retained_bytes = set()
        for copied in copies:
            download = client.get(base + '/library/originals/' + copied.object_id, headers=headers)
            assert download.status_code == 200, download.text
            retained_bytes.add(download.content)
        assert retained_bytes == {attachment.read_bytes(), extra}
        experience, _revision = ensure_document_experience(data.documents, recognition_service(data.records),
            project['id'], target_id)
        graph = SourceEgressService(data.records).snapshot(WorkScope('local-user', project['id']),
            [{'type': 'experience', 'id': experience, 'revision': 1}])
        assert {row['id'] for row in graph['nodes'] if row['type'] == 'original_item'} == {row.object_id for row in copies}
        assert client.post(url, headers=headers, json=request).json() == result.json()
