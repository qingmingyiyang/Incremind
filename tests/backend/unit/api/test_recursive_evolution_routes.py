from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.recursive_evolution_runtime import RecursiveEvolutionRuntimeError
from backend.api.routes import include_api_routers
from backend.api.routes.recursive_evolution import router


PROJECT = "project-alpha"
EPISODE = "episode-alpha"
PROPOSAL = "proposal-alpha"
_HUMAN = {
    "command_id": "decision-alpha",
    "user_id": "human-alpha",
    "confirmation_ref": "crp://approval/alpha",
    "evidence_refs": ["crp://evidence/alpha"],
}


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.projection = SimpleNamespace(
            episode_id=EPISODE,
            project_id=PROJECT,
            target_kind=SimpleNamespace(value="agent_profile"),
            current_generation=1,
            candidate_count=1,
            evaluation_count=1,
            budget_used=2,
            is_terminal=False,
            stop_reason=None,
            proposal_statuses={PROPOSAL: "canary_passed"},
            policy=SimpleNamespace(
                max_generations=2,
                max_candidates_per_generation=2,
                max_evaluations_per_candidate=2,
                total_budget_units=10,
                max_no_improvement_cycles=2,
                minimum_improvement=0.01,
                minimum_canary_samples=1,
                requires_user_confirmation=True,
                auto_promote_allowed=False,
                canary_required=True,
            ),
        )

    def _result(self, name: str, **kwargs):
        self.calls.append((name, kwargs))
        return SimpleNamespace(replayed=False, projection=self.projection)

    def list_projections(self, **kwargs):
        self.calls.append(("list_projections", kwargs))
        return (self.projection,)

    def get_projection(self, **kwargs):
        self.calls.append(("get_projection", kwargs))
        return self.projection

    def create_episode(self, **kwargs): return self._result("create_episode", **kwargs)
    def record_candidate(self, **kwargs): return self._result("record_candidate", **kwargs)
    def record_evaluation_from_receipt(self, **kwargs): return self._result("record_evaluation", **kwargs)
    def record_review(self, **kwargs): return self._result("record_review", **kwargs)
    def approve_canary(self, **kwargs): return self._result("approve_canary", **kwargs)
    def observe_canary(self, **kwargs): return self._result("observe_canary", **kwargs)
    def promote(self, **kwargs): return self._result("promote", **kwargs)
    def rollback(self, **kwargs): return self._result("rollback", **kwargs)
    def reject(self, **kwargs): return self._result("reject", **kwargs)
    def stop(self, **kwargs): return self._result("stop", **kwargs)


def _client(runtime: object | None = None, *, client: tuple[str, int] | None = None) -> TestClient:
    app = FastAPI()
    if runtime is not None:
        app.state.recursive_evolution_runtime = runtime
    app.include_router(router)
    return TestClient(app, client=client or ("testclient", 50000))


def _episode_body() -> dict[str, object]:
    return {
        "command_id": EPISODE,
        "episode_id": EPISODE,
        "target_kind": "agent_profile",
        "policy": {
            "max_generations": 2,
            "max_candidates_per_generation": 2,
            "max_evaluations_per_candidate": 2,
            "total_budget_units": 10,
            "max_no_improvement_cycles": 2,
            "deadline_at": "2026-09-05T00:00:00Z",
            "minimum_improvement": 0.01,
            "minimum_canary_samples": 1,
            "requires_user_confirmation": True,
            "auto_promote_allowed": False,
            "canary_required": True,
        },
    }


def _candidate_body() -> dict[str, object]:
    envelope = {
        "capabilities": ["read"], "budget_units": 1, "max_depth": 0,
        "max_concurrency": 0, "egress": [],
    }
    return {
        "command_id": PROPOSAL,
        "proposal_id": PROPOSAL,
        "episode_id": EPISODE,
        "generation": 1,
        "target_kind": "agent_profile",
        "proposer_id": "proposer-alpha",
        "executor_id": "executor-alpha",
        "baseline_ref": "crp://target/baseline",
        "baseline_revision": "base-v1",
        "parent_proposal_id": None,
        "candidate_ref": "crp://target/candidate",
        "candidate_revision": "candidate-v1",
        "baseline_envelope": envelope,
        "candidate_envelope": envelope,
    }


