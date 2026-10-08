import json
import time
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.v2 import install_v2_routes
from backend.memory_app.workspace import install_workspace_routes
from backend.recognition import RecognitionService, WorkScope
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


class Model:
    def __init__(self):
        self.calls = 0
        self.insights = [{"text": "原文证据认识一", "conditions": []}, {"text": "原文证据认识二", "conditions": []}]
        self.fail = False
        self.fail_insight = False

    def public(self):
        return {"generation": {"base_url": "http://localhost/v1", "allow_remote": False}}

    def complete(self, messages, *, max_tokens, validate_current=None):
        self.calls += 1
        if validate_current:
            validate_current()
        if self.fail:
            raise RuntimeError("synthetic sensitive source")
        if '"insights"' in messages[0]["content"]:
            from tests.memory_app.v2.test_insight_generation import response_for
            if self.fail_insight:
                raise RuntimeError("synthetic sensitive source")
            return response_for(messages, json.dumps({"insights": self.insights})), {"model": "fake", "configuration_revision": 1}
        return json.dumps({"title": "整理稿", "summary": "原文证据摘要", "topics": [],
            "facts": [], "todos": [], "uncertainties": [], "people": [], "dates": [], "suggestions": []}), {}


def assemble(root, records, documents, service, model):
    app = FastAPI()
    from backend.memory_app.v2.devices import DeviceRegistry
    app.state.device_registry = DeviceRegistry(root / 'server')
    domains = install_workspace_routes(app, runtime_root=root, records=records,
        models=model, documents=documents, service=service)
    install_v2_routes(app, runtime_root=root, records=records, models=model,
        documents=documents, service=service, workspace=domains)
    return app, domains


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    documents, service, model = SQLiteDocumentRepository(records), RecognitionService(records), Model()
    app, domains = assemble(tmp_path, records, documents, service, model)
    with TestClient(app) as http:
        yield SimpleNamespace(root=tmp_path, records=records, documents=documents, service=service,
            model=model, app=app, domains=domains, http=http)


def post(env, **body):
    response = env.http.post("/api/v2/workbench/turns", json={"project_id": "alpha", "text": "原文证据", **body})
    assert response.status_code == 200, response.text
    return response.json()


def wait(env, result, project="alpha"):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        response = env.http.get(f"/api/v2/workbench/threads/{result['thread_id']}", params={"project_id": project})
        assert response.status_code == 200, response.text
        turn = next(row for row in response.json()["turns"] if row["id"] == result["turn"]["id"])
        if turn["receipt"]["remember"]["state"] != "processing":
            return turn
        time.sleep(.02)
    pytest.fail("background turn did not complete")


def test_full_chain_pending_confirm_drop_and_restart(env):
    result = post(env)
    assert result["turn"]["receipt"]["remember"]["state"] == "processing"
    turn = wait(env, result)
    receipt = turn["receipt"]["remember"]
    assert receipt["state"] == "done" and receipt["progress"] == {"done": 4, "total": 4}
    assert receipt["verified"] is False and len(receipt["insights"]) == 2
    assert all(row["state"] == "pending" for row in receipt["insights"])
    assert env.records.list("recognitions") == ()
    first, second = receipt["insights"]
    confirmed = env.http.post(f"/api/v2/library/insights/{first['id']}/confirm", json={"project_id": "alpha", "expected_revision": 1})
    assert confirmed.status_code == 200 and confirmed.json()["kind"] == "recognition"
    assert env.http.post(f"/api/v2/library/insights/{second['id']}/drop", json={"project_id": "alpha", "expected_revision": 1}).status_code == 200
    calls = env.model.calls
    other, _ = assemble(env.root, env.records, env.documents, env.service, env.model)
    with TestClient(other) as restarted:
        saved = restarted.get(f"/api/v2/workbench/threads/{result['thread_id']}?project_id=alpha").json()["turns"][0]
        assert len(saved["receipt"]["remember"]["insights"]) == 1
        assert saved["receipt"]["remember"]["insights"][0]["id"] == confirmed.json()["id"]
        assert restarted.get("/api/v2/workbench/threads?project_id=alpha").json()["items"][0]["id"] == result["thread_id"]
    assert env.model.calls == calls


def test_inspiration_is_local_and_defaults_to_inbox(env):
    result = post(env, text="灵感 原文证据", intent="inspiration")
    insight = result["turn"]["receipt"]["inspiration"]["insight"]
    assert insight["state"] == "pending" and env.model.calls == 0
    assert env.records.read("recognition_candidates", insight["id"]).payload["project_id"] == "inbox"
    assert env.http.get(f"/api/v2/workbench/threads/{result['thread_id']}?project_id=inbox").status_code == 200


