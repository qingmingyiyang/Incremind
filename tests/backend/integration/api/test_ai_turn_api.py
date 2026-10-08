from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.ai_runtime import RecallFirstLocalPlanner
from backend.api import ai_runtime
from backend.api.routes.ai import router
from backend.api.routes import include_api_routers
from backend.model_routing_snapshot import (
    payload_requirement_identity,
    turn_model_routing_snapshot_revision,
    validate_turn_model_routing_snapshot,
)
from core.ai_kernel import AIKernelContractError, CapabilityDefinition, CapabilityManifest, ContextEntry, ContextManifest, InMemoryTurnEventStore, InMemoryTurnPayloadStore, InMemoryTurnStateStore, MemoryRecallCapability, RunLeaseToken, ScopedCapabilityRegistry, SQLiteAITurnStore, SynchronousAIRuntime, TurnReceipt
from core.search_and_recall import RecallHit
from core.model_gateway import ModelResult
from backend.api.workbench_ai_runtime import WorkbenchQuestionPlanner


ROOT = Path(__file__).resolve().parents[4]


class _Recall:
    def recall(self, query):
        assert query.project_id == "project-alpha"
        return (RecallHit("atom-1", "atom", "durable evidence", ("source-1",), "verified", 0.95),)


def test_ai_turn_post_returns_after_durable_acceptance_before_slow_planner_finishes(tmp_path: Path) -> None:
    gate = Event()
    started = Event()

    class SlowPlanner:
        def plan(self, request, events, capabilities, payloads, execution_control=None):
            started.set()
            gate.wait(5)
            return RecallFirstLocalPlanner().plan(
                request, events, capabilities, payloads, execution_control,
            )

    client = _client(_runtime(tmp_path / "ai-turns.sqlite3", planner=SlowPlanner()))
    before = time.monotonic()
    try:
        response = client.post("/api/ai/turns", json=_request())
        elapsed = time.monotonic() - before
        assert response.status_code == 202
        assert response.json()["status"] == "accepted"
        assert elapsed < 2
        assert started.wait(1)
        projection = client.get(
            f"/api/ai/turns/{response.json()['turn_id']}/events?view=simple"
        ).json()["projection"]
        assert projection["terminal"] is False
    finally:
        gate.set()


def test_ai_turn_api_replays_after_runtime_restart_and_supports_event_cursor(tmp_path: Path) -> None:
    database = tmp_path / "ai-turns.sqlite3"
    request = _request()
    first = _client(_runtime(database))
    submitted = first.post("/api/ai/turns", json=request)
    assert submitted.status_code == 202
    receipt = submitted.json()
    assert receipt["status"] == "accepted" and submitted.headers["cache-control"] == "no-store"
    _wait_for_terminal(first, receipt["turn_id"])

    events = first.get(f"/api/ai/turns/{receipt['turn_id']}/events")
    assert events.status_code == 200
    assert "projection" not in events.json()
    simple = first.get(f"/api/ai/turns/{receipt['turn_id']}/events?view=simple")
    developer = first.get(f"/api/ai/turns/{receipt['turn_id']}/events?view=developer")
    assert simple.json()["projection"]["view"] == "simple"
    assert simple.json()["projection"]["current_stage"]["kind"] == "completed"
    assert "events" not in simple.json()
    assert developer.json()["projection"]["view"] == "developer"
    assert developer.json()["projection"]["tool_steps"][0]["capability_id"] == "memory.recall"
    sequence = events.json()["events"][-2]["sequence"]
    tail = first.get(f"/api/ai/turns/{receipt['turn_id']}/events?after={sequence}&view=simple")
    assert "events" not in tail.json()
    assert tail.json()["projection"]["current_sequence"] == simple.json()["projection"]["current_sequence"]

    restarted = _client(_runtime(database))
    replay = restarted.post("/api/ai/turns", json=request)
    assert replay.status_code == 202
    assert replay.json()["turn_id"] == receipt["turn_id"] and replay.json()["replayed"] is True


def test_ai_turn_api_rejects_invalid_body_identity_and_cursor(tmp_path: Path) -> None:
    client = _client(_runtime(tmp_path / "ai-turns.sqlite3"))
    invalid = _request()
    invalid.pop("scope")
    response = client.post("/api/ai/turns", json=invalid)
    assert response.status_code == 400 and response.json()["detail"] == "AI turn rejected"
    unknown = _request()
    unknown["provider"] = "bypass"
    assert client.post("/api/ai/turns", json=unknown).status_code == 400
    assert client.get("/api/ai/turns/missing/events").status_code == 404
    assert client.get("/api/ai/turns/missing/events?after=-1").status_code == 400
    assert client.get("/api/ai/turns/missing/events?after=1&view=simple").status_code == 404
    assert client.get("/api/ai/turns/missing/events?view=raw").status_code == 400

    action = _action("turn-0123456789abcdef0123456789abcdef", 1, "event-0123456789abcdef0123456789abcdef")
    mismatch = client.post("/api/ai/turns/turn-ffffffffffffffffffffffffffffffff/actions", json=action)
    assert mismatch.status_code == 400


