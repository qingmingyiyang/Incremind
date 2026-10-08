from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.agent_organization_runtime import AgentOrganizationError
from backend.api.routes import ai_agents
from backend.api.routes.ai_agents import router
from backend.api.ai_turn_runner import AITurnRunnerCapacityError
from core.ai_kernel import AgentProfileRegistry, InMemoryAgentProfileStore


class _Coordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return {"summary": "safe", "run": {"run_id": "main-1", "status": "running"}, "children": [{"run_id": "child-1", "status": "completed", "hidden_context": "no"}], "links": []}

    def interrupt(self, **kwargs): self.calls.append(("interrupt", kwargs)); return {"summary": "interrupted", "requested": True}
    def wait(self, **kwargs): self.calls.append(("wait", kwargs)); return {"summary": "waited", "terminal": True}
    def fan_in(self, **kwargs): self.calls.append(("fan_in", kwargs)); return {"summary": "joined", "status": "open"}


class AgentCoordinatorError(RuntimeError): pass
class AgentStoreConflict(RuntimeError): pass


class _Organization:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[dict[str, object], bool]] = []
        self.error = error

    def start(self, request: dict[str, object], *, agent_turn_mode: bool):
        self.calls.append((request, agent_turn_mode))
        if self.error is not None:
            raise self.error
        return {
            "status": "accepted",
            "main": {"run_id": "main-1", "role": "main", "input": "private"},
            "steward": {"run_id": "steward-1", "role": "subagent", "hidden_context": "private"},
        }


class _RejectedCoordinator(_Coordinator):
    def list(self, **kwargs):
        raise AgentCoordinatorError("outside frozen capability set")


class _ConflictedCoordinator(_Coordinator):
    def interrupt(self, **kwargs):
        raise AgentStoreConflict("revision changed")


class _SensitiveCoordinator(_Coordinator):
    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return {
            "run": {"run_id": "main-1", "status": "running", "model_name": "private-model"},
            "children": [{"run_id": "child-1", "status": "running", "provider_id": "private-provider", "nested": {
                "api_key_name": "private-key", "local_path": "C:/private", "hidden_context": "private",
                "providerId": "private-provider-camel", "modelName": "private-model-camel",
                "baseUrl": "https://private.invalid", "privateKeyName": "private-key-camel",
                "contextWindow": "private-context-camel",
            }}],
            "links": [],
        }


class _LeakyCoordinator(_Coordinator):
    def list(self, **kwargs):
        raise AgentCoordinatorError("api_key=private-value model_name=private-model")


class _StreamRequest:
    def __init__(self, app) -> None:
        self.app = app

    async def is_disconnected(self) -> bool:
        return False


def _request(turn_id: str):
    return {"scope": {"kind": "project", "project_id": "alpha", "series_id": None}, "privacy": {"mode": "local_first", "allow_remote": False, "pii": "none", "consent_refs": [], "retention": "session"}}


def _client(*, configured: bool = True, coordinator_type=_Coordinator, organization=None) -> tuple[TestClient, _Coordinator]:
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=Path(__file__).resolve().parents[4])
    coordinator = coordinator_type()
    if configured:
        app.state.agent_profile_registry = AgentProfileRegistry(InMemoryAgentProfileStore())
        app.state.agent_run_coordinator = coordinator
        app.state.agent_turn_request_loader = _request
    if organization is not None:
        app.state.agent_organization_runtime = organization
    app.include_router(router)
    return TestClient(app), coordinator


def test_agent_turns_start_organization_with_host_mode_and_safe_projection(monkeypatch) -> None:
    organization = _Organization()
    client, _ = _client(organization=organization)
    monkeypatch.setattr("backend.api.routes.ai_agents.get_or_build_ai_runtime", lambda *_args: object())

    response = client.post("/api/ai/agent-turns", json={
        "scope": {"kind": "project", "project_id": "alpha", "series_id": None},
        "privacy": {"mode": "local_first", "allow_remote": False, "pii": "none", "consent_refs": [], "retention": "session"},
    })

    assert response.status_code == 202
    assert organization.calls[0][1] is True
    assert "input" not in response.text
    assert "hidden_context" not in response.text
    assert response.json()["main"] == {"run_id": "main-1", "role": "main"}


