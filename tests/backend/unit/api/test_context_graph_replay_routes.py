from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.context_graph_replay_composition import (
    ContextGraphReplayCompositionError,
    PreparedReplayTurn,
    ReplayCompletionResult,
)
from backend.api.desktop_session import DESKTOP_SESSION_HEADER
from backend.api.routes.context_graph import router
from core.context_graph import FrozenContextRevisions
from core.context_graph.replay_completion import (
    NodeGenerationReceipt,
    ReplayPlan,
    ReplayRequest,
)


SECRET = "s" * 43
INSTANCE = "linemap-replay-route-instance"
REVISIONS = FrozenContextRevisions("cap-r1", "boundary-r1", "provider-r1", "route-r1", "compiler-r1")


class _Bindings:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.binding = object()

    def resolve(self, binding_ref: str, *, project_id: str) -> SimpleNamespace:
        self.calls.append((binding_ref, project_id))
        if binding_ref != "crp://context-bindings/project-a/binding-a":
            raise ValueError("binding unavailable")
        return SimpleNamespace(binding=self.binding)


class _ReplayService:
    def __init__(self, *, expected_session: str | None = None) -> None:
        self.plan_commands = []
        self.sessions: list[tuple[str, str]] = []
        self.prepare_calls: list[str] = []
        self.accept_calls: list[tuple[str, object]] = []
        self.error: Exception | None = None
        self.expected_session = expected_session or f"desktop:{INSTANCE}"
        self.prepared: PreparedReplayTurn | None = _prepared()
        self.completion: ReplayCompletionResult = ReplayCompletionResult(_receipt(), None)

    def create_plan(self, command):
        self.plan_commands.append(command)
        if self.error:
            raise self.error
        return _plan()

    def assert_session(self, plan_id: str, session_id: str) -> None:
        self.sessions.append((plan_id, session_id))
        if session_id != self.expected_session:
            raise ContextGraphReplayCompositionError("replay_session_drift")

    def prepare_next(self, plan_id: str):
        self.prepare_calls.append(plan_id)
        if self.error:
            raise self.error
        return self.prepared

    def accept_completed_turn(self, plan_id: str, evidence: object):
        self.accept_calls.append((plan_id, evidence))
        if self.error:
            raise self.error
        return self.completion


def _plan() -> ReplayPlan:
    return ReplayPlan(
        "replay-plan-a", "project-a", "graph-a", "graph-r1", "replay-r2",
        ("node-a",), (("node-a", "fingerprint-a"),),
        "crp://context-bindings/project-a/binding-a", REVISIONS,
    )


def _request() -> ReplayRequest:
    return ReplayRequest(
        "replay-request-a", "replay-plan-a", "project-a", "graph-a", "graph-r1",
        "replay-r2", "node-a", 0, "fingerprint-a",
        "crp://context-bindings/project-a/binding-a", "turn-a", "operation-a", (),
        REVISIONS,
    )


def _prepared() -> PreparedReplayTurn:
    return PreparedReplayTurn(
        _request(), (), {"turn_id": "turn-a", "operation_id": "operation-a"},
    )


def _receipt() -> NodeGenerationReceipt:
    return NodeGenerationReceipt(
        "receipt-a", "replay-request-a", "replay-plan-a", "project-a", "graph-a",
        "graph-r1", "replay-r2", "node-a", 0, "fingerprint-a",
        "crp://context-bindings/project-a/binding-a", "turn-a", "operation-a",
        "terminal-a", "wire-a", (), "turn-terminal-summary:terminal-a",
        "2026-08-30T00:00:00+00:00", REVISIONS,
    )


