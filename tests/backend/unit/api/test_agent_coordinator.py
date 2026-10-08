from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.agent_capabilities import AgentCapabilityProvider
from backend.api.agent_coordinator import AgentCoordinator, SpawnRequest, _child_run, _observed_usage
from core.ai_kernel import (
    AgentBudget,
    AgentProfile,
    AgentProfileRegistry,
    SQLiteAgentStore,
    TurnReceipt,
)
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.ai_kernel.agent_dispatch_contracts import AgentDispatchPlan, DispatchPermit, ExpertAssignment


ROOT = Path(__file__).resolve().parents[4]


class _Profiles:
    def __init__(self, *profiles: AgentProfile) -> None:
        self.items = {item.profile_id: item for item in profiles}

    def get(self, profile_id: str) -> AgentProfile | None:
        return self.items.get(profile_id)


class _Runtime:
    def __init__(self) -> None:
        self.accepted: list[dict[str, object]] = []

    def accept_turn(self, request):
        self.accepted.append(dict(request))
        return TurnReceipt(str(request["turn_id"]), str(request["session_id"]), str(request["operation_id"]), "accepted", 1, False)

    def receipt_for(self, turn_id, *, replayed=False):
        return TurnReceipt(turn_id, "session", "operation", "accepted", 1, replayed)


class _Runner:
    def __init__(self) -> None:
        self.submitted: list[dict[str, object]] = []
        self.cancel_result = True

    def accept_and_submit(self, request):
        self.submitted.append(dict(request))
        return TurnReceipt(str(request["turn_id"]), str(request["session_id"]), str(request["operation_id"]), "accepted", 1, False)

    def request_turn_cancel(self, turn_id, *, reason):
        return self.cancel_result

    def wait_for_terminal(self, turn_id, *, timeout_seconds=None, poll_interval_seconds=0.05):
        return TurnReceipt(turn_id, "session", "operation", "completed", 2, False)

    def terminal_receipt(self, turn_id):
        return None


class _Dispatch:
    def __init__(self): self.calls = []
    def route_intake(self, request): self.calls.append(("route", dict(request))); return "intake"
    def snapshot_load(self, main_run, *, operation_id): self.calls.append(("load", main_run.run_id, operation_id)); return "load"
    def publish_steward_plan(self, **kwargs):
        self.calls.append(("publish", kwargs)); return SimpleNamespace(plan=SimpleNamespace(plan_id=kwargs["proposal"]["plan_id"]), permits=())


class _PolicySnapshots:
    def __init__(self, *, revision: int = 1, policy=None) -> None:
        self.revision = revision
        self.policy = policy or _policy()
        self.frozen: dict[tuple[str, str], SimpleNamespace] = {}
        self.freeze_calls: list[tuple[str, str]] = []

    def freeze_for_turn(self, *, project_id, turn_id):
        self.freeze_calls.append((project_id, turn_id))
        snapshot = self.frozen.get((project_id, turn_id))
        if snapshot is None:
            snapshot = SimpleNamespace(policy_id="workbench.default", selected_revision=self.revision)
            self.frozen[(project_id, turn_id)] = snapshot
        return snapshot

    def get_revision(self, policy_id, revision):
        assert policy_id == "workbench.default" and revision >= 1
        return self.policy

    def load_turn_snapshot(self, policy_id, *, project_id, turn_id):
        assert policy_id == "workbench.default"
        return self.frozen.get((project_id, turn_id))


def _policy(*, scheduler=None, context=None):
    scheduler = scheduler or SimpleNamespace(cluster_mode="expert_cluster", max_assignments=2, parallelism_cap=2, prefer_main_only=False, allowed_expert_ids=("research",), allowed_skill_ids=("project-skill",))
    context = context or SimpleNamespace(include_project_skill=True, include_memory=True, include_session_history=True, max_context_bytes=32_000)
    routing = SimpleNamespace(profile_ids=("main.orchestrator", "steward.scheduler", "subagent.worker", "subagent.explorer"))
    return SimpleNamespace(
        target_roles=("main", "subagent"),
        routing=routing,
        scheduler=scheduler,
        context=context,
        to_payload=lambda: {
            "target_roles": ["main", "subagent"],
            "routing": {"profile_ids": list(routing.profile_ids)},
            "scheduler": {
                "cluster_mode": scheduler.cluster_mode,
                "max_assignments": scheduler.max_assignments,
                "parallelism_cap": scheduler.parallelism_cap,
                "prefer_main_only": scheduler.prefer_main_only,
                "allowed_expert_ids": list(scheduler.allowed_expert_ids),
                "allowed_skill_ids": list(scheduler.allowed_skill_ids),
            },
            "context": {
                "include_project_skill": context.include_project_skill,
                "include_memory": context.include_memory,
                "include_session_history": context.include_session_history,
                "max_context_bytes": context.max_context_bytes,
            },
        },
    )


class _PermitStore:
    def __init__(self, *, plan, permit, assignment) -> None:
        self.plan, self.permit, self.assignment = plan, permit, assignment
        self.bound: list[tuple[str, str, str]] = []

    def get_plan(self, plan_id, *, project_id):
        return self.plan if self.plan.plan_id == plan_id and self.plan.project_id == project_id else None

    def get_assignment_for_permit(self, permit_id, *, project_id):
        if self.permit.permit_id != permit_id or self.permit.project_id != project_id:
            return None
        return self.permit, self.assignment

    def claim_permit_assignment(self, permit_id, *, project_id, operation_id):
        assert permit_id == self.permit.permit_id and project_id == self.permit.project_id
        self.permit = replace(self.permit, status="consumed")
        return self.permit, self.assignment

    def bind_permit_to_child_run(self, permit_id, *, project_id, child_run_id, operation_id):
        assert permit_id == self.permit.permit_id and project_id == self.permit.project_id
        if self.bound:
            assert self.bound[0][1] == child_run_id
        else:
            self.bound.append((operation_id, child_run_id, permit_id))
            self.permit = replace(self.permit, status="consumed")
        return self.permit, child_run_id


class _PlanProjectionStore:
    def __init__(self, *plans) -> None: self.plans = plans

    def list_plans_for_main(self, *, project_id, main_run_id):
        return tuple(
            plan for plan in self.plans
            if plan.project_id == project_id and plan.main_run_id == main_run_id
        )


class _MessagePayloadAuthority:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, str]] = []

    def copy_for_recipient(self, **kwargs) -> str:
        if self.fail:
            raise ValueError("payload copy rejected")
        self.calls.append(dict(kwargs))
        return f"crp://recipient-turn/messages/{kwargs['message_id']}"


class _DurableRuntime(_Runtime):
    def __init__(self, turns: SQLiteAITurnStore) -> None:
        super().__init__()
        self._turns = turns

    def accept_turn(self, request):
        turn_id, created = self._turns.claim_turn(request)
        self.accepted.append(dict(request))
        return TurnReceipt(
            turn_id, str(request["session_id"]), str(request["operation_id"]),
            "accepted", 1, not created,
        )


class _Store:
    def __init__(self) -> None:
        self.runs = {}
        self.links = {}
        self.reservations = {}

    def register_run(self, run, *, operation_id):
        self.runs[run.run_id] = (run, 1)
        return run

    def get_run_by_turn_id(self, turn_id, *, project_id):
        return next((value for run_id, value in self.runs.items() if value[0].turn_id == turn_id and value[0].project_id == project_id), None)

    def get_run_with_revision(self, run_id, *, project_id):
        item = self.runs.get(run_id)
        return item if item is not None and item[0].project_id == project_id else None

    def reserve_spawn(self, *, parent, child, link, reservation):
        self.runs[child.run_id] = (child, 1)
        self.links[link.link_id] = link
        self.reservations[reservation.reservation_id] = reservation
        return child, link, reservation, True

    def finalize_spawn(self, link_id, *, operation_id, expected_cancel_epoch):
        self.links[link_id] = replace(self.links[link_id], status="spawned")
        return self.links[link_id]

    def abort_reserved_spawn(self, *args, **kwargs):
        raise AssertionError("unexpected abort")

    def get_child_link(self, link_id, *, project_id):
        link = self.links.get(link_id)
        return link if link is not None and link.parent_project_id == project_id else None

    def get_reservation(self, reservation_id, *, project_id):
        value = self.reservations.get(reservation_id)
        return value if value is not None and value.project_id == project_id else None

    def list_child_links(self, *, project_id, parent_run_id=None):
        return tuple(link for link in self.links.values() if link.parent_project_id == project_id and (parent_run_id is None or link.parent_run_id == parent_run_id))


