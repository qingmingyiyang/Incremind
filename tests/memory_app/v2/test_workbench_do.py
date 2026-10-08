import time
from pathlib import Path

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    (tmp_path / 'config').mkdir(exist_ok=True)
    (tmp_path / 'config' / 'settings.toml').write_bytes((Path(__file__).parents[3] / 'config' / 'settings.toml.example').read_bytes())
    from tests.memory_app.test_api import TurnModels, _shutdown
    from backend.memory_app.storage_authority import resolve_recognition_document_store
    from backend.memory_app.app import create_app
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import importlib
    importlib.import_module("litellm")
    records, _namespace = resolve_recognition_document_store(tmp_path)
    model = TurnModels(records, tmp_path)
    model.update("generation", {"base_url": "https://example.test", "model": "test-model",
        "api_key": "synthetic-only", "allow_remote": True, "expected_revision": 0})
    client = TestClient(create_app(runtime_root=tmp_path, legacy_app=FastAPI(), model_configuration=model))
    with client:
        yield client, model
    _shutdown(client)




@pytest.mark.parametrize('blocked_by', ['private_project', 'global_switch'])
def test_private_project_blocks_empty_context_task_before_creation(env, blocked_by):
    client, model = env
    from backend.memory_app.v2.privacy import set_private_project
    records = client.app.state.recognition_service.records
    if blocked_by == 'private_project':
        set_private_project(records, 'project-a', True, 0)
    else:
        model.update('generation', {'allow_remote':False, 'expected_revision':1})
    response = client.post('/api/v2/workbench/turns', json={'project_id': 'project-a', 'intent': 'do', 'text': '写一段总结'})
    assert response.status_code == 409, response.text
    assert records.list('recognition_tasks') == ()
    assert model.calls == []
