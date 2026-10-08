from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.api.agent_steward_proposal import (
    AgentStewardProposalBuilder,
    AgentStewardProposalError,
    is_valid_steward_proposal,
)
from backend.api.workbench_ai_runtime import WORLD_PROJECT_SESSION_ID
from core.ai_kernel import AgentBudget, AgentProfileRegistry


class _Store:
    def __init__(self, main, steward): self.runs = {main.run_id: main, steward.run_id: steward}; self.links = (SimpleNamespace(status="spawned"),); self.reservations = ()
    def get_run(self, run_id): return self.runs.get(run_id)
    def list_child_links(self, **_): return self.links
    def list_reservations(self, **_): return self.reservations


class _Capabilities:
    def get(self, _): return SimpleNamespace(profile=SimpleNamespace(enabled_skill_ids=("evidence-read", "other")))


class _Catalog:
    def get(self, expert_id):
        return {"expert_id": expert_id, "revision": 2, "status": "active", "tools": ["memory.recall", "source.evidence.read"], "applicable_tasks": ["research", "review"], "skills": [{"skill_id": "evidence-read"}]}


class _Bindings:
    def list_for_project(self, _): return ({"default": True, "selection_mode": "manual", "expert_id": "expert-a", "enabled_expert_revision": 2},)


def _main():
    return SimpleNamespace(run_id="main-run", turn_id="turn-main", project_id="project-a", role="main", max_concurrent_children=3, budget_limit=AgentBudget(8, 8, 8000, 4000, 80000), capability_ids=("memory.recall", "source.evidence.read", "project_skill.evidence.read"))


def _steward():
    return SimpleNamespace(run_id="steward-run", turn_id="turn-steward", project_id="project-a", role="subagent", profile_id="steward.scheduler", profile_revision=1, parent_run_id="main-run", depth=1, cancel_epoch=0, budget_snapshot_ref="crp://agent/steward/budget")


def _request():
    steward = _steward()
    return {"turn_id": steward.turn_id, "desired_outcome": "agent.steward.plan", "scope": {"kind": "project", "project_id": "project-a"}, "agent_binding": {"run_id": steward.run_id, "role": steward.role, "profile_id": steward.profile_id, "profile_revision": steward.profile_revision, "parent_run_id": steward.parent_run_id, "depth": steward.depth, "cancel_epoch": steward.cancel_epoch, "budget_snapshot_ref": steward.budget_snapshot_ref}}


def _main_request():
    return {"scope": {"kind": "project", "project_id": "project-a"}, "input": {"kind": "text", "text": "Research and review the supplied evidence before drafting a concise answer.", "refs": [{}, {}, {}]}, "desired_outcome": "research.answer", "capability_policy": {"allowed": ["memory.recall", "source.evidence.read", "project_skill.evidence.read"]}}


def _builder(*, main=None, request=None, capability_definition=None):
    main, steward = main or _main(), _steward()
    return AgentStewardProposalBuilder(profiles=AgentProfileRegistry(), agent_store=_Store(main, steward), request_loader=lambda turn_id: (request or _main_request()) if turn_id == "turn-main" else {}, capability_profiles=_Capabilities(), expert_catalog=_Catalog(), expert_bindings=_Bindings(), capability_definition=capability_definition)


def test_builds_exact_deterministic_plan_with_governed_expert_and_skill() -> None:
    builder = _builder()
    proposal = builder.build(_request())
    assert proposal == builder.build(_request())
    assert set(proposal) == {"mode", "plan_id", "cluster_id", "assignments"}
    assert 1 <= len(proposal["assignments"]) <= 2
    assignment = proposal["assignments"][0]
    assert set(assignment) == {"assignment_id", "profile_id", "profile_revision", "task", "budget", "capability_ids", "expert", "skill"}
    assert assignment["expert"] == {"expert_id": "expert-a", "task_intents": ["research", "review"], "budget": "standard"}
    assert assignment["skill"] == {"skill_ids": ["evidence-read"]}
    assert set(assignment["capability_ids"]).issubset({"memory.recall", "source.evidence.read", "project_skill.evidence.read"})
    assert not ({"provider", "model", "secret", "endpoint", "path"} & _keys(proposal))
    assert is_valid_steward_proposal(proposal)


def test_rejects_unverified_steward_request() -> None:
    request = _request(); request["agent_binding"]["profile_id"] = "subagent.explorer"
    with pytest.raises(AgentStewardProposalError): _builder().build(request)