def test_tag_resolves_project_name_and_scene(env):
    created = env.http.post("/api/v2/projects", json={"name": "研究"}).json()
    result = post(env, text="#研究/阅读 原文证据")
    turn = wait(env, result, created["id"])
    receipt = turn["receipt"]["remember"]
    assert turn["user_text"] == "原文证据"
    assert all(row["scene"] == "阅读" for row in receipt["insights"])
    assert env.records.read("workspace_items", receipt["item_id"]).payload["project_id"] == created["id"]
    assert env.http.get(f"/api/v2/workbench/threads/{result['thread_id']}?project_id=alpha").status_code == 404


@pytest.mark.parametrize("stage", ["intake", "insights"])
def test_failed_receipt_and_jobs_retry_preserve_frozen_item(env, stage):
    if stage == "intake":
        env.model.fail = True
    else:
        env.model.fail_insight = True
    result = post(env)
    turn = wait(env, result)
    receipt = turn["receipt"]["remember"]
    assert receipt["state"] == "failed" and receipt["error"]
    assert "synthetic sensitive" not in json.dumps(receipt)
    item = env.records.read("workspace_items", receipt["item_id"])
    assert item.payload["status"] == ("failed" if stage == "intake" else "confirmed")
    jobs = env.http.get("/api/v2/jobs?project_id=alpha").json()["items"]
    assert jobs[0]["state"] == "failed" and jobs[0]["target"] == {
        "type": "turn", "id": turn["id"], "turn_id": turn["id"], "thread_id": turn["thread_id"]}
    calls = env.model.calls
    assert wait(env, result)["receipt"]["remember"]["state"] == "failed"
    assert env.model.calls == calls
    env.model.fail = env.model.fail_insight = False
    retry = env.http.post(f"/api/v2/workbench/turns/{turn['id']}/retry", json={"project_id": "alpha"})
    assert retry.status_code == 200
    assert wait(env, result)["receipt"]["remember"]["state"] == "done"
    if stage == "insights":
        assert env.records.read("workspace_items", receipt["item_id"]) == item
    assert all(row["state"] != "failed" for row in env.http.get("/api/v2/jobs?project_id=alpha").json()["items"])


def test_empty_insights_is_success(env):
    env.model.insights = []
    receipt = wait(env, post(env))["receipt"]["remember"]
    assert receipt["state"] == "done" and receipt["insights"] == [] and receipt["error"] is None


def test_private_project_can_complete_local_intake_without_insight_model(env):
    from backend.memory_app.v2.privacy import set_private_project
    set_private_project(env.records, "alpha", True, 0)
    receipt = wait(env, post(env))["receipt"]["remember"]
    assert receipt["state"] == "done" and receipt["error"] is None and receipt["insights"] == []
    assert env.model.calls == 1
    assert env.records.read("workspace_items", receipt["item_id"]).payload["status"] == "confirmed"


def test_uploaded_item_is_reused_and_cross_project_is_rejected(env):
    uploaded = env.http.post("/api/v2/workbench/files", data={"project_id": "alpha"}, files={"file": ("notes.txt", "原文证据".encode(), "text/plain")})
    assert uploaded.status_code == 200 and "original_path" not in uploaded.json()
    item = uploaded.json()["id"]
    assert env.http.post("/api/v2/workbench/turns", json={"project_id": "beta", "item_id": item}).status_code == 404
    result = post(env, item_id=item, text="")
    receipt = wait(env, result)["receipt"]["remember"]
    assert receipt["item_id"] == item and receipt["state"] == "done"
    assert len(env.records.list("workspace_items")) == 1


def without_task_runtime(env):
    # Answer startup now installs an organization. Explicitly remove it to
    # exercise an unavailable runtime with no lazy construction fallback.
    del env.app.state.agent_organization_runtime
    assert getattr(env.app.state, 'container', None) is None
    assert getattr(env.app.state, 'recognition_turn_dispatcher', None) is None


@pytest.mark.parametrize("intent", ["ask", "do"])
def test_intent_availability_is_explicit_without_unnecessary_model_calls(env, intent):
    if intent == 'do':
        without_task_runtime(env)
    response = env.http.post("/api/v2/workbench/turns", json={"project_id": "alpha", "text": "原文证据", "intent": intent})
    if intent == "ask":
        assert response.status_code == 200 and response.json()["turn"]["receipt"]["ask"]["no_match"] is True
        assert len(env.records.list("v2_turns")) == 1
    else:
        assert response.status_code == 503 and response.json()["detail"] == "task_runtime_unavailable"
        assert env.records.list("v2_turns") == ()
    assert env.model.calls == 0