def _profile(profile_id: str, role: str, *, budget: int, capabilities: tuple[str, ...], children: bool) -> AgentProfile:
    return AgentProfile(profile_id, 1, profile_id, True, role, "standard", AgentBudget(budget, budget, budget, budget, budget), capabilities, 2 if children else 0, 2, 8, 8_000, children)


def _request() -> dict[str, object]:
    return json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))


def _durable_coordinator(tmp_path: Path):
    turns = SQLiteAITurnStore(tmp_path / "ai-turns.sqlite3")
    store = SQLiteAgentStore(tmp_path / "ai-turns.sqlite3")
    runtime, runner = _DurableRuntime(turns), _Runner()
    profiles = AgentProfileRegistry(store)
    authority = _MessagePayloadAuthority()
    coordinator = AgentCoordinator(
        runtime=runtime,
        runner=runner,
        store=store,
        profiles=profiles,
        request_loader=lambda turn_id: turns.get_request(turn_id) or {},
        events_loader=turns.events_after,
        payload_loader=turns.get,
        immutable_payload_writer=lambda turn_id, kind, payload: (
            turns.get_or_create_immutable_payload(turn_id, kind, payload)
        ),
        message_payload_authority=authority,
    )
    return turns, store, runtime, runner, profiles, coordinator


def _converge_child_for_fan_in(
    store: SQLiteAgentStore, project_id: str, child_run_id: str, status: str,
) -> None:
    found = store.get_run_with_revision(child_run_id, project_id=project_id)
    assert found is not None
    child, _ = found
    terminal = replace(
        child,
        status=status,
        model_routing_snapshot_ref="crp://fan-in/model-route",
        capability_manifest_ref="crp://fan-in/capability-manifest",
        context_manifest_ref="crp://fan-in/context-manifest",
        terminal_receipt_ref=f"crp://fan-in/terminal-receipts/{child.run_id}",
    )
    store.converge_terminal_child(
        terminal, usage=AgentBudget(0, 0, 0, 0, 0),
        operation_id=f"test-terminal-converge-{child.run_id}",
    )


def test_main_acceptance_records_a_main_run_before_submission() -> None:
    main = _profile("main.orchestrator", "main", budget=8, capabilities=("memory.recall", "agent.spawn"), children=True)
    runtime, runner, store = _Runtime(), _Runner(), _Store()
    request = _request()
    coordinator = AgentCoordinator(runtime=runtime, runner=runner, store=store, profiles=_Profiles(main), request_loader=lambda _turn_id: request)

    run = coordinator.accept_main_and_submit(request)

    assert run.role == "main"
    assert runtime.accepted == runner.submitted
    assert runtime.accepted[0]["agent_binding"]["role"] == "main"
    assert runtime.accepted[0]["agent_binding"]["model_tier"] == main.model_tier
    assert runtime.accepted[0]["capability_policy"] == {
        "allowed": ["memory.recall"],
        "denied": ["answer.generate", "external.web_search", "project_skill.read"],
        "require_approval": [],
    }
    assert run.capability_ids == ("memory.recall",)
    assert store.runs[run.run_id][0] == run


def test_registered_frozen_agent_turn_can_be_resubmitted_without_reaccepting(tmp_path: Path) -> None:
    turns, _store, runtime, runner, _profiles, coordinator = _durable_coordinator(tmp_path)
    prepared = coordinator.accept_and_register_main(_request())

    receipt = coordinator.resubmit_existing_turn(
        turn_id=prepared.run.turn_id, project_id=prepared.run.project_id,
    )

    assert receipt.turn_id == prepared.run.turn_id
    assert len(runtime.accepted) == 1
    assert runner.submitted == [prepared.request]
    persisted = turns.get_request(prepared.run.turn_id)
    assert persisted is not None and persisted["agent_binding"] == prepared.request["agent_binding"]


def test_profile_route_binding_is_frozen_into_run_and_replay_request() -> None:
    main = replace(
        _profile("main.orchestrator", "main", budget=8, capabilities=("memory.recall",), children=True),
        model_route_key="synthetic.route", model_route_revision=4,
    )
    runtime, runner, store = _Runtime(), _Runner(), _Store()
    profiles = _Profiles(main)
    coordinator = AgentCoordinator(
        runtime=runtime, runner=runner, store=store, profiles=profiles,
        request_loader=lambda _turn_id: runtime.accepted[0],
    )
    run = coordinator.accept_main_and_submit(_request())
    assert runtime.accepted[0]["agent_binding"]["model_route_key"] == "synthetic.route"
    profiles.items[main.profile_id] = replace(
        main, revision=2, model_route_key="synthetic.route.next", model_route_revision=1,
    )
    coordinator.resubmit_existing_turn(turn_id=run.turn_id, project_id=run.project_id)
    assert runner.submitted[-1]["agent_binding"]["model_route_key"] == "synthetic.route"
    assert store.runs[run.run_id][0].model_route_revision == 4


def test_prepared_main_and_child_are_not_submitted_until_explicitly_dispatched() -> None:
    main = _profile("main.orchestrator", "main", budget=8, capabilities=("memory.recall", "agent.spawn"), children=True)
    worker = _profile("subagent.worker", "subagent", budget=4, capabilities=("memory.recall",), children=False)
    runtime, runner, store = _Runtime(), _Runner(), _Store()
    request = _request(); request["capability_policy"]["allowed"].append("agent.spawn")
    coordinator = AgentCoordinator(runtime=runtime, runner=runner, store=store, profiles=_Profiles(main, worker), request_loader=lambda turn_id: next(item for item in runtime.accepted if item["turn_id"] == turn_id))
    prepared_main = coordinator.accept_and_register_main(request)
    assert runtime.accepted and runner.submitted == []
    coordinator.submit_accepted_turn(prepared_main)
    prepared_child = coordinator.prepare_child(
        parent_turn_id=prepared_main.run.turn_id, operation_id="op-agent-prepared-child-0001", tool_call_id="tool-prepared-child-0001",
        project_id=prepared_main.run.project_id, scope=request["scope"], privacy=request["privacy"],
        arguments={"profile_id": "subagent.worker", "task": "inspect"},
    )
    assert prepared_child.run.turn_id in {item["turn_id"] for item in runtime.accepted}
    assert len(runner.submitted) == 1
    coordinator.submit_accepted_turn(prepared_child)
    assert len(runner.submitted) == 2


def test_steward_plan_requires_direct_frozen_steward_and_forwards_main_only_and_cluster() -> None:
    runtime, runner, store, dispatch = _Runtime(), _Runner(), _Store(), _Dispatch()
    main = _profile("main.orchestrator", "main", budget=8, capabilities=("agent.spawn", "agent.plan"), children=True)
    steward = _profile("steward.scheduler", "subagent", budget=4, capabilities=("agent.plan",), children=False)
    request = _request(); request["capability_policy"]["allowed"].extend(["agent.spawn", "agent.plan"])
    coordinator = AgentCoordinator(runtime=runtime, runner=runner, store=store, profiles=_Profiles(main, steward), request_loader=lambda turn_id: next(item for item in runtime.accepted if item["turn_id"] == turn_id), dispatch_runtime=dispatch)
    parent = coordinator.accept_main_and_submit(request)
    child = coordinator.spawn(parent_turn_id=parent.turn_id, operation_id="op-agent-steward-0001", tool_call_id="tool-steward-0001", project_id=parent.project_id, scope=request["scope"], privacy=request["privacy"], arguments={"profile_id": "steward.scheduler", "task": "schedule"})
    child_run = store.runs[str(child["run"]["run_id"])][0]
    child_request = next(item for item in runtime.accepted if item["turn_id"] == child_run.turn_id)
    main_only = coordinator.plan(parent_turn_id=child_run.turn_id, operation_id="op-agent-plan-main-0001", project_id=parent.project_id, scope=child_request["scope"], privacy=child_request["privacy"], arguments={"mode": "main_only", "plan_id": "plan-main"})
    cluster = coordinator.plan(parent_turn_id=child_run.turn_id, operation_id="op-agent-plan-cluster-0001", project_id=parent.project_id, scope=child_request["scope"], privacy=child_request["privacy"], arguments={"mode": "cluster", "plan_id": "plan-cluster", "cluster_id": "cluster-1", "assignments": []})
    assert main_only["plan_id"] == "plan-main" and cluster["plan_id"] == "plan-cluster"
    assert "agent_binding" not in dispatch.calls[0][1]