def test_world_action_cluster_requires_reviewer_with_bounded_verification_task() -> None:
    request = _main_request() | {"session_id": WORLD_PROJECT_SESSION_ID}

    proposal = _builder(request=request).build(_request())

    assert proposal["mode"] == "cluster"
    assert len(proposal["assignments"]) <= 2
    reviewer = next(item for item in proposal["assignments"] if item["profile_id"] == "subagent.reviewer")
    assert all(token in reviewer["task"] for token in ("hypothesis", "expected outcome", "falsification", "stop conditions"))
    assert "additional authority" in reviewer["task"]
    assert "VERDICT=<supported|weakened|refuted|inconclusive>" in reviewer["task"]
    assert "DISPOSITION=<continue|replan_required|stop_required|escalate_user>" in reviewer["task"]
    assert "FINDING=<bounded text>" in reviewer["task"]
    assert "Do not include receipt, ref, path, or secret" in reviewer["task"]


def test_short_world_action_still_assigns_reviewer_when_capacity_is_safe() -> None:
    request = _main_request() | {
        "session_id": WORLD_PROJECT_SESSION_ID,
        "input": {"kind": "text", "text": "Check this.", "refs": []},
        "desired_outcome": "answer",
    }

    proposal = _builder(request=request).build(_request())

    assert proposal["mode"] == "cluster"
    assert [item["profile_id"] for item in proposal["assignments"]] == ["subagent.reviewer"]


def test_world_action_without_safe_reviewer_capacity_stays_main_only() -> None:
    main = _main(); main.max_concurrent_children = 1
    request = _main_request() | {"session_id": WORLD_PROJECT_SESSION_ID}

    proposal = _builder(main=main, request=request).build(_request())

    assert proposal == {"mode": "main_only", "plan_id": "steward-main-only"}


def test_proposal_validator_rejects_unbounded_or_malformed_provider_output() -> None:
    assert is_valid_steward_proposal({"mode": "main_only", "plan_id": "main-only"})
    assert not is_valid_steward_proposal({"mode": "main_only", "plan_id": "main-only", "provider": "x"})
    proposal = _builder().build(_request())
    proposal["assignments"][0]["capability_ids"] = [{"untrusted": True}]
    assert not is_valid_steward_proposal(proposal)


def _keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(item) for item in value.values()))
    if isinstance(value, list): return set().union(*(_keys(item) for item in value)) if value else set()
    return set()



def test_rule_proposal_removes_worker_spawn_before_permit_freeze():
    main = _main(); main.capability_ids += ("agent.spawn",)
    request = _main_request()
    request["desired_outcome"] = "draft.generate"
    request["capability_policy"]["allowed"].append("agent.spawn")
    proposal = _builder(main=main, request=request).build(_request())
    worker = next(item for item in proposal["assignments"] if item["profile_id"] == "subagent.worker")
    assert "agent.spawn" not in worker["capability_ids"]


def test_brief_is_bounded_and_complete_fills_authority_for_distinct_subtasks():
    request = _main_request(); request["input"]["text"] = "x" * 2600
    builder = _builder(request=request)
    brief = builder.brief(_request())
    assert brief["task"] == "x" * 2500 + "（已截断）"
    assert brief["slots"] == 2 and brief["budget_usable"] is True
    chosen = {"mode": "cluster", "assignments": [
        {"profile_id": "subagent.explorer", "task": "research the source"},
        {"profile_id": "subagent.reviewer", "task": "review the constraints"}]}
    proposal = builder.complete(_request(), chosen)
    assert [item["task"] for item in proposal["assignments"]] == [item["task"] for item in chosen["assignments"]]
    assert [item["assignment_id"] for item in proposal["assignments"]] == ["steward-assignment-1", "steward-assignment-2"]
    assert all(item["profile_revision"] == 1 for item in proposal["assignments"])
    assert is_valid_steward_proposal(proposal)


@pytest.mark.parametrize("assignments", [[],
    [{"profile_id": "subagent.explorer", "task": "a"}] * 3,
    [{"profile_id": "subagent.custom.unknown", "task": "a"}],
    [{"profile_id": "subagent.explorer", "task": "a b"}, {"profile_id": "subagent.reviewer", "task": " a  b "}],
    [{"profile_id": "subagent.explorer", "task": " "}],
    [{"profile_id": "subagent.explorer", "task": "x" * 601}],
    [{"profile_id": "subagent.explorer", "task": "a", "capability_ids": ["agent.spawn"]}]])
def test_complete_rejects_entire_invalid_model_selection(assignments):
    with pytest.raises(AgentStewardProposalError):
        _builder().complete(_request(), {"mode": "cluster", "assignments": assignments})
