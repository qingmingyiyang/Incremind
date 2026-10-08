from __future__ import annotations

from types import SimpleNamespace

from backend.api.agent_organization_projection import build_agent_organization_projection
from core.ai_kernel import AgentBudget, AgentProfile


def _profile(profile_id: str, *, role: str = "subagent", tier: str = "fast", route_key: str | None = None, route_revision: int | None = None) -> AgentProfile:
    return AgentProfile(
        profile_id=profile_id, revision=2, display_name=profile_id, enabled=True,
        role=role, model_tier=tier, budget_limit=AgentBudget(1, 1, 1, 1, 1),
        capability_ids=("agent.list",), max_concurrent_children=1 if role == "main" else 0,
        max_depth=1, max_steps=1, timeout_ms=1, allow_child_spawn=role == "main",
        model_route_key=route_key, model_route_revision=route_revision,
    )


class _Profiles:
    def list_profiles(self):
        return (
            _profile("main.orchestrator", role="main", tier="deep"),
            _profile("steward.scheduler", tier="standard"),
            _profile("subagent.explorer"),
        )


class _Dispatch:
    def list_plans_for_main(self, *, project_id, main_run_id):
        assert (project_id, main_run_id) == ("alpha", "main-1")
        return (SimpleNamespace(plan_id="plan-1", status="dispatched", mode="cluster", revision=3, assignment_ids=("assignment-1",), max_concurrent_assignments=2),)

    def list_permits(self, *, project_id, plan_id):
        assert (project_id, plan_id) == ("alpha", "plan-1")
        return (SimpleNamespace(permit_id="permit-1", assignment_id="assignment-1", status="consumed"),)

    def get_permit_child_run_binding(self, permit_id, *, project_id):
        assert (permit_id, project_id) == ("permit-1", "alpha")
        return "expert-1"

    def get_assignment(self, assignment_id, *, project_id):
        assert (assignment_id, project_id) == ("assignment-1", "alpha")
        return SimpleNamespace(
            assignment_id=assignment_id, cluster_id="cluster-research",
            profile_ref="crp://agent/profiles/subagent.explorer/revisions/2",
            expert_id="researcher", skill_ids=("evidence-read",),
            task_payload_ref="crp://turn/private-task", context_policy_ref="crp://policy/private",
        )


def test_profile_skeleton_is_safe_and_does_not_invent_a_run() -> None:
    result = build_agent_organization_projection(project_id="alpha", profiles=_Profiles())

    assert result["has_active_run"] is False
    assert result["main"]["status"] == "idle"
    assert result["expert_clusters"] == []
    assert "crp://" not in str(result)
    assert "model_name" not in str(result)


def test_live_projection_joins_frozen_assignment_identity_without_payload_contents() -> None:
    topology = {
        "run": {"run_id": "main-1", "profile_id": "main.orchestrator", "status": "running"},
        "children": [
            {"run_id": "steward-1", "profile_id": "steward.scheduler", "status": "completed"},
            {"run_id": "expert-1", "profile_id": "subagent.explorer", "status": "running"},
        ],
        "plans": [{"plan_id": "plan-1", "status": "dispatched"}],
    }
    result = build_agent_organization_projection(project_id="alpha", profiles=_Profiles(), topology=topology, dispatch_store=_Dispatch())

    agent = result["expert_clusters"][0]["assignments"][0]
    task = agent["task"]
    assert task == {"assignment_id": "assignment-1", "label": "已分配专家任务", "expert_id": "researcher", "skill_ids": ("evidence-read",), "status": "running"}
    assert agent["expert_identity"] == "researcher"
    assert agent["skill_ids"] == ("evidence-read",)
    assert result["overview"] == {"status": "working", "progress": {"completed": 0, "total": 1}, "load": {"active": 1, "queued": 0, "capacity": 2}}
    assert result["projection_revision"].startswith("profiles[")
    assert "private-task" not in str(result)
    assert "crp://" not in str(result)


def test_live_projection_prefers_the_frozen_run_route_over_the_current_profile() -> None:
    class BoundProfiles:
        def list_profiles(self):
            return (_profile(
                "main.orchestrator", role="main", tier="deep",
                route_key="route.current", route_revision=2,
            ),)

    result = build_agent_organization_projection(
        project_id="alpha",
        profiles=BoundProfiles(),
        topology={"run": {
            "run_id": "main-1", "profile_id": "main.orchestrator", "status": "running",
            "model_route_key": "route.frozen", "model_route_revision": 1,
        }, "children": [], "plans": []},
    )

    assert result["main"]["model_route_key"] == "route.frozen"
    assert result["main"]["model_route_revision"] == 1
    assert result["main"]["route_binding_source"] == "frozen_run"


def test_published_assignment_without_a_child_run_remains_queued_in_the_projection() -> None:
    topology = {
        "run": {"run_id": "main-1", "profile_id": "main.orchestrator", "status": "running"},
        "children": [{"run_id": "steward-1", "profile_id": "steward.scheduler", "status": "completed"}],
        "plans": [{"plan_id": "plan-1", "status": "ready"}],
    }
    dispatch = _Dispatch()
    dispatch.list_plans_for_main = lambda **_: (SimpleNamespace(
        plan_id="plan-1", status="ready", mode="cluster", revision=2,
        assignment_ids=("assignment-1",), max_concurrent_assignments=2,
    ),)
    dispatch.list_permits = lambda **_: (SimpleNamespace(permit_id="permit-1", assignment_id="assignment-1", status="issued"),)
    dispatch.get_permit_child_run_binding = lambda *_args, **_kwargs: None

    result = build_agent_organization_projection(
        project_id="alpha", profiles=_Profiles(), topology=topology, dispatch_store=dispatch,
    )

    assignment = result["expert_clusters"][0]["assignments"][0]
    assert assignment["status"] == "queued"
    assert result["overview"]["load"] == {"active": 0, "queued": 1, "capacity": 2}