def test_permit_materializes_a_frozen_prepared_child_without_submission() -> None:
    runtime, runner, store = _Runtime(), _Runner(), _Store()
    main = _profile("main.orchestrator", "main", budget=8, capabilities=("agent.spawn", "agent.plan", "memory.recall"), children=True)
    steward = _profile("steward.scheduler", "subagent", budget=4, capabilities=("agent.plan",), children=False)
    worker = _profile("subagent.worker", "subagent", budget=4, capabilities=("memory.recall",), children=False)
    request = _request(); request["capability_policy"]["allowed"].extend(["agent.spawn", "agent.plan"])
    task_payload = {"schema_version": "1.0.0", "assignment_id": "assignment-permit-001", "task": "Recall the frozen evidence", "expert": {"expert_id": "expert-permit-001", "task_intents": ["research"], "budget": "small"}}
    coordinator = AgentCoordinator(runtime=runtime, runner=runner, store=store, profiles=_Profiles(main, steward, worker), request_loader=lambda turn_id: next(item for item in runtime.accepted if item["turn_id"] == turn_id), payload_loader=lambda _ref: task_payload)
    main_run = coordinator.accept_main_and_submit(request)
    steward_result = coordinator.spawn(parent_turn_id=main_run.turn_id, operation_id="op-permit-steward-0001", tool_call_id="tool-permit-steward-0001", project_id=main_run.project_id, scope=request["scope"], privacy=request["privacy"], arguments={"profile_id": "steward.scheduler", "task": "schedule"})
    steward_run = store.runs[str(steward_result["run"]["run_id"])][0]
    assignment = ExpertAssignment(
        "assignment-permit-001", main_run.project_id, "cluster-permit-001", 1,
        "crp://agent/profiles/subagent.worker", 1, "crp://tasks/assignment-permit-001", 1,
        ("memory.recall",), "expert-permit-001", ("research",),
        "crp://experts/expert-permit-001", 1, "crp://skills/research", 1,
        "crp://policies/project-001", 1, AgentBudget(2, 2, 2, 2, 2),
    )
    plan = AgentDispatchPlan(
        "plan-permit-001", main_run.project_id, main_run.run_id, steward_run.run_id,
        1, "ready", "cluster", "crp://intakes/permit-001", 1,
        "crp://workloads/permit-001", 1, "crp://capacity/permit-001", 1,
        "crp://clusters/permit-001", 1, (assignment.assignment_id,), AgentBudget(4, 4, 4, 4, 4), 1,
    )
    permit = DispatchPermit("permit-001", main_run.project_id, plan.plan_id, plan.revision, assignment.assignment_id, "op-plan-permit-001")
    permits = _PermitStore(plan=plan, permit=permit, assignment=assignment)
    coordinator._permit_store = permits
    # Production terminal observation converges the steward before the
    # organization runtime materializes its frozen permits.
    store.runs[steward_run.run_id] = (
        replace(
            steward_run, status="completed",
            model_routing_snapshot_ref="crp://snapshots/steward/model",
            capability_manifest_ref="crp://snapshots/steward/capabilities",
            context_manifest_ref="crp://snapshots/steward/context",
            terminal_receipt_ref="crp://receipts/steward/completed",
        ),
        2,
    )
    steward_link = next(
        link for link in store.links.values()
        if link.child_run_id == steward_run.run_id
    )
    store.links[steward_link.link_id] = replace(steward_link, status="completed")

    prepared = coordinator.prepare_child_from_permit(project_id=main_run.project_id, permit_id=permit.permit_id, operation_id="op-permit-child-001")

    assert len(runner.submitted) == 2
    assert permits.bound == [("op-permit-child-001", prepared.run.run_id, permit.permit_id)]
    assert prepared.run.capability_ids == assignment.capability_ids
    assert prepared.run.budget_limit == assignment.delegated_budget
    assert prepared.request["input"] == {"kind": "text", "text": "Recall the frozen evidence", "refs": []}
    assert prepared.request["expert_request"] == {"expert_id": assignment.expert_id, "task_intents": ["research"], "budget": "small", "skill_ids": ["research"]}
    assert "capability_request" not in prepared.request
    assert runtime.accepted[-1]["input"] == prepared.request["input"]
    assert runtime.accepted[-1]["expert_request"] == prepared.request["expert_request"]
    coordinator.submit_accepted_turn(prepared)
    assert len(runner.submitted) == 3


def test_permit_rejects_profile_revision_drift_before_binding() -> None:
    runtime, runner, store = _Runtime(), _Runner(), _Store()
    main = _profile("main.orchestrator", "main", budget=8, capabilities=("agent.spawn", "agent.plan", "memory.recall"), children=True)
    steward = _profile("steward.scheduler", "subagent", budget=4, capabilities=("agent.plan",), children=False)
    worker = _profile("subagent.worker", "subagent", budget=4, capabilities=("memory.recall",), children=False)
    request = _request(); request["capability_policy"]["allowed"].extend(["agent.spawn", "agent.plan"])
    coordinator = AgentCoordinator(runtime=runtime, runner=runner, store=store, profiles=_Profiles(main, steward, worker), request_loader=lambda turn_id: next(item for item in runtime.accepted if item["turn_id"] == turn_id), payload_loader=lambda _ref: {"schema_version": "1.0.0", "assignment_id": "assignment-drift-001", "task": "review", "expert": None})
    main_run = coordinator.accept_main_and_submit(request)
    steward_result = coordinator.spawn(parent_turn_id=main_run.turn_id, operation_id="op-permit-drift-steward-001", tool_call_id="tool-permit-drift-steward-001", project_id=main_run.project_id, scope=request["scope"], privacy=request["privacy"], arguments={"profile_id": "steward.scheduler", "task": "schedule"})
    steward_run = store.runs[str(steward_result["run"]["run_id"])][0]
    assignment = ExpertAssignment("assignment-drift-001", main_run.project_id, "cluster-drift-001", 1, "crp://agent/profiles/subagent.worker", 2, "crp://tasks/assignment-drift-001", 1, ("memory.recall",), None, (), None, None, None, None, "crp://policies/project-001", 1, AgentBudget(2, 2, 2, 2, 2))
    plan = AgentDispatchPlan("plan-drift-001", main_run.project_id, main_run.run_id, steward_run.run_id, 1, "ready", "cluster", "crp://intakes/drift-001", 1, "crp://workloads/drift-001", 1, "crp://capacity/drift-001", 1, "crp://clusters/drift-001", 1, (assignment.assignment_id,), AgentBudget(4, 4, 4, 4, 4), 1)
    permit = DispatchPermit("permit-drift-001", main_run.project_id, plan.plan_id, 1, assignment.assignment_id, "op-plan-drift-001")
    permits = _PermitStore(plan=plan, permit=permit, assignment=assignment)
    coordinator._permit_store = permits

    with pytest.raises(Exception, match="profile revision"):
        coordinator.prepare_child_from_permit(project_id=main_run.project_id, permit_id=permit.permit_id, operation_id="op-permit-drift-child-001")
    assert permits.bound == []


def test_child_terms_are_a_strict_intersection_of_parent_profile_and_request() -> None:
    main = _profile("main.orchestrator", "main", budget=8, capabilities=("memory.recall", "document.draft"), children=True)
    worker = _profile("subagent.worker", "subagent", budget=4, capabilities=("memory.recall", "network.fetch"), children=True)
    runtime, runner, store = _Runtime(), _Runner(), _Store()
    request = _request()
    coordinator = AgentCoordinator(runtime=runtime, runner=runner, store=store, profiles=_Profiles(main), request_loader=lambda _turn_id: request)
    parent = coordinator.accept_main_and_submit(request)
    child_request = SpawnRequest("spawn-001", "child-run-001", "child-turn-001", "link-001", "reservation-001", "subagent.worker", "child-session-001", "child-idempotency-001", "read only", ("memory.recall", "not.allowed"), AgentBudget(6, 6, 6, 6, 6), 9, 9_000, 8, 4, True)

    child = _child_run(parent, worker, child_request)

    assert child.capability_ids == ("memory.recall",)
    assert child.budget_limit == AgentBudget(4, 4, 4, 4, 4)
    assert child.max_steps == 8 and child.timeout_ms == 8_000
    assert child.max_concurrent_children == 2 and child.max_depth == 2