def _review_body() -> dict[str, object]:
    return {
        "command_id": "review-alpha",
        "review_id": "review-alpha",
        "episode_id": EPISODE,
        "proposal_id": PROPOSAL,
        "reviewer_id": "reviewer-alpha",
        "verdict": "qualified",
        "evaluation_ids": ["evaluation-alpha"],
        "evidence_refs": ["crp://evidence/review-alpha"],
    }


def _assert_safe_response(body: object) -> None:
    forbidden = {
        "candidate_ref", "baseline_ref", "prompt", "context", "evidence_refs",
        "source_ref", "receipt_ref", "provider", "model", "endpoint", "secret",
        "path", "revision", "evaluation_input_ref", "baseline_result_ref",
        "candidate_result_ref",
    }

    def visit(value: object, key: str | None = None) -> None:
        if key is not None:
            lowered = key.lower()
            assert lowered not in forbidden, key
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(body)


def test_routes_are_loopback_only_and_authority_is_required() -> None:
    with _client(_Runtime(), client=("203.0.113.9", 50000)) as client:
        forbidden = client.get(f"/api/rebuild/projects/{PROJECT}/recursive-evolution/episodes")
    assert forbidden.status_code == 403
    assert forbidden.headers["cache-control"] == "no-store"
    with _client() as client:
        unavailable = client.get(f"/api/rebuild/projects/{PROJECT}/recursive-evolution/episodes")
    assert unavailable.status_code == 503
    assert unavailable.headers["cache-control"] == "no-store"


def test_route_lazily_initializes_the_shared_ai_runtime(monkeypatch) -> None:
    import backend.api.ai_runtime as ai_runtime

    runtime = _Runtime()
    calls: list[object] = []

    def build(request, container):
        calls.append(container)
        request.app.state.recursive_evolution_runtime = runtime
        return object()

    monkeypatch.setattr(ai_runtime, "get_or_build_ai_runtime", build)
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir="unused")
    app.include_router(router)
    with TestClient(app, client=("testclient", 50000)) as client:
        response = client.get(f"/api/rebuild/projects/{PROJECT}/recursive-evolution/episodes")
    assert response.status_code == 200
    assert calls == [app.state.container]
    assert response.json()["episodes"][0]["episode_id"] == EPISODE


def test_exact_bodies_reject_raw_evaluation_and_canary_claims() -> None:
    runtime = _Runtime()
    with _client(runtime) as client:
        evaluation = client.post(
            f"/api/rebuild/projects/{PROJECT}/recursive-evolution/episodes/{EPISODE}/candidates/{PROPOSAL}/evaluations",
            json={"command_id": "evaluation-alpha", "receipt_ref": "crp://receipt/alpha", "candidate_score": 1.0},
        )
        observe = client.post(
            f"/api/rebuild/projects/{PROJECT}/recursive-evolution/episodes/{EPISODE}/candidates/{PROPOSAL}/canary/observe",
            json={"command_id": "observe-alpha", "evidence_ref": "crp://evidence/alpha", "passed": True, "samples": 100},
        )
        missing_human_proof = client.post(
            f"/api/rebuild/projects/{PROJECT}/recursive-evolution/episodes/{EPISODE}/candidates/{PROPOSAL}/promote",
            json={"command_id": "decision-alpha", "user_id": "agent", "evidence_refs": ["crp://evidence/alpha"]},
        )
    assert evaluation.status_code == observe.status_code == 400
    assert missing_human_proof.status_code == 400
    assert not runtime.calls


def test_runtime_remains_human_authority_for_user_actions() -> None:
    class _HumanRuntime(_Runtime):
        def promote(self, **kwargs):
            if kwargs["user_id"] == "agent":
                raise RecursiveEvolutionRuntimeError("local human identity is required")
            return super().promote(**kwargs)

    with _client(_HumanRuntime()) as client:
        response = client.post(
            f"/api/rebuild/projects/{PROJECT}/recursive-evolution/episodes/{EPISODE}/candidates/{PROPOSAL}/promote",
            json={**_HUMAN, "user_id": "agent"},
        )
    assert response.status_code == 409


