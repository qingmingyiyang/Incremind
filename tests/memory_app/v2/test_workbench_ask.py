import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.source_egress import SourceEgressService, recognition_service
from backend.memory_app.v2 import install_v2_routes
from backend.memory_app.v2.auto_confirm import process_and_confirm
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2.projects import assign_scene
from backend.memory_app.workspace import install_workspace_routes
from backend.recognition import RecognitionService, WorkScope
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


from tests.memory_app.governed_model_fixture import GovernedModel


class Model(GovernedModel):
    def __init__(self):
        self.calls = 0
        self.allowed = True
        self.intake = False
        self.before = lambda: None
        self.after = lambda: None
        self.fail = False
        self.numbers = None

    def public(self):
        return {"generation": {"base_url": "https://example.invalid/v1", "allow_remote": self.allowed,
                              "revision": 2, "model": "fake", "configured": True, "has_api_key": True,
                              "provider": "openai", "purpose": "generation"}, "generation_mode": {"revision": 3}}

    def complete(self, messages, *, max_tokens, validate_current=None, wire_attempt_sink=None, timeout_seconds=None,
                 retry_policy=None):
        def calculate():
            if self.intake:
                return json.dumps({"title": "Synthetic", "summary": "alpha", "facts": [], "topics": [],
                    "todos": [], "uncertainties": [], "people": [], "dates": [], "suggestions": []}), {}
            self.before()
            if validate_current:
                validate_current()
            self.calls += 1
            self.messages = messages
            if self.fail:
                raise RuntimeError("synthetic secret must stay out of response")
            numbers = self.numbers if self.numbers is not None else [int(n) for n in re.findall(r"^\[(\d+)\]", messages[-1]["content"], re.MULTILINE)]
            self.after()
            return json.dumps({"answer": "Synthetic answer", "citations": numbers}), {"usage": {"total_tokens": 7}}
        if wire_attempt_sink is None:
            return calculate()
        wire = wire_attempt_sink.begin_model_wire_attempt()
        def invoke():
            try:
                text, meta = calculate()
                wire.succeeded(usage=meta.get("usage", {}), cache_observation=None)
                return text, meta
            except BaseException:
                wire.failed_transport(error_code="synthetic_failure")
                raise
        return wire.invoke_wire(invoke)


def assemble(root, records, documents, service, model):
    app = FastAPI()
    app.state.recognition_service = service
    from backend.memory_app.v2.devices import DeviceRegistry
    app.state.device_registry = DeviceRegistry(root / 'server')
    domains = install_workspace_routes(app, runtime_root=root, records=records, models=model,
        documents=documents, service=service)
    install_v2_routes(app, runtime_root=root, records=records, models=model, documents=documents,
        service=service, workspace=domains)
    return app, domains


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    documents, service, model = SQLiteDocumentRepository(records), recognition_service(records), Model()
    app, domains = assemble(tmp_path, records, documents, service, model)
    with TestClient(app) as http:
        yield SimpleNamespace(root=tmp_path, records=records, documents=documents, service=service,
                              model=model, http=http, domains=domains)


def publish(env, text="alpha beta gamma", project="alpha", doc=None):
    scope = WorkScope("local-user", project)
    experience = ensure_document_experience(env.documents, env.service, project, doc)[0] if doc else env.service.stage_experience(scope=scope, content="Synthetic evidence")
    proposed = env.service.propose(scope=scope, content=text, source_experience_ids=[experience])
    return env.service.publish(scope=scope, candidate_id=proposed.id, expected_revision=1, reviewer="local-user"), experience


def add_document(env, project="alpha", summary="alpha", body="alpha", original="alpha beta gamma", scene=None):
    env.model.intake = True
    item = asyncio.run(env.domains.intake.add_text({"project_id": project, "text": original}))
    result = asyncio.run(process_and_confirm(env.domains, item["id"], project))
    doc = result["document_id"]
    env.documents.save_user_edit(doc, expected_revision=1,
        markdown="# Synthetic\n\n## 摘要\n" + summary + "\n\n## 正文\n" + body)
    env.model.intake = False
    if scene:
        assign_scene(env.records, "document", doc, project, scene)
    return doc, item["id"]


def ask(env, text="alpha beta gamma?", **body):
    return env.http.post("/api/v2/workbench/turns", json={"project_id": "alpha", "text": text, **body})