@pytest.mark.parametrize("status", ("completed", "failed", "cancelled"))
def test_terminal_receipt_writer_uses_authoritative_receipt_not_turn_result_payload(status: str) -> None:
    main = _profile("main.orchestrator", "main", budget=8, capabilities=("memory.recall",), children=True)
    runtime, runner, store = _Runtime(), _Runner(), _Store()
    captured: list[dict[str, object]] = []
    coordinator = AgentCoordinator(
        runtime=runtime, runner=runner, store=store, profiles=_Profiles(main),
        request_loader=lambda _turn_id: _request(),
        terminal_receipt_writer=lambda _turn_id, payload: captured.append(dict(payload)) or "crp://receipts/terminal/immutable-001",
    )
    receipt = TurnReceipt("turn-0123456789abcdef0123456789abcdef", "session-001", "op-001", status, 9, False)

    receipt_ref = coordinator._persist_terminal_receipt(receipt, status=status)

    assert receipt_ref == "crp://receipts/terminal/immutable-001"
    assert captured == [{
        "schema_version": "1.0.0", "kind": "agent.turn-terminal-receipt.v1",
        "turn_id": receipt.turn_id, "session_id": receipt.session_id,
        "operation_id": receipt.operation_id, "status": status,
        "sequence": 9, "replayed": False,
    }]


@pytest.mark.parametrize(
    ("policy", "statuses", "expected"),
    [
        ("all", ("completed", "completed"), "completed"),
        ("all", ("completed", "failed"), "failed"),
        ("any", ("failed", "completed"), "completed"),
        ("quorum", ("completed", "completed"), "completed"),
    ],
)
def test_terminal_children_automatically_converge_fan_in(
    tmp_path: Path, policy: str, statuses: tuple[str, str], expected: str,
) -> None:
    turns, store, _runtime, _runner, _profiles, coordinator = _durable_coordinator(tmp_path)
    request = _request()
    request["capability_policy"]["allowed"].extend([
        "agent.spawn", "agent.fan_in", "agent.list",
    ])
    parent = coordinator.accept_main_and_submit(request)
    children = [
        coordinator.spawn(
            parent_turn_id=parent.turn_id,
            operation_id=f"op-fan-in-spawn-{index:04d}",
            tool_call_id=f"tool-fan-in-spawn-{index:04d}",
            project_id=parent.project_id, scope=request["scope"], privacy=request["privacy"],
            arguments={"profile_id": "subagent.explorer", "task": f"inspect branch {index}"},
        )
        for index in (1, 2)
    ]
    child_run_ids = [str(item["run"]["run_id"]) for item in children]
    joined = coordinator.fan_in(
        parent_turn_id=parent.turn_id,
        operation_id=f"op-fan-in-{policy}-0001",
        tool_call_id=f"tool-fan-in-{policy}-0001",
        project_id=parent.project_id, scope=request["scope"], privacy=request["privacy"],
        arguments={
            "child_run_ids": child_run_ids, "policy": policy,
            **({"quorum": 2} if policy == "quorum" else {}),
        },
    )
    assert joined["status"] == "open"
    for child_run_id, status in zip(child_run_ids, statuses, strict=True):
        _converge_child_for_fan_in(store, parent.project_id, child_run_id, status)

    coordinator._reconcile_parent_fan_ins(parent)
    fan_in = store.get_fan_in(str(joined["fan_in_id"]), project_id=parent.project_id)
    result = store.get_fan_in_result(str(joined["fan_in_id"]), project_id=parent.project_id)

    assert fan_in is not None and fan_in.status == expected
    assert result is not None and result.status == expected
    assert all(item.summary_ref.startswith(f"crp://session/{parent.turn_id}/agent-child-terminal-summary-v1/") for item in result.child_summaries)
    listing = coordinator.list(
        parent_turn_id=parent.turn_id, project_id=parent.project_id,
        scope=request["scope"], privacy=request["privacy"],
        arguments={"include_messages": False},
    )
    assert listing["fan_ins"][0]["result"]["status"] == expected
    coordinator._reconcile_parent_fan_ins(parent)
    assert store.get_fan_in_result(str(joined["fan_in_id"]), project_id=parent.project_id) == result


def test_terminal_child_summary_is_parent_owned_when_message_copy_rejects(tmp_path: Path) -> None:
    turns, store, _runtime, _runner, _profiles, coordinator = _durable_coordinator(tmp_path)
    request = _request()
    request["capability_policy"]["allowed"].extend(["agent.spawn", "agent.fan_in"])
    parent = coordinator.accept_main_and_submit(request)
    spawned = coordinator.spawn(
        parent_turn_id=parent.turn_id, operation_id="op-sensitive-spawn-0001",
        tool_call_id="tool-sensitive-spawn-0001", project_id=parent.project_id,
        scope=request["scope"], privacy=request["privacy"],
        arguments={"profile_id": "subagent.explorer", "task": "inspect sensitive branch"},
    )
    child_run_id = str(spawned["run"]["run_id"])
    joined = coordinator.fan_in(
        parent_turn_id=parent.turn_id, operation_id="op-sensitive-fan-in-0001",
        tool_call_id="tool-sensitive-fan-in-0001", project_id=parent.project_id,
        scope=request["scope"], privacy=request["privacy"],
        arguments={"child_run_ids": [child_run_id], "policy": "all"},
    )
    _converge_child_for_fan_in(store, parent.project_id, child_run_id, "completed")
    coordinator._events_loader = lambda _turn_id: ({
        "type": "turn.completed", "data": {"summary": "检查已完成 sk-example123456"},
    },)
    coordinator._message_payload_authority = _MessagePayloadAuthority(fail=True)

    coordinator._reconcile_parent_fan_ins(parent)
    result = store.get_fan_in_result(str(joined["fan_in_id"]), project_id=parent.project_id)

    assert result is not None
    payload = turns.get(result.child_summaries[0].summary_ref)
    assert payload["kind"] == "agent.child-terminal-summary.v1"
    assert payload["final_summary"] == "检查已完成 [已移除]"
    assert "usage" not in payload


def test_observed_usage_reads_only_valid_model_receipts_and_caps_limits() -> None:
    receipt_ref = "crp://receipts/model/valid-001"
    bad_ref = "crp://receipts/model/bad-002"
    valid = {
        "schema_version": "1.0.0", "receipt_id": "model-receipt-usage-001",
        "turn_id": "turn-usage-001", "model_request_id": "model-request-usage-001",
        "status": "completed", "requested_at": "2026-09-02T00:00:00+00:00",
        "completed_at": "2026-09-02T00:00:01+00:00", "duration_ms": 1000,
        "provider_id": "provider-usage", "model_id": "model-usage",
        "usage_status": "recorded", "usage": {"input_tokens": 90, "output_tokens": 60, "total_tokens": 150},
        "input_recorded": False, "output_recorded": False, "error_code": None,
    }
    events = (
        {"type": "model.completed", "correlation": {"model_request_id": "model-request-usage-001"}, "data": {"receipt_ref": receipt_ref}},
        {"type": "model.completed", "correlation": {"model_request_id": "model-request-usage-001"}, "data": {"receipt_ref": receipt_ref}},
        {"type": "model.failed", "correlation": {"model_request_id": "model-request-usage-002"}, "data": {"receipt_ref": bad_ref}},
        {"type": "tool.completed", "data": {}},
    )

    usage = _observed_usage(events, {receipt_ref: valid, bad_ref: {"not": "a receipt"}}.__getitem__, AgentBudget(1, 1, 50, 40, 10))

    assert usage == AgentBudget(1, 1, 50, 40, 0)


def test_spawn_requires_the_parent_frozen_agent_capability() -> None:
    main = _profile("main.orchestrator", "main", budget=8, capabilities=("memory.recall", "agent.spawn"), children=True)
    worker = _profile("subagent.worker", "subagent", budget=4, capabilities=("memory.recall",), children=False)
    runtime, runner, store = _Runtime(), _Runner(), _Store()
    request = _request()
    coordinator = AgentCoordinator(
        runtime=runtime, runner=runner, store=store,
        profiles=_Profiles(main, worker),
        request_loader=lambda turn_id: next(item for item in runtime.accepted if item["turn_id"] == turn_id),
    )
    coordinator.accept_main_and_submit(request)

    with pytest.raises(Exception, match="frozen capability"):
        coordinator.spawn(
            parent_turn_id=str(request["turn_id"]),
            operation_id="op-agent-spawn-denied-0001",
            tool_call_id="tool-call-agent-spawn-denied-0001",
            project_id=str(request["scope"]["project_id"]),
            scope=request["scope"], privacy=request["privacy"],
            arguments={"profile_id": "subagent.worker", "task": "inspect"},
        )
    assert len(runtime.accepted) == 1