def test_unavailable_task_runtime_precedes_enabled_egress(env, monkeypatch):
    without_task_runtime(env)
    monkeypatch.setattr(env.model, 'public', lambda: {
        'generation': {'base_url':'https://example.invalid/v1', 'allow_remote':True}})
    response = env.http.post('/api/v2/workbench/turns', json={
        'project_id':'alpha', 'text':'synthetic task', 'intent':'do'})
    assert response.status_code == 503
    assert response.json()['detail'] == 'task_runtime_unavailable'
    assert env.records.list('v2_turns') == ()
    assert env.records.list('v2_task_executions') == ()
    assert env.model.calls == 0


def test_lazy_availability_check_runs_outside_event_loop(env, monkeypatch):
    without_task_runtime(env)
    import asyncio
    from backend.memory_app.v2 import _LazyOrganization
    original, checked = _LazyOrganization.available, []
    def available(self):
        with pytest.raises(RuntimeError, match='no running event loop'):
            asyncio.get_running_loop()
        checked.append(True)
        return original(self)
    monkeypatch.setattr(_LazyOrganization, 'available', available)
    response = env.http.post('/api/v2/workbench/turns', json={
        'project_id':'alpha', 'text':'synthetic task', 'intent':'do'})
    assert response.status_code == 503
    assert checked == [True]
    assert env.records.list('v2_turns') == ()
    assert env.model.calls == 0


def test_direct_organization_without_availability_keeps_egress_gate(env):
    from backend.memory_app.v2.workbench import install_workbench_routes
    from tests.memory_app.v2.test_workbench_do_agents import Organization
    organization = Organization()
    app = FastAPI()
    install_workbench_routes(app, records=env.records, models=env.model,
        documents=env.documents, service=env.service, workspace=env.domains,
        organization=organization, research_reader=organization.read,
        topology_reader=organization.topology)
    with TestClient(app) as client:
        response = client.post('/api/v2/workbench/turns', json={
            'project_id':'alpha', 'text':'synthetic task', 'intent':'do'})
    assert response.status_code == 409
    assert response.json()['detail'] == 'task_remote_blocked'
    assert env.records.list('v2_turns') == ()
    assert organization.calls == [] and env.model.calls == 0


def test_confirm_drop_scope_revision_and_alias_safety(env):
    candidate = post(env, intent="inspiration")["turn"]["receipt"]["inspiration"]["insight"]
    url = f"/api/v2/library/insights/{candidate['id']}"
    assert env.http.post(url + "/confirm", json={"project_id": "alpha", "expected_revision": 1}).status_code == 404
    assert env.http.post(url + "/confirm", json={"project_id": "inbox", "expected_revision": 2}).status_code == 409
    confirmed = env.http.post(url + "/confirm", json={"project_id": "inbox", "expected_revision": 1})
    assert confirmed.status_code == 200
    assert env.http.post(url + "/drop", json={"project_id": "inbox", "expected_revision": 1}).status_code == 409
    assert env.http.post(url + "/confirm", json={"project_id": "inbox", "expected_revision": 1}).status_code == 409
    assert len(env.records.list("recognitions")) == 1


def test_unknown_and_ambiguous_tags_have_no_side_effects(env):
    env.http.post("/api/v2/projects", json={"name": "同名"})
    env.http.post("/api/v2/projects", json={"name": "同名"})
    for text, code in [("#未知/阅读 原文", 404), ("#同名/阅读 原文", 409)]:
        response = env.http.post("/api/v2/workbench/turns", json={"project_id": "alpha", "text": text})
        assert response.status_code == code
    assert env.records.list("v2_turns") == () and env.records.list("workspace_items") == ()
    assert env.records.list("recognition_experiences") == ()


def test_builtin_tags_are_unambiguous_even_before_project_discovery(env):
    env.http.post("/api/v2/projects", json={"name": "我"})
    assert env.http.post("/api/v2/workbench/turns", json={"text": "#我 灵感 内容"}).status_code == 409
    result = post(env, text="#me 灵感 内容")
    assert env.http.get(f"/api/v2/workbench/threads/{result['thread_id']}?project_id=me").status_code == 200
    assert env.model.calls == 0


