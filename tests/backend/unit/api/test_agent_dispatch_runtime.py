from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.agent_dispatch_runtime import AgentDispatchRuntime, AgentDispatchRuntimeError
from core.ai_kernel.agent_contracts import AgentBudget, AgentChildLink, AgentBudgetReservation, AgentRun
from core.ai_kernel.agent_dispatch_contracts import AgentDispatchPlan


ROOT = Path(__file__).resolve().parents[4]


class _Payloads:
    def __init__(self) -> None: self.items: list[dict[str, object]] = []
    def __call__(self, turn_id, kind, payload):
        self.items.append({"turn_id": turn_id, "kind": kind, "payload": dict(payload)})
        return f"crp://session/{turn_id}/{kind}"


class _Topology:
    def __init__(self, *, runs=(), links=(), reservations=()) -> None:
        self.runs, self.links, self.reservations = tuple(runs), tuple(links), tuple(reservations)
    def list_runs(self, *, project_id, parent_run_id=None):
        return tuple(item for item in self.runs if item.project_id == project_id and (parent_run_id is None or item.parent_run_id == parent_run_id))
    def list_child_links(self, *, project_id, parent_run_id=None):
        return tuple(item for item in self.links if item.parent_project_id == project_id and (parent_run_id is None or item.parent_run_id == parent_run_id))
    def list_reservations(self, *, project_id, parent_run_id=None):
        return tuple(item for item in self.reservations if item.project_id == project_id and (parent_run_id is None or item.parent_run_id == parent_run_id))


class _Profiles:
    def __init__(self, *profiles): self.items = {item.profile_id: item for item in profiles}
    def get(self, profile_id): return self.items.get(profile_id)


class _DispatchStore:
    def __init__(self) -> None: self.values, self.plans, self.permits = {}, {}, {}
    def _put(self, value, operation_id):
        prior = self.values.get((type(value).__name__, getattr(value, "receipt_id", getattr(value, "snapshot_id", getattr(value, "cluster_id", getattr(value, "assignment_id", ""))))))
        if prior is None: self.values[(type(value).__name__, getattr(value, "receipt_id", getattr(value, "snapshot_id", getattr(value, "cluster_id", getattr(value, "assignment_id", "")))))] = value; return value, True
        assert prior == value; return value, False
    def put_intake_receipt(self, value, *, operation_id): return self._put(value, operation_id)
    def put_workload_snapshot(self, value, *, operation_id): return self._put(value, operation_id)
    def put_capacity_snapshot(self, value, *, operation_id): return self._put(value, operation_id)
    def put_expert_cluster(self, value, *, operation_id): return self._put(value, operation_id)
    def put_assignment(self, value, *, operation_id): return self._put(value, operation_id)
    def create_plan(self, value, *, operation_id):
        prior = self.plans.get(value.plan_id)
        if prior is None: self.plans[value.plan_id] = value; return value, True
        assert prior == value or prior.status == "ready"; return prior, False
    def transition_plan(self, value, *, expected_revision, operation_id):
        assert self.plans[value.plan_id].revision == expected_revision; self.plans[value.plan_id] = value; return value
    def issue_permit(self, value):
        created = value.permit_id not in self.permits
        self.permits.setdefault(value.permit_id, value)
        return self.permits[value.permit_id], created
    def publish_ready_plan(self, draft, *, cluster, assignments, permits, operation_id):
        prior = self.plans.get(draft.plan_id)
        if prior is not None:
            assert prior.status == "ready"
            return prior, tuple(self.permits[item.permit_id] for item in permits)
        if cluster is not None: self.put_expert_cluster(cluster, operation_id=f"{operation_id}.cluster")
        for assignment in assignments: self.put_assignment(assignment, operation_id=f"{operation_id}.assignment.{assignment.assignment_id}")
        ready = replace(draft, revision=draft.revision + 1, status="ready")
        self.plans[ready.plan_id] = ready
        for permit in permits: self.issue_permit(permit)
        return ready, permits


def _budget(n=8): return AgentBudget(n, n, n * 100, n * 50, n * 1000)
def _profile(profile_id, *, revision=1, capabilities=("memory.recall",), budget=None):
    return SimpleNamespace(profile_id=profile_id, revision=revision, enabled=True, role="subagent", capability_ids=capabilities, budget_limit=budget or _budget())
