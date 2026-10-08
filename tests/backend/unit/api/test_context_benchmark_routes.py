from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.context_benchmark_composition import ContextBenchmarkCompositionError
from backend.api.context_benchmark_run_authority import ContextBenchmarkRunError
from backend.api.desktop_session import DESKTOP_SESSION_HEADER, DesktopSession
from backend.api.routes.context_graph import router
from core.context_graph import FrozenContextRevisions


SECRET = "s" * 43
INSTANCE = "linemap-benchmark-route-instance"
REVISIONS = FrozenContextRevisions(
    "cap-r1", "boundary-r1", "provider-r1", "route-r1", "compiler-r1",
)


@dataclass(frozen=True)
class _SuiteResult:
    qualified: bool
    recovered_cases: int


class _Runtime:
    def __init__(self) -> None:
        self.prepare_calls: list[object] = []
        self.get_calls: list[tuple[str, str]] = []
        self.status_calls: list[tuple[str, str]] = []
        self.submit_calls: list[tuple[str, str, str, bool]] = []
        self.finalize_calls: list[tuple[str, str]] = []
        self.error: Exception | None = None
        self.plan = SimpleNamespace(
            run_id="run-a", suite_run_id="suite-a", project_id="project-a",
            session_id=f"desktop:{INSTANCE}", replicate_index=0,
            capability_id="fixture-capability", capability_revision="cap-r1",
            revisions=REVISIONS,
            envelopes=(SimpleNamespace(request={"prompt": "private prompt"}),),
        )
        self.statuses_value = (
            SimpleNamespace(
                turn_id="turn-a", state="not_submitted", terminal_event_type=None,
            ),
        )

    def prepare(self, command):
        self.prepare_calls.append(command)
        if self.error is not None:
            raise self.error
        return self.plan

    def get(self, run_id: str, *, session_id: str):
        self.get_calls.append((run_id, session_id))
        if self.error is not None:
            raise self.error
        return self.plan

    def statuses(self, run_id: str, *, session_id: str):
        self.status_calls.append((run_id, session_id))
        if self.error is not None:
            raise self.error
        return self.statuses_value

    def submit(self, run_id: str, turn_id: str, *, session_id: str, confirmed: bool):
        self.submit_calls.append((run_id, turn_id, session_id, confirmed))
        if self.error is not None:
            raise self.error
        return {"turn_id": turn_id, "receipt": "private receipt"}

    def finalize(self, run_id: str, *, session_id: str):
        self.finalize_calls.append((run_id, session_id))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            artifact_ref="crp://payloads/turn-a/context-benchmark-suite-v1",
            suite=_SuiteResult(qualified=True, recovered_cases=3),
        )


