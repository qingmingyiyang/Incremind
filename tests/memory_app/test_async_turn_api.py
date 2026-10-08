from time import monotonic, sleep
import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.mcp_runtime import shutdown_ai_mcp_runtime
from backend.memory_app.app import create_app
from backend.memory_app.model_config import ModelConfiguration
from backend.security.secrets import InMemorySecretStore
from core.storage_provider import SQLiteStructuredRecordStore
from core.document_engine import DocumentDraft, SQLiteDocumentRepository


def _client(tmp_path):
    # The preserved gateway imports LiteLLM even with a synthetic transport.
    # Load that dependency during fixture setup so the task deadline measures
    # execution, not filesystem-dependent first-time library imports.
    import importlib
    importlib.import_module("litellm")
    calls = []
    def completion(**_kwargs):
        calls.append(True)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "private result"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}}
    records = SQLiteStructuredRecordStore(tmp_path / "recognition.sqlite3")
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=completion)
    models.update("generation", {"base_url": "https://example.test", "model": "test-model", "api_key": "synthetic",
                                 "allow_remote": True, "expected_revision": 0})
    app = create_app(runtime_root=tmp_path, legacy_app=FastAPI(), model_configuration=models)
    return TestClient(app), calls


def _close(client):
    runner = getattr(client.app.state, "ai_turn_runner", None)
    if runner is not None:
        runner.shutdown(timeout_seconds=5)
    shutdown_ai_mcp_runtime(client.app)










@pytest.mark.parametrize("state", ["result_ready", "cancelled", "failed", "stale"])
def test_unpublished_task_document_is_hidden_from_every_workbench_route(tmp_path, state):
    client, calls = _client(tmp_path)
    records = client.app.state.recognition_records
    document = SQLiteDocumentRepository(records, namespace_id="recognition").create(DocumentDraft(
        title="staged-result", document_type="agent-result", markdown="unpublished content", project_id="project-a",
        source_refs=({"source_id": "task-staged", "locator": "task://task-staged"},)))
    with records.begin() as tx:
        tx.put("recognition_tasks", "task-staged", {"project_id": "project-a", "state": state,
               "document_id": document["id"], "input": "test", "context_packet_id": "packet-staged"}, expected_revision=0)
        tx.commit()
    status = client.get("/api/recognition/tasks/task-staged?project_id=project-a").json()
    assert status["document_id"] is None
    assert status["status"] == ("running" if state == "result_ready" else state)
    assert client.get("/api/recognition/workbench?project_id=project-a").json()["documents"] == []
    endpoint = f"/api/recognition/documents/{document['id']}"
    assert client.get(endpoint + "?project_id=project-a").status_code == 409
    assert client.patch(endpoint, json={"project_id": "project-a", "expected_revision": 1, "markdown": "edit"}).status_code == 409
    assert client.post("/api/recognition/tasks/task-staged/experience", json={"project_id": "project-a"}).status_code == 409
    assert calls == []