def test_default_tag_works_in_empty_project_registry_and_rejects_name_ambiguity(env):
    assert env.records.list("v2_projects") == ()
    result = post(env, text="#默认/阅读 原文证据", project_id="default")
    receipt = wait(env, result, "default")["receipt"]["remember"]
    assert receipt["state"] == "done" and all(row["scene"] == "阅读" for row in receipt["insights"])
    assert env.records.read("workspace_items", receipt["item_id"]).payload["project_id"] == "default"
    assert env.records.list("v2_projects") == ()
    env.http.post("/api/v2/projects", json={"name": "默认"})
    ambiguous = env.http.post("/api/v2/workbench/turns", json={"project_id": "default", "text": "#默认/阅读 原文证据"})
    assert ambiguous.status_code == 409 and ambiguous.json()["detail"] == "ambiguous_project_tag"
    assert len(env.records.list("workspace_items")) == 1
    explicit = post(env, text="#default/阅读 原文证据", project_id="default")
    assert wait(env, explicit, "default")["receipt"]["remember"]["state"] == "done"


def test_related_is_top_three_local_and_excludes_forgotten(env):
    scope = WorkScope("local-user", "alpha")
    published = []
    for n in range(5):
        experience = env.service.stage_experience(scope=scope, content=f"project alpha evidence {n}",
            provenance={"kind": "user_statement", "actor": "local-user"})
        candidate = env.service.propose(scope=scope, content=f"project alpha evidence {n}", source_experience_ids=[experience])
        published.append(env.service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer="local-user"))
    with env.records.begin() as tx:
        tx.put("recognition_recall_preferences", published[0].id,
            {"user_id": "local-user", "project_id": "alpha", "state": "forgotten"}, expected_revision=0)
        tx.commit()
    receipt = wait(env, post(env, text="project alpha evidence"))["receipt"]["remember"]
    assert receipt["state"] == "done" and len(receipt["related"]) == 3
    assert published[0].id not in {row["id"] for row in receipt["related"]}
    assert env.model.calls == 2


def test_related_excludes_own_published_candidate_alias(env, monkeypatch):
    from backend.memory_app.v2 import workbench
    original = workbench.generate_insights
    def generate_and_confirm(*args, **kwargs):
        insights = original(*args, **kwargs)
        env.service.publish(scope=WorkScope("local-user", "alpha"), candidate_id=insights[0]["id"],
            expected_revision=1, reviewer="local-user")
        return insights
    monkeypatch.setattr(workbench, "generate_insights", generate_and_confirm)
    receipt = wait(env, post(env))["receipt"]["remember"]
    assert receipt["state"] == "done" and receipt["related"] == []
    assert receipt["insights"][0]["kind"] == "recognition"


def test_link_with_note_reuses_safe_fetch_of_first_url(env, monkeypatch):
    from backend.memory_app import workspace_links
    seen = []
    monkeypatch.setattr(workspace_links, "_fetch_url", lambda url: seen.append(url) or "原文证据")
    result = post(env, text="https://example.com/article 备注")
    receipt = wait(env, result)["receipt"]["remember"]
    item = env.records.read("workspace_items", receipt["item_id"])
    assert seen == ["https://example.com/article"] and item.payload["input_kind"] == "link"
    assert result["turn"]["user_text"] == "https://example.com/article 备注"


def test_related_failure_persists_insights_and_restart_does_not_retry(env, monkeypatch):
    from backend.memory_app.v2 import workbench
    original = workbench.retrieve
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic sensitive related body")
    monkeypatch.setattr(workbench, "retrieve", fail)
    result = post(env)
    receipt = wait(env, result)["receipt"]["remember"]
    assert receipt["state"] == "failed" and receipt["error"] == "related_retrieval_failed"
    assert len(receipt["insights"]) == 2
    frozen = env.records.read("workspace_items", receipt["item_id"])
    calls = env.model.calls
    restarted, _ = assemble(env.root, env.records, env.documents, env.service, env.model)
    with TestClient(restarted) as http:
        saved = http.get(f"/api/v2/workbench/threads/{result['thread_id']}?project_id=alpha").json()["turns"][0]
        assert saved["receipt"]["remember"]["error"] == "related_retrieval_failed"
        assert len(saved["receipt"]["remember"]["insights"]) == 2 and env.model.calls == calls
    monkeypatch.setattr(workbench, "retrieve", original)
    env.http.post(f"/api/v2/workbench/turns/{result['turn']['id']}/retry", json={"project_id": "alpha"})
    assert wait(env, result)["receipt"]["remember"]["state"] == "done"
    assert env.model.calls == calls and env.records.read("workspace_items", receipt["item_id"]) == frozen


