from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import ai as ai_routes
from backend.api.series_turn_scope_authority import (
    SeriesTurnScopeAuthority,
    SeriesTurnScopeAuthorityError,
)
from core.ai_kernel import TurnReceipt
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


class _Runner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def accept_and_submit(self, request: dict[str, object]) -> TurnReceipt:
        self.calls.append(request)
        return TurnReceipt(
            turn_id=str(request["turn_id"]), session_id=str(request["session_id"]),
            operation_id=str(request["operation_id"]), status="accepted",
            current_sequence=1, replayed=False,
        )


def _snapshot() -> ProjectSeriesScopeSnapshot:
    return ProjectSeriesScopeSnapshot(
        namespace_id="default", project_id="project-alpha", series_id="series-alpha",
        object_id="series-memory-alpha", payload_revision=7, storage_revision=3,
        authority_identity="json:object-store-v1",
        authority_ref="crp://default/memory/series/series-memory-alpha",
    )


def _request(kind: str = "series") -> dict[str, object]:
    request = json.loads(
        (ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"),
    )
    if kind == "series":
        request["scope"] = {"kind": "series", "project_id": None, "series_id": "series-alpha"}
    elif kind == "global":
        request["scope"] = {"kind": "global", "project_id": None, "series_id": None}
    return request


def test_series_canonicalization_freezes_only_authoritative_project_scope() -> None:
    original = _request()
    resolver = _Resolver(snapshot=_snapshot())

    canonical = SeriesTurnScopeAuthority(resolver).canonicalize(original)

    assert original["scope"] == {"kind": "series", "project_id": None, "series_id": "series-alpha"}
    assert canonical["scope"] == {
        "kind": "series", "project_id": "project-alpha", "series_id": "series-alpha",
        "authority": {
            "kind": "project_series_scope_v1", "object_id": "series-memory-alpha",
            "payload_revision": 7, "storage_revision": 3,
            "authority_identity": "json:object-store-v1",
            "authority_ref": "crp://default/memory/series/series-memory-alpha",
        },
    }
    assert resolver.calls == 1


def test_series_canonicalization_rejects_project_assertion_and_client_authority() -> None:
    authority = SeriesTurnScopeAuthority(_Resolver(snapshot=_snapshot()))
    mismatch = _request()
    mismatch["scope"]["project_id"] = "project-other"  # type: ignore[index]
    with pytest.raises(SeriesTurnScopeAuthorityError, match="assertion"):
        authority.canonicalize(mismatch)

    forged = _request()
    forged["scope"]["authority"] = None  # type: ignore[index]
    with pytest.raises(SeriesTurnScopeAuthorityError, match="client_supplied"):
        authority.canonicalize(forged)


def test_global_and_project_scope_do_not_invoke_resolver() -> None:
    resolver = _Resolver(error=AssertionError("resolver must not run"))
    authority = SeriesTurnScopeAuthority(resolver)

    assert authority.canonicalize(_request("global"))["scope"]["kind"] == "global"  # type: ignore[index]
    assert authority.canonicalize(_request("project"))["scope"]["kind"] == "project"  # type: ignore[index]
    assert resolver.calls == 0


@pytest.mark.parametrize("error", [
    ProjectSeriesScopeError("series_scope_not_found"),
    ProjectSeriesScopeError("series_scope_stale"),
])
def test_series_resolution_errors_propagate_before_accept(error: BaseException) -> None:
    authority = SeriesTurnScopeAuthority(_Resolver(error=error))
    with pytest.raises(ProjectSeriesScopeError, match=str(error)):
        authority.canonicalize(_request())


def test_route_rejects_series_authority_failure_before_runner_accept(monkeypatch) -> None:
    runner = _Runner()
    failing = SeriesTurnScopeAuthority(_Resolver(error=ProjectSeriesScopeError("series_scope_stale")))
    monkeypatch.setattr(ai_routes, "build_series_turn_scope_authority", lambda _root: failing)
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=ROOT)
    app.state.ai_runtime = object()
    app.state.ai_turn_runner = runner
    app.include_router(ai_routes.router)

    response = TestClient(app).post("/api/ai/turns", json=_request())

    assert response.status_code == 400
    assert response.json()["detail"] == "AI turn rejected"
    assert runner.calls == []


def test_route_freezes_series_authority_before_runner_accept(monkeypatch) -> None:
    runner = _Runner()
    authority = SeriesTurnScopeAuthority(_Resolver(snapshot=_snapshot()))
    monkeypatch.setattr(ai_routes, "build_series_turn_scope_authority", lambda _root: authority)
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=ROOT)
    app.state.ai_runtime = object()
    app.state.ai_turn_runner = runner
    app.include_router(ai_routes.router)

    response = TestClient(app).post("/api/ai/turns", json=_request())

    assert response.status_code == 202
    assert runner.calls[0]["scope"] == {
        "kind": "series", "project_id": "project-alpha", "series_id": "series-alpha",
        "authority": {
            "kind": "project_series_scope_v1", "object_id": "series-memory-alpha",
            "payload_revision": 7, "storage_revision": 3,
            "authority_identity": "json:object-store-v1",
            "authority_ref": "crp://default/memory/series/series-memory-alpha",
        },
    }


def test_route_leaves_project_turn_without_resolver(monkeypatch) -> None:
    runner = _Runner()
    monkeypatch.setattr(
        ai_routes, "build_series_turn_scope_authority",
        lambda _root: (_ for _ in ()).throw(AssertionError("must not build resolver")),
    )
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=ROOT)
    app.state.ai_runtime = object()
    app.state.ai_turn_runner = runner
    app.include_router(ai_routes.router)

    response = TestClient(app).post("/api/ai/turns", json=_request("project"))

    assert response.status_code == 202
    assert runner.calls == [_request("project")]
