from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from backend.api.agent_coordinator import PreparedChildTurn, PreparedMainTurn, SpawnRequest
from backend.api.agent_organization_runtime import AgentOrganizationError, AgentOrganizationRuntime, _permit_child_run_id
from core.ai_kernel import AgentBudget, AgentBudgetReservation, AgentChildLink, AgentFanIn, AgentRun, TurnReceipt
from core.ai_kernel.agent_dispatch_contracts import AgentDispatchPlan, DispatchPermit


ROOT = Path(__file__).resolve().parents[4]


def _budget() -> AgentBudget:
    return AgentBudget(8, 8, 800, 800, 8_000)


def _request() -> dict[str, object]:
    return json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))


def _run(*, run_id: str, turn_id: str, profile_id: str, role: str, status: str, parent: str | None = None) -> AgentRun:
    terminal = "crp://receipts/terminal" if status in {"completed", "failed", "cancelled", "timed_out", "quarantined"} else None
    return AgentRun(
        run_id, turn_id, "project-alpha", profile_id, 1, role, "standard", status,
        0 if role == "main" else 1, 0, _budget(),
        ("agent.spawn", "agent.fan_in", "agent.plan"), 2 if role == "main" else 0,
        2, 8, 8_000, role == "main",
        "crp://snapshots/model", "crp://snapshots/capability", "crp://snapshots/context",
        "crp://snapshots/budget", terminal, parent,
    )


def _plan(*, mode: str, status: str = "ready", assignments: tuple[str, ...] = ()) -> AgentDispatchPlan:
    return AgentDispatchPlan(
        "plan-organization-001", "project-alpha", "main-run", "steward-run", 1,
        status, mode, "crp://dispatch/intake/one", 1, "crp://dispatch/workload/one", 1,
        "crp://dispatch/capacity/one", 1,
        "crp://dispatch/cluster/one" if mode == "cluster" else None,
        1 if mode == "cluster" else None, assignments, _budget(),
        len(assignments) if mode == "cluster" else 0,
    )


class _Coordinator:
    def __init__(self) -> None:
        self.main = _run(run_id="main-run", turn_id="turn-main-organization-001", profile_id="main.orchestrator", role="main", status="queued")
        self.steward = _run(run_id="steward-run", turn_id="turn-steward-organization-001", profile_id="steward.scheduler", role="subagent", status="queued", parent="main-run")
        self.accepted: list[dict[str, object]] = []
        self.prepared_requests: list[dict[str, object]] = []
        self.submitted: list[str] = []
        self.permits: list[str] = []
        self.fan_ins: list[AgentFanIn] = []
        self.replayed: list[str] = []
        self.fail_permit: str | None = None

    def accept_and_register_main(self, request):
        self.accepted.append(dict(request))
        request = dict(request); request["turn_id"] = self.main.turn_id
        return PreparedMainTurn(self.main, request)

    def prepare_child(self, *, parent_turn_id, request: SpawnRequest, project_id, scope, privacy, child_request_factory=None):
        assert parent_turn_id == self.main.turn_id and request.profile_id == "steward.scheduler"
        assert request.input_text == _request()["input"]["text"]
        child_request = _request(); child_request.update({"turn_id": self.steward.turn_id, "session_id": "session-steward", "operation_id": request.operation_id, "idempotency_key": request.idempotency_key, "desired_outcome": "agent.steward.plan"})
        child_request = dict(child_request_factory(child_request)) if child_request_factory else child_request
        self.prepared_requests.append(child_request)
        return _prepared(self.steward, self.main, child_request)

    def submit_accepted_turn(self, prepared):
        self.submitted.append(prepared.run.run_id)
        return TurnReceipt(prepared.run.turn_id, "session", "op", "accepted", 1, False)

    def resubmit_existing_turn(self, *, turn_id, project_id):
        assert project_id == "project-alpha"
        self.replayed.append(turn_id)
        return TurnReceipt(turn_id, "session", "op", "accepted", 1, True)

    def prepare_child_from_permit(self, *, project_id, permit_id, operation_id):
        if permit_id == self.fail_permit:
            raise RuntimeError("test permit failure")
        self.permits.append(permit_id)
        run = _run(run_id=f"permit-child-{permit_id}", turn_id=f"turn-permit-child-{permit_id}", profile_id="subagent.worker", role="subagent", status="queued", parent="main-run")
        return _prepared(run, self.main, _request())

    def fan_in(self, *, parent_turn_id, fan_in, operation_id, project_id, scope, privacy):
        assert parent_turn_id == self.main.turn_id
        self.fan_ins.append(fan_in)
        return {"status": "created", "fan_in_id": fan_in.fan_in_id}