def test_new_turn_for_same_item_success_does_not_inherit_old_failure(env):
    env.model.fail_insight = True
    old = post(env)
    failed = wait(env, old)["receipt"]["remember"]
    env.model.fail_insight = False
    new = post(env, item_id=failed["item_id"], text="原文证据")
    assert wait(env, new)["receipt"]["remember"]["state"] == "done"
    assert wait(env, old)["receipt"]["remember"]["state"] == "failed"
    assert all(row["state"] != "failed" for row in env.http.get("/api/v2/jobs?project_id=alpha").json()["items"])


def test_interrupted_old_instance_is_readable_and_retryable(env):
    result = post(env)
    turn = wait(env, result)
    row = env.records.read("v2_turns", turn["id"])
    receipt = {"remember": {**row.payload["receipt"]["remember"], "state": "processing"}}
    with env.records.begin() as tx:
        tx.put("v2_turns", row.object_id, {**row.payload, "receipt": receipt, "instance": "old-instance"}, expected_revision=row.revision)
        tx.commit()
    calls = env.model.calls
    current = wait(env, result)
    assert current["receipt"]["remember"]["error"] == "interrupted" and env.model.calls == calls
    state = env.records.read("v2_workbench_item_states", row.object_id)
    with env.records.begin() as tx:
        tx.put("v2_workbench_item_states", row.object_id,
            {**state.payload, "state": "processing", "error": None}, expected_revision=state.revision)
        tx.commit()
    job = env.http.get("/api/v2/jobs?project_id=alpha").json()["items"][0]
    assert job["state"] == "failed" and job["error"] == "interrupted" and job["target"]["type"] == "turn"
    assert env.http.post(f"/api/v2/workbench/turns/{turn['id']}/retry", json={"project_id": "alpha"}).status_code == 200
    assert wait(env, result)["receipt"]["remember"]["state"] == "done" and env.model.calls == calls


def test_retry_polling_remains_processing_before_failed_item_is_reclaimed(env, monkeypatch):
    from starlette.concurrency import run_in_threadpool
    env.model.fail = True
    result = post(env)
    receipt = wait(env, result)["receipt"]["remember"]
    env.model.fail = False
    entered, release = threading.Event(), threading.Event()
    original = env.domains.intake.process
    async def gated(item_id, body):
        # Hold the dependency before its lease claim; the real intake and fake
        # model still run after release. This reproduces the original failed
        # item / new running turn window deterministically.
        entered.set()
        await run_in_threadpool(release.wait, 5)
        return await original(item_id, body)
    monkeypatch.setattr(env.domains.intake, "process", gated)
    try:
        response = env.http.post(f"/api/v2/workbench/turns/{result['turn']['id']}/retry", json={"project_id": "alpha"})
        assert response.status_code == 200 and entered.wait(2)
        assert env.records.read("workspace_items", receipt["item_id"]).payload["status"] == "failed"
        current = env.http.get(f"/api/v2/workbench/threads/{result['thread_id']}?project_id=alpha").json()["turns"][0]
        assert current["receipt"]["remember"]["state"] == "processing"
    finally:
        release.set()
    assert wait(env, result)["receipt"]["remember"]["state"] == "done"


def test_scene_failure_is_durable_and_retry_repairs_without_processing_again(env, monkeypatch):
    from backend.memory_app.v2 import auto_confirm
    original = auto_confirm.assign_scene
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic sensitive scene failure")
    project = env.http.post("/api/v2/projects", json={"name": "研究"}).json()["id"]
    monkeypatch.setattr(auto_confirm, "assign_scene", fail)
    result = post(env, text="#研究/阅读 原文证据")
    receipt = wait(env, result, project)["receipt"]["remember"]
    assert receipt["state"] == "failed" and receipt["error"] == "scene_assignment_failed"
    frozen = env.records.read("workspace_items", receipt["item_id"])
    assert frozen.payload["status"] == "confirmed" and env.model.calls == 1
    job = env.http.get("/api/v2/jobs", params={"project_id": project}).json()["items"][0]
    assert job["error"] == "scene_assignment_failed" and job["state"] == "failed"
    monkeypatch.setattr(auto_confirm, "assign_scene", original)
    assert env.http.post(f"/api/v2/workbench/turns/{result['turn']['id']}/retry", json={"project_id": project}).status_code == 200
    final = wait(env, result, project)["receipt"]["remember"]
    assert final["state"] == "done" and all(row["scene"] == "阅读" for row in final["insights"])
    assert env.model.calls == 2 and env.records.read("workspace_items", receipt["item_id"]) == frozen