def test_ai_turn_action_endpoint_uses_runner_owned_durable_lease() -> None:
    class LeaseRuntime:
        def __init__(self) -> None:
            self.acquired: list[RunLeaseToken] = []
            self.applied_with: list[RunLeaseToken | None] = []
            self.released: list[RunLeaseToken] = []

        def try_acquire_run_lease(self, turn_id, owner_id, *, now, stale_after):
            token = RunLeaseToken(turn_id, owner_id, 1)
            self.acquired.append(token)
            return token

        def apply_action(self, action, run_lease=None):
            self.applied_with.append(run_lease)
            return TurnReceipt(str(action["turn_id"]), "session-test", "op-test", "cancelled", 6, False)

        def events_after(self, turn_id, after_sequence=0):
            if not self.applied_with:
                return ()
            return ({"type": "approval.resolved", "sequence": after_sequence + 1},)

        def receipt_for(self, turn_id, *, replayed=False):
            return TurnReceipt(turn_id, "session-test", "op-test", "running", 6, replayed)

        def fail_accepted_turn(self, turn_id, run_lease=None):
            raise AssertionError("action should not use runner fallback")

        def release_strict_run_lease(self, token):
            self.released.append(token)

        def renew_run_lease(self, token, *, now, stale_after):
            return object()

        def request_background_cancel(self, turn_id, run_lease=None):
            return False

    runtime = LeaseRuntime()
    client = _client(runtime)  # type: ignore[arg-type]
    action = _action("turn-0123456789abcdef0123456789abcdef", 5, "event-0123456789abcdef0123456789")
    action.update({"type": "mcp_reject", "idempotency_key": "mcp-reject-api-lease-0001"})
    try:
        response = client.post(f"/api/ai/turns/{action['turn_id']}/actions", json=action)
        assert response.status_code == 202 and response.json()["status"] in {"running", "cancelled"}
        assert len(runtime.acquired) == 1
        assert runtime.applied_with == runtime.acquired
        assert runtime.released == runtime.acquired
    finally:
        client.app.state.ai_turn_runner.shutdown()


def test_ai_turn_action_returns_after_durable_fence_before_worker_completion() -> None:
    class SlowActionRuntime:
        def __init__(self) -> None:
            self.resolved = Event()
            self.release = Event()
            self.released: list[RunLeaseToken] = []

        def try_acquire_run_lease(self, turn_id, owner_id, *, now, stale_after):
            return RunLeaseToken(turn_id, owner_id, 1)

        def apply_action(self, action, run_lease=None):
            self.resolved.set()
            assert self.release.wait(timeout=5)
            return TurnReceipt(str(action["turn_id"]), "session-test", "op-test", "completed", 7, False)

        def events_after(self, turn_id, after_sequence=0):
            if not self.resolved.is_set():
                return ()
            return ({"type": "approval.resolved", "sequence": after_sequence + 1},)

        def receipt_for(self, turn_id, *, replayed=False):
            return TurnReceipt(turn_id, "session-test", "op-test", "running", 6, replayed)

        def fail_accepted_turn(self, turn_id, run_lease=None):
            raise AssertionError("action should remain in the worker")

        def release_strict_run_lease(self, token):
            self.released.append(token)

        def renew_run_lease(self, token, *, now, stale_after):
            return object()

        def request_background_cancel(self, turn_id, run_lease=None):
            return False

    runtime = SlowActionRuntime()
    client = _client(runtime)  # type: ignore[arg-type]
    action = _action("turn-0123456789abcdef0123456789abcdef", 5, "event-0123456789abcdef0123456789")
    try:
        response = client.post(f"/api/ai/turns/{action['turn_id']}/actions", json=action)
        assert response.status_code == 202
        assert response.json()["status"] == "running"
        assert runtime.release.is_set() is False
        assert runtime.released == []
        replay = client.post(f"/api/ai/turns/{action['turn_id']}/actions", json=action)
        assert replay.status_code == 202
        assert replay.json()["replayed"] is True
        assert runtime.release.is_set() is False
    finally:
        runtime.release.set()
        for _ in range(100):
            if runtime.released:
                break
            time.sleep(0.01)
        client.app.state.ai_turn_runner.shutdown()
    assert len(runtime.released) == 1


def test_sqlite_ai_turn_action_returns_202_and_converges_through_projection(tmp_path: Path) -> None:
    client = _client(_runtime(tmp_path / "ai-turn-actions.sqlite3"))
    request = _request()
    request["capability_policy"] = {
        "allowed": ["memory.recall"],
        "denied": [],
        "require_approval": ["memory.recall"],
    }
    request["approval_policy"] = {"mode": "explicit", "auto_approve_read_only": False}
    submitted = client.post("/api/ai/turns", json=request)
    assert submitted.status_code == 202
    turn_id = submitted.json()["turn_id"]
    for _ in range(100):
        events = client.get(f"/api/ai/turns/{turn_id}/events").json()["events"]
        if events and events[-1]["type"] == "approval.required":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("AI Turn did not reach approval")
    action = _action(turn_id, events[-1]["sequence"], events[-1]["event_id"])
    accepted = client.post(f"/api/ai/turns/{turn_id}/actions", json=action)
    assert accepted.status_code == 202, accepted.json()
    assert accepted.json()["current_sequence"] >= events[-1]["sequence"] + 1
    _wait_for_terminal(client, turn_id)
    projection = client.get(f"/api/ai/turns/{turn_id}/events?view=simple").json()["projection"]
    assert projection["terminal"] is True
    assert projection["status"] == "completed"
    final_events = client.get(f"/api/ai/turns/{turn_id}/events").json()["events"]
    assert [item["type"] for item in final_events].count("approval.resolved") == 1
    assert [item["type"] for item in final_events].count("tool.completed") == 1
    client.app.state.ai_turn_runner.shutdown()


