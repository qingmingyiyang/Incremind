from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from backend.api.routes import include_api_routers
from tests.architecture.route_introspection import registered_route_paths


def test_production_router_exposes_turn_api_without_legacy_agent_graph() -> None:
    app = FastAPI()
    include_api_routers(app)
    paths = set(registered_route_paths(app))

    assert "/api/ai/turns" in paths
    assert not any(path.startswith("/api/agent/") for path in paths)


def test_production_bootstrap_cannot_reactivate_legacy_agent_model_transport() -> None:
    bootstrap = (Path(__file__).resolve().parents[2] / "src/backend/api/bootstrap.py").read_text(encoding="utf-8")
    method = bootstrap[bootstrap.index("    def get_agent_graph_service("):bootstrap.index("    def invalidate_agent_graph_service(")]

    assert "Legacy Agent API is retired; use the unified AI Turn API" in method
    for forbidden in ("LiteLLMChatGateway", "secret_store", "build_agent_graph", "planner_gateway"):
        assert forbidden not in method
