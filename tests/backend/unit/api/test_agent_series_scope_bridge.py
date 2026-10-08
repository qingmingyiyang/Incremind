from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agent.memory.context import AgentContext
from backend.agent.schemas.action_plan import AgentActionPlan, AgentTurnResult, ScopeType
from backend.agent.schemas.stream_events import AgentStreamEvent
from backend.agent.session.models import AgentSessionSnapshot, utc_now_iso
from backend.api.routes import agent as agent_routes
from backend.api.series_turn_scope_authority import SeriesTurnScopeAuthority
from core.product_core.project_series_scope import (
    ProjectSeriesScopeError,
    ProjectSeriesScopeSnapshot,
)


ROOT = Path(__file__).resolve().parents[4]


@dataclass
class _Resolver:
    snapshot: ProjectSeriesScopeSnapshot | None = None
    error: BaseException | None = None
    calls: int = 0

    def resolve(self, series_id: str) -> ProjectSeriesScopeSnapshot:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.snapshot is not None
        assert series_id == self.snapshot.series_id
        return self.snapshot


class _SessionStore:
    def __init__(self) -> None:
        self.snapshots: dict[str, AgentSessionSnapshot] = {}

    def get_snapshot(self, session_id: str) -> AgentSessionSnapshot | None:
        return self.snapshots.get(session_id)

    def append_turn(self, *, session_id, memory_key, context, messages) -> None:
        self.snapshots[session_id] = AgentSessionSnapshot(
            session_id=session_id,
            memory_key=memory_key,
            context=context,
            messages=messages,
            updated_at=utc_now_iso(),
        )

    def clear_snapshot(self, session_id: str) -> None:
        self.snapshots.pop(session_id, None)


class _GraphService:
    def __init__(self, session_store: _SessionStore) -> None:
        self.session_store = session_store
        self.turns: list[dict[str, object]] = []

    def _record(self, *, session_id: str, user_message: str, context_override) -> AgentContext:
        context = context_override if context_override is not None else AgentContext(session_id=session_id)
        self.turns.append(
            {
                "session_id": session_id,
                "user_message": user_message,
                "context_override": context,
            }
        )
        self.session_store.append_turn(
            session_id=session_id,
            memory_key=session_id,
            context=context,
            messages=[],
        )
        return context

    def run_turn(self, *, session_id, user_message, context_override=None, **kwargs):
        self._record(session_id=session_id, user_message=user_message, context_override=context_override)
        return AgentTurnResult(
            assistant_message="ok",
            plan=AgentActionPlan(scope_type=ScopeType.SERIES, tool_calls=[], reason="ok", use_answerer=False),
            tool_results=[],
            citations=[],
        )

    def stream_with_context(self, *, session_id, user_message, context_override=None, **kwargs):
        self._record(session_id=session_id, user_message=user_message, context_override=context_override)
        yield AgentStreamEvent(type="answer_delta", payload={"delta": "ok"})


def _snapshot() -> ProjectSeriesScopeSnapshot:
    return ProjectSeriesScopeSnapshot(
        namespace_id="default", project_id="project-alpha", series_id="series-alpha",
        object_id="series-memory-alpha", payload_revision=7, storage_revision=3,
        authority_identity="json:object-store-v1",
        authority_ref="crp://default/memory/series/series-memory-alpha",
    )


def _series_request(**context_overrides) -> dict[str, object]:
    context: dict[str, object] = {"scope_type": "series", "series_id": "series-alpha"}
    context.update(context_overrides)
    return {"session_id": "session-alpha", "message": "hello", "context": context}


def _frozen_authority() -> dict[str, object]:
    return {
        "kind": "project_series_scope_v1", "project_id": "project-alpha", "series_id": "series-alpha",
        "object_id": "series-memory-alpha", "payload_revision": 7, "storage_revision": 3,
        "authority_identity": "json:object-store-v1",
        "authority_ref": "crp://default/memory/series/series-memory-alpha",
    }