def _main(): return AgentRun("main-run", "turn-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "project-a", "main.orchestrator", 1, "main", "deep", "queued", 0, 0, _budget(), ("memory.recall", "agent.spawn"), 3, 2, 8, 8000, True, None, None, None, "crp://agent/main/budget", None, None)
def _steward(*, profile_id="steward.scheduler", revision=1, project_id="project-a", parent_run_id="main-run"):
    # The Stage 3 profile identity is introduced outside this bounded runtime
    # file.  A narrow read-model is sufficient here and avoids manufacturing a
    # second Profile authority in the service under test.
    return SimpleNamespace(run_id="steward-run", project_id=project_id, profile_id=profile_id,
                           profile_revision=revision, role="subagent", parent_run_id=parent_run_id)
def _link(): return AgentChildLink("steward-link", "main-run", "steward-run", "project-a", "project-a", "spawn-steward", 0, 1, _budget(4), ("memory.recall",), "spawned")
def _request():
    request = json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))
    request["scope"]["project_id"] = "project-a"
    return request
def _runtime(topology=None, profiles=None, store=None, payloads=None):
    return AgentDispatchRuntime(payload_writer=payloads or _Payloads(), topology=topology or _Topology(links=(_link(),)), profiles=profiles or _Profiles(_profile("steward.scheduler"), _profile("subagent.explorer")), dispatch_store=store or _DispatchStore(), expert_resolver=lambda _p, _a, _k: ("crp://dispatch/expert/frozen", 1), skill_resolver=lambda _p, _a, _k: ("crp://dispatch/skill/frozen", 1))


def test_intake_is_deterministic_and_never_freezes_raw_input() -> None:
    payloads = _Payloads(); runtime = _runtime(payloads=payloads); request = _request(); request["input"]["text"] = "private raw input must not be persisted"
    result = runtime.route_intake(request)
    assert result.receipt.route == "steward_required"
    serialized = json.dumps(payloads.items, ensure_ascii=False)
    assert "private raw input" not in serialized and "text_length" in serialized
    assert runtime.route_intake(request).receipt == result.receipt


def test_load_is_derived_from_real_links_and_reservations() -> None:
    reservation = AgentBudgetReservation("r1", "project-a", "main-run", "child", "spawn", 0, _budget(2), None, "reserved")
    child = AgentChildLink("c1", "main-run", "child", "project-a", "project-a", "spawn", 0, 1, _budget(2), ("memory.recall",), "started")
    runtime = _runtime(topology=_Topology(links=(_link(), child), reservations=(reservation,)))
    load = runtime.snapshot_load(_main(), operation_id="load-op")
    assert load.workload.queued_assignments == 0 and load.workload.active_assignments == 2
    assert load.capacity.available_slots == 1 and load.capacity.remaining_budget == _budget().remaining_after(_budget(2))


@pytest.mark.parametrize("main,steward,profiles", [
    (_main(), _steward(profile_id="subagent.explorer"), _Profiles(_profile("steward.scheduler"), _profile("subagent.explorer"))),
    (replace(_main(), project_id="project-b"), _steward(), _Profiles(_profile("steward.scheduler"), _profile("subagent.explorer"))),
    (_main(), _steward(revision=2), _Profiles(_profile("steward.scheduler"), _profile("subagent.explorer"))),
])
def test_steward_identity_and_scope_are_fail_closed(main, steward, profiles) -> None:
    runtime = _runtime(profiles=profiles); intake = runtime.route_intake(_request()); load = runtime.snapshot_load(main, operation_id="load")
    with pytest.raises(AgentDispatchRuntimeError): runtime.publish_steward_plan(main_run=main, steward_run=steward, intake=intake, load=load, proposal={"mode": "main_only", "plan_id": "plan-main"}, operation_id="plan")