def test_policy_canary_freezes_only_new_turns_and_replay_uses_the_original_snapshot() -> None:
    main = _profile("main.orchestrator", "main", budget=8, capabilities=("memory.recall",), children=True)
    runtime, runner, store = _Runtime(), _Runner(), _Store()
    policy = _PolicySnapshots()
    frozen_payloads: dict[str, object] = {}

    def write_snapshot(turn_id, kind, payload):
        ref = f"crp://{turn_id}/{kind}"
        frozen_payloads[ref] = payload
        return ref

    coordinator = AgentCoordinator(
        runtime=runtime, runner=runner, store=store, profiles=_Profiles(main),
        request_loader=lambda turn_id: next(item for item in runtime.accepted if item["turn_id"] == turn_id),
        immutable_payload_writer=write_snapshot,
        payload_loader=frozen_payloads.__getitem__,
        agent_policy_snapshots=policy,
    )
    first = _request()
    coordinator.accept_main_and_submit(first)
    frozen = runtime.accepted[-1]
    assert frozen["agent_policy_binding"] == {"policy_id": "workbench.default", "revision": 1, "snapshot_ref": f"crp://{first['turn_id']}/agent-policy-snapshot-v1"}
    policy.revision = 2
    coordinator.resubmit_existing_turn(turn_id=str(first["turn_id"]), project_id=str(first["scope"]["project_id"]))
    assert policy.freeze_calls == [(first["scope"]["project_id"], first["turn_id"])]
    assert runner.submitted[-1]["agent_policy_binding"]["revision"] == 1

    second = _request()
    second.update({"turn_id": "turn-policy-canary-new-0001", "session_id": "session-policy-canary-new-001", "operation_id": "op-policy-canary-new-0001", "idempotency_key": "policy-canary-new-key-001"})
    coordinator.accept_main_and_submit(second)
    assert runtime.accepted[-1]["agent_policy_binding"]["revision"] == 2

    first_ref = frozen["agent_policy_binding"]["snapshot_ref"]
    frozen_payloads[first_ref] = {
        **frozen_payloads[first_ref], "revision": 2,
    }
    with pytest.raises(Exception, match="immutable snapshot drifted"):
        coordinator.resubmit_existing_turn(
            turn_id=str(first["turn_id"]),
            project_id=str(first["scope"]["project_id"]),
        )


def test_main_turn_rejects_caller_supplied_policy_binding() -> None:
    main = _profile("main.orchestrator", "main", budget=8, capabilities=("memory.recall",), children=True)
    request = _request()
    request["agent_policy_binding"] = {"policy_id": "workbench.default", "revision": 1, "snapshot_ref": "crp://turn-policy-forged-0001/snapshot"}
    coordinator = AgentCoordinator(runtime=_Runtime(), runner=_Runner(), store=_Store(), profiles=_Profiles(main), request_loader=lambda _turn_id: request)

    with pytest.raises(Exception, match="host-owned"):
        coordinator.accept_and_register_main(request)


def test_policy_intersects_context_and_scheduler_limits_for_main() -> None:
    main = _profile("main.orchestrator", "main", budget=8, capabilities=("memory.recall", "agent.spawn"), children=True)
    worker = _profile("subagent.worker", "subagent", budget=4, capabilities=("memory.recall",), children=False)
    restricted = _policy(
        scheduler=SimpleNamespace(cluster_mode="main_only", max_assignments=0, parallelism_cap=0, prefer_main_only=True, allowed_expert_ids=(), allowed_skill_ids=()),
        context=SimpleNamespace(include_project_skill=False, include_memory=False, include_session_history=True, max_context_bytes=1),
    )
    runtime, runner, store = _Runtime(), _Runner(), _Store()
    policy = _PolicySnapshots(policy=restricted)
    coordinator = AgentCoordinator(
        runtime=runtime, runner=runner, store=store, profiles=_Profiles(main, worker),
        request_loader=lambda turn_id: next(item for item in runtime.accepted if item["turn_id"] == turn_id),
        immutable_payload_writer=lambda turn_id, kind, _payload: f"crp://{turn_id}/{kind}",
        agent_policy_snapshots=policy,
    )
    request = _request(); request["capability_policy"]["allowed"].append("agent.spawn")
    parent = coordinator.accept_main_and_submit(request)
    accepted = runtime.accepted[-1]
    assert accepted["context_policy"] == {"include_project_skill": False, "include_memory": False, "include_session_history": True, "max_context_bytes": 1}
    with pytest.raises(Exception, match="frozen capability"):
        coordinator.spawn(parent_turn_id=parent.turn_id, operation_id="op-policy-child-0001", tool_call_id="tool-policy-child-0001", project_id=parent.project_id, scope=request["scope"], privacy=request["privacy"], arguments={"profile_id": "subagent.worker", "task": "blocked by frozen scheduler"})


def test_policy_freezes_and_intersects_a_child_before_turn_acceptance() -> None:
    main = _profile("main.orchestrator", "main", budget=8, capabilities=("memory.recall", "agent.spawn"), children=True)
    worker = _profile("subagent.worker", "subagent", budget=4, capabilities=("memory.recall",), children=False)
    policy = _PolicySnapshots(policy=_policy(context=SimpleNamespace(include_project_skill=True, include_memory=False, include_session_history=False, max_context_bytes=2)))
    runtime, runner, store = _Runtime(), _Runner(), _Store()
    coordinator = AgentCoordinator(
        runtime=runtime, runner=runner, store=store, profiles=_Profiles(main, worker),
        request_loader=lambda turn_id: next(item for item in runtime.accepted if item["turn_id"] == turn_id),
        immutable_payload_writer=lambda turn_id, kind, _payload: f"crp://{turn_id}/{kind}",
        agent_policy_snapshots=policy,
    )
    request = _request(); request["capability_policy"]["allowed"].append("agent.spawn")
    parent = coordinator.accept_main_and_submit(request)
    child = coordinator.prepare_child(
        parent_turn_id=parent.turn_id, operation_id="op-policy-child-accept-0001", tool_call_id="tool-policy-child-accept-0001",
        project_id=parent.project_id, scope=request["scope"], privacy=request["privacy"],
        arguments={"profile_id": "subagent.worker", "task": "read bounded evidence"},
    )
    assert policy.freeze_calls[-1] == (parent.project_id, child.run.turn_id)
    assert child.request["agent_policy_binding"]["revision"] == 1
    assert child.request["context_policy"] == {"include_project_skill": True, "include_memory": False, "include_session_history": False, "max_context_bytes": 2}
    assert runtime.accepted[-1] == child.request


def test_frozen_policy_limits_steward_assignments_before_permits_are_published() -> None:
    main = _profile(
        "main.orchestrator", "main", budget=8,
        capabilities=("agent.spawn", "agent.plan"), children=True,
    )
    steward = _profile(
        "steward.scheduler", "subagent", budget=4,
        capabilities=("agent.plan",), children=False,
    )
    restricted = _policy(scheduler=SimpleNamespace(
        cluster_mode="expert_cluster", max_assignments=1,
        parallelism_cap=1, prefer_main_only=False,
        allowed_expert_ids=("research",),
        allowed_skill_ids=("project-skill",),
    ))
    policy = _PolicySnapshots(policy=restricted)
    runtime, runner, store, dispatch = _Runtime(), _Runner(), _Store(), _Dispatch()
    payloads: dict[str, object] = {}

    def write_snapshot(turn_id, kind, payload):
        ref = f"crp://{turn_id}/{kind}"
        payloads[ref] = payload
        return ref

    coordinator = AgentCoordinator(
        runtime=runtime, runner=runner, store=store,
        profiles=_Profiles(main, steward),
        request_loader=lambda turn_id: next(
            item for item in runtime.accepted if item["turn_id"] == turn_id
        ),
        immutable_payload_writer=write_snapshot,
        payload_loader=payloads.__getitem__,
        agent_policy_snapshots=policy,
        dispatch_runtime=dispatch,
    )
    request = _request()
    request["capability_policy"]["allowed"].extend(("agent.spawn", "agent.plan"))
    parent = coordinator.accept_main_and_submit(request)
    spawned = coordinator.spawn(
        parent_turn_id=parent.turn_id,
        operation_id="op-policy-steward-spawn-0001",
        tool_call_id="tool-policy-steward-spawn-0001",
        project_id=parent.project_id,
        scope=request["scope"], privacy=request["privacy"],
        arguments={"profile_id": "steward.scheduler", "task": "schedule"},
    )
    steward_turn = str(spawned["run"]["turn_id"])
    steward_request = next(
        item for item in runtime.accepted if item["turn_id"] == steward_turn
    )

    with pytest.raises(Exception, match="excludes steward cluster plan"):
        coordinator.plan(
            parent_turn_id=steward_turn,
            operation_id="op-policy-steward-plan-0001",
            project_id=parent.project_id,
            scope=steward_request["scope"], privacy=steward_request["privacy"],
            arguments={
                "mode": "cluster", "plan_id": "policy-plan",
                "cluster_id": "policy-cluster",
                "assignments": [{}, {}],
            },
        )
    assert not any(call[0] == "publish" for call in dispatch.calls)