def test_ai_turn_projection_integrity_failure_is_not_reported_as_missing() -> None:
    class CorruptProjectionRuntime:
        def events_after(self, _turn_id, _after=0):
            return ({"sequence": 1},)

        def execution_projection_for(self, _turn_id, _view):
            raise AIKernelContractError("corrupt projection")

        def presentation_for(self, _turn_id):
            return None

    response = _client(CorruptProjectionRuntime()).get(
        "/api/ai/turns/turn-corrupt/events?view=simple"
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": "AI execution projection unavailable",
        "error_code": "ai.projection_integrity",
    }


def test_ai_turn_sse_uses_validated_coalesced_projection_and_cursor_contract(tmp_path: Path) -> None:
    client = _client(_runtime(tmp_path / "ai-turns.sqlite3"))
    receipt = client.post("/api/ai/turns", json=_request()).json()
    _wait_for_terminal(client, receipt["turn_id"])
    response = client.get(f"/api/ai/turns/{receipt['turn_id']}/stream?after=0")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, no-cache"
    lines = [line for line in response.text.splitlines() if line]
    event_id = int(next(line[4:] for line in lines if line.startswith("id: ")))
    payload = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
    assert payload["turn_id"] == receipt["turn_id"]
    assert payload["cursor"] == event_id == payload["projection"]["current_sequence"]
    assert payload["projection"]["view"] == "simple" and payload["projection"]["terminal"] is True
    encoded = response.text.lower()
    assert "payload_ref" not in encoded and "raw" not in encoded and "secret" not in encoded
    assert client.get(f"/api/ai/turns/{receipt['turn_id']}/stream?after=999").status_code == 409
    resumed = client.get(
        f"/api/ai/turns/{receipt['turn_id']}/stream?after=0",
        headers={"Last-Event-ID": str(event_id)},
    )
    assert resumed.status_code == 200 and resumed.text == ""


def test_ai_turn_sse_rejects_initial_projection_corruption() -> None:
    class CorruptProjectionRuntime:
        def events_after(self, _turn_id, _after=0):
            return ({"sequence": 1},)

        def execution_projection_for(self, _turn_id, _view):
            raise AIKernelContractError("corrupt projection")

    response = _client(CorruptProjectionRuntime()).get("/api/ai/turns/turn-corrupt/stream")
    assert response.status_code == 409
    assert response.json()["error_code"] == "ai.projection_integrity"


def test_production_composition_uses_remote_planner_only_with_turn_and_provider_consent(tmp_path: Path, monkeypatch) -> None:
    class Search:
        calls = 0

        def search(self, **_kwargs):
            self.calls += 1
            hit = SimpleNamespace(object_id="atom-1", layer="atom", content="local", source_refs=("source-1",), trust_status="verified", score=0.9)
            return SimpleNamespace(hits=(hit,))

    class Gateway:
        calls = 0

        def invoke(self, request):
            self.calls += 1
            assert request.privacy_scope == "remote_allowed"
            return ModelResult({"type": "complete", "summary": "remote", "evidence_refs": []}, "test", "model", {})

    search = Search()
    gateway = Gateway()
    resolutions = []
    monkeypatch.setattr(
        ai_runtime,
        "build_rebuild_object_store",
        lambda _root: (SimpleNamespace(namespace_id="default"), object()),
    )
    monkeypatch.setattr(ai_runtime, "build_library_search_service", lambda _root, _store: search)
    def resolve(*args, **kwargs):
        resolutions.append((args[1], kwargs["egress_purpose"], kwargs["egress_categories"]))
        return SimpleNamespace(gateway=gateway, egress_consented=True)
    monkeypatch.setattr(ai_runtime, "resolve_model_gateway_runtime", resolve)
    runtime = ai_runtime.build_ai_runtime(SimpleNamespace(root_dir=tmp_path))

    assert resolutions == [
        ("search.answer", "search_answer", ("instructions", "source_excerpt")),
        ("search.answer", "memory_candidate", ("instructions", "source_excerpt")),
        ("search.answer", "document_draft", ("instructions", "source_excerpt")),
        ("companion.chat", "companion_chat", ("instructions", "source_excerpt")),
        ("companion.vision", "companion_vision", ("image_frame", "instructions")),
            ("intake.classification", "intake_classification", ("instructions", "source_excerpt")),
            ("search.answer", "series_intake_organize", ("instructions", "source_excerpt")),
        ]

    local = _request()
    local_receipt = runtime.submit_turn(local)
    assert local_receipt.status == "completed"
    assert search.calls == 1 and gateway.calls == 0
    local_events = tuple(runtime.events_after(local_receipt.turn_id))
    assert any(event["type"] == "hook.invoked" for event in local_events)
    assert any(event["type"] == "tool.intent.recorded" for event in local_events)

    remote = _request()
    remote["turn_id"] = "turn-ffffffffffffffffffffffffffffffff"
    remote["operation_id"] = "op-remote-answer-0001"
    remote["idempotency_key"] = "remote-answer-key-0001"
    remote["privacy"] = {"mode": "remote_allowed", "allow_remote": True, "pii": "none", "consent_refs": [], "retention": "local_durable"}
    assert runtime.submit_turn(remote).status == "completed"
    assert gateway.calls == 1 and search.calls == 1


