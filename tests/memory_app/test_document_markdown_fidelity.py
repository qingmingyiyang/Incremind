from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.app import create_app
from tests.memory_app.test_api import _client, _completed_task, _shutdown


SAMPLES = json.loads((Path(__file__).parents[1] / 'fixtures/rich_markdown/samples.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('sample', SAMPLES, ids=[sample['name'] for sample in SAMPLES])
def test_patch_preserves_markdown_bytes_in_sqlite_and_after_restart(tmp_path, sample):
    client, models = _client(tmp_path)
    _, task = _completed_task(client, query='合成保真稿')
    document_id = task['document_id']
    endpoint = '/api/recognition/documents/' + document_id
    markdown = sample['markdown']
    try:
        response = client.patch(endpoint, json={'project_id': 'project-a', 'expected_revision': 1, 'markdown': markdown})
        assert response.status_code == 200, response.text
        assert response.json()['markdown'].encode('utf-8') == markdown.encode('utf-8')
        assert client.app.state.recognition_documents.markdown(document_id).encode('utf-8') == markdown.encode('utf-8')
        assert client.get(endpoint + '?project_id=project-a').json()['markdown'].encode('utf-8') == markdown.encode('utf-8')
        assert models.calls == []
    finally:
        _shutdown(client)
    restarted = TestClient(create_app(runtime_root=tmp_path, legacy_app=FastAPI(), model_configuration=models))
    try:
        assert restarted.get(endpoint + '?project_id=project-a').json()['markdown'].encode('utf-8') == markdown.encode('utf-8')
    finally:
        _shutdown(restarted)


@pytest.mark.parametrize('invalid', [None, 7, [], '', ' \r\n\t ', 'x' * 40001], ids=['null', 'number', 'list', 'empty', 'whitespace', 'oversize'])
def test_markdown_preservation_keeps_original_validation_and_revision(tmp_path, invalid):
    client, _ = _client(tmp_path)
    _, task = _completed_task(client, query='合成校验稿')
    endpoint = '/api/recognition/documents/' + task['document_id']
    try:
        before = client.get(endpoint + '?project_id=project-a').json()
        rejected = client.patch(endpoint, json={'project_id': 'project-a', 'expected_revision': 1, 'markdown': invalid})
        assert rejected.status_code == 422
        assert rejected.json()['detail'] == 'markdown is invalid'
        assert client.get(endpoint + '?project_id=project-a').json() == before
    finally:
        _shutdown(client)


def test_paragraph_edit_preserves_current_todo_ids_status_and_bytes(tmp_path):
    client, models = _client(tmp_path)
    _, task = _completed_task(client, query='合成待办稿')
    endpoint = '/api/recognition/documents/' + task['document_id']
    original = '# 标题\r\n\r\n旧摘要。\r\n\r\n## 待办\r\n- 联系甲\r\n- 联系乙\r\n'
    try:
        assert client.patch(endpoint, json={'project_id': 'project-a', 'expected_revision': 1, 'markdown': original}).status_code == 200
        before = client.get('/api/v2/todos?project_id=project-a').json()['items']
        assert [row['text'] for row in before] == ['联系甲', '联系乙']
        changed = original.replace('旧摘要。', '新摘要。')
        result = client.patch(endpoint, json={'project_id': 'project-a', 'expected_revision': 2, 'markdown': changed})
        assert result.status_code == 200
        assert result.json()['markdown'].encode('utf-8') == changed.encode('utf-8')
        assert client.get('/api/v2/todos?project_id=project-a').json()['items'] == before
        assert models.calls == []
    finally:
        _shutdown(client)