def test_agent_turns_canonicalizes_series_scope_before_organization_start(monkeypatch) -> None:
    organization = _Organization()
    client, _ = _client(organization=organization)

    class _SeriesAuthority:
        def canonicalize(self, request):
            return {**request, "scope": {
                "kind": "series", "project_id": "canonical-project", "series_id": "series-1",
                "authority": {"kind": "project_series_scope_v1"},
            }}

    monkeypatch.setattr("backend.api.routes.ai_agents.get_or_build_ai_runtime", lambda *_args: object())
    monkeypatch.setattr("backend.api.routes.ai_agents.build_series_turn_scope_authority", lambda *_args: _SeriesAuthority())
    response = client.post("/api/ai/agent-turns", json={
        "scope": {"kind": "series", "project_id": None, "series_id": "series-1"},
        "privacy": {"mode": "local_first", "allow_remote": False, "pii": "none", "consent_refs": [], "retention": "session"},
    })

    assert response.status_code == 202
    assert organization.calls[0][0]["scope"]["project_id"] == "canonical-project"


def test_agent_turns_preserves_capacity_and_organization_rejection_boundaries(monkeypatch) -> None:
    monkeypatch.setattr("backend.api.routes.ai_agents.get_or_build_ai_runtime", lambda *_args: object())
    capacity_client, _ = _client(organization=_Organization(error=AITurnRunnerCapacityError("full")))
    capacity = capacity_client.post("/api/ai/agent-turns", json=_request("turn-1"))
    assert capacity.status_code == 503
    assert capacity.json()["error_code"] == "ai.runner_capacity"

    rejected_client, _ = _client(organization=_Organization(error=AgentOrganizationError("bad plan")))
    rejected = rejected_client.post("/api/ai/agent-turns", json=_request("turn-1"))
    assert rejected.status_code == 400


def test_agent_turns_fails_closed_when_organization_composition_is_missing(monkeypatch) -> None:
    client, _ = _client()
    monkeypatch.setattr("backend.api.routes.ai_agents.get_or_build_ai_runtime", lambda *_args: object())
    response = client.post("/api/ai/agent-turns", json=_request("turn-1"))
    assert response.status_code == 503


def _custom_profile() -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "profile_id": "subagent.custom.audit", "revision": 1,
        "display_name": "审计", "enabled": True, "role": "subagent", "model_tier": "fast",
        "budget_limit": {"model_calls": 1, "tool_calls": 1, "input_tokens": 1, "output_tokens": 1, "wall_time_ms": 1},
        "capability_ids": ["agent.list"], "max_concurrent_children": 0, "max_depth": 1,
        "max_steps": 1, "timeout_ms": 1, "allow_child_spawn": False,
    }


def test_profiles_are_local_governed_and_cas_backed() -> None:
    client, _ = _client()
    created = client.post("/api/ai/agent-profiles", json={"profile": _custom_profile()})
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    assert created.json()["model_tier"] == "fast"
    conflict = client.put("/api/ai/agent-profiles/subagent.custom.audit", json={"expected_revision": 7, "profile": {**_custom_profile(), "revision": 2}})
    assert conflict.status_code == 409


def test_profiles_allow_only_the_opaque_model_route_binding_pair() -> None:
    client, _ = _client()
    bound = {
        **_custom_profile(),
        "schema_version": "1.2.0",
        "organization_role": "审计专家",
        "work_description": "核验受管任务。",
        "model_route_key": "route.local.text",
        "model_route_revision": 3,
    }
    created = client.post("/api/ai/agent-profiles", json={"profile": bound})

    assert created.status_code == 201
    assert created.json()["model_route_key"] == "route.local.text"
    assert created.json()["model_route_revision"] == 3
    # The exemption is deliberately exact: route metadata never opens a
    # general model/provider namespace on the Agent governance endpoint.
    invalid = {**bound, "model_route_provider": "private-provider"}
    assert client.put("/api/ai/agent-profiles/subagent.custom.audit", json={
        "expected_revision": 1, "profile": {**invalid, "revision": 2},
    }).status_code == 400