def test_full_api_install_has_one_owner_for_each_ai_turn_endpoint() -> None:
    app = FastAPI()
    include_api_routers(app)
    expected = {
        ("/api/ai/model-routing-profile", "GET"),
        ("/api/ai/model-routing-profile", "PUT"),
        ("/api/ai/media-hands-policy", "GET"),
        ("/api/ai/media-hands-policy/revisions", "POST"),
        ("/api/ai/media-ingress-selection", "GET"),
        ("/api/ai/media-ingress-selection/revisions", "POST"),
        ("/api/ai/turns", "POST"),
        ("/api/ai/turns/{turn_id}/events", "GET"),
        ("/api/ai/turns/{turn_id}/stream", "GET"),
        ("/api/ai/turns/{turn_id}/actions", "POST"),
        ("/api/ai/external-agents/context-sessions", "POST"),
        ("/api/ai/external-agents/context-sessions/{session_id}/resolve", "POST"),
        ("/api/ai/external-agents/context-sessions/{session_id}/changes", "GET"),
        ("/api/ai/external-agents/context-sessions/{session_id}/acknowledgements", "POST"),
            ("/api/ai/projects/{project_id}/capabilities", "GET"),
            ("/api/ai/projects/{project_id}/mcp-status", "GET"),
        ("/api/ai/projects/{project_id}/boundary-summary", "GET"),
        ("/api/ai/projects/{project_id}/boundary-mode", "POST"),
        ("/api/ai/projects/{project_id}/boundary-mode-commands/{command_id}", "GET"),
        ("/api/ai/projects/{project_id}/boundary-grants", "POST"),
        ("/api/ai/projects/{project_id}/boundary-grants/{grant_id}/revoke", "POST"),
        ("/api/ai/projects/{project_id}/boundary-grant-commands/{command_id}", "GET"),
        ("/api/ai/projects/{project_id}/capability-selection", "POST"),
        ("/api/ai/projects/{project_id}/capability-selection/confirmations", "POST"),
        ("/api/ai/projects/{project_id}/capability-selection-commands/{command_id}", "GET"),
        ("/api/ai/projects/{project_id}/source-permissions/current", "GET"),
        ("/api/ai/projects/{project_id}/source-permissions/grant", "POST"),
        ("/api/ai/projects/{project_id}/source-permissions/{permission_id}/revoke", "POST"),
        ("/api/ai/recovery-reviews", "GET"),
        ("/api/ai/recovery-reviews/{review_id}", "GET"),
            ("/api/ai/recovery-reviews/{review_id}/confirm-no-effect", "POST"),
            ("/api/ai/recovery-reviews/{review_id}/keep-quarantined", "POST"),
            ("/api/ai/governance/plugins/packages", "GET"),
            ("/api/ai/governance/plugins/packages/discover", "POST"),
            ("/api/ai/governance/plugins/packages/{plugin_id}/install-disabled", "POST"),
            ("/api/ai/governance/plugins/packages/{plugin_id}/skills/review", "POST"),
            ("/api/ai/governance/plugins/packages/{plugin_id}/skills/activate", "POST"),
            ("/api/ai/governance/plugins/packages/{plugin_id}/skills/disable", "POST"),
            ("/api/ai/governance/plugins/packages/{plugin_id}/tools/review", "POST"),
            ("/api/ai/governance/plugins/packages/{plugin_id}/tools/activate", "POST"),
            ("/api/ai/governance/plugins/packages/{plugin_id}/tools/disable", "POST"),
                ("/api/ai/governance/plugins/packages/{plugin_id}/tools/projects/{project_id}/enable", "POST"),
                ("/api/ai/governance/plugins/packages/{plugin_id}/mcp/review", "POST"),
                ("/api/ai/governance/plugins/packages/{plugin_id}/mcp/activate", "POST"),
                ("/api/ai/governance/plugins/packages/{plugin_id}/mcp/projects/{project_id}/enable", "POST"),
            }
    actual = [
        (route.path, method)
        for route in _installed_routes(app)
        for method in getattr(route, "methods", ())
        if route.path.startswith("/api/ai/")
    ]
    assert set(actual) == expected
    assert len(actual) == len(expected)


