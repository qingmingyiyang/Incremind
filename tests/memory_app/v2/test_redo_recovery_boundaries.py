from pathlib import Path
from shutil import copyfile

from fastapi.testclient import TestClient
import pytest

from backend.memory_app.v2.task_divisions import COLLECTION, TaskDivisions
from backend.recognition import RecognitionConflict
from core.storage_provider import SQLiteStructuredRecordStore


def test_removed_learning_sample_keeps_original_revision_and_cas(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    divisions = TaskDivisions(records)
    original = [{'goal': '整理材料', 'deliverable': '整理稿', 'capabilities': [], 'depends_on': []}]
    divisions.complete('sample', project='default', text='整理材料', items=original, outcome='done')
    edited = [{**original[0], 'goal': '保存的编辑'}]
    divisions.adjust('sample', project='default', items=edited, expected_revision=1)
    with pytest.raises(RecognitionConflict):
        divisions.remove('sample', project='default', expected_revision=1)
    divisions.remove('sample', project='default', expected_revision=2)
    row = records.read(COLLECTION, 'sample')
    assert row.revision == 3
    assert row.payload['deleted'] is True
    assert row.payload['items'] == edited
    assert original[0]['goal'] == '整理材料'
    with pytest.raises(ValueError, match='task_division_not_found'):
        divisions.read('sample', 'default')
    assert divisions.similar('default', '整理材料') == []
    divisions.complete('sample', project='default', text='整理材料', items=original, outcome='done')
    assert records.read(COLLECTION, 'sample') == row


def test_actual_unconfigured_app_rejects_private_command_and_redo_without_source(tmp_path):
    from backend.memory_app.app import create_app
    (tmp_path / 'config').mkdir()
    copyfile(Path(__file__).parents[3] / 'config/settings.toml.example', tmp_path / 'config/settings.toml')
    app = create_app(runtime_root=tmp_path)
    with TestClient(app) as client:
        response = client.post('/api/v2/workbench/turns', json={
            'project_id': 'default', 'intent': 'do', 'text': '整理材料', '_redo': 'missing'})
        assert response.status_code == 400
        assert response.json()['detail'] == 'invalid_turn_fields'
        for value in (True, 0):
            response = client.post('/api/v2/workbench/turns/missing/redo', json={
                'project_id': 'default', 'expected_revision': value})
            assert response.status_code == 400
        response = client.post('/api/v2/workbench/turns/missing/redo', json={
            'project_id': 'default', 'expected_revision': 1, 'source_kind': []})
        assert response.status_code == 400
        response = client.post('/api/v2/workbench/turns/missing/redo',
            headers={'Idempotency-Key': 'g07-missing-source'}, json={
                'project_id': 'default', 'expected_revision': 1, 'source_kind': 'execution'})
        assert response.status_code == 404
        assert app.state.recognition_records.list('v2_turns') == ()
        response = client.post('/api/v2/workbench/turns/missing/redo',
            headers={'Idempotency-Key': 'g07-missing-source'}, json={
                'project_id': 'default', 'expected_revision': 2, 'source_kind': 'execution'})
        assert response.status_code == 409
        assert response.json()['detail'] == 'idempotency_key_conflict'
        response = client.post('/api/v2/workbench/turns', json={
            'project_id': 'default', 'intent': 'do', 'text': '整理材料'})
        assert response.status_code == 409
        assert response.json()['detail'] == 'turn_interrupted'
        assert app.state.recognition_records.list('v2_turns') == ()