def test_same_profile_assignments_join_their_exact_permit_bound_runs() -> None:
    topology = {
        "run": {"run_id": "main-1", "profile_id": "main.orchestrator", "status": "running"},
        "children": [
            {"run_id": "steward-1", "profile_id": "steward.scheduler", "status": "completed"},
            # Deliberately reverse the assignment order. Profile-order joins
            # would attach these terminal states to the wrong task cards.
            {"run_id": "expert-2", "profile_id": "subagent.explorer", "status": "completed"},
            {"run_id": "expert-1", "profile_id": "subagent.explorer", "status": "running"},
        ],
        "plans": [{"plan_id": "plan-1", "status": "dispatched"}],
    }
    dispatch = _Dispatch()
    dispatch.list_plans_for_main = lambda **_: (SimpleNamespace(
        plan_id="plan-1", status="dispatched", mode="cluster", revision=4,
        assignment_ids=("assignment-1", "assignment-2"), max_concurrent_assignments=2,
    ),)
    dispatch.list_permits = lambda **_: (
        SimpleNamespace(permit_id="permit-1", assignment_id="assignment-1", status="consumed"),
        SimpleNamespace(permit_id="permit-2", assignment_id="assignment-2", status="consumed"),
    )
    dispatch.get_assignment = lambda assignment_id, **_: SimpleNamespace(
        assignment_id=assignment_id,
        cluster_id="cluster-research",
        profile_ref="crp://agent/profiles/subagent.explorer/revisions/2",
        expert_id=f"researcher-{assignment_id[-1]}",
        skill_ids=(f"skill-{assignment_id[-1]}",),
    )
    dispatch.get_permit_child_run_binding = lambda permit_id, **_: {
        "permit-1": "expert-1",
        "permit-2": "expert-2",
    }[permit_id]

    result = build_agent_organization_projection(
        project_id="alpha", profiles=_Profiles(), topology=topology, dispatch_store=dispatch,
    )

    first, second = result["expert_clusters"][0]["assignments"]
    assert (first["task"]["assignment_id"], first["run_id"], first["status"], first["task"]["status"]) == (
        "assignment-1", "expert-1", "running", "running",
    )
    assert (second["task"]["assignment_id"], second["run_id"], second["status"], second["task"]["status"]) == (
        "assignment-2", "expert-2", "completed", "completed",
    )
    assert result["overview"] == {
        "status": "working",
        "progress": {"completed": 1, "total": 2},
        "load": {"active": 1, "queued": 0, "capacity": 2},
    }


def test_projection_revision_tracks_safe_dispatch_identity_and_capacity() -> None:
    topology = {
        "run": {"run_id": "main-1", "profile_id": "main.orchestrator", "status": "running"},
        "children": [
            {"run_id": "steward-1", "profile_id": "steward.scheduler", "status": "completed"},
            {"run_id": "expert-1", "profile_id": "subagent.explorer", "status": "running"},
        ],
        "plans": [{"plan_id": "plan-1", "status": "dispatched"}],
    }
    dispatch = _Dispatch()
    first = build_agent_organization_projection(
        project_id="alpha", profiles=_Profiles(), topology=topology, dispatch_store=dispatch,
    )
    original_get_assignment = dispatch.get_assignment
    dispatch.get_assignment = lambda assignment_id, **kwargs: SimpleNamespace(
        **{
            **vars(original_get_assignment(assignment_id, **kwargs)),
            "expert_id": "alternate-researcher",
            "skill_ids": ("alternate-skill",),
        }
    )
    dispatch.list_plans_for_main = lambda **_: (SimpleNamespace(
        plan_id="plan-1", status="dispatched", mode="cluster", revision=4,
        assignment_ids=("assignment-1",), max_concurrent_assignments=1,
    ),)
    changed = build_agent_organization_projection(
        project_id="alpha", profiles=_Profiles(), topology=topology, dispatch_store=dispatch,
    )

    assert first["overview"]["progress"] == changed["overview"]["progress"]
    assert first["main"]["status"] == changed["main"]["status"]
    assert first["projection_revision"] != changed["projection_revision"]


def test_terminal_plan_status_is_projected_onto_its_exact_cluster() -> None:
    topology = {
        "run": {"run_id": "main-1", "profile_id": "main.orchestrator", "status": "running"},
        "children": [{"run_id": "steward-1", "profile_id": "steward.scheduler", "status": "completed"}],
        "plans": [{"plan_id": "plan-1", "status": "failed"}],
    }
    dispatch = _Dispatch()
    dispatch.list_plans_for_main = lambda **_: (SimpleNamespace(
        plan_id="plan-1", status="failed", mode="cluster", revision=5,
        assignment_ids=("assignment-1",), max_concurrent_assignments=1,
        expert_cluster_ref="crp://dispatch/cluster/cluster-research",
    ),)
    dispatch.list_permits = lambda **_: (
        SimpleNamespace(permit_id="permit-1", assignment_id="assignment-1", status="revoked"),
    )
    dispatch.get_permit_child_run_binding = lambda *_args, **_kwargs: None

    result = build_agent_organization_projection(
        project_id="alpha", profiles=_Profiles(), topology=topology, dispatch_store=dispatch,
    )

    cluster = result["expert_clusters"][0]
    assert cluster["cluster_id"] == "cluster-research"
    assert cluster["status"] == "failed"
    assert cluster["assignments"][0]["status"] == "cancelled"