def test_recovery_review_api_exposes_only_public_metadata_and_rejects_authority_fields(monkeypatch, tmp_path: Path) -> None:
    public = {
        "review_id": "review-opaque",
        "project_id": "project-alpha",
        "status": "quarantined",
        "revision": 1,
        "reason_code": "ai.recovery_unknown_tool_effect",
        "created_at": "2026-08-24T00:00:00+00:00",
        "updated_at": "2026-08-24T00:00:00+00:00",
    }

    class Service:
        def __init__(self, _root): pass
        def list(self, **_kwargs): return (public,)
        def get(self, _review_id): return public
        def decide(self, *_args, **_kwargs): raise AssertionError("invalid body must not decide")

    monkeypatch.setattr("backend.api.routes.ai.AIRecoveryReviewService", Service)
    client = _client(_runtime(tmp_path / "turns.sqlite3"))
    response = client.get("/api/ai/recovery-reviews")
    assert response.status_code == 200 and response.json() == {"items": [public]}
    encoded = response.text.lower()
    assert all(name not in encoded for name in ("owner_id", "generation", "payload_ref", "arguments", "token"))
    forbidden = client.post(
        "/api/ai/recovery-reviews/review-opaque/confirm-no-effect",
        json={"expected_revision": 1, "owner_id": "client-owner", "generation": 99},
    )
    assert forbidden.status_code == 400


def test_keep_quarantined_action_uses_only_opaque_identity_and_revision(monkeypatch, tmp_path: Path) -> None:
    calls = []

    class Service:
        def __init__(self, _root): pass
        def decide(self, review_id, **kwargs):
            calls.append((review_id, kwargs))
            return {"review_id": review_id, "status": "quarantined", "revision": 2}

    monkeypatch.setattr("backend.api.routes.ai.AIRecoveryReviewService", Service)
    response = _client(_runtime(tmp_path / "turns.sqlite3")).post(
        "/api/ai/recovery-reviews/review-opaque/keep-quarantined",
        json={"expected_revision": 1},
    )
    assert response.status_code == 200
    assert calls == [("review-opaque", {"expected_revision": 1, "action": "keep_quarantined"})]


def test_confirm_returns_durable_wake_pending_when_runtime_start_fails(monkeypatch, tmp_path: Path) -> None:
    class Service:
        def __init__(self, _root): self.store = object()
        def decide(self, review_id, **_kwargs):
            return {"review_id": review_id, "status": "resume_queued", "revision": 2}

    monkeypatch.setattr("backend.api.routes.ai.AIRecoveryReviewService", Service)
    monkeypatch.setattr(
        "backend.api.routes.ai.get_or_build_ai_runtime",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("runtime unavailable")),
    )
    response = _client(_runtime(tmp_path / "turns.sqlite3")).post(
        "/api/ai/recovery-reviews/review-opaque/confirm-no-effect",
        json={"expected_revision": 1},
    )
    assert response.status_code == 202
    assert response.json()["wake_status"] == "pending_startup"


def test_workbench_turn_persists_model_synthesized_presentation() -> None:
    class Gateway:
        def invoke(self, request):
            assert request.privacy_scope == "remote_allowed"
            assert request.parameters["_model_routing_snapshot"]["requirement"]["required_capability"] == "structured"
            return ModelResult({"answer": "统一模型依据证据生成的结论", "citations": ["crp://default/memory/atom-1"]}, "provider-test", "model-test", {})

    runtime = _workbench_question_runtime(Gateway())
    request = _remote_workbench_request()

    receipt = runtime.submit_turn(request)

    assert receipt.status == "completed"
    presentation = runtime.presentation_for(receipt.turn_id)
    assert presentation is not None
    assert presentation["answer"]["text"] == "统一模型依据证据生成的结论"
    assert presentation["provider_status"] == "succeeded"
    assert "model_routing" not in presentation
    assert "prompt_cache_scope_identity" not in json.dumps(presentation)
    assert "model_routing_snapshot_revision" not in json.dumps(presentation)
    assert "model_routing_catalog_revision" not in json.dumps(presentation)


