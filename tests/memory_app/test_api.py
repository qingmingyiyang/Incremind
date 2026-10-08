from __future__ import annotations

from time import monotonic, sleep
import importlib
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.app import create_app
from backend.memory_app.model_config import ModelConfigurationError
from backend.recognition import WorkScope
from backend.memory_app.model_config import ModelConfiguration
from backend.security.secrets import InMemorySecretStore
from core.storage_provider import SQLiteStructuredRecordStore


class FakeModels:
    def __init__(self):
        self.calls: list[list[dict]] = []
        self.configured = False

    def local_generation_allowed(self):
        return False

    def public(self):
        return {
            purpose: {
                "purpose": purpose, "provider": "openai", "base_url": "", "model": "",
                "allow_remote": False, "revision": 0, "has_api_key": False, "configured": self.configured,
            }
            for purpose in ("generation", "embedding", "rerank")
        }

    def update(self, purpose, data):
        self.configured = bool(data.get("model") and data.get("base_url") and data.get("api_key"))
        return {**self.public()[purpose], "model": data.get("model", ""), "base_url": data.get("base_url", ""), "has_api_key": bool(data.get("api_key")), "configured": self.configured}

    def complete(self, messages, *, max_tokens=1800, validate_current=None):
        if validate_current is not None:
            validate_current()
        self.calls.append(messages)
        return "生成的可编辑结果", {"model": "fake", "configuration_revision": 1, "usage": {}}