def _client(runtime: _Runtime | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    active = runtime or _Runtime()
    app.state.context_benchmark_runtime_factory = lambda request: active
    return TestClient(app)


def _session() -> DesktopSession:
    return DesktopSession(
        secret=SECRET, instance_id=INSTANCE, nonce="n" * 43,
        expires_at="2030-01-01T00:00:00+00:00", allowed_origin="http://127.0.0.1:8317",
    )


def _configure_desktop(monkeypatch) -> None:
    monkeypatch.setattr("backend.api.routes.context_graph.desktop_session", _session)
    monkeypatch.setattr(
        "backend.api.routes.context_graph.desktop_session_authorized",
        lambda value: value == SECRET,
    )


def _headers() -> dict[str, str]:
    return {DESKTOP_SESSION_HEADER: SECRET}


def _prepare_body(**extra: object) -> dict[str, object]:
    return {
        "run_id": "run-a", "suite_run_id": "suite-a", "project_id": "project-a",
        "confirm_benchmark": True, "consent_refs": ["consent-a"], "replicate_index": 0,
        **extra,
    }


def test_prepare_requires_authenticated_real_desktop_session(monkeypatch) -> None:
    runtime = _Runtime()
    monkeypatch.setattr("backend.api.routes.context_graph.desktop_session", lambda: None)
    with _client(runtime) as client:
        response = client.post("/api/rebuild/context-benchmark-runs", json=_prepare_body())

    assert response.status_code == 403
    assert response.json()["code"] == "benchmark_desktop_session_required"
    assert response.headers["cache-control"] == "no-store"
    assert runtime.prepare_calls == []

    _configure_desktop(monkeypatch)
    with _client(runtime) as client:
        unauthenticated = client.post(
            "/api/rebuild/context-benchmark-runs", json=_prepare_body(),
        )
    assert unauthenticated.status_code == 403


def test_prepare_derives_authority_fields_and_rejects_client_envelope(monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    runtime = _Runtime()
    with _client(runtime) as client:
        prepared = client.post(
            "/api/rebuild/context-benchmark-runs", json=_prepare_body(), headers=_headers(),
        )
        forged = client.post(
            "/api/rebuild/context-benchmark-runs",
            json=_prepare_body(turn_envelope={"prompt": "forged"}), headers=_headers(),
        )
        unconfirmed = client.post(
            "/api/rebuild/context-benchmark-runs",
            json=_prepare_body(confirm_benchmark=False), headers=_headers(),
        )
        no_consent = client.post(
            "/api/rebuild/context-benchmark-runs",
            json=_prepare_body(consent_refs=[]), headers=_headers(),
        )

    assert prepared.status_code == 201
    assert prepared.headers["cache-control"] == "no-store"
    command = runtime.prepare_calls[0]
    assert command.session_id == f"desktop:{INSTANCE}"
    assert command.actor_id == f"desktop:{INSTANCE}"
    assert command.confirmed is True
    assert command.consent_refs == ("consent-a",)
    assert "prompt" not in str(prepared.json()).lower()
    assert "session_id" not in prepared.json()
    assert forged.status_code == 400
    assert unconfirmed.status_code == 409
    assert no_consent.status_code == 409
    assert len(runtime.prepare_calls) == 1


def test_get_and_submit_bind_session_and_never_accept_client_turn_payload(monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    runtime = _Runtime()
    with _client(runtime) as client:
        status = client.get("/api/rebuild/context-benchmark-runs/run-a", headers=_headers())
        submitted = client.post(
            "/api/rebuild/context-benchmark-runs/run-a/turns/turn-a/submit",
            json={"confirm": True}, headers=_headers(),
        )
        forged = client.post(
            "/api/rebuild/context-benchmark-runs/run-a/turns/turn-a/submit",
            json={"confirm": True, "output": "forged", "receipt": "forged"},
            headers=_headers(),
        )

    assert status.status_code == 200
    assert status.json()["turns"] == [{
        "turn_id": "turn-a", "state": "not_submitted", "terminal_event_type": None,
    }]
    assert "prompt" not in str(status.json()).lower()
    assert submitted.status_code == 202
    assert submitted.json() == {"run_id": "run-a", "turn_id": "turn-a", "submitted": True}
    assert runtime.submit_calls == [("run-a", "turn-a", f"desktop:{INSTANCE}", True)]
    assert forged.status_code == 400


def test_session_drift_is_forbidden_and_finalization_projects_generic_dataclass(monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    runtime = _Runtime()
    runtime.error = ContextBenchmarkCompositionError("benchmark session drifted")
    with _client(runtime) as client:
        drift = client.get("/api/rebuild/context-benchmark-runs/run-a", headers=_headers())
    assert drift.status_code == 403

    runtime.error = ContextBenchmarkRunError("benchmark run is unavailable")
    with _client(runtime) as client:
        missing = client.get(
            "/api/rebuild/context-benchmark-runs/missing", headers=_headers(),
        )
    assert missing.status_code == 404

    runtime.error = None
    with _client(runtime) as client:
        finalized = client.post(
            "/api/rebuild/context-benchmark-runs/run-a/finalize", json={}, headers=_headers(),
        )
        forged = client.post(
            "/api/rebuild/context-benchmark-runs/run-a/finalize",
            json={"suite_result": "forged"}, headers=_headers(),
        )

    assert finalized.status_code == 200
    assert finalized.headers["cache-control"] == "no-store"
    assert finalized.json() == {
        "run_id": "run-a",
        "artifact_ref": "crp://payloads/turn-a/context-benchmark-suite-v1",
        "suite_result": {"qualified": True, "recovered_cases": 3},
    }
    assert runtime.finalize_calls == [("run-a", f"desktop:{INSTANCE}")]
    assert forged.status_code == 400