def test_workbench_persists_verified_route_and_provider_cache_receipt() -> None:
    class Gateway:
        def invoke(self, request):
            snapshot = request.parameters["_model_routing_snapshot"]
            selected = snapshot["selected"]
            sink = request.metadata_sink
            sink.model_call_routed(
                snapshot_ref=request.parameters["_model_routing_snapshot_ref"],
                snapshot_revision=request.parameters["_model_routing_snapshot_revision"],
                prompt_cache_scope_identity=snapshot["prompt_cache_scope"]["identity"],
                provider=selected["provider_id"],
                model=selected["model_name"],
                execution_location=selected["execution_location"],
            )
            sink.model_call_started(
                provider=selected["provider_id"], model=selected["model_name"],
            )
            sink.model_call_cache_observed(observation={
                "cache_read_input_tokens": 17,
                "cache_miss_input_tokens": 3,
            })
            sink.model_call_completed(usage={
                "input_tokens": 20, "output_tokens": 4, "total_tokens": 24,
            })
            return ModelResult(
                {"answer": "缓存事实可追溯", "citations": ["crp://default/memory/atom-1"]},
                selected["provider_id"], selected["model_name"],
                {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24},
            )

    runtime = _workbench_question_runtime(Gateway())
    receipt = runtime.submit_turn(_remote_workbench_request())

    assert receipt.status == "completed"
    events = tuple(runtime.events_after(receipt.turn_id))
    event_types = [event["type"] for event in events]
    requested_index = event_types.index("model.requested", event_types.index("tool.completed") + 1)
    routed_index = event_types.index("model.routed")
    completed_index = event_types.index("model.completed", routed_index + 1)
    assert requested_index < routed_index < completed_index
    routed = events[routed_index]
    assert routed["data"]["payload_ref"].startswith(
        f"crp://session/{receipt.turn_id}/turn-model-routing-snapshot-v1/"
    )
    terminal = events[completed_index]
    assert len(terminal["data"]["evidence_refs"]) == 2
    projection = runtime.execution_projection_for(receipt.turn_id, "developer")
    step = next(
        item for item in projection["model_steps"]
        if item["model_request_id"] == routed["correlation"]["model_request_id"]
    )
    assert step["routing_status"] == "recorded"
    assert step["prompt_cache_status"] == "recorded"
    assert step["prompt_cache"] == {
        "status": "reported",
        "source": "provider_usage",
        "cache_read_input_tokens": 17,
        "cache_write_input_tokens": None,
        "uncached_input_tokens": 3,
    }


def test_workbench_provider_failure_fallback_keeps_model_receipt_failed() -> None:
    class Gateway:
        def invoke(self, request):
            request.metadata_sink.model_call_started(provider="provider-test", model="model-test")
            request.metadata_sink.model_call_failed()
            raise OSError("provider unavailable")

    runtime = _workbench_question_runtime(Gateway())

    receipt = runtime.submit_turn(_remote_workbench_request())

    assert receipt.status == "completed"
    presentation = runtime.presentation_for(receipt.turn_id)
    assert presentation is not None and presentation["provider_status"] == "fallback_provider_error"
    events = tuple(runtime.events_after(receipt.turn_id))
    model_event = next(event for event in events if event["type"] == "model.failed")
    projection = runtime.execution_projection_for(receipt.turn_id, "developer")
    model_step = next(
        step for step in projection["model_steps"]
        if step["model_request_id"] == model_event["correlation"]["model_request_id"]
    )
    assert model_step["provider_id"] == "provider-test"
    assert model_step["model_id"] == "model-test"
    assert model_step["usage_status"] == "not_recorded"
    assert model_step["recorded"]["input"] is False
    assert model_step["recorded"]["output"] is False


def test_workbench_never_egresses_when_routing_snapshot_is_missing_or_tampered() -> None:
    class Gateway:
        def __init__(self):
            self.calls = 0

        def invoke(self, _request):
            self.calls += 1
            raise AssertionError("model gateway must not run without a trusted snapshot")

    for kwargs in (
        {"expose_routing_snapshot": False},
        {"tamper_routing_snapshot_binding": True},
    ):
        gateway = Gateway()
        runtime = _workbench_question_runtime(gateway, **kwargs)
        receipt = runtime.submit_turn(_remote_workbench_request())

        assert receipt.status == "completed"
        assert gateway.calls == 0
        presentation = runtime.presentation_for(receipt.turn_id)
        assert presentation is not None
        assert presentation["provider_status"] == "fallback_provider_error"


class _WorkbenchQuestionCapability:
    def invoke(self, _request):
        presentation = {
            "schema_version": "1.0.0",
            "status": "answered",
            "question": "项目结论是什么？",
            "answer": {"status": "evidence_found", "text": "本地证据摘要"},
            "answer_preview": "本地证据摘要",
            "evidence_items": [{"target_ref": "crp://default/memory/atom-1", "snippet": "证据"}],
            "source_links": [],
            "provider_call_performed": False,
            "provider_status": "fallback_not_configured",
            "provider_route": "",
            "privacy": {"mode": "local_only", "source_path_exposed": False, "provider_call_performed": False},
            "project_route": {"status": "explicit", "selected_project_id": "project-alpha", "ai_assist": None},
        }
        return {
            "summary": "本地证据摘要",
            "result": {"schema_version": "1.0.0", "kind": "workbench.question.answer", "content": presentation},
            "evidence_refs": ["crp://default/memory/atom-1"],
        }