def test_capability_shaped_spawn_derives_ids_and_rejects_binding_spoof() -> None:
    main = _profile("main.orchestrator", "main", budget=8, capabilities=("memory.recall", "agent.spawn"), children=True)
    worker = _profile("subagent.worker", "subagent", budget=4, capabilities=("memory.recall",), children=False)
    runtime, runner, store = _Runtime(), _Runner(), _Store()
    request = _request()
    request["capability_policy"]["allowed"].append("agent.spawn")
    coordinator = AgentCoordinator(runtime=runtime, runner=runner, store=store, profiles=_Profiles(main, worker), request_loader=lambda turn_id: next(item for item in runtime.accepted if item["turn_id"] == turn_id))
    coordinator.accept_main_and_submit(request)

    result = coordinator.spawn(
        parent_turn_id=str(request["turn_id"]), operation_id="op-agent-spawn-0001",
        tool_call_id="tool-call-agent-spawn-0001", project_id=str(request["scope"]["project_id"]),
        scope=request["scope"], privacy=request["privacy"],
        arguments={"profile_id": "subagent.worker", "task": "inspect", "capability_ids": ["memory.recall"]},
    )

    assert isinstance(result, dict) and result["run"]["role"] == "subagent"
    child_request = runtime.accepted[-1]
    binding = dict(child_request["agent_binding"])
    assert coordinator.verify_agent_binding(child_request, binding) == binding
    binding["profile_id"] = "subagent.explorer"
    with pytest.raises(Exception, match="binding"):
        coordinator.verify_agent_binding(child_request, binding)


def test_host_capability_authorization_requires_exact_registered_run_subset() -> None:
    main = _profile(
        "main.orchestrator", "main", budget=8,
        capabilities=("memory.recall", "agent.plan"), children=True,
    )
    runtime, runner, store = _Runtime(), _Runner(), _Store()
    request = _request()
    request["capability_policy"]["allowed"].append("agent.plan")
    coordinator = AgentCoordinator(
        runtime=runtime, runner=runner, store=store,
        profiles=_Profiles(main),
        request_loader=lambda turn_id: next(
            item for item in runtime.accepted if item["turn_id"] == turn_id
        ),
    )
    coordinator.accept_main_and_submit(request)
    frozen_request = runtime.accepted[-1]

    assert coordinator.authorize_agent_capability(
        frozen_request, "agent.plan",
    ) == frozen_request["agent_binding"]
    with pytest.raises(Exception, match="identity"):
        coordinator.authorize_agent_capability(frozen_request, "agent.unknown")

    restricted_main = _profile(
        "main.orchestrator", "main", budget=8,
        capabilities=("memory.recall",), children=True,
    )
    restricted_runtime, restricted_store = _Runtime(), _Store()
    restricted = AgentCoordinator(
        runtime=restricted_runtime, runner=_Runner(), store=restricted_store,
        profiles=_Profiles(restricted_main),
        request_loader=lambda turn_id: next(
            item for item in restricted_runtime.accepted if item["turn_id"] == turn_id
        ),
    )
    restricted.accept_main_and_submit(request)
    with pytest.raises(Exception, match="frozen capability"):
        restricted.authorize_agent_capability(
            restricted_runtime.accepted[-1], "agent.plan",
        )


def test_spawn_uses_independent_turn_and_durable_topology(tmp_path: Path) -> None:
    turns, store, _runtime, _runner, _profiles, coordinator = _durable_coordinator(tmp_path)
    request = _request()
    request["capability_policy"]["allowed"].append("agent.spawn")
    parent = coordinator.accept_main_and_submit(request)
    persisted_parent_request = turns.get_request(parent.turn_id)
    assert persisted_parent_request is not None
    assert coordinator.verify_agent_binding(
        persisted_parent_request, persisted_parent_request["agent_binding"],
    ) == persisted_parent_request["agent_binding"]
    provider = AgentCapabilityProvider(coordinator=coordinator, capability_id="agent.spawn")
    provider_request = {
        "turn_id": parent.turn_id,
        "operation_id": "op-agent-spawn-durable-0001",
        "tool_call_id": "tool-call-agent-spawn-durable-0001",
        "capability_id": "agent.spawn",
        "scope": request["scope"],
        "privacy": request["privacy"],
        "arguments": {
            "profile_id": "subagent.explorer",
            "task": "inspect the frozen project evidence",
            "capability_ids": ["memory.recall"],
        },
    }

    first = provider.invoke(provider_request)
    replay = provider.invoke(provider_request)

    assert first["result"]["run"] == replay["result"]["run"]
    assert first["operation_receipt"]["status"] == "completed"
    child_turn_id = str(first["result"]["run"]["turn_id"])
    assert child_turn_id != parent.turn_id
    child_request = turns.get_request(child_turn_id)
    assert child_request is not None
    assert child_request["desired_outcome"] == "agent.child.execute"
    binding = child_request["agent_binding"]
    assert coordinator.verify_agent_binding(child_request, binding) == binding
    children = store.list_runs(project_id=parent.project_id, parent_run_id=parent.run_id)
    assert len(children) == 1 and children[0].turn_id == child_turn_id
    links = store.list_child_links(project_id=parent.project_id, parent_run_id=parent.run_id)
    assert len(links) == 1 and links[0].status == "spawned"