def test_safe_projection_responses_and_happy_lifecycle() -> None:
    runtime = _Runtime()
    prefix = f"/api/rebuild/projects/{PROJECT}/recursive-evolution"
    with _client(runtime) as client:
        responses = [
            client.post(f"{prefix}/episodes", json=_episode_body()),
            client.post(f"{prefix}/episodes/{EPISODE}/candidates", json=_candidate_body()),
            client.post(f"{prefix}/episodes/{EPISODE}/candidates/{PROPOSAL}/evaluations", json={
                "command_id": "evaluation-alpha", "receipt_ref": "crp://receipt/evaluation-alpha",
            }),
            client.post(f"{prefix}/episodes/{EPISODE}/reviews", json=_review_body()),
            client.post(f"{prefix}/episodes/{EPISODE}/candidates/{PROPOSAL}/canary/approve", json=_HUMAN),
            client.post(f"{prefix}/episodes/{EPISODE}/candidates/{PROPOSAL}/canary/observe", json={
                "command_id": "observe-alpha", "evidence_ref": "crp://evidence/canary-alpha",
            }),
            client.post(f"{prefix}/episodes/{EPISODE}/candidates/{PROPOSAL}/promote", json=_HUMAN),
            client.post(f"{prefix}/episodes/{EPISODE}/candidates/{PROPOSAL}/rollback", json=_HUMAN),
            client.post(f"{prefix}/episodes/{EPISODE}/candidates/{PROPOSAL}/reject", json=_HUMAN),
            client.post(f"{prefix}/episodes/{EPISODE}/stop", json=_HUMAN),
            client.get(f"{prefix}/episodes"),
            client.get(f"{prefix}/episodes/{EPISODE}"),
        ]
    assert all(response.status_code == 200 for response in responses)
    assert all(response.headers["cache-control"] == "no-store" for response in responses)
    for response in responses:
        _assert_safe_response(response.json())
    assert responses[-2].json()["episodes"][0]["policy_limits"] == {
        "max_generations": 2,
        "max_candidates_per_generation": 2,
        "max_evaluations_per_candidate": 2,
        "total_budget_units": 10,
        "max_no_improvement_cycles": 2,
        "minimum_improvement": 0.01,
        "minimum_canary_samples": 1,
    }
    assert responses[-2].json()["episodes"][0]["available_actions"] == {
        PROPOSAL: ["promote"],
        "episode": ["stop"],
    }
    assert {name for name, _kwargs in runtime.calls} >= {
        "create_episode", "record_candidate", "record_evaluation", "record_review",
        "approve_canary", "observe_canary", "promote", "rollback", "reject", "stop",
        "list_projections", "get_projection",
    }


def test_local_action_contract_uses_server_side_confirmation_and_evidence() -> None:
    class _Actions:
        def __init__(self, runtime: _Runtime) -> None:
            self.runtime = runtime
            self.calls: list[dict[str, object]] = []

        def execute(self, **kwargs):
            self.calls.append(kwargs)
            return self.runtime._result("local_action", **kwargs)

    runtime = _Runtime()
    actions = _Actions(runtime)
    app = FastAPI()
    app.state.recursive_evolution_runtime = runtime
    app.state.recursive_evolution_local_actions = actions
    app.include_router(router)
    prefix = f"/api/rebuild/projects/{PROJECT}/recursive-evolution"
    with TestClient(app, client=("testclient", 50000)) as client:
        response = client.post(
            f"{prefix}/episodes/{EPISODE}/candidates/{PROPOSAL}/actions/promote",
            json={"command_id": "local-promote"},
        )
        invalid = client.post(
            f"{prefix}/episodes/{EPISODE}/candidates/{PROPOSAL}/actions/promote",
            json={"command_id": "local-promote-2", "evidence_refs": ["crp://bad"]},
        )
        stop = client.post(
            f"{prefix}/episodes/{EPISODE}/actions/stop",
            json={"command_id": "local-stop"},
        )
    assert response.status_code == stop.status_code == 200
    assert invalid.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert actions.calls == [
        {
            "project_id": PROJECT,
            "episode_id": EPISODE,
            "proposal_id": PROPOSAL,
            "action": "promote",
            "command_id": "local-promote",
        },
        {
            "project_id": PROJECT,
            "episode_id": EPISODE,
            "proposal_id": None,
            "action": "stop",
            "command_id": "local-stop",
        },
    ]
    _assert_safe_response(response.json())


def test_runtime_errors_are_conflicts_and_routers_are_registered() -> None:
    class _ConflictRuntime(_Runtime):
        def list_projections(self, **kwargs):
            raise RecursiveEvolutionRuntimeError("busy")

    with _client(_ConflictRuntime()) as client:
        response = client.get(f"/api/rebuild/projects/{PROJECT}/recursive-evolution/episodes")
    assert response.status_code == 409
    app = FastAPI()
    include_api_routers(app)
    assert any(getattr(route, "original_router", None) is router for route in app.routes)