def _prepared(child: AgentRun, main: AgentRun, request: dict[str, object]) -> PreparedChildTurn:
    link = AgentChildLink(f"link-{child.run_id}", main.run_id, child.run_id, main.project_id, main.project_id, "operation-organization", 0, 1, _budget(), child.capability_ids, "spawned")
    reservation = AgentBudgetReservation(f"reservation-{child.run_id}", main.project_id, main.run_id, child.run_id, "operation-organization", 0, _budget(), None, "reserved")
    return PreparedChildTurn(child, link, reservation, request)


class _Runs:
    def __init__(self, coordinator: _Coordinator) -> None:
        self.by_id = {coordinator.main.run_id: coordinator.main, coordinator.steward.run_id: coordinator.steward}
        self.fan_in_results = {}

    def get_run_by_turn_id(self, turn_id, *, project_id):
        for value in self.by_id.values():
            if value.turn_id == turn_id and value.project_id == project_id:
                return value, 1
        return None

    def get_run_with_revision(self, run_id, *, project_id):
        value = self.by_id.get(run_id)
        return (value, 1) if value is not None and value.project_id == project_id else None

    def get_fan_in_result(self, fan_in_id, *, project_id):
        value = self.fan_in_results.get(fan_in_id)
        return value if value is not None and value.project_id == project_id else None


class _Dispatch:
    def __init__(self, plan: AgentDispatchPlan, permits: tuple[DispatchPermit, ...] = ()) -> None:
        self.plan = plan
        self.permits = permits
        self.transitions: list[str] = []

    def list_active_plans(self, *, project_id, main_run_id, steward_run_id):
        return (self.plan,) if self.plan.status in {"ready", "dispatching"} and (project_id, main_run_id, steward_run_id) == ("project-alpha", "main-run", "steward-run") else ()

    def list_plans_for_main(self, *, project_id, main_run_id):
        return (self.plan,) if (project_id, main_run_id) == (self.plan.project_id, self.plan.main_run_id) else ()

    def list_permits(self, *, project_id, plan_id=None):
        return self.permits if project_id == self.plan.project_id and plan_id == self.plan.plan_id else ()

    def get_plan(self, plan_id, *, project_id):
        return self.plan if (plan_id, project_id) == (self.plan.plan_id, self.plan.project_id) else None

    def transition_plan(self, value, *, expected_revision, operation_id):
        assert expected_revision == self.plan.revision
        self.plan = value
        self.transitions.append(value.status)
        return value


def _runtime(coordinator: _Coordinator, dispatch: _Dispatch, runs: _Runs, *, start_pairs=(), recovery_plans=(), freshness_gate=None) -> AgentOrganizationRuntime:
    requests = {
        coordinator.main.turn_id: _request(),
        coordinator.steward.turn_id: {**_request(), "turn_id": coordinator.steward.turn_id, "session_id": "session-steward", "operation_id": "operation-steward", "idempotency_key": "idempotency-steward"},
    }
    for run in runs.by_id.values():
        requests.setdefault(
            run.turn_id,
            {
                **_request(), "turn_id": run.turn_id,
                "session_id": f"session-{run.run_id}",
                "operation_id": f"operation-{run.run_id}",
                "idempotency_key": f"idempotency-{run.run_id}",
            },
        )
    return AgentOrganizationRuntime(
        coordinator=coordinator, dispatch_store=dispatch, run_store=runs,
        request_loader=lambda turn_id: requests[turn_id],
        start_pair_scanner=lambda _limit: start_pairs,
        recovery_plan_scanner=lambda _limit: recovery_plans,
        freshness_gate=freshness_gate,
    )


def test_start_rejects_caller_agent_routing_fields() -> None:
    coordinator = _Coordinator(); runs = _Runs(coordinator); runtime = _runtime(coordinator, _Dispatch(_plan(mode="main_only")), runs)
    request = _request(); request["agent_binding"] = {"run_id": "spoof"}
    with pytest.raises(AgentOrganizationError):
        runtime.start(request)
    assert coordinator.accepted == []