class _WorkbenchRoutingSnapshotManifestResolver:
    """Minimal strict fixture authority for model-capable workbench Turns."""

    def __init__(self, payloads: InMemoryTurnPayloadStore, *, expose_snapshot: bool) -> None:
        self._payloads = payloads
        self._expose_snapshot = expose_snapshot

    def resolve(self, request, capabilities) -> CapabilityManifest:
        turn_id = str(request["turn_id"])
        capability_ids = tuple(item.capability_id for item in capabilities)
        snapshot_ref = None
        snapshot_revision = None
        if self._expose_snapshot:
            policy = request["context_policy"]
            assert isinstance(policy, dict)
            snapshot = _workbench_routing_snapshot(turn_id, capability_ids, policy)
            snapshot_ref = self._payloads.get_or_create_immutable_payload(
                turn_id, "turn-model-routing-snapshot-v1", snapshot,
            )
            snapshot_revision = turn_model_routing_snapshot_revision(snapshot)
        return CapabilityManifest(
            manifest_id=f"capability-manifest-{turn_id}",
            turn_id=turn_id,
            resolver_id="integration-workbench-routing-snapshot",
            profile_id="test-project-profile",
            profile_revision=1,
            capability_ids=capability_ids,
            excluded_reason_counts=(),
            descriptor_bytes=0,
            boundary_profile_id="test-boundary-profile",
            boundary_profile_revision=1,
            model_routing_snapshot_ref=snapshot_ref,
            model_routing_snapshot_revision=snapshot_revision,
        )


class _WorkbenchRoutingSnapshotContextResolver:
    def __init__(self, *, tamper_snapshot_binding: bool = False) -> None:
        self._tamper_snapshot_binding = tamper_snapshot_binding

    def resolve(self, request, capability_manifest_ref, capability_manifest) -> ContextManifest:
        entries = [ContextEntry(
            entry_id="context-entry-capability-manifest",
            kind="capability_manifest",
            source_ref=None,
            payload_ref=capability_manifest_ref,
            source_project_id="project-alpha",
            revision_identity="1",
            content_fingerprint=None,
            provenance_refs=(),
            disclosure="tool_only",
            selection_reason="kernel_execution_boundary",
            content_bytes=0,
        )]
        if capability_manifest.model_routing_snapshot_ref is not None:
            policy = request["context_policy"]
            assert isinstance(policy, dict)
            snapshot = _workbench_routing_snapshot(
                str(request["turn_id"]), capability_manifest.capability_ids, policy,
            )
            entries.append(ContextEntry(
                entry_id="context-entry-model-routing-snapshot",
                kind="model_routing_snapshot",
                source_ref=None,
                payload_ref=capability_manifest.model_routing_snapshot_ref,
                source_project_id="project-alpha",
                revision_identity=(
                    "tampered-routing-revision" if self._tamper_snapshot_binding
                    else capability_manifest.model_routing_snapshot_revision
                ),
                content_fingerprint=str(snapshot["catalog_revision"]),
                provenance_refs=(),
                disclosure="audit_only",
                selection_reason="turn_model_routing_authority",
                content_bytes=0,
            ))
        return ContextManifest(
            manifest_id=f"context-manifest-{request['turn_id']}",
            turn_id=str(request["turn_id"]),
            resolver_id="integration-workbench-routing-context",
            project_id="project-alpha",
            series_id=None,
            project_profile_id=capability_manifest.profile_id,
            project_profile_revision=capability_manifest.profile_revision,
            boundary_profile_id="test-boundary-profile",
            boundary_profile_revision=1,
            capability_manifest_ref=capability_manifest_ref,
            entries=tuple(entries),
            compactions=(),
            excluded_reason_counts=(),
            max_context_bytes=int(request["context_policy"]["max_context_bytes"]),
            selected_context_bytes=0,
        )