def test_main_only_has_no_permits_and_cluster_freezes_only_resolved_refs() -> None:
    payloads, store = _Payloads(), _DispatchStore()
    runtime = _runtime(payloads=payloads, store=store); intake = runtime.route_intake(_request()); load = runtime.snapshot_load(_main(), operation_id="load")
    with pytest.raises(AgentDispatchRuntimeError, match="frozen intake route result"):
        runtime.publish_steward_plan(main_run=_main(), steward_run=_steward(), intake=intake.receipt, load=load, proposal={"mode": "main_only", "plan_id": "wrong-intake"}, operation_id="wrong-intake")
    main_only = runtime.publish_steward_plan(main_run=_main(), steward_run=_steward(), intake=intake, load=load, proposal={"mode": "main_only", "plan_id": "plan-main"}, operation_id="main-plan")
    assert main_only.plan.status == "ready" and main_only.permits == ()
    proposal = {"mode": "cluster", "plan_id": "plan-cluster", "cluster_id": "cluster-1", "assignments": [{"assignment_id": "assignment-1", "profile_id": "subagent.explorer", "profile_revision": 1, "task": "private task only in immutable payload authority", "budget": {"model_calls": 1, "tool_calls": 1, "input_tokens": 10, "output_tokens": 10, "wall_time_ms": 10}, "capability_ids": ["memory.recall"], "expert": {"expert_id": "researcher", "task_intents": ["review"], "budget": "small"}, "skill": {"skill_ids": ["evidence-read"]}}]}
    cluster = runtime.publish_steward_plan(main_run=_main(), steward_run=_steward(), intake=intake, load=load, proposal=proposal, operation_id="cluster-plan")
    assert runtime.publish_steward_plan(main_run=_main(), steward_run=_steward(), intake=intake, load=load, proposal=proposal, operation_id="cluster-plan") == cluster
    assert cluster.plan.status == "ready" and len(cluster.permits) == 1
    assert cluster.assignments[0].expert_snapshot_ref == "crp://dispatch/expert/frozen"
    assert cluster.assignments[0].expert_id == "researcher"
    assert cluster.assignments[0].skill_ids == ("evidence-read",)
    assert cluster.plan.main_run_id == "main-run" and cluster.plan.steward_run_id == "steward-run"
    assert cluster.assignments[0].task_payload_ref.startswith("crp://") and cluster.assignments[0].capability_ids == ("memory.recall",)
    task_payload = next(item["payload"] for item in payloads.items if item["kind"].startswith("agent.dispatch.task."))
    assert task_payload["expert"] == proposal["assignments"][0]["expert"]
    assert "private task only in immutable payload authority" in json.dumps(payloads.items)
    assert "private task only in immutable payload authority" not in repr(store.values)


def test_cluster_rejects_profile_budget_and_capability_expansion() -> None:
    runtime = _runtime(); intake = runtime.route_intake(_request()); load = runtime.snapshot_load(_main(), operation_id="load")
    proposal = {"mode": "cluster", "plan_id": "plan-cluster", "cluster_id": "cluster-1", "assignments": [{"assignment_id": "assignment-1", "profile_id": "subagent.explorer", "profile_revision": 1, "task": "research", "budget": {"model_calls": 99, "tool_calls": 1, "input_tokens": 10, "output_tokens": 10, "wall_time_ms": 10}, "capability_ids": ["external.admin"], "expert": None, "skill": None}]}
    with pytest.raises(AgentDispatchRuntimeError): runtime.publish_steward_plan(main_run=_main(), steward_run=_steward(), intake=intake, load=load, proposal=proposal, operation_id="bad")


def test_proposal_nonces_are_scoped_to_project_and_parent_runs() -> None:
    store = _DispatchStore()
    proposal = {"mode": "cluster", "plan_id": "shared-plan", "cluster_id": "shared-cluster", "assignments": [{"assignment_id": "shared-assignment", "profile_id": "subagent.explorer", "profile_revision": 1, "task": "research", "budget": {"model_calls": 1, "tool_calls": 1, "input_tokens": 10, "output_tokens": 10, "wall_time_ms": 10}, "capability_ids": ["memory.recall"], "expert": {"expert_id": "researcher"}, "skill": {"skill_ids": ["evidence-read"]}}]}
    first_runtime = _runtime(store=store); first_intake = first_runtime.route_intake(_request()); first_load = first_runtime.snapshot_load(_main(), operation_id="shared-operation")
    first = first_runtime.publish_steward_plan(main_run=_main(), steward_run=_steward(), intake=first_intake, load=first_load, proposal=proposal, operation_id="shared-operation")
    main_b = replace(_main(), run_id="main-run-b", project_id="project-b")
    steward_b = _steward(project_id="project-b", parent_run_id="main-run-b")
    link_b = AgentChildLink("steward-link-b", "main-run-b", "steward-run", "project-b", "project-b", "spawn-steward", 0, 1, _budget(4), ("memory.recall",), "spawned")
    second_runtime = _runtime(store=store, topology=_Topology(links=(link_b,)))
    request_b = _request(); request_b["scope"]["project_id"] = "project-b"; request_b["turn_id"] = "turn-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    second_intake = second_runtime.route_intake(request_b); second_load = second_runtime.snapshot_load(main_b, operation_id="shared-operation")
    second = second_runtime.publish_steward_plan(main_run=main_b, steward_run=steward_b, intake=second_intake, load=second_load, proposal=proposal, operation_id="shared-operation")
    assert first.plan.plan_id != second.plan.plan_id
    assert first.cluster is not None and second.cluster is not None and first.cluster.cluster_id != second.cluster.cluster_id
    assert first.assignments[0].assignment_id != second.assignments[0].assignment_id