def test_profiles_reject_provider_model_and_secret_fields() -> None:
    client, _ = _client()
    payload = _custom_profile(); payload["provider"] = "openai"
    assert client.post("/api/ai/agent-profiles", json={"profile": payload}).status_code == 400
    payload = _custom_profile(); payload["model"] = "gpt-x"
    assert client.post("/api/ai/agent-profiles", json={"profile": payload}).status_code == 400
    payload = _custom_profile(); payload["budget_limit"] = {"nested": [{"provider": "openai"}]}
    assert client.post("/api/ai/agent-profiles", json={"profile": payload}).status_code == 400
    created = client.post("/api/ai/agent-profiles", json={"profile": _custom_profile()})
    assert created.status_code == 201
    update = {**_custom_profile(), "revision": 2, "nested": {
        "provider_id": "private", "model_name": "private", "api_key_name": "private",
        "providerId": "private", "modelName": "private", "apiKeyName": "private",
        "baseUrl": "private", "privateKeyName": "private", "contextWindow": "private",
    }}
    assert client.put("/api/ai/agent-profiles/subagent.custom.audit", json={"expected_revision": 1, "profile": update}).status_code == 400
    invalid = _custom_profile(); invalid["display_name"] = ""
    rejected = client.post("/api/ai/agent-profiles", json={"profile": invalid})
    assert rejected.status_code == 400 and rejected.json() == {"detail": "Agent profile rejected"}


def test_uncomposed_agent_api_fails_closed() -> None:
    client, _ = _client(configured=False)
    assert client.get("/api/ai/agent-profiles").status_code == 503
    assert client.get("/api/ai/projects/alpha/agent-topology?turn_id=turn-1").status_code == 503


def test_topology_and_operations_use_frozen_project_authority() -> None:
    client, coordinator = _client()
    response = client.get("/api/ai/projects/alpha/agent-topology?turn_id=turn-1")
    assert response.status_code == 200
    assert "hidden_context" not in response.text
    assert coordinator.calls[0][1]["scope"] == _request("turn-1")["scope"]
    operation = client.post("/api/ai/projects/alpha/agent-operations/interrupt", json={"turn_id": "turn-1", "child_run_id": "child-1", "operation_id": "op-agent-api-0001", "reason": "stop"})
    assert operation.status_code == 202
    assert coordinator.calls[-1][0] == "interrupt"
    assert coordinator.calls[-1][1]["arguments"] == {"child_run_id": "child-1", "reason": "stop"}


def test_agent_organization_can_render_a_profile_skeleton_without_turn_id() -> None:
    client, coordinator = _client()
    response = client.get("/api/ai/projects/alpha/agent-organization")

    assert response.status_code == 200
    body = response.json()
    assert body["has_active_run"] is False
    assert body["main"]["profile_id"] == "main.orchestrator"
    assert body["projection_revision"].startswith("profiles[")
    assert coordinator.calls == []


def test_agent_organization_uses_frozen_turn_authority_and_stays_safe() -> None:
    client, coordinator = _client()
    response = client.get("/api/ai/projects/alpha/agent-organization?turn_id=turn-1")

    assert response.status_code == 200
    assert response.json()["has_active_run"] is True
    assert "hidden_context" not in response.text
    assert coordinator.calls[0][1]["scope"] == _request("turn-1")["scope"]


def test_agent_organization_rejects_cross_project_turns_before_the_coordinator() -> None:
    client, coordinator = _client()
    assert client.get("/api/ai/projects/beta/agent-organization?turn_id=turn-1").status_code == 400
    assert client.get("/api/ai/projects/beta/agent-organization/stream?turn_id=turn-1").status_code == 400
    assert coordinator.calls == []


def test_agent_organization_stream_requires_a_turn_and_a_bounded_cursor() -> None:
    client, _ = _client()
    assert client.get("/api/ai/projects/alpha/agent-organization/stream").status_code == 400
    assert client.get("/api/ai/projects/alpha/agent-organization/stream?turn_id=turn-1&after=-1").status_code == 400
    assert client.get(
        "/api/ai/projects/alpha/agent-organization/stream?turn_id=turn-1&after=1",
        headers={"last-event-id": "not-a-cursor"},
    ).status_code == 400