def test_ask_receipt_fields_persona_counts_trace_and_durable_read(env):
    primary, _ = publish(env)
    persona, _ = publish(env, text="alpha beta gamma my preferred tools are notebooks", project="me")
    response = ask(env)
    assert response.status_code == 200, response.text
    saved = response.json()
    turn, receipt = saved["turn"], saved["turn"]["receipt"]["ask"]
    assert turn["intent"] == "ask" and turn["user_text"] == "alpha beta gamma?"
    assert set(receipt) == {"answer", "citations", "layers", "trace", "egress_receipt_id", "no_match",
                            "context", "model_usage", "model_cost", "excluded_sources"}
    assert receipt['model_cost'] is None
    assert receipt["answer"] == "Synthetic answer" and receipt["no_match"] is False
    assert receipt["layers"] == {"insight": 1, "summary": 0, "note": 0, "source": 0, "persona": 1}
    parts = {part["key"]: part for part in receipt["context"]["parts"]}
    assert parts["insight"]["count"] == 1 and parts["persona"]["count"] == 1
    assert parts["question"]["count"] == 1 and parts["instruction"]["count"] == 1
    assert receipt["model_usage"] == {"total_tokens": 7}
    assert receipt["trace"] == [{"layer": "insight", "considered": 1, "selected": 1, "skipped_budget": 0, "coverage": 1.0, "stopped": True,
        "condensed_question": None, "condense_receipt_ids": [], "condense_status": "skipped",
        "condense_usage_known": False, "history_turn_ids": [],
        "rewrite": {"queries": [], "used": False}, "rewrite_status": "skipped", "rewrite_receipt_ids": [], "bookshelf": {"hits": 0, "used": 0}}]
    assert [c["id"] for c in receipt["citations"]] == [primary.id]
    assert [c["persona"] for c in receipt["citations"]] == [False]
    assert all(set(c) == {"n", "layer", "persona", "id", "title", "quote", "locator"} for c in receipt["citations"])
    egress = env.records.read("workspace_ask_receipts", receipt["egress_receipt_id"])
    assert egress.payload["status"] == "completed" and '"content"' not in json.dumps(egress.payload)
    assert "alpha beta gamma" not in json.dumps(egress.payload)
    assert [{**t, "layer": "insight"} for t in egress.payload["trace"]] == receipt["trace"]
    assert env.model.calls == 1
    for _ in range(2):
        assert env.http.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=alpha").json()["turns"] == [turn]
    app, _ = assemble(env.root, env.records, env.documents, env.service, env.model)
    with TestClient(app) as restarted:
        assert restarted.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=alpha").json()["turns"] == [turn]
    assert env.model.calls == 1
    assert env.http.post(f"/api/v2/workbench/turns/{turn['id']}/retry", json={"project_id": "alpha"}).status_code == 409


def test_no_match_has_complete_empty_receipt_without_model_or_egress(env):
    env.model.allowed = False
    response = ask(env, intent="ask")
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["no_match"] is True and receipt["answer"]
    assert receipt["citations"] == [] and receipt["egress_receipt_id"] is None
    assert receipt["context"]["window"] is None and receipt["context"]["reserve"] is None
    assert all(part["tokens"] == 0 and part["count"] == 0 for part in receipt["context"]["parts"])
    assert receipt["layers"] == dict.fromkeys(("insight", "summary", "note", "source", "persona"), 0)
    assert len(receipt["trace"]) == 4 and all(t["selected"] == 0 for t in receipt["trace"])
    assert env.model.calls == 0 and env.records.list("workspace_ask_receipts") == ()


def test_tag_name_scene_and_cross_project_thread_are_checked_before_model(env):
    project = env.http.post("/api/v2/projects", json={"name": "研究"}).json()["id"]
    included, _ = publish(env, project=project)
    excluded, _ = publish(env, project=project)
    assign_scene(env.records, "recognition", included.id, project, "阅读")
    assign_scene(env.records, "recognition", excluded.id, project, "写作")
    response = ask(env, text="#研究/阅读 alpha beta gamma?")
    assert response.status_code == 200, response.text
    saved = response.json()
    assert [c["id"] for c in saved["turn"]["receipt"]["ask"]["citations"]] == [included.id]
    assert saved["turn"]["user_text"] == "alpha beta gamma?"
    assert env.records.read("v2_turns", saved["turn"]["id"]).payload["project_id"] == project
    assert env.http.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=alpha").status_code == 404
    assert ask(env, thread_id=saved["thread_id"]).status_code == 404
    assert env.model.calls == 1


@pytest.mark.parametrize("change", ["global", "private", "source", "provider"])
def test_permission_source_and_model_failures_preserve_safe_query_errors(env, change):
    _, experience = publish(env)
    if change == "global":
        env.model.before = lambda: setattr(env.model, "allowed", False)
    elif change == "private":
        env.model.before = lambda: set_private_project(env.records, "alpha", True, 0)
    elif change == "source":
        env.model.before = lambda: SourceEgressService(env.records).set_policy(
            WorkScope("local-user", "alpha"), "experience", experience, 1, 0, [])
    else:
        env.model.fail = True
    response = ask(env)
    assert response.status_code == (502 if change == "provider" else 409), response.text
    assert response.json()["detail"] == {"global": "remote_disabled", "private": "private_project_remote_blocked",
        "source": "source_changed_retry", "provider": "answer_generation_failed"}[change]
    assert "synthetic secret" not in response.text
    assert env.records.list("v2_turns") == () and env.records.list("v2_threads") == ()
    assert env.records.list("workspace_ask_receipts")[0].payload["status"] == "failed"


def test_current_private_project_keeps_remote_409_before_no_match(env):
    set_private_project(env.records, "alpha", True, 0)
    assert ask(env).status_code == 409
    assert env.model.calls == 0 and env.records.list("v2_turns") == ()