def test_start_submits_steward_before_main_and_only_adds_non_denied_host_controls() -> None:
    coordinator = _Coordinator(); runs = _Runs(coordinator); runtime = _runtime(coordinator, _Dispatch(_plan(mode="main_only")), runs)
    request = _request(); request["capability_policy"]["denied"].append("agent.wait")
    result = runtime.start(request, agent_turn_mode=True)
    policy = coordinator.accepted[0]["capability_policy"]
    assert result["status_code"] == 202
    assert coordinator.submitted == ["steward-run", "main-run"]
    assert coordinator.prepared_requests[0]["desired_outcome"] == "agent.steward.plan"
    assert "agent.spawn" in policy["allowed"] and "agent.wait" not in policy["allowed"]


def test_terminal_main_only_progresses_to_completed() -> None:
    coordinator = _Coordinator(); coordinator.steward = replace(coordinator.steward, status="completed", terminal_receipt_ref="crp://receipts/steward")
    runs = _Runs(coordinator); dispatch = _Dispatch(_plan(mode="main_only")); runtime = _runtime(coordinator, dispatch, runs)
    result = runtime.on_terminal(coordinator.steward.turn_id)
    assert result["plans"][0]["status"] == "completed"
    assert dispatch.transitions == ["dispatching", "dispatched", "completed"]


def test_main_only_recovery_continues_forward_from_dispatched() -> None:
    coordinator = _Coordinator(); coordinator.steward = replace(coordinator.steward, status="completed", terminal_receipt_ref="crp://receipts/steward")
    runs = _Runs(coordinator); dispatch = _Dispatch(replace(_plan(mode="main_only"), status="dispatched")); runtime = _runtime(coordinator, dispatch, runs)
    runtime.on_terminal(coordinator.steward.turn_id)
    assert dispatch.plan.status == "completed"
    assert dispatch.transitions == ["completed"]


def test_terminal_cluster_submits_permits_then_creates_one_stable_fan_in_and_replays() -> None:
    coordinator = _Coordinator(); coordinator.steward = replace(coordinator.steward, status="completed", terminal_receipt_ref="crp://receipts/steward")
    permits = tuple(DispatchPermit(f"permit-{index}", "project-alpha", "plan-organization-001", 1, f"assignment-{index}", "operation-plan") for index in (1, 2))
    runs = _Runs(coordinator); dispatch = _Dispatch(_plan(mode="cluster", assignments=("assignment-1", "assignment-2")), permits); runtime = _runtime(coordinator, dispatch, runs)
    runtime.on_terminal(coordinator.steward.turn_id)
    runtime.on_terminal(coordinator.steward.turn_id)
    assert coordinator.permits == ["permit-1", "permit-2"]
    assert coordinator.submitted[-2:] == ["permit-child-permit-1", "permit-child-permit-2"]
    assert dispatch.transitions == ["dispatching", "dispatched"]
    assert len(coordinator.fan_ins) == 1 and coordinator.fan_ins[0].policy == "all"


def test_cluster_partial_failure_stays_dispatching_for_recovery() -> None:
    coordinator = _Coordinator(); coordinator.steward = replace(coordinator.steward, status="completed", terminal_receipt_ref="crp://receipts/steward")
    permits = tuple(DispatchPermit(f"permit-{index}", "project-alpha", "plan-organization-001", 1, f"assignment-{index}", "operation-plan") for index in (1, 2))
    coordinator.fail_permit = "permit-2"
    runs = _Runs(coordinator); dispatch = _Dispatch(_plan(mode="cluster", assignments=("assignment-1", "assignment-2")), permits); runtime = _runtime(coordinator, dispatch, runs)
    with pytest.raises(RuntimeError):
        runtime.on_terminal(coordinator.steward.turn_id)
    assert dispatch.plan.status == "dispatching"
    assert dispatch.transitions == ["dispatching"] and coordinator.fan_ins == []


def test_freshness_gate_keeps_ready_cluster_unbound_and_unconsumed() -> None:
    coordinator = _Coordinator(); coordinator.steward = replace(coordinator.steward, status="completed", terminal_receipt_ref="crp://receipts/steward")
    permits = tuple(DispatchPermit(f"permit-{index}", "project-alpha", "plan-organization-001", 1, f"assignment-{index}", "operation-plan") for index in (1, 2))
    runs = _Runs(coordinator); dispatch = _Dispatch(_plan(mode="cluster", assignments=("assignment-1", "assignment-2")), permits)
    seen = []
    runtime = _runtime(coordinator, dispatch, runs, freshness_gate=lambda project_id: seen.append(project_id) or False)

    result = runtime.on_terminal(coordinator.steward.turn_id)

    assert seen == ["project-alpha"]
    assert result["plans"] == ({"plan_id": dispatch.plan.plan_id, "status": "ready", "mode": "cluster", "revision": 1},)
    assert dispatch.plan.status == "ready" and dispatch.transitions == [] and coordinator.permits == []