def test_agent_organization_route_recursively_redacts_sensitive_projection_fields() -> None:
    client, _ = _client(coordinator_type=_SensitiveCoordinator)
    response = client.get("/api/ai/projects/alpha/agent-organization?turn_id=turn-1")
    assert response.status_code == 200
    rendered = response.text
    for private in ("private-model", "private-provider", "private-key", "C:/private", "hidden_context", "model_name", "provider_id", "api_key_name", "local_path"):
        assert private not in rendered


def test_agent_errors_are_publicly_generic_even_when_domain_errors_contain_secrets() -> None:
    client, _ = _client(coordinator_type=_LeakyCoordinator)
    response = client.get("/api/ai/projects/alpha/agent-organization?turn_id=turn-1")
    assert response.status_code == 400
    assert response.json() == {"detail": "Agent organization rejected"}
    assert "private" not in response.text


def test_project_isolation_rejects_mismatch_before_coordinator() -> None:
    client, coordinator = _client()
    assert client.get("/api/ai/projects/beta/agent-topology?turn_id=turn-1").status_code == 400
    assert coordinator.calls == []


def test_domain_rejection_and_conflict_are_not_reported_as_service_unavailable() -> None:
    client, _ = _client(coordinator_type=_RejectedCoordinator)
    assert client.get("/api/ai/projects/alpha/agent-topology?turn_id=turn-1").status_code == 400
    client, _ = _client(coordinator_type=_ConflictedCoordinator)
    response = client.post("/api/ai/projects/alpha/agent-operations/interrupt", json={"turn_id": "turn-1", "child_run_id": "child-1", "operation_id": "op-agent-api-0001", "reason": "stop"})
    assert response.status_code == 409


def test_delete_is_empty_204_and_stream_cursor_is_bounded() -> None:
    client, _ = _client()
    created = client.post("/api/ai/agent-profiles", json={"profile": _custom_profile()})
    assert created.status_code == 201
    deleted = client.request("DELETE", "/api/ai/agent-profiles/subagent.custom.audit", json={"expected_revision": 1})
    assert deleted.status_code == 204 and deleted.content == b""
    response = client.get("/api/ai/projects/alpha/agent-topology/stream?turn_id=turn-1&after=9999999999999999999")
    assert response.status_code == 400


def test_agent_governance_requires_the_active_desktop_session_header(monkeypatch) -> None:
    client, coordinator = _client()
    monkeypatch.setattr(ai_agents, "desktop_session", lambda: object())
    monkeypatch.setattr(ai_agents, "desktop_session_authorized", lambda value: value == "valid-session")
    assert client.get("/api/ai/agent-profiles").status_code == 403
    assert client.get("/api/ai/projects/alpha/agent-organization?turn_id=turn-1").status_code == 403
    assert client.post("/api/ai/projects/alpha/agent-operations/interrupt", json={
        "turn_id": "turn-1", "child_run_id": "child-1", "operation_id": "op-1", "reason": "stop",
    }).status_code == 403
    assert coordinator.calls == []
    assert client.get("/api/ai/agent-profiles", headers={ai_agents.DESKTOP_SESSION_HEADER: "valid-session"}).status_code == 200


def test_organization_stream_emits_revision_changes_and_a_safe_error(monkeypatch) -> None:
    client, _ = _client()
    request = _StreamRequest(client.app)
    revisions = iter((
        {"projection_revision": "safe-v1", "main": {"status": "running"}},
        {"projection_revision": "safe-v2", "main": {"status": "completed"}},
    ))
    monkeypatch.setattr(ai_agents, "build_agent_organization_projection", lambda **_: next(revisions))

    async def two_events():
        stream = ai_agents._organization_stream(request, "alpha", "turn-1", 3)
        try:
            return await anext(stream), await anext(stream)
        finally:
            await stream.aclose()

    first, second = asyncio.run(two_events())
    assert '"cursor":4' in first and '"projection_revision":"safe-v1"' in first
    assert '"cursor":5' in second and '"projection_revision":"safe-v2"' in second

    monkeypatch.setattr(ai_agents, "build_agent_organization_projection", lambda **_: (_ for _ in ()).throw(RuntimeError("api_key=private")))

    async def error_event():
        stream = ai_agents._organization_stream(request, "alpha", "turn-1", 0)
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    event = asyncio.run(error_event())
    assert event == 'event: stream_error\ndata: {"error_code":"agent.organization_unavailable"}\n\n'
