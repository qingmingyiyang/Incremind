from copy import deepcopy
from threading import RLock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.processing_lease import ProcessingLease
from backend.memory_app.v2.projects import assign_scene, install_project_routes, scene_of
from backend.memory_app.workspace_items import WorkspaceItems
from core.storage_provider import SQLiteStructuredRecordStore


def test_scene_removal_and_same_name_restore_preserve_original_identity_and_history(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    application = FastAPI()
    install_project_routes(application, records=records)
    items = WorkspaceItems(records, ProcessingLease(records, 'workspace_items', 'scene-qa'), RLock())
    with TestClient(application) as client:
        project = client.post('/api/v2/projects', json={'name': '场景验证'}).json()
        path = f"/api/v2/projects/{project['id']}"
        added = client.patch(path, json={'expected_revision': project['revision'], 'scenes': ['会议', '预算']}).json()
        original = items.create(project['id'], 'text', '会议记录', '原始会议内容')
        assign_scene(records, 'item', original['id'], project['id'], '会议')
        before_item = deepcopy(records.read('workspace_items', original['id']))
        before_assignment = deepcopy(records.read('v2_scene_assignments_item', original['id']))
        removed = client.patch(path, json={'expected_revision': added['revision'], 'scenes': ['预算']})
        assert removed.status_code == 200
        assert removed.json()['scenes'] == ['预算']
        listed = client.get('/api/v2/projects').json()['items']
        assert next(row for row in listed if row['id'] == project['id'])['scenes'] == ['预算']
        assert items.item_for(original['id'], project['id']) == before_item
        assert scene_of(records, 'item', original['id']) == before_assignment.payload
        restored = client.patch(path, json={'expected_revision': removed.json()['revision'], 'scenes': ['预算', '会议']})
        assert restored.status_code == 200
        assert restored.json()['scenes'] == ['预算', '会议']
        assert records.read('workspace_items', original['id']) == before_item
        assert records.read('v2_scene_assignments_item', original['id']) == before_assignment


def test_stale_removal_returns_current_project_and_preserves_concurrent_scenes(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    application = FastAPI()
    install_project_routes(application, records=records)
    with TestClient(application) as client:
        project = client.post('/api/v2/projects', json={'name': '并发场景验证'}).json()
        path = f"/api/v2/projects/{project['id']}"
        added = client.patch(path, json={'expected_revision': project['revision'], 'scenes': ['会议', '预算']}).json()
        current = client.patch(path, json={'expected_revision': added['revision'], 'scenes': ['会议', '预算', '周报']}).json()
        stale = client.patch(path, json={'expected_revision': added['revision'], 'scenes': ['预算']})
        assert stale.status_code == 409
        assert stale.json()['current'] == current
        assert records.read('v2_projects', project['id']).payload['scenes'] == ['会议', '预算', '周报']