def _configure_desktop(monkeypatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_SESSION_MODE", "desktop_production")
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_SESSION_SECRET", SECRET)
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_INSTANCE_ID", INSTANCE)
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_NONCE", "n" * 43)
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_PROTOCOL_VERSION", "desktop-loopback/1")
    monkeypatch.setenv(
        "CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT",
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN", "http://127.0.0.1:8317")


_MISSING = object()


def _client(*, service: _ReplayService | None = None, store: object = _MISSING) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.context_graph_replay_service = service or _ReplayService()
    app.state.context_graph_runtime = SimpleNamespace(bindings=_Bindings())
    if store is not _MISSING:
        app.state.ai_turn_effect_store = store
    else:
        app.state.ai_turn_effect_store = object()
    return TestClient(app)


def _plan_body(**extra: object) -> dict[str, object]:
    return {
        "command_id": "command-a", "project_id": "project-a", "graph_id": "graph-a",
        "graph_revision": "graph-r1", "binding_ref": "crp://context-bindings/project-a/binding-a",
        "confirm_replay": True, "allow_remote": False, "consent_refs": [], **extra,
    }


def _headers() -> dict[str, str]:
    return {DESKTOP_SESSION_HEADER: SECRET}


def test_plan_resolves_server_binding_and_never_accepts_client_binding_payload(monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    service = _ReplayService()
    with _client(service=service) as client:
        response = client.post("/api/rebuild/context-graph-replays", json=_plan_body(), headers=_headers())
        forged = client.post(
            "/api/rebuild/context-graph-replays",
            json=_plan_body(binding={"forged": "payload"}), headers=_headers(),
        )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["replay_plan_id"] == "replay-plan-a"
    assert service.plan_commands[0].binding is not None
    assert service.plan_commands[0].session_id == f"desktop:{INSTANCE}"
    assert forged.status_code == 400
    assert not any("forged" in str(item) for item in service.plan_commands)


@pytest.mark.parametrize(
    "body",
    (
        _plan_body(confirm_replay=False),
        _plan_body(allow_remote=True),
        _plan_body(consent_refs=["dup", "dup"]),
        _plan_body(output="client-output"),
    ),
)
def test_plan_fails_closed_for_confirmation_consent_and_client_authority(monkeypatch, body) -> None:
    _configure_desktop(monkeypatch)
    service = _ReplayService()
    with _client(service=service) as client:
        response = client.post("/api/rebuild/context-graph-replays", json=body, headers=_headers())

    assert response.status_code in {400, 409}
    assert response.headers["cache-control"] == "no-store"
    assert service.plan_commands == []


def test_next_only_returns_persisted_envelope_and_never_submits(monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    service = _ReplayService()
    with _client(service=service) as client:
        response = client.post("/api/rebuild/context-graph-replays/replay-plan-a/next", json={}, headers=_headers())
        malformed = client.post("/api/rebuild/context-graph-replays/replay-plan-a/next", json={"submit": True}, headers=_headers())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["turn_envelope"] == {"turn_id": "turn-a", "operation_id": "operation-a"}
    assert service.prepare_calls == ["replay-plan-a"]
    assert malformed.status_code == 400


def test_wrong_persisted_session_is_rejected_before_next_preparation(monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    service = _ReplayService(expected_session="desktop:another-instance")
    with _client(service=service) as client:
        response = client.post(
            "/api/rebuild/context-graph-replays/replay-plan-a/next",
            json={}, headers=_headers(),
        )

    assert response.status_code == 403
    assert response.json()["code"] == "replay_session_drift"
    assert service.prepare_calls == []


def test_accept_reads_only_server_evidence(monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    service = _ReplayService()
    evidence = object()
    with _client(service=service, store=evidence) as client:
        accepted = client.post("/api/rebuild/context-graph-replays/replay-plan-a/accept", json={}, headers=_headers())
        forged = client.post(
            "/api/rebuild/context-graph-replays/replay-plan-a/accept",
            json={"output": "forged", "receipt": "forged"}, headers=_headers(),
        )

    assert accepted.status_code == 200
    assert accepted.json()["receipt_ref"] == "receipt-a"
    assert service.accept_calls == [("replay-plan-a", evidence)]
    assert forged.status_code == 400


def test_routes_use_local_development_session_and_reject_missing_completion_evidence(monkeypatch) -> None:
    monkeypatch.delenv("CHRIPTMAS_DESKTOP_SESSION_MODE", raising=False)
    with _client() as client:
        local = client.post("/api/rebuild/context-graph-replays", json=_plan_body())

    _configure_desktop(monkeypatch)
    with _client(store=None) as client:
        unavailable = client.post("/api/rebuild/context-graph-replays/replay-plan-a/accept", json={}, headers=_headers())

    # The existing loopback policy intentionally permits a development-only
    # local session when desktop production credentials are absent.
    assert local.status_code == 201
    assert unavailable.status_code == 503