def _build_app(monkeypatch, resolver: _Resolver, session_store: _SessionStore | None = None) -> tuple[TestClient, _GraphService]:
    store = session_store or _SessionStore()
    service = _GraphService(store)
    authority = SeriesTurnScopeAuthority(resolver)
    monkeypatch.setattr(agent_routes, "build_series_turn_scope_authority", lambda _root: authority)
    app = FastAPI()
    app.state.container = SimpleNamespace(
        root_dir=ROOT,
        config_path=ROOT,
        agent_session_store=store,
        get_agent_graph_service=lambda: service,
    )
    app.include_router(agent_routes.router)
    return TestClient(app), service


def test_series_chat_freezes_authority_before_turn(monkeypatch) -> None:
    client, service = _build_app(monkeypatch, _Resolver(snapshot=_snapshot()))

    response = client.post("/api/agent/chat", json=_series_request())

    assert response.status_code == 200
    context = service.turns[0]["context_override"]
    assert isinstance(context, AgentContext)
    assert context.series_authority is not None
    assert context.series_authority.model_dump(mode="json") == _frozen_authority()
    persisted = service.session_store.get_snapshot("session-alpha")
    assert persisted is not None
    assert persisted.context.series_authority is not None
    assert persisted.context.series_authority.model_dump(mode="json") == _frozen_authority()


def test_series_chat_rejects_client_authority(monkeypatch) -> None:
    client, service = _build_app(monkeypatch, _Resolver(error=AssertionError("resolver must not run")))
    body = _series_request()
    body["context"]["series_authority"] = None  # type: ignore[index]

    response = client.post("/api/agent/chat", json=body)

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "series_turn_authority_client_supplied"
    assert service.turns == []


def test_series_chat_rejects_missing_series_id(monkeypatch) -> None:
    client, service = _build_app(monkeypatch, _Resolver(error=AssertionError("resolver must not run")))

    response = client.post("/api/agent/chat", json=_series_request(series_id=None))

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "series_turn_series_id_invalid"
    assert service.turns == []


def test_series_chat_resolver_failure_zero_runner_calls(monkeypatch) -> None:
    client, service = _build_app(
        monkeypatch,
        _Resolver(error=ProjectSeriesScopeError("series_scope_stale")),
    )

    response = client.post("/api/agent/chat", json=_series_request())

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "series_scope_stale"
    assert service.turns == []


def test_series_chat_rejects_project_assertion_mismatch(monkeypatch) -> None:
    resolver = _Resolver(snapshot=_snapshot())
    client, service = _build_app(monkeypatch, resolver)

    response = client.post("/api/agent/chat", json=_series_request(project_id="project-other"))

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "series_turn_project_assertion_mismatch"
    assert resolver.calls == 1
    assert service.turns == []


def test_video_chat_freezes_series_membership(monkeypatch) -> None:
    resolver = _Resolver(snapshot=_snapshot())
    client, service = _build_app(monkeypatch, resolver)
    body = _series_request(scope_type="video", video_id="video-alpha")

    response = client.post("/api/agent/chat", json=body)

    assert response.status_code == 200
    assert resolver.calls == 1
    context = service.turns[0]["context_override"]
    assert context.series_authority is not None
    assert context.series_authority.model_dump(mode="json") == _frozen_authority()


def test_video_chat_rejects_missing_series_id(monkeypatch) -> None:
    client, service = _build_app(monkeypatch, _Resolver(error=AssertionError("resolver must not run")))

    response = client.post("/api/agent/chat", json=_series_request(scope_type="video", series_id=None, video_id="video-alpha"))

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "series_turn_series_id_invalid"
    assert service.turns == []


def test_unknown_scope_keeps_legacy_pass_through(monkeypatch) -> None:
    client, service = _build_app(
        monkeypatch,
        _Resolver(error=AssertionError("resolver must not run")),
    )

    response = client.post("/api/agent/chat", json=_series_request(scope_type="team", series_id=None))

    assert response.status_code == 200
    context = service.turns[0]["context_override"]
    assert context.series_authority is None


