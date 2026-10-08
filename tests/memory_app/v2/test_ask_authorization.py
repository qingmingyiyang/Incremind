import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.workspace import install_workspace_routes
from backend.memory_app.v2.privacy import set_private_project
from backend.recognition import RecognitionService
from core.document_engine import DocumentDraft, SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


from tests.memory_app.governed_model_fixture import GovernedModel


class Model(GovernedModel):
    def __init__(self):
        self.allowed, self.revision, self.mode_revision = True, 3, 4
        self.calls = 0
        self.before = self.after = lambda: None

    def public(self):
        return {"generation": {"provider": "openai", "base_url": "https://example.test/v1",
            "model": "fake", "allow_remote": self.allowed, "revision": self.revision},
            "generation_mode": {"revision": self.mode_revision}}

    def complete(self, messages, *, max_tokens, validate_current=None):
        self.before()
        validate_current()
        self.calls += 1
        self.after()
        return json.dumps({"answer": "证据回答", "citations": [1]}), {"usage": {"total_tokens": 12}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    documents = SQLiteDocumentRepository(records, namespace_id="recognition")
    documents.create(DocumentDraft(title="Evidence", document_type="legacy",
        markdown="alphaomega private evidence", project_id="alpha",
        source_refs=({"source_id": "synthetic", "locator": "text:0:1"},)))
    model, app = Model(), FastAPI()
    domains = install_workspace_routes(app, runtime_root=tmp_path, records=records,
        models=model, documents=documents, service=RecognitionService(records))
    with TestClient(app) as http:
        yield SimpleNamespace(records=records, model=model, http=http, domains=domains)


BODY = {"project_id": "alpha", "question": "alphaomega"}


def test_remote_question_runs_without_preview_or_per_request_consent(env):
    response = env.http.post("/api/workspace/v1/ask", json=BODY)
    assert response.status_code == 200, response.text
    assert response.json()["model_used"] is True and env.model.calls == 1
    receipt = env.records.list("workspace_ask_receipts")[0]
    assert receipt.payload["status"] == "completed"
    assert receipt.payload["consent_basis"] == {"scope": "global_setting", "settings_revision": {"generation": 3, "mode": 4}}
    assert "alphaomega" not in json.dumps(receipt.payload)
    assert "private evidence" not in json.dumps(receipt.payload)


@pytest.mark.parametrize("private", [False, True])
def test_global_or_private_block_prevents_question_wire(env, private):
    if private:
        set_private_project(env.records, "alpha", True, 0)
    else:
        env.model.allowed = False
    response = env.http.post("/api/workspace/v1/ask", json=BODY)
    assert response.status_code == 409
    assert response.json()["detail"] == ("private_project_remote_blocked" if private else "remote_disabled")
    assert env.model.calls == 0 and env.records.list("workspace_ask_receipts") == ()


@pytest.mark.parametrize("change", ["global", "private", "mode"])
@pytest.mark.parametrize("when", ["preview", "before_wire", "after_wire"])
def test_authorization_and_settings_are_rechecked_after_preview_and_at_wire(env, change, when):
    preview = env.http.post("/api/workspace/v1/ask/preview", json=BODY).json()
    def mutate():
        if change == "global":
            env.model.allowed = False
        elif change == "private":
            set_private_project(env.records, "alpha", True, 0)
        else:
            env.model.mode_revision += 1
    if when == "preview":
        mutate()
    else:
        setattr(env.model, "before" if when == "before_wire" else "after", mutate)
    response = env.http.post("/api/workspace/v1/ask", json={**BODY, "preview_id": preview["preview_id"]})
    assert response.status_code == 409, response.text
    expected = {"global": "remote_disabled", "private": "private_project_remote_blocked", "mode": "ask_model_target_changed"}
    assert response.json()["detail"] == expected[change]
    assert env.model.calls == (1 if when == "after_wire" else 0)
    receipts = env.records.list("workspace_ask_receipts")
    assert (receipts[0].payload["status"] == "failed") if when != "preview" else not receipts


def test_preview_scope_and_single_execution_remain_protected(env):
    preview = env.http.post("/api/workspace/v1/ask/preview", json=BODY).json()
    assert env.model.calls == 0
    request = {**BODY, "preview_id": preview["preview_id"]}
    assert env.http.post("/api/workspace/v1/ask", json={**request, "project_id": "other"}).status_code == 404
    first = env.http.post("/api/workspace/v1/ask", json=request)
    assert first.status_code == 200
    env.model.allowed = False
    set_private_project(env.records, "alpha", True, 0)
    assert env.http.post("/api/workspace/v1/ask", json=request).json() == first.json()
    assert env.model.calls == 1


def test_no_match_does_not_require_remote_authorization_or_create_receipt(env):
    env.model.allowed = False
    response = env.http.post("/api/workspace/v1/ask", json={**BODY, "question": "unfindablexyz"})
    assert response.status_code == 200 and response.json()["no_match"] is True
    assert env.model.calls == 0 and env.records.list("workspace_ask_receipts") == ()


@pytest.mark.parametrize("native_code", ["model_configuration_changed_before_request", "model_egress_remote_not_consented"])
@pytest.mark.parametrize("change", ["global", "private", "configuration"])
def test_native_model_guard_failure_is_mapped_before_query_callback(env, monkeypatch, native_code, change):
    from backend.memory_app.model_config import ModelConfigurationError
    def fail_before_callback(messages, *, max_tokens, validate_current=None):
        if change == "global":
            env.model.allowed = False
        elif change == "private":
            set_private_project(env.records, "alpha", True, 0)
        else:
            env.model.revision += 1
        raise ModelConfigurationError(native_code)
    monkeypatch.setattr(env.model, "complete", fail_before_callback)
    response = env.http.post("/api/workspace/v1/ask", json=BODY)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"global": "remote_disabled", "private": "private_project_remote_blocked",
        "configuration": "ask_model_target_changed"}[change]
    assert env.model.calls == 0 and env.records.list("workspace_ask_receipts")[0].payload["status"] == "failed"