def test_freshness_gate_does_not_block_dispatching_cluster_recovery() -> None:
    coordinator = _Coordinator(); coordinator.steward = replace(coordinator.steward, status="completed", terminal_receipt_ref="crp://receipts/steward")
    permits = (DispatchPermit("permit-1", "project-alpha", "plan-organization-001", 1, "assignment-1", "operation-plan"),)
    runs = _Runs(coordinator); dispatch = _Dispatch(replace(_plan(mode="cluster", assignments=("assignment-1",)), status="dispatching"), permits)
    runtime = _runtime(coordinator, dispatch, runs, freshness_gate=lambda _project_id: False)

    runtime.on_terminal(coordinator.steward.turn_id)

    assert dispatch.plan.status == "dispatched"
    assert coordinator.permits == ["permit-1"]


def test_terminal_expert_converges_dispatched_plan_from_parent_fan_in() -> None:
    coordinator = _Coordinator()
    coordinator.steward = replace(
        coordinator.steward, status="completed",
        terminal_receipt_ref="crp://receipts/steward",
    )
    expert = _run(
        run_id="expert-run", turn_id="turn-expert-organization-001",
        profile_id="subagent.worker", role="subagent", status="completed",
        parent="main-run",
    )
    runs = _Runs(coordinator)
    runs.by_id[expert.run_id] = expert
    dispatch = _Dispatch(replace(_plan(mode="cluster", assignments=("assignment-1",)), status="dispatched"))
    fan_in_id = "organization-fan-in-" + uuid5(NAMESPACE_URL, dispatch.plan.plan_id).hex
    runs.fan_in_results[fan_in_id] = SimpleNamespace(
        fan_in_id=fan_in_id, project_id="project-alpha",
        parent_run_id="main-run", status="completed",
    )
    runtime = _runtime(coordinator, dispatch, runs)
    runtime.on_terminal(expert.turn_id)
    assert dispatch.plan.status == "completed"
    assert dispatch.transitions == ["completed"]


def test_recovery_replays_active_steward_before_main_then_progresses_ready_plan() -> None:
    coordinator = _Coordinator()
    runs = _Runs(coordinator)
    dispatch = _Dispatch(_plan(mode="main_only"))
    runtime = _runtime(
        coordinator, dispatch, runs,
        start_pairs=((coordinator.main, coordinator.steward),),
        recovery_plans=(dispatch.plan,),
    )

    result = runtime.recover()

    assert coordinator.replayed == [coordinator.steward.turn_id, coordinator.main.turn_id]
    assert result["replayed_turn_ids"] == tuple(coordinator.replayed)
    # The steward is still active, so its unpublished decision cannot be
    # progressed merely because a scanner supplied a ready plan.
    assert result["plans"] == () and dispatch.transitions == []


def test_recovery_progresses_dispatched_cluster_without_resubmitting_terminal_steward() -> None:
    coordinator = _Coordinator()
    coordinator.steward = replace(coordinator.steward, status="completed", terminal_receipt_ref="crp://receipts/steward")
    runs = _Runs(coordinator)
    dispatch = _Dispatch(replace(_plan(mode="cluster", assignments=("assignment-1",)), status="dispatched"))
    fan_in_id = "organization-fan-in-" + uuid5(NAMESPACE_URL, dispatch.plan.plan_id).hex
    runs.fan_in_results[fan_in_id] = SimpleNamespace(fan_in_id=fan_in_id, project_id="project-alpha", parent_run_id="main-run", status="completed")
    runtime = _runtime(
        coordinator, dispatch, runs,
        start_pairs=((coordinator.main, coordinator.steward),),
        recovery_plans=(dispatch.plan,),
    )

    result = runtime.recover()

    assert coordinator.replayed == [coordinator.main.turn_id]
    assert dispatch.plan.status == "completed"
    assert result["plans"] == ({"plan_id": dispatch.plan.plan_id, "status": "completed", "mode": "cluster", "revision": 2},)