def _workbench_routing_snapshot(
    turn_id: str, capability_ids: tuple[str, ...], context_policy: dict[str, object],
) -> dict[str, object]:
    requirement = {
        "required_capability": "structured", "modality": "text",
        "output_contract": "json_object", "egress_purpose": "search_answer",
        "egress_categories": ["instructions", "source_excerpt"],
        "privacy_scope": "remote_allowed", "retention_policy": "turn_only",
        "protocol_version": "1.0.0", "capability_ids": sorted(capability_ids),
        "skill_snapshot_revision": None, "context_policy": context_policy,
        "input_refs": [],
    }
    selected = {
        "tier": "standard", "route_key": "tier.standard", "route_revision": 1,
        "provider_id": "provider-test", "provider_revision": "1",
        "model_name": "model-test", "adapter_kind": "openai-compatible",
        "execution_location": "remote",
        "reason": "integration_test_frozen_route",
    }
    route = {
        "route_key": "tier.standard", "provider_id": "provider-test",
        "provider_revision": "1", "model_name": "model-test",
        "adapter_kind": "openai-compatible", "enabled": True, "revision": 1,
    }
    prompt_scope = {
        "project_id": "project-alpha", "profile": ["test-project-profile", 1],
        "boundary": ["test-boundary-profile", 1], "skill_snapshot_revision": None,
        "protocol_version": "1.0.0",
        "requirement": payload_requirement_identity(
            "structured", "text", "json_object", "search_answer",
            ("instructions", "source_excerpt"), "remote_allowed", "turn_only",
            tuple(sorted(capability_ids)), context_policy, [],
        ),
        "selected": selected,
    }
    payload = {
        "schema_version": "1.0.0", "turn": {"turn_id": turn_id},
        "project": {"project_id": "project-alpha"},
        "profile": {"profile_id": "test-project-profile", "profile_revision": 1, "preferred_model_tier": "standard"},
        "boundary": {"profile_id": "test-boundary-profile", "profile_revision": 1},
        "requirement": requirement,
        "routing": {"profile_revision": 1, "rules_version": 1, "text_default_tier": "standard", "authority_binding": None},
        "registry": {"registry_revision": 0},
        "runtime": {"runtime_revision": 0, "mode": "inactive", "runtime_activation": False, "activation_fingerprint": None},
        "activation": {"activation_fingerprint": None, "binding_drift": False},
        "tiers": [
            {"tier": "fast", "route": None, "capabilities": [], "execution_location": None, "eligible": False, "exclusion_reasons": ["tier_unconfigured"]},
            {"tier": "standard", "route": route, "capabilities": ["text", "structured"], "execution_location": "remote", "eligible": True, "exclusion_reasons": []},
            {"tier": "deep", "route": None, "capabilities": [], "execution_location": None, "eligible": False, "exclusion_reasons": ["tier_unconfigured"]},
            {"tier": "vision", "route": None, "capabilities": [], "execution_location": None, "eligible": False, "exclusion_reasons": ["tier_not_applicable", "tier_unconfigured"]},
            {"tier": "image_generation", "route": None, "capabilities": [], "execution_location": None, "eligible": False, "exclusion_reasons": ["image_generation_unavailable", "tier_unconfigured"]},
        ],
        "selected": selected, "catalog_revision": "b" * 64,
        "prompt_cache_scope": {
            "identity": hashlib.sha256(json.dumps(prompt_scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
            "project_id": "project-alpha", "profile_id": "test-project-profile", "profile_revision": 1,
            "boundary_profile_id": "test-boundary-profile", "boundary_profile_revision": 1,
            "skill_snapshot_revision": None, "protocol_version": "1.0.0",
        },
    }
    return validate_turn_model_routing_snapshot(payload)


def _workbench_question_runtime(
    gateway, *, expose_routing_snapshot: bool = True,
    tamper_routing_snapshot_binding: bool = False,
) -> SynchronousAIRuntime:
    registry = ScopedCapabilityRegistry()
    registry.register(CapabilityDefinition("workbench.question.answer", 1, "read", False, "read_only", "crp://default/contracts/in.schema.json", "crp://default/contracts/out.schema.json"), _WorkbenchQuestionCapability())
    payloads = InMemoryTurnPayloadStore()
    return SynchronousAIRuntime(
        planner=WorkbenchQuestionPlanner(gateway),
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=payloads,
        state=InMemoryTurnStateStore(),
        manifest_resolver=_WorkbenchRoutingSnapshotManifestResolver(
            payloads, expose_snapshot=expose_routing_snapshot,
        ),
        context_manifest_resolver=_WorkbenchRoutingSnapshotContextResolver(
            tamper_snapshot_binding=tamper_routing_snapshot_binding,
        ),
    )


def _remote_workbench_request() -> dict[str, object]:
    request = _request()
    request["desired_outcome"] = "workbench.question.answer"
    request["capability_policy"] = {"allowed": ["workbench.question.answer"], "denied": [], "require_approval": []}
    request["privacy"] = {"mode": "remote_allowed", "allow_remote": True, "pii": "possible", "consent_refs": ["crp://default/consent/provider-egress-policy"], "retention": "local_durable"}
    return request


def _installed_routes(app: FastAPI):
    for installed in app.routes:
        original = getattr(installed, "original_router", None)
        if original is not None:
            yield from original.routes
        else:
            yield installed


def _client(runtime: SynchronousAIRuntime) -> TestClient:
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=ROOT)
    app.state.ai_runtime = runtime
    app.include_router(router)
    return TestClient(app)


def _runtime(database: Path, *, planner=None) -> SynchronousAIRuntime:
    store = SQLiteAITurnStore(database)
    registry = ScopedCapabilityRegistry()
    registry.register(CapabilityDefinition("memory.recall", 1, "read", False, "read_only", "crp://default/contracts/in.schema.json", "crp://default/contracts/out.schema.json"), MemoryRecallCapability(_Recall()))
    return SynchronousAIRuntime(planner=planner or RecallFirstLocalPlanner(), registry=registry, events=store, payloads=store, state=store)


def _request() -> dict[str, object]:
    return json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))


def _action(turn_id: str, sequence: int, target_event_id: str) -> dict[str, object]:
    return {"schema_version": "1.0.0", "action_id": "action-0123456789abcdef0123456789abcdef", "turn_id": turn_id, "type": "approve", "target_event_id": target_event_id, "reason": "approved", "actor": "user", "expected_sequence": sequence, "idempotency_key": "approve-api-turn-001", "created_at": "2026-08-23T07:00:00Z"}


def _wait_for_terminal(client: TestClient, turn_id: str) -> None:
    for _ in range(100):
        events = client.get(f"/api/ai/turns/{turn_id}/events").json()["events"]
        if events and events[-1]["type"] in {"turn.completed", "turn.failed", "turn.cancelled"}:
            return
        time.sleep(0.01)
    raise AssertionError("AI Turn did not reach a terminal event")