def test_citation_subset_maps_number_to_same_document_layer_and_sent_counts(env):
    doc, item = add_document(env, summary="alpha", body="beta", original="gamma 原文")
    env.model.numbers = [3, 1]
    response = ask(env, text="alpha beta gamma 原文?")
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert [c["n"] for c in receipt["citations"]] == [3, 1]
    assert [(c["layer"], c["id"]) for c in receipt["citations"]] == [("source", item), ("summary", doc)]
    assert receipt["layers"] == {"insight": 0, "summary": 1, "note": 1, "source": 1, "persona": 0}
    from backend.shared.llm.litellm_gateway import _estimate_input_tokens
    context = receipt["context"]
    parts = {part["key"]: part for part in context["parts"]}
    assert [parts[key]["count"] for key in ("summary", "note", "source")] == [1, 1, 1]
    assert parts["insight"]["count"] == 0 and parts["persona"]["count"] == 0
    assert sum(part["tokens"] for part in context["parts"]) == _estimate_input_tokens(env.model.messages)
    assert [t["layer"] for t in receipt["trace"]] == ["insight", "summary", "note", "source"]


def test_legacy_result_with_unknown_context_does_not_invent_zero_input():
    from backend.memory_app.v2.workbench import _ask_receipt
    receipt = _ask_receipt({"trace": []}, (), {"answer": "old answer", "sources": []}, "old")
    assert receipt["context"] is None
    for citation in receipt["citations"]:
        content = "gamma 原文" if citation["layer"] == "source" else env.documents.markdown(doc)
        windows = citation["locator"]["windows"]
        assert "\n…\n".join(content[w["start"]:w["end"]] for w in windows) == citation["quote"]
        assert citation["locator"]["coordinate_space"] == ("workspace_source_text_v1" if citation["layer"] == "source" else "document_markdown_v1")


def test_disjoint_source_windows_remain_exact_in_locator_and_quote(env):
    content = "alpha " + "middle-gap " * 400 + " beta"
    env.domains.query.source_store.write("sources", "window-source", {"id": "window-source", "title": "Original",
        "project_id": "alpha", "metadata": {"content": content}}, expected_revision=0)
    response = ask(env, text="alpha beta?")
    assert response.status_code == 200, response.text
    citation = response.json()["turn"]["receipt"]["ask"]["citations"][0]
    assert citation["layer"] == "source" and citation["id"] == "window-source"
    assert citation["locator"]["coordinate_space"] == "source_content_v1"
    windows = citation["locator"]["windows"]
    assert len(windows) == 2 and windows[0]["end"] < windows[1]["start"]
    assert citation["quote"] == "\n…\n".join(content[w["start"]:w["end"]] for w in windows)
    from backend.memory_app.v2.budget import text_tokens
    assert "\n…\n" in citation["quote"]
    assert all(text_tokens(content[w["start"]:w["end"]]) <= 1200 for w in windows)


def test_existing_thread_accepts_consecutive_ask_turns_once_each(env):
    first = env.http.post("/api/v2/workbench/turns", json={"project_id": "alpha", "text": "Synthetic idea", "intent": "inspiration"}).json()
    publish(env, project="inbox")
    answers = []
    for _ in range(2):
        response = ask(env, project_id="inbox", thread_id=first["thread_id"])
        assert response.status_code == 200, response.text
        turn = response.json()["turn"]
        assert "ask" in turn["receipt"] and response.json()["thread_id"] == first["thread_id"]
        answers.append(turn)
    assert answers[0]["id"] != answers[1]["id"]
    assert env.model.calls == 3
    assert answers[1]["receipt"]["ask"]["trace"][0]["condense_status"] == "failed"
    assert env.http.get(f"/api/v2/workbench/threads/{first['thread_id']}?project_id=inbox").json()["turns"][1:] == answers
    assert env.model.calls == 3
    assert answers[1]["receipt"]["ask"]["trace"][0]["condense_status"] == "failed"


def test_context_lists_all_sent_entries_not_only_answer_citations(env):
    primary, _ = publish(env)
    persona, _ = publish(env, text='alpha beta gamma my preferred tools are notebooks', project='me')
    env.model.numbers = [1]
    response = ask(env)
    assert response.status_code == 200
    receipt = response.json()['turn']['receipt']['ask']
    context = receipt['context']
    assert len(receipt['citations']) == 1
    assert {entry['id'] for entry in context['entries']} == {primary.id, persona.id}
    assert all(set(entry) == {'layer', 'id', 'title', 'tokens', 'persona'} for entry in context['entries'])
    assert all(type(entry['tokens']) is int and entry['tokens'] > 0 for entry in context['entries'])
    assert sorted(entry['persona'] for entry in context['entries']) == [False, True]
    egress = env.records.read('workspace_ask_receipts', receipt['egress_receipt_id']).payload
    assert context['egress'] == {'model': egress['target']['model'],
        'consent_scope': egress['consent_basis']['scope'],
        'settings_revision': egress['consent_basis']['settings_revision'], 'excluded_private': None}
    assert 'quote' not in json.dumps(context['entries']) and 'content' not in json.dumps(context['entries'])