def test_restart_replays_frozen_scope_without_resolve(monkeypatch) -> None:
    resolver = _Resolver(snapshot=_snapshot())
    client, service = _build_app(monkeypatch, resolver)
    assert client.post("/api/agent/chat", json=_series_request()).status_code == 200
    assert resolver.calls == 1

    # Membership drift: the resolver would now fail if consulted again.
    resolver.snapshot = None
    resolver.error = ProjectSeriesScopeError("series_scope_not_found")

    response = client.post("/api/agent/chat", json=_series_request())

    assert response.status_code == 200
    assert resolver.calls == 1
    context = service.turns[1]["context_override"]
    assert context.series_authority is not None
    assert context.series_authority.model_dump(mode="json") == _frozen_authority()


def test_replay_rejects_assertion_mismatch_against_frozen(monkeypatch) -> None:
    resolver = _Resolver(snapshot=_snapshot())
    client, service = _build_app(monkeypatch, resolver)
    assert client.post("/api/agent/chat", json=_series_request()).status_code == 200
    resolver.error = AssertionError("resolver must not run")

    response = client.post("/api/agent/chat", json=_series_request(project_id="project-other"))

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "series_turn_project_assertion_mismatch"
    assert resolver.calls == 1
    assert len(service.turns) == 1


def test_same_session_new_series_resolves_fresh(monkeypatch) -> None:
    resolver = _Resolver(snapshot=_snapshot())
    client, service = _build_app(monkeypatch, resolver)
    assert client.post("/api/agent/chat", json=_series_request()).status_code == 200

    resolver.snapshot = ProjectSeriesScopeSnapshot(
        namespace_id="default", project_id="project-beta", series_id="series-beta",
        object_id="series-memory-beta", payload_revision=11, storage_revision=5,
        authority_identity="json:object-store-v1",
        authority_ref="crp://default/memory/series/series-memory-beta",
    )
    response = client.post("/api/agent/chat", json=_series_request(series_id="series-beta"))

    assert response.status_code == 200
    assert resolver.calls == 2
    context = service.turns[1]["context_override"]
    assert context.series_authority is not None
    assert context.series_authority.project_id == "project-beta"


def test_recovery_returns_frozen_scope_without_resolve(monkeypatch) -> None:
    resolver = _Resolver(snapshot=_snapshot())
    client, service = _build_app(monkeypatch, resolver)
    assert client.post("/api/agent/chat", json=_series_request()).status_code == 200
    resolver.error = AssertionError("resolver must not run")

    response = client.post("/api/agent/session/recover", json={"session_id": "session-alpha"})

    assert response.status_code == 200
    body = response.json()
    assert body["restored"] is True
    assert body["series_authority"] == _frozen_authority()
    assert resolver.calls == 1


def test_stream_rejects_scope_failure_as_http_error(monkeypatch) -> None:
    client, service = _build_app(
        monkeypatch,
        _Resolver(error=ProjectSeriesScopeError("series_scope_stale")),
    )

    response = client.post("/api/agent/chat/stream", json=_series_request())

    assert response.status_code == 400
    assert response.headers.get("content-type", "").startswith("application/json")
    assert service.turns == []


def test_stream_series_chat_uses_frozen_context(monkeypatch) -> None:
    client, service = _build_app(monkeypatch, _Resolver(snapshot=_snapshot()))

    response = client.post("/api/agent/chat/stream", json=_series_request())

    assert response.status_code == 200
    assert "answer_delta" in response.text
    assert "error" not in response.text
    context = service.turns[0]["context_override"]
    assert context.series_authority is not None
    assert context.series_authority.model_dump(mode="json") == _frozen_authority()


def test_contextless_request_keeps_legacy_path(monkeypatch) -> None:
    client, service = _build_app(
        monkeypatch,
        _Resolver(error=AssertionError("resolver must not run")),
    )
    body = {"session_id": "session-alpha", "message": "hello"}

    response = client.post("/api/agent/chat", json=body)

    assert response.status_code == 200
    context = service.turns[0]["context_override"]
    assert isinstance(context, AgentContext)
    assert context.series_authority is None