def test_concurrent_spawns_create_two_independent_child_turns(tmp_path: Path) -> None:
    turns, store, _runtime, _runner, _profiles, coordinator = _durable_coordinator(tmp_path)
    request = _request()
    request["capability_policy"]["allowed"].append("agent.spawn")
    parent = coordinator.accept_main_and_submit(request)

    def spawn(number: int):
        return coordinator.spawn(
            parent_turn_id=parent.turn_id,
            operation_id=f"op-agent-spawn-concurrent-{number:04d}",
            tool_call_id=f"tool-call-agent-spawn-concurrent-{number:04d}",
            project_id=parent.project_id,
            scope=request["scope"], privacy=request["privacy"],
            arguments={
                "profile_id": "subagent.explorer",
                "task": f"inspect branch {number}",
                "capability_ids": ["memory.recall"],
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(spawn, (1, 2)))

    child_turn_ids = {str(item["run"]["turn_id"]) for item in results}
    assert len(child_turn_ids) == 2
    assert all(turns.get_request(turn_id) is not None for turn_id in child_turn_ids)
    assert len(store.list_runs(project_id=parent.project_id, parent_run_id=parent.run_id)) == 2


def test_configured_worker_can_delegate_one_more_bounded_level(tmp_path: Path) -> None:
    turns, store, _runtime, _runner, profiles, coordinator = _durable_coordinator(tmp_path)
    worker_profile = profiles.get("subagent.worker")
    assert worker_profile is not None and "agent.spawn" in worker_profile.capability_ids
    profiles.update(
        replace(
            worker_profile, revision=2, allow_child_spawn=True,
            max_concurrent_children=1, max_depth=2,
        ),
        expected_revision=1,
    )
    request = _request()
    request["capability_policy"]["allowed"].append("agent.spawn")
    parent = coordinator.accept_main_and_submit(request)
    worker_result = coordinator.spawn(
        parent_turn_id=parent.turn_id,
        operation_id="op-agent-spawn-worker-0001",
        tool_call_id="tool-call-agent-spawn-worker-0001",
        project_id=parent.project_id,
        scope=request["scope"], privacy=request["privacy"],
        arguments={"profile_id": "subagent.worker", "task": "coordinate one bounded branch"},
    )
    worker_run_id = str(worker_result["run"]["run_id"])
    worker = store.get_run_with_revision(worker_run_id, project_id=parent.project_id)
    assert worker is not None and worker[0].allow_child_spawn is True
    assert worker[0].max_concurrent_children == 1 and worker[0].depth == 1
    worker_request = turns.get_request(worker[0].turn_id)
    assert worker_request is not None

    leaf_result = coordinator.spawn(
        parent_turn_id=worker[0].turn_id,
        operation_id="op-agent-spawn-leaf-0001",
        tool_call_id="tool-call-agent-spawn-leaf-0001",
        project_id=parent.project_id,
        scope=worker_request["scope"], privacy=worker_request["privacy"],
        arguments={"profile_id": "subagent.explorer", "task": "inspect the leaf evidence"},
    )

    leaf = store.get_run_with_revision(
        str(leaf_result["run"]["run_id"]), project_id=parent.project_id,
    )
    assert leaf is not None and leaf[0].parent_run_id == worker[0].run_id
    assert leaf[0].depth == 2 and leaf[0].allow_child_spawn is False


def test_native_operations_share_one_durable_parent_child_topology(tmp_path: Path) -> None:
    turns, store, _runtime, _runner, _profiles, coordinator = _durable_coordinator(tmp_path)
    request = _request()
    request["capability_policy"]["allowed"].extend([
        "agent.spawn", "agent.message", "agent.interrupt",
        "agent.wait", "agent.fan_in", "agent.list",
    ])
    parent = coordinator.accept_main_and_submit(request)

    def invoke(capability_id: str, turn_id: str, number: int, arguments):
        return AgentCapabilityProvider(
            coordinator=coordinator, capability_id=capability_id,
        ).invoke({
            "turn_id": turn_id,
            "operation_id": f"op-agent-native-{number:04d}",
            "tool_call_id": f"tool-call-agent-native-{number:04d}",
            "capability_id": capability_id,
            "scope": request["scope"], "privacy": request["privacy"],
            "arguments": arguments,
        })

    spawned = invoke(
        "agent.spawn", parent.turn_id, 1,
        {"profile_id": "subagent.explorer", "task": "inspect evidence"},
    )
    child_run_id = str(spawned["result"]["run"]["run_id"])
    child_turn_id = str(spawned["result"]["run"]["turn_id"])
    messaged = invoke(
        "agent.message", parent.turn_id, 2,
        {
            "recipient_run_id": child_run_id, "kind": "task",
            "payload_ref": "crp://agent/messages/native-0002",
        },
    )
    listed = invoke("agent.list", child_turn_id, 3, {"include_messages": True})
    waited = invoke(
        "agent.wait", parent.turn_id, 4,
        {"child_run_ids": [child_run_id], "timeout_ms": 1000},
    )
    joined = invoke(
        "agent.fan_in", parent.turn_id, 5,
        {"child_run_ids": [child_run_id], "policy": "all"},
    )
    interrupted = invoke(
        "agent.interrupt", parent.turn_id, 6,
        {"child_run_id": child_run_id, "reason": "parent no longer needs this branch"},
    )

    assert messaged["result"]["status"] == "pending"
    assert listed["result"]["messages"][0]["payload_ref"] != "crp://agent/messages/native-0002"
    assert listed["result"]["messages"][0]["payload_ref"].startswith("crp://recipient-turn/messages/")
    assert waited["result"]["terminal"] is True
    assert joined["result"]["status"] == "open"
    assert interrupted["result"]["requested"] is True
    links = store.list_child_links(project_id=parent.project_id, parent_run_id=parent.run_id)
    assert links[0].status == "cancelling"
    assert turns.get_request(child_turn_id) is not None


def test_list_projects_only_safe_dispatch_progress_for_owning_main(tmp_path: Path) -> None:
    _turns, _store, _runtime, _runner, _profiles, coordinator = _durable_coordinator(tmp_path)
    request = _request()
    request["capability_policy"]["allowed"].append("agent.list")
    parent = coordinator.accept_main_and_submit(request)
    plan = AgentDispatchPlan(
        "plan-visible-001", parent.project_id, parent.run_id, "steward-run-visible", 3,
        "dispatched", "cluster", "crp://intakes/visible", 1, "crp://workloads/visible", 1,
        "crp://capacity/visible", 1, "crp://clusters/visible", 1, ("assignment-visible",),
        AgentBudget(4, 4, 4, 4, 4), 1,
    )
    coordinator._permit_store = _PlanProjectionStore(plan)

    result = coordinator.list(
        parent_turn_id=parent.turn_id, project_id=parent.project_id,
        scope=request["scope"], privacy=request["privacy"], arguments={"include_messages": False},
    )

    assert result["plans"] == ({
        "plan_id": plan.plan_id, "mode": "cluster", "status": "dispatched",
        "revision": 3, "steward_run_id": "steward-run-visible",
    },)


def test_messages_copy_into_recipient_turn_scope_and_replay_stably(tmp_path: Path) -> None:
    turns = SQLiteAITurnStore(tmp_path / "ai-turns.sqlite3")
    store = SQLiteAgentStore(tmp_path / "ai-turns.sqlite3")
    runtime, runner, authority = _DurableRuntime(turns), _Runner(), _MessagePayloadAuthority()
    profiles = AgentProfileRegistry(store)
    coordinator = AgentCoordinator(
        runtime=runtime, runner=runner, store=store, profiles=profiles,
        request_loader=lambda turn_id: turns.get_request(turn_id) or {},
        message_payload_authority=authority,
    )
    request = _request()
    request["capability_policy"]["allowed"].extend(["agent.spawn", "agent.message"])
    parent = coordinator.accept_main_and_submit(request)
    spawned = coordinator.spawn(
        parent_turn_id=parent.turn_id, operation_id="op-message-spawn-0001",
        tool_call_id="tool-message-spawn-0001", project_id=parent.project_id,
        scope=request["scope"], privacy=request["privacy"],
        arguments={"profile_id": "subagent.explorer", "task": "inspect"},
    )
    child = store.get_run_with_revision(str(spawned["run"]["run_id"]), project_id=parent.project_id)
    assert child is not None
    source_ref = "crp://sender-owned/messages/source-001"
    first = coordinator.message(
        parent_turn_id=parent.turn_id, operation_id="op-message-copy-0001",
        tool_call_id="tool-message-copy-0001", project_id=parent.project_id,
        scope=request["scope"], privacy=request["privacy"],
        arguments={"recipient_run_id": child[0].run_id, "kind": "task", "payload_ref": source_ref},
    )
    replay = coordinator.message(
        parent_turn_id=parent.turn_id, operation_id="op-message-copy-0001",
        tool_call_id="tool-message-copy-0001", project_id=parent.project_id,
        scope=request["scope"], privacy=request["privacy"],
        arguments={"recipient_run_id": child[0].run_id, "kind": "task", "payload_ref": source_ref},
    )
    stored = store.list_messages(project_id=parent.project_id, run_id=parent.run_id)
    assert first == replay and len(stored) == 1
    assert stored[0].payload_ref != source_ref
    assert authority.calls[0]["recipient_turn_id"] == child[0].turn_id
    child_request = turns.get_request(child[0].turn_id)
    assert child_request is not None
    reply = coordinator.message(
        parent_turn_id=child[0].turn_id, operation_id="op-message-reply-0001",
        tool_call_id="tool-message-reply-0001", project_id=parent.project_id,
        scope=child_request["scope"], privacy=child_request["privacy"],
        arguments={"recipient_run_id": parent.run_id, "kind": "task", "payload_ref": "crp://sender-owned/messages/source-002"},
    )
    assert reply["status"] == "pending" and authority.calls[-1]["recipient_turn_id"] == parent.turn_id


def test_message_copy_authority_failure_never_persists_a_message(tmp_path: Path) -> None:
    turns, store, runtime, runner, profiles, _coordinator = _durable_coordinator(tmp_path)
    request = _request()
    request["capability_policy"]["allowed"].extend(["agent.spawn", "agent.message"])
    coordinator = AgentCoordinator(
        runtime=runtime, runner=runner, store=store, profiles=profiles,
        request_loader=lambda turn_id: turns.get_request(turn_id) or {},
        message_payload_authority=_MessagePayloadAuthority(fail=True),
    )
    parent = coordinator.accept_main_and_submit(request)
    child = coordinator.spawn(
        parent_turn_id=parent.turn_id, operation_id="op-message-fail-spawn-0001",
        tool_call_id="tool-message-fail-spawn-0001", project_id=parent.project_id,
        scope=request["scope"], privacy=request["privacy"],
        arguments={"profile_id": "subagent.explorer", "task": "inspect"},
    )
    with pytest.raises(ValueError, match="copy rejected"):
        coordinator.message(
            parent_turn_id=parent.turn_id, operation_id="op-message-fail-0001",
            tool_call_id="tool-message-fail-0001", project_id=parent.project_id,
            scope=request["scope"], privacy=request["privacy"],
            arguments={"recipient_run_id": child["run"]["run_id"], "kind": "task", "payload_ref": "crp://sender-owned/messages/source"},
        )
    assert store.list_messages(project_id=parent.project_id, run_id=parent.run_id) == ()


def test_message_without_copy_authority_fails_closed(tmp_path: Path) -> None:
    turns, store, runtime, runner, profiles, _coordinator = _durable_coordinator(tmp_path)
    request = _request()
    request["capability_policy"]["allowed"].extend(["agent.spawn", "agent.message"])
    coordinator = AgentCoordinator(
        runtime=runtime, runner=runner, store=store, profiles=profiles,
        request_loader=lambda turn_id: turns.get_request(turn_id) or {},
    )
    parent = coordinator.accept_main_and_submit(request)
    child = coordinator.spawn(
        parent_turn_id=parent.turn_id, operation_id="op-message-none-spawn-0001",
        tool_call_id="tool-message-none-spawn-0001", project_id=parent.project_id,
        scope=request["scope"], privacy=request["privacy"],
        arguments={"profile_id": "subagent.explorer", "task": "inspect"},
    )
    with pytest.raises(Exception, match="payload authority"):
        coordinator.message(
            parent_turn_id=parent.turn_id, operation_id="op-message-none-0001",
            tool_call_id="tool-message-none-0001", project_id=parent.project_id,
            scope=request["scope"], privacy=request["privacy"],
            arguments={"recipient_run_id": child["run"]["run_id"], "kind": "task", "payload_ref": "crp://sender-owned/messages/source"},
        )
    assert store.list_messages(project_id=parent.project_id, run_id=parent.run_id) == ()


def test_interrupt_does_not_claim_or_persist_cancelling_without_local_lease(
    tmp_path: Path,
) -> None:
    _turns, store, _runtime, runner, _profiles, coordinator = _durable_coordinator(tmp_path)
    request = _request()
    request["capability_policy"]["allowed"].extend(["agent.spawn", "agent.interrupt"])
    parent = coordinator.accept_main_and_submit(request)
    spawned = coordinator.spawn(
        parent_turn_id=parent.turn_id,
        operation_id="op-agent-spawn-interrupt-false-0001",
        tool_call_id="tool-call-agent-spawn-interrupt-false-0001",
        project_id=parent.project_id,
        scope=request["scope"], privacy=request["privacy"],
        arguments={"profile_id": "subagent.explorer", "task": "inspect evidence"},
    )
    child_run_id = str(spawned["run"]["run_id"])
    runner.cancel_result = False

    result = coordinator.interrupt(
        parent_turn_id=parent.turn_id,
        child_run_id=child_run_id,
        operation_id="op-agent-interrupt-false-0001",
        reason="stop only if this runner owns the lease",
        project_id=parent.project_id,
        scope=request["scope"], privacy=request["privacy"],
    )

    assert result["requested"] is False
    links = store.list_child_links(project_id=parent.project_id, parent_run_id=parent.run_id)
    assert links[0].status == "spawned"


@pytest.mark.parametrize("timeout_ms", (0, 120_001))
def test_direct_wait_uses_the_same_bounded_timeout_as_the_public_contract(
    tmp_path: Path, timeout_ms: int,
) -> None:
    _turns, _store, _runtime, _runner, _profiles, coordinator = _durable_coordinator(tmp_path)
    request = _request()
    request["capability_policy"]["allowed"].extend(["agent.spawn", "agent.wait"])
    parent = coordinator.accept_main_and_submit(request)
    spawned = coordinator.spawn(
        parent_turn_id=parent.turn_id,
        operation_id=f"op-agent-spawn-wait-bound-{timeout_ms}",
        tool_call_id=f"tool-call-agent-spawn-wait-bound-{timeout_ms}",
        project_id=parent.project_id,
        scope=request["scope"], privacy=request["privacy"],
        arguments={"profile_id": "subagent.explorer", "task": "inspect evidence"},
    )

    with pytest.raises(Exception, match="between 1 and 120000"):
        coordinator.wait(
            parent_turn_id=parent.turn_id,
            child_run_ids=(str(spawned["run"]["run_id"]),),
            timeout_ms=timeout_ms,
            project_id=parent.project_id,
            scope=request["scope"], privacy=request["privacy"],
        )



def test_durable_role_briefs_freeze_main_steward_and_api_custom_child(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api.routes.ai_agents import router
    from core.ai_kernel import ModelGatewayAgentPlanner
    from core.ai_kernel.agent_contracts import agent_profile_to_payload
    from core.model_gateway import ModelResult
    from backend.api.agent_runtime_composition import build_agent_runtime_composition
    from backend.api.capability_admission import ReviewedCoreCapabilityRegistry, RuntimeCapabilityAdmission
    from core.ai_kernel import ScopedCapabilityRegistry
    turns = SQLiteAITurnStore(tmp_path / ".rebuild-data" / "ai-turns.sqlite3")
    composition = build_agent_runtime_composition(runtime_root=tmp_path, session_store=turns,
        registry=ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(ScopedCapabilityRegistry())))
    runtime = _DurableRuntime(turns)
    class Runner(_Runner):
        def subscribe_terminal(self, observer):
            return lambda: None
    runner = Runner(); composition.bind_runtime(runtime); composition.bind_runner(runner)
    store, profiles, coordinator = composition.store, composition.profiles, composition.coordinator
    app = FastAPI(); app.state.agent_profile_registry = profiles; app.include_router(router)
    custom = agent_profile_to_payload(replace(profiles.get("subagent.explorer"),
        profile_id="subagent.custom.audit", instructions="逐条核验并交回依据。"))
    with TestClient(app) as client:
        response = client.post("/api/ai/agent-profiles", json={"profile": custom})
        assert response.status_code == 201
        assert response.json()["instructions"] == custom["instructions"]
        custom = {**custom, "revision": 2, "instructions": "第二版核验指令。"}
        updated = client.put("/api/ai/agent-profiles/subagent.custom.audit",
            json={"expected_revision": 1, "profile": custom})
        assert updated.status_code == 200
    request = _request(); request["capability_policy"]["allowed"].extend(["agent.spawn", "agent.plan"])
    main = coordinator.accept_and_register_main(request)
    prepared = [main]
    for index, profile_id in enumerate(("steward.scheduler", "subagent.custom.audit")):
        prepared.append(coordinator.prepare_child(parent_turn_id=main.run.turn_id,
            operation_id=f"op-role-child-{index:04}", tool_call_id=f"tool-role-child-{index:04}",
            project_id=main.run.project_id, scope=request["scope"], privacy=request["privacy"],
            arguments={"profile_id": profile_id, "task": "核验资料"}))
    class Gateway:
        requests = []
        def invoke(self, request):
            self.requests.append(request)
            return ModelResult({"type": "complete", "summary": "done", "payload_ref": None, "evidence_refs": []}, "test", "test", {})
    gateway = Gateway()
    for item in prepared:
        profile = profiles.get(item.run.profile_id)
        frozen = turns.get_immutable_payload(item.run.turn_id, "agent-role-brief-v1")
        assert frozen is not None
        assert frozen[1] == {"schema_version": "1.0.0", "kind": "agent.role-brief.v1",
            "profile_id": profile.profile_id, "profile_revision": profile.revision,
            "organization_role": profile.organization_role, "work_description": profile.work_description,
            "instructions": profile.instructions}
        ModelGatewayAgentPlanner(gateway).plan(item.request, [], [], turns)
        assert json.loads(gateway.requests[-1].input)["role"]["instructions"] == profile.instructions
    profile = profiles.get(main.run.profile_id)
    profiles.update(replace(profile, revision=2, instructions="改后的指令。"), expected_revision=1)
    coordinator.resubmit_existing_turn(turn_id=main.run.turn_id, project_id=main.run.project_id)
    assert turns.get_immutable_payload(main.run.turn_id, "agent-role-brief-v1")[1]["instructions"] == profile.instructions