def test_restructure_api_requires_review_and_replays_without_revising_twice(tmp_path):
    client, models = _client(tmp_path)
    scope = WorkScope("local-user", "project-a")
    service = client.app.state.recognition_service
    try:
        eid = service.stage_experience(scope=scope, content="Evidence")
        draft = service.propose(scope=scope, content="Before", source_experience_ids=[eid])
        original = service.publish(scope=scope, candidate_id=draft.id,
            expected_revision=draft.revision, reviewer="local-user")
        from backend.recognition.restructuring import RestructureProposalService
        proposals = RestructureProposalService(service.records)
        snapshot = proposals.capture(scope=scope, recognition_ids=[original.id],
            expected_revisions={original.id: original.revision})
        saved = proposals.save(scope=scope, proposal_id="proposal-api", snapshot=snapshot,
            operation="revise", outputs=[{"content": "After", "conditions": [],
                "source_experience_ids": [eid], "source_recognition_ids": []}],
            reason="Clarify the conclusion",
            step_metadata={"source": "manual", "implementation_version": "manual-restructure-v1"})
        assert service.get_recognition(scope=scope, recognition_id=original.id).content == "Before"
        body = {"project_id": "project-a", "expected_revision": saved["revision"], "decision": "approved"}
        approved = client.patch("/api/recognition/restructure-proposals/proposal-api", json=body)
        assert approved.status_code == 200, approved.text
        replay = client.patch("/api/recognition/restructure-proposals/proposal-api", json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json() == approved.json()
        changed = service.get_recognition(scope=scope, recognition_id=original.id)
        assert changed.content == "After" and changed.revision == original.revision + 1
        assert models.calls == []
        foreign = client.get("/api/recognition/restructure-proposals?project_id=other")
        assert foreign.json() == {"items": []}
    finally:
        _shutdown(client)






def _client(tmp_path):
    models = FakeModels()
    app = create_app(runtime_root=tmp_path, legacy_app=FastAPI(), model_configuration=models)
    return TestClient(app), models




class TurnModels(ModelConfiguration):
    """Real governed config with a local, inspectable LiteLLM transport."""

    def __init__(self, records, root):
        self.calls: list[list[dict]] = []
        self.handler = lambda _messages, **_kwargs: "生成的可编辑结果"
        super().__init__(records, root, InMemorySecretStore(), completion_fn=self._complete)

    def _complete(self, **kwargs):
        messages = kwargs["messages"]
        self.calls.append(messages)
        result = self.handler(messages, **{key: value for key, value in kwargs.items() if key != "messages"})
        if isinstance(result, tuple):
            result = result[0]
        if kwargs.get('stream') is True:
            return iter([{'choices': [{'delta': {'content': result}, 'finish_reason': None}]},
                {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                 'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}}])
        return {"choices": [{"finish_reason": "stop", "message": {"content": result}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}}


def _turn_client(tmp_path):
    # Keep optional LiteLLM telemetry imports out of the execution deadline.
    importlib.import_module("litellm")
    records = SQLiteStructuredRecordStore(tmp_path / "recognition.sqlite3")
    models = TurnModels(records, tmp_path)
    models.update("generation", {"base_url": "https://example.test", "model": "test-model", "api_key": "synthetic-only",
                                  "allow_remote": True, "expected_revision": 0})
    app = create_app(runtime_root=tmp_path, legacy_app=FastAPI(), model_configuration=models)
    return TestClient(app), models


def _waiting(client, task_id):
    deadline = monotonic() + 12
    state = client.get(f"/api/recognition/tasks/{task_id}?project_id=project-a").json()
    while state["status"] != "waiting_approval" and monotonic() < deadline:
        sleep(.2)
        state = client.get(f"/api/recognition/tasks/{task_id}?project_id=project-a").json()
    assert state["status"] == "waiting_approval", state
    runtime = client.app.state.ai_runtime
    if not hasattr(runtime, "test_failures"):
        runtime.test_failures = []
        original_fail = runtime._fail
        def record_failure(turn_id, error):
            runtime.test_failures.append((type(error).__name__, str(error), repr(error.__cause__)))
            return original_fail(turn_id, error)
        runtime._fail = record_failure
    return state


def _approve(client, task):
    response = client.post(f"/api/recognition/tasks/{task['task_id']}/approve", json={
        "project_id": "project-a", "target_event_id": task["approval_event_id"], "expected_sequence": task["approval_sequence"],
    })
    assert response.status_code == 202, response.text
    return response.json()


def _terminal(client, task_id):
    deadline = monotonic() + 18
    state = client.get(f"/api/recognition/tasks/{task_id}?project_id=project-a").json()
    while state["status"] not in {"completed", "failed", "cancelled", "stale", "interrupted"} and monotonic() < deadline:
        sleep(.2)
        state = client.get(f"/api/recognition/tasks/{task_id}?project_id=project-a").json()
    assert state["status"] in {"completed", "failed", "cancelled", "stale", "interrupted"}, state
    if state["status"] in {"failed", "stale"}:
        record = client.app.state.recognition_service.records.read("recognition_tasks", task_id)
        print("Terminal projection:", record.payload.get("terminal_projection") if record else None)
        print("Turn failures:", getattr(client.app.state.ai_runtime, "test_failures", []))
        print("Last events:", [(event["type"], event["data"]) for event in tuple(client.app.state.ai_runtime.events_after(state["turn_id"]))[-4:]])
    return state


def _shutdown(client):
    runner = getattr(client.app.state, "ai_turn_runner", None)
    if runner is not None:
        runner.shutdown(timeout_seconds=5)
    from backend.api.mcp_runtime import shutdown_ai_mcp_runtime
    shutdown_ai_mcp_runtime(client.app)


def _completed_task(client, *, query, selected=(), temporary_note=""):
    from uuid import uuid4
    from backend.memory_app.context_adapter import compile_selected
    from backend.memory_app.packet_egress import capture_packet_egress
    from core.document_engine import DocumentDraft
    service = client.app.state.recognition_service
    scope = WorkScope("local-user", "project-a")
    records = service.records
    task_id = "task-history-" + uuid4().hex
    packet_id = "packet-history-" + uuid4().hex
    packet = compile_selected(scope.project_id, service.retrieval_entries(scope=scope), selected,
        query, client.app.state.recognition_models.public()["generation"]["revision"], temporary_note)
    packet.update({"id": packet_id, "context_packet_id": packet_id, "state": "consumed", "task_id": task_id})
    packet["source_egress"] = capture_packet_egress(service, scope, packet)
    document = client.app.state.recognition_documents.create(DocumentDraft(title=query,
        document_type="agent-result", markdown="生成的可编辑结果", project_id=scope.project_id,
        source_refs=({"source_id": task_id, "locator": "task://" + task_id},)))
    with records.begin() as tx:
        tx.put("recognition_context_packets", packet_id, packet, expected_revision=0)
        tx.put("recognition_tasks", task_id, {"id": task_id, "title": query, "input": query,
            "project_id": scope.project_id, "state": "completed", "document_id": document["id"],
            "context_packet_id": packet_id}, expected_revision=0)
        tx.commit()
    return packet, client.get(f"/api/recognition/tasks/{task_id}?project_id=project-a").json()


def _published(client):
    experience = client.post("/api/recognition/experiences", json={"project_id": "project-a", "content": "先用网页验证业务链"}).json()
    grant = client.put(f"/api/recognition/source-policies/experience/{experience['id']}", json={
        "project_id": "project-a", "expected_source_revision": experience["revision"],
        "expected_policy_revision": 0, "allowed_purposes": ["generation", "embedding", "rerank"]})
    assert grant.status_code == 200, grant.text
    candidate = client.app.state.recognition_service.propose(scope=WorkScope("local-user", "project-a"),
        source_experience_ids=[experience["id"]], content="网页优先，桌面壳后置")
    return client.patch(f"/api/recognition/candidates/{candidate.id}", json={"project_id": "project-a", "expected_revision": candidate.revision, "decision": "approve"}).json()


















def test_graph_exposes_scoped_sources_questions_and_bounded_neighborhood(tmp_path):
    client, _ = _client(tmp_path)
    recognition = _published(client)
    client.app.state.recognition_service.upsert_question(scope=WorkScope("local-user", "project-a"),
        question_id="historical-question", question="为什么网页优先", content="先验证业务",
        recognition_ids=[recognition["id"]], source_revisions={recognition["id"]:recognition["revision"]}, expected_revision=0)
    graph = client.get("/api/recognition/graph?project_id=project-a").json()
    assert {node["type"] for node in graph["nodes"]} == {"recognition", "experience", "question"}
    assert sum(node["selectable"] for node in graph["nodes"]) == 1
    assert len(graph["edges"]) == 2
    page = client.get("/api/recognition/graph?project_id=project-a&limit=1").json()
    assert len(page["nodes"]) == 1 and page["next_offset"] == 1 and page["total"] == 3
    assert page["edges"] == []
    focus_url = "/api/recognition/graph?project_id=project-a&focus=" + recognition["id"]
    assert len(client.get(focus_url).json()["nodes"]) == 3
    assert client.get(focus_url.replace("project-a", "project-b")).status_code == 409
    assert client.get("/api/recognition/graph?project_id=project-b").json()["nodes"] == []






def test_relation_edges_require_review_and_current_endpoint_versions(tmp_path):
    client, _ = _client(tmp_path)
    first, second = _published(client), _published(client)
    proposal = client.post("/api/recognition/relation-proposals", json={"project_id": "project-a",
        "from_id": first["id"], "to_id": second["id"], "relation": "supplements", "evidence": "人工判断补充关系"}).json()
    graph_url = "/api/recognition/graph?project_id=project-a"
    assert not any(edge["id"] == proposal["id"] for edge in client.get(graph_url).json()["edges"])
    assert len(client.get("/api/recognition/relation-proposals?project_id=project-a").json()["proposals"]) == 1
    reviewed = client.patch("/api/recognition/relation-proposals/" + proposal["id"], json={"project_id": "project-a", "expected_revision": 1, "decision": "approve"})
    assert reviewed.status_code == 200
    assert any(edge["id"] == proposal["id"] for edge in client.get(graph_url).json()["edges"])
    client.app.state.recognition_service.revise(scope=WorkScope("local-user", "project-a"),
        recognition_id=first["id"], expected_revision=1, content="修订后的新认识")
    assert not any(edge["id"] == proposal["id"] for edge in client.get(graph_url).json()["edges"])


def test_erasure_preview_confirms_actual_task_document_and_retained_experience(tmp_path):
    client, _ = _turn_client(tmp_path)
    try:
        recognition = _published(client)
        _packet, task = _completed_task(client, query="产生成果", selected=[recognition["id"]])
        retained = client.post(f"/api/recognition/tasks/{task['task_id']}/experience", json={"project_id": "project-a"}).json()
        endpoint = f"/api/recognition/recognitions/{recognition['id']}"
        base = {"project_id": "project-a", "expected_revision": recognition["revision"]}
        preview = client.post(endpoint + "/erase-preview", json=base).json()
        assert preview["counts"]["documents"] == 1
        assert preview["counts"]["recognition_experiences"] == 1
        payload = {**base, "preview_id": preview["preview_id"]}
        assert client.post(endpoint + "/erase", json=payload).status_code == 409
        payload["confirm"] = recognition["id"]
        erased = client.post(endpoint + "/erase", json=payload)
        assert erased.status_code == 200, erased.text
        snapshot = client.get("/api/recognition/workbench?project_id=project-a").json()
        assert snapshot["recognitions"] == [] and snapshot["documents"] == [] and snapshot["recent_tasks"] == []
        assert all(item["id"] != retained["experience_id"] for item in snapshot["experiences"])
        assert client.post(endpoint + "/erase", json=payload).json()["idempotent"] is True
    finally:
        _shutdown(client)




def test_readable_version_history_requires_project_scope(tmp_path):
    client, _ = _client(tmp_path)
    recognition = _published(client)
    client.app.state.recognition_service.revise(scope=WorkScope("local-user", "project-a"),
        recognition_id=recognition["id"], expected_revision=1, content="纠正后的正文")
    endpoint = f"/api/recognition/recognitions/{recognition['id']}/versions"
    assert client.get(endpoint + "?project_id=project-b").status_code == 409
    versions = client.get(endpoint + "?project_id=project-a").json()["versions"]
    assert [item["version"] for item in versions] == [2, 1]
    assert versions[0]["snapshot"]["content"] == "纠正后的正文"
    assert versions[1]["snapshot"]["content"] == recognition["content"]


def test_recall_priority_route_keeps_content_revision_and_manual_context(tmp_path):
    client, _ = _client(tmp_path)
    recognition = _published(client)
    endpoint = f"/api/recognition/recognitions/{recognition['id']}/recall"
    body = {"project_id": "project-a", "expected_revision": 1, "expected_preference_revision": 0, "state": "cooled"}
    assert client.patch(endpoint, json={**body, "project_id": "project-b"}).status_code == 409
    assert client.patch(endpoint, json=body).json()["recall_state"] == "cooled"
    assert client.patch(endpoint, json=body).status_code == 409
    item = client.get("/api/recognition/workbench?project_id=project-a").json()["recognitions"][0]
    assert item["revision"] == 1 and item["recall_state"] == "cooled"
    from backend.memory_app.context_adapter import compile_selected
    from backend.memory_app.recall_state import annotate
    service = client.app.state.recognition_service
    scope = WorkScope("local-user", "project-a")
    entries = annotate(service.records, scope, service.retrieval_entries(scope=scope))
    packet = compile_selected("project-a", entries, [recognition["id"]], "网页原型", 0)
    assert packet["items"][0]["id"] == recognition["id"]
    assert next(entry for entry in entries if entry["id"] == recognition["id"])["recall_state"] == "cooled"


def test_candidate_draft_must_be_saved_before_review_and_keeps_conditions(tmp_path):
    client, _ = _client(tmp_path)
    experience = client.post("/api/recognition/experiences", json={"project_id": "project-a", "content": "原型阶段网页验证"}).json()
    candidate_row = client.app.state.recognition_service.propose(scope=WorkScope("local-user", "project-a"),
        source_experience_ids=[experience["id"]], content="初稿")
    candidate = {"id": candidate_row.id, "revision": candidate_row.revision}
    endpoint = "/api/recognition/candidates/" + candidate["id"]
    review = {"project_id": "project-a", "expected_revision": 1, "decision": "approve", "content": "修正稿"}
    assert client.patch(endpoint, json=review).status_code == 409
    edit = {"project_id": "project-a", "expected_revision": 1, "content": "修正稿", "conditions": ["仅限原型阶段"]}
    assert client.patch(endpoint + "/draft", json={**edit, "project_id": "project-b"}).status_code == 409
    updated = client.patch(endpoint + "/draft", json=edit).json()
    assert updated["revision"] == 2 and updated["conditions"] == ["仅限原型阶段"]
    assert updated["source_experience_revisions"] == {experience["id"]: 1}
    assert client.get("/api/recognition/workbench?project_id=project-a").json()["recognitions"] == []
    assert client.patch(endpoint, json=review).status_code == 409
    published = client.patch(endpoint, json={**review, "expected_revision": 2}).json()
    assert published["content"] == "修正稿" and published["conditions"] == ["仅限原型阶段"]
    assert client.patch(endpoint + "/draft", json={**edit, "expected_revision": 3}).status_code == 409







def test_settings_never_returns_api_key_and_non_generation_is_not_faked(tmp_path):
    client, _models = _client(tmp_path)

    saved = client.put("/api/recognition/settings", json={"purpose": "embedding", "base_url": "http://127.0.0.1:9000", "model": "embed", "api_key": "private-key", "allow_remote": False, "expected_revision": 0})
    settings = client.get("/api/recognition/settings")
    tested = client.post("/api/recognition/settings/test", json={"purpose": "embedding"})

    assert saved.status_code == 200
    assert "private-key" not in settings.text
    assert tested.json()["status"] == "not_verified"


def test_generation_mode_api_requires_explicit_local_selection_and_keeps_remote_profile(tmp_path):
    client, models = _turn_client(tmp_path)
    local_client = TestClient(client.app, client=("127.0.0.1", 50123))
    model_file = tmp_path / "data/models/qwen2.5-1.5b-instruct/model.safetensors"
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(b"installed-model-test")
    local_request = {"model": "qwen2.5-1.5b-instruct", "messages": [{"role": "user", "content": "test"}]}
    try:
        original = client.get("/api/recognition/settings").json()
        assert original["generation_mode"]["mode"] == "api"
        assert local_client.post("/local-model/v1/chat/completions", json=local_request).status_code == 403
        chosen = client.put("/api/recognition/settings/generation-mode", json={
            "mode": "local", "local_enabled": True,
            "local_base_url": "http://127.0.0.1:8001/local-model/v1", "expected_revision": 0,
        })
        assert chosen.status_code == 200, chosen.text
        assert chosen.json()["mode"] == "local"
        assert models.snapshot("generation")["api_key"] == "local-model"
        assert client.get("/api/recognition/settings").json()["generation_mode"]["api_configured"] is True
        restored = client.put("/api/recognition/settings/generation-mode", json={
            "mode": "api", "local_enabled": False,
            "local_base_url": "http://127.0.0.1:8001/local-model/v1", "expected_revision": 1,
        })
        assert restored.status_code == 200, restored.text
        assert models.snapshot("generation")["api_key"] == "synthetic-only"
        assert local_client.post("/local-model/v1/chat/completions", json=local_request).status_code == 403
    finally:
        _shutdown(client)


def test_remote_origin_cannot_trigger_a_write(tmp_path):
    client, _models = _client(tmp_path)

    response = client.post("/api/recognition/experiences", headers={"Origin": "https://evil.example"}, json={"project_id": "project-a", "content": "不应写入"})

    assert response.status_code == 403
    assert client.get("/api/recognition/workbench?project_id=project-a").json()["experiences"] == []






def test_document_edit_conflict_project_scope_and_restart_readback(tmp_path):
    client, models = _turn_client(tmp_path)
    packet, task = _completed_task(client, query="生成成果")
    endpoint = "/api/recognition/documents/" + task["document_id"]
    assert client.get(endpoint + "?project_id=project-b").status_code == 409
    payload = {"project_id": "project-a", "expected_revision": 1, "markdown": "人工纠正的成果"}
    assert client.patch(endpoint, json=payload).json()["revision"] == 2
    assert client.patch(endpoint, json=payload).status_code == 409
    assert client.get("/api/recognition/workbench?project_id=project-a").json()["experiences"] == []
    selected = client.post(f"/api/recognition/tasks/{task['task_id']}/experience", json={"project_id": "project-a"})
    assert selected.status_code == 200
    experiences = client.get("/api/recognition/workbench?project_id=project-a").json()["experiences"]
    assert len(experiences) == 1 and "人工纠正的成果" in experiences[0]["content"]
    provenance = experiences[0]["provenance"]
    assert provenance["kind"] == "model_generated_artifact"
    assert provenance["epistemic_status"] == "unverified"
    assert provenance["outcome_status"] == "unknown"
    assert provenance["artifact_status"] == "committed"
    assert {"type": "document", "id": task["document_id"], "revision": 2} in provenance["source_refs"]
    retained_id = selected.json()["experience_id"]
    policy_path = f"/api/recognition/source-policies/experience/{retained_id}"
    policy = client.get(policy_path + "?project_id=project-a&revision=1")
    assert policy.status_code == 200, policy.text
    assert next(n for n in policy.json()["nodes"] if n["id"] == retained_id)["effective_purposes"] == ["embedding", "generation", "rerank"]
    private = client.put(policy_path, json={"project_id": "project-a", "expected_source_revision": 1,
        "expected_policy_revision": 0, "allowed_purposes": []})
    assert private.status_code == 200, private.text
    snapshot = client.get(policy_path + "?project_id=project-a&revision=1")
    assert next(n for n in snapshot.json()["nodes"] if n["id"] == retained_id)["effective_purposes"] == []
    grant = client.put(policy_path, json={"project_id": "project-a", "expected_source_revision": 1,
        "expected_policy_revision": 1, "allowed_purposes": ["generation", "embedding", "rerank"]})
    assert grant.status_code == 200, grant.text
    repeated = client.post(f"/api/recognition/tasks/{task['task_id']}/experience", json={"project_id": "project-a"}).json()
    assert repeated["already_retained"] is True
    assert repeated["experience_id"] == selected.json()["experience_id"]
    assert len(client.get("/api/recognition/workbench?project_id=project-a").json()["experiences"]) == 1
    _shutdown(client)
    restarted = TestClient(create_app(runtime_root=tmp_path, legacy_app=FastAPI(), model_configuration=models))
    try:
        assert restarted.get(endpoint + "?project_id=project-a").json()["markdown"] == "人工纠正的成果"
    finally:
        _shutdown(restarted)






def test_retained_edited_artifact_inherits_frozen_source_permissions(tmp_path):
    client, _ = _turn_client(tmp_path)
    try:
        recognition = _published(client)
        packet, task = _completed_task(client, query="Write a result", selected=[recognition["id"]])
        assert task["status"] == "completed", task
        edited = client.patch("/api/recognition/documents/" + task["document_id"], json={
            "project_id": "project-a", "expected_revision": 1, "markdown": "Human edited result"})
        assert edited.status_code == 200, edited.text
        retained = client.post(f"/api/recognition/tasks/{task['task_id']}/experience", json={"project_id": "project-a"})
        assert retained.status_code == 200, retained.text
        eid = retained.json()["experience_id"]
        endpoint = f"/api/recognition/source-policies/experience/{eid}"
        policy = client.get(endpoint + "?project_id=project-a&revision=1")
        assert policy.status_code == 200, policy.text
        node = next(n for n in policy.json()["nodes"] if n["id"] == eid)
        assert node["effective_purposes"] == ["embedding", "generation", "rerank"]
        # A retained historical revision remains valid when the live document
        # receives another user edit.
        later_edit = client.patch("/api/recognition/documents/" + task["document_id"], json={
            "project_id": "project-a", "expected_revision": 2, "markdown": "A later document revision"})
        assert later_edit.status_code == 200, later_edit.text
        unchanged = client.get(endpoint + "?project_id=project-a&revision=1")
        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json() == policy.json()
        experiences = client.get("/api/recognition/workbench?project_id=project-a").json()["experiences"]
        original = next(e for e in experiences if e["id"] != eid)
        grant = {"project_id": "project-a", "expected_source_revision": 1,
                 "expected_policy_revision": 1, "allowed_purposes": ["generation", "embedding", "rerank"]}
        assert client.put(f"/api/recognition/source-policies/experience/{original['id']}", json=grant).status_code == 200
        broaden = client.put(endpoint, json={**grant, "expected_policy_revision": 0})
        assert broaden.status_code == 200, broaden.text
        revoke = client.put(f"/api/recognition/source-policies/experience/{original['id']}",
                            json={**grant, "expected_policy_revision": 2, "allowed_purposes": []})
        assert revoke.status_code == 200, revoke.text
        policy = client.get(endpoint + "?project_id=project-a&revision=1")
        assert policy.status_code == 200, policy.text
        assert next(n for n in policy.json()["nodes"] if n["id"] == eid)["effective_purposes"] == []
        private_broaden = client.put(endpoint, json={**grant, "expected_policy_revision": 1})
        assert private_broaden.status_code == 422, private_broaden.text
    finally:
        _shutdown(client)


def test_restored_completed_task_and_retained_authority_read_without_turn_store(tmp_path):
    from backend.memory_app.backup import backup_database

    source_root = tmp_path / "source"
    client, _ = _turn_client(source_root)
    try:
        recognition = _published(client)
        _, task = _completed_task(client, query="Create a portable result", selected=[recognition["id"]])
        assert task["status"] == "completed", task
        retained = client.post(f"/api/recognition/tasks/{task['task_id']}/experience", json={"project_id": "project-a"})
        assert retained.status_code == 200, retained.text
        eid = retained.json()["experience_id"]
        policy_path = f"/api/recognition/source-policies/experience/{eid}?project_id=project-a&revision=1"
        original_policy = client.get(policy_path)
        assert original_policy.status_code == 200, original_policy.text
        backup = tmp_path / "backup.sqlite3"
        target = tmp_path / "restored"
        backup_database(source_root / "recognition.sqlite3", backup)
        backup_database(backup, target / "recognition.sqlite3")
    finally:
        _shutdown(client)
    restored = TestClient(create_app(runtime_root=target, legacy_app=FastAPI()))
    try:
        def unavailable_runtime():
            raise AssertionError("Restored completed tasks should not initialize the execution runtime")
        restored.app.state.recognition_turn_dispatcher._runtime = unavailable_runtime
        status = restored.get(f"/api/recognition/tasks/{task['task_id']}?project_id=project-a")
        assert status.status_code == 200, status.text
        assert status.json()["status"] == "completed"
        assert status.json()["document_id"] == task["document_id"]
        document = restored.get(f"/api/recognition/documents/{task['document_id']}?project_id=project-a")
        assert document.status_code == 200 and document.json()["markdown"]
        policy = restored.get(policy_path)
        assert policy.status_code == 200 and policy.json() == original_policy.json()
        assert not restored.app.state.recognition_models.public()["generation"]["has_api_key"]
    finally:
        _shutdown(restored)


def test_manual_experience_provenance_cannot_be_forged(tmp_path):
    client, _ = _client(tmp_path)
    payload = {"project_id": "project-a", "content": "部署已完成"}
    created = client.post("/api/recognition/experiences", json=payload)
    assert created.status_code == 200
    provenance = created.json()["provenance"]
    assert provenance["kind"] == "user_statement"
    assert provenance["epistemic_status"] == "user_asserted"
    assert provenance["source_refs"] == []
    forged = client.post("/api/recognition/experiences", json={
        **payload, "provenance": {"kind": "model_generated_artifact", "epistemic_status": "verified"},
    })
    assert forged.status_code == 422


def test_merge_api_can_select_evidence_and_replace_conditions(tmp_path):
    client, _ = _client(tmp_path)
    service = client.app.state.recognition_service
    scope = WorkScope("local-user", "project-a")
    parents = []
    evidence = []
    try:
        for index in range(2):
            eid = service.stage_experience(scope=scope, content=f"Evidence {index}")
            evidence.append(eid)
            draft = service.propose(scope=scope, content=f"Parent {index}",
                source_experience_ids=[eid], conditions=[f"Condition {index}"])
            parents.append(service.publish(scope=scope, candidate_id=draft.id,
                expected_revision=draft.revision, reviewer="local-user"))
        response = client.post("/api/recognition/recognitions/merge", json={
            "project_id": "project-a", "expected_revisions": {p.id: p.revision for p in parents},
            "content": "Selected supported conclusion", "source_experience_ids": [evidence[0]],
            "source_recognition_ids": [], "replacement_conditions": ["New condition"],
        })
        assert response.status_code == 200, response.text
        merged = service.get_recognition(scope=scope, recognition_id=response.json()["id"])
        assert merged.source_experience_ids == (evidence[0],)
        assert merged.conditions == ("New condition",)
        assert all(service.get_recognition(scope=scope, recognition_id=p.id).state == "superseded" for p in parents)
    finally:
        _shutdown(client)


def test_merge_api_rejects_null_explicit_selection_before_mutation(tmp_path):
    client, _ = _client(tmp_path)
    try:
        for field in ("source_experience_ids", "source_recognition_ids", "replacement_conditions"):
            response = client.post("/api/recognition/recognitions/merge", json={
                "project_id": "project-a", "expected_revisions": {"a": 1, "b": 1},
                "content": "A conclusion", field: None,
            })
            assert response.status_code == 422, response.text
            assert field in response.json()["detail"]
    finally:
        _shutdown(client)
