from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.mcp_runtime import MCPConnectionManager
from backend.api.routes.ai import router
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.ai_kernel import (
    CapabilityDefinition,
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    InMemoryTurnStateStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
)
from core.ai_tooling import ToolDefinition, ToolRetryPolicy


class _Planner:
    def plan(self, *_args, **_kwargs):
        return {"type": "complete", "summary": "unused"}


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, _arguments):
        self.calls += 1
        return {"status": "completed"}


class _MCPConnection:
    def __init__(self) -> None:
        self.connected = True
        self.connect_calls = 0
        self.probe_calls = 0
        self.installation_reason_counts = (("catalog_missing", 1),)

    def close(self) -> None:
        self.connected = False

    def capabilities(self):
        return ()

    def probe(self, *, timeout_ms: int) -> None:
        self.probe_calls += 1


def test_capability_catalog_is_local_read_only_and_uses_existing_runtime(tmp_path) -> None:
    provider = _Provider()
    registry = ScopedCapabilityRegistry()
    registry.register(_capability(), provider)
    runtime = SynchronousAIRuntime(
        planner=_Planner(), registry=registry, events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(), state=InMemoryTurnStateStore(),
    )
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.state.ai_runtime = runtime
    app.include_router(router)

    response = TestClient(app).get("/api/ai/projects/project-a/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["project_id"] == "project-a"
    assert payload["capability_profile"] == {"profile_id": "project-capability-project-a", "revision": 1}
    assert payload["boundary_profile"] == {"profile_id": "project-boundary-project-a", "revision": 1}
    assert payload["supported_kinds"] == ["tool"]
    assert payload["entries"] == [{
        "kind": "tool", "stable_id": "core.read", "display_name": "Read project notes",
        "source": "core", "owner_id": "core", "selected": True, "state": "available",
        "reason": "selected", "revision_identity": {"contract_version": 4},
    }]
    assert payload["excluded_reason_counts"] == []
    assert "description" not in str(payload)
    assert "schema" not in str(payload)
    assert provider.calls == 0


def test_capability_catalog_does_not_build_runtime_for_read(tmp_path) -> None:
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.include_router(router)

    response = TestClient(app).get("/api/ai/projects/project-a/capabilities")

    assert response.status_code == 503
    assert not hasattr(app.state, "ai_runtime")


def test_boundary_summary_is_local_read_only_and_does_not_invoke_provider(tmp_path) -> None:
    provider = _Provider()
    registry = ScopedCapabilityRegistry()
    registry.register(_capability(), provider)
    runtime = SynchronousAIRuntime(
        planner=_Planner(), registry=registry, events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(), state=InMemoryTurnStateStore(),
    )
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.state.ai_runtime = runtime
    app.include_router(router)

    response = TestClient(app).get("/api/ai/projects/project-a/boundary-summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["boundary_profile"]["mode"] == "guarded"
    assert payload["capability_profile"]["boundary_profile_revision"] == 1
    assert payload["persistent_grants"] == []
    assert {item["kind"] for item in payload["authorities"]} == {
        "project_boundary_profile", "project_capability_profile", "turn_approval",
        "session_file_proof", "global_provider_consent", "machine_mcp_approval",
    }
    assert provider.calls == 0


def test_boundary_summary_returns_conflict_when_capability_boundary_binding_drifts(tmp_path) -> None:
    provider = _Provider()
    registry = ScopedCapabilityRegistry()
    registry.register(_capability(), provider)
    runtime = SynchronousAIRuntime(planner=_Planner(), registry=registry, events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore(), state=InMemoryTurnStateStore())
    ProjectBoundaryProfileStore(tmp_path).update(
        "project-a", mode="open", remote_default="allow", expected_revision=0,
    )
    ProjectBoundaryProfileStore(tmp_path).update(
        "project-a", mode="guarded", remote_default="review", expected_revision=1,
    )
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.state.ai_runtime = runtime
    app.include_router(router)

    response = TestClient(app).get("/api/ai/projects/project-a/boundary-summary")

    assert response.status_code == 409
    assert provider.calls == 0


def test_boundary_summary_does_not_build_runtime_for_read(tmp_path) -> None:
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.include_router(router)

    response = TestClient(app).get("/api/ai/projects/project-a/boundary-summary")

    assert response.status_code == 503
    assert not hasattr(app.state, "ai_runtime")


def test_boundary_summary_rejects_non_local_client_before_reading_runtime(tmp_path) -> None:
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.include_router(router)

    response = TestClient(app, client=("203.0.113.10", 50123)).get(
        "/api/ai/projects/project-a/boundary-summary"
    )

    assert response.status_code == 403
    assert not hasattr(app.state, "ai_runtime")


def test_capability_catalog_rejects_non_local_client_before_reading_runtime(tmp_path) -> None:
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.include_router(router)

    response = TestClient(app, client=("203.0.113.10", 50123)).get(
        "/api/ai/projects/project-a/capabilities"
    )

    assert response.status_code == 403


def test_mcp_status_is_local_bounded_and_does_not_reconcile_or_disclose_canaries(tmp_path) -> None:
    runtime = SynchronousAIRuntime(
        planner=_Planner(), registry=ScopedCapabilityRegistry(),
        events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore(),
        state=InMemoryTurnStateStore(),
    )
    calls = {"load": 0, "connect": 0}
    connection = _MCPConnection()
    record = SimpleNamespace(
        server_id="approved-server", approval_revision=4,
        host_connection=SimpleNamespace(protocol_profile="stateless_2026_07_28"),
        transport_kind="streamable_http", header_policy_rejected_count=1,
        endpoint_url="https://private-canary.invalid",
    )

    def load():
        calls["load"] += 1
        return SimpleNamespace(enabled_servers=(record,))

    def connect(_record):
        calls["connect"] += 1
        return connection

    manager = MCPConnectionManager(authority_loader=load, connector=connect)
    manager.reconcile_once()
    ProjectCapabilityProfileStore(tmp_path).update(
        "project-a", expected_revision=0,
        boundary_profile_id="project-boundary-project-a", boundary_profile_revision=1,
        enabled_sources=("core", "mcp"), enabled_mcp_server_ids=("approved-server", "hidden-canary"),
    )
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.state.ai_runtime = runtime
    app.state.ai_mcp_connection_manager = manager
    app.include_router(router)

    response = TestClient(app).get("/api/ai/projects/project-a/mcp-status")

    assert response.status_code == 200
    assert calls == {"load": 1, "connect": 1}
    assert response.json() == {
        "project_id": "project-a",
        "capability_profile": {"profile_id": "project-capability-project-a", "revision": 1},
        "servers": [{
            "server_id": "approved-server", "approved_revision": 4,
            "protocol_profile": "stateless_2026_07_28", "transport": "streamable_http",
            "connection_state": "connected", "error_code": None,
        }],
        "global": [],
        "anonymous_reason_counts": [
            {"reason": "approved_server_missing", "count": 1},
            {"reason": "catalog_missing", "count": 1},
            {"reason": "header_policy_rejected", "count": 1},
        ],
        "excluded_reason_counts": [],
    }
    assert "canary" not in response.text


def test_mcp_status_requires_existing_runtime_and_manager(tmp_path) -> None:
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.include_router(router)

    response = TestClient(app).get("/api/ai/projects/project-a/mcp-status")

    assert response.status_code == 503
    assert not hasattr(app.state, "ai_runtime")


def test_mcp_status_hides_server_identity_when_project_disables_mcp_source(tmp_path) -> None:
    runtime = SynchronousAIRuntime(
        planner=_Planner(), registry=ScopedCapabilityRegistry(),
        events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore(),
        state=InMemoryTurnStateStore(),
    )
    record = SimpleNamespace(
        server_id="must-not-appear", approval_revision=1,
        host_connection=SimpleNamespace(protocol_profile="stateless_2026_07_28"),
        transport_kind="streamable_http", header_policy_rejected_count=0,
    )
    manager = MCPConnectionManager(
        authority_loader=lambda: SimpleNamespace(enabled_servers=(record,)),
        connector=lambda _record: _MCPConnection(),
    )
    manager.reconcile_once()
    ProjectCapabilityProfileStore(tmp_path).update(
        "project-a", expected_revision=0,
        boundary_profile_id="project-boundary-project-a", boundary_profile_revision=1,
        enabled_sources=("core",), enabled_mcp_server_ids=("must-not-appear",),
    )
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.state.ai_runtime = runtime
    app.state.ai_mcp_connection_manager = manager
    app.include_router(router)

    response = TestClient(app).get("/api/ai/projects/project-a/mcp-status")

    assert response.status_code == 200
    assert response.json()["servers"] == []
    assert "must-not-appear" not in response.text


def _capability() -> CapabilityDefinition:
    tool = ToolDefinition(
        tool_id="core.read", version=4, display_name="Read project notes", description="must not leave the runtime",
        source="core", owner_id="core", effect="read", data_classes=("project_content",), destination="local",
        input_schema_uri="crp://private/input", output_schema_uri="crp://private/output", receipt_schema_uri=None,
        operation_semantics="read_only", execution_mode="parallel", resource_locks=(), idempotency="idempotent",
        retry_policy=ToolRetryPolicy(2, 100, ("timeout",)), verification_tool_id=None, compensation_tool_id=None,
        mutability="read_only", egress_class="none", network_scope=(), data_egress_scope=(), timeout_ms=5_000,
        required_scopes=(), boundary_requirements=(),
    )
    return CapabilityDefinition(
        tool.tool_id, tool.version, tool.effect, False, tool.operation_semantics,
        tool.input_schema_uri, tool.output_schema_uri, tool,
    )
