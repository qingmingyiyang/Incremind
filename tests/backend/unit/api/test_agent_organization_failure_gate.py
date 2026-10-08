"""Crash-replay gate for the durable Agent organization boundaries.

The runner below never invokes a Provider.  SQLite remains real so this test
exercises the boundary that matters for a restart: a consumed dispatch permit
and already accepted child Turn must be resumed, never recreated.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from backend.api.agent_organization_runtime import AgentOrganizationRuntime
from backend.api.agent_runtime_composition import build_agent_runtime_composition
from backend.api.capability_admission import (
    ReviewedCoreCapabilityRegistry,
    RuntimeCapabilityAdmission,
)
from core.ai_kernel import (
    AgentBudget,
    AgentFanInResult,
    AgentTerminalChildSummary,
    ScopedCapabilityRegistry,
    SQLiteAITurnStore,
    TurnReceipt,
)


ROOT = Path(__file__).resolve().parents[4]
_ZERO = AgentBudget(0, 0, 0, 0, 0)


class _AcceptedTurnRuntime:
    """Canonical Turn-claim seam: local persistence only, no model/provider."""

    def __init__(self, store: SQLiteAITurnStore) -> None:
        self._store = store
        self._receipts: dict[str, TurnReceipt] = {}

    def accept_turn(self, request: dict[str, object]) -> TurnReceipt:
        turn_id, replayed = self._store.claim_turn(request)
        receipt = TurnReceipt(
            turn_id, str(request["session_id"]), str(request["operation_id"]),
            "accepted", 1, replayed,
        )
        self._receipts[turn_id] = receipt
        return receipt

    def receipt_for(self, turn_id: str, *, replayed: bool = False) -> TurnReceipt:
        receipt = self._receipts.get(turn_id)
        if receipt is None:
            request = self._store.get_request(turn_id)
            receipt = TurnReceipt(
                turn_id, str(request["session_id"]), str(request["operation_id"]),
                "accepted", 1, True,
            )
            self._receipts[turn_id] = receipt
        return replace(receipt, replayed=replayed)


class _Runner:
    def __init__(self, runtime: _AcceptedTurnRuntime) -> None:
        self._runtime = runtime
        self.submitted: list[str] = []
        self._observers: list[object] = []

    def subscribe_terminal(self, observer):
        self._observers.append(observer)
        return lambda: self._observers.remove(observer)

    def accept_and_submit(self, request):
        self.submitted.append(str(request["turn_id"]))
        return self._runtime.accept_turn(request)

    def request_turn_cancel(self, _turn_id, *, reason):
        return False

    def wait_for_terminal(self, _turn_id, **_kwargs):
        return None

    def terminal_receipt(self, _turn_id):
        return None


def _request() -> dict[str, object]:
    request = json.loads(
        (ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8")
    )
    request.update({
        "turn_id": "turn-organization-failure-gate-001",
        "session_id": "session-organization-failure-gate-001",
        "operation_id": "operation-organization-failure-gate-001",
        "idempotency_key": "idempotency-organization-failure-gate-001",
    })
    return request


def _compose(tmp_path: Path):
    sessions = SQLiteAITurnStore(tmp_path / ".rebuild-data" / "ai-turns.sqlite3")
    composition = build_agent_runtime_composition(
        runtime_root=tmp_path,
        session_store=sessions,
        registry=ReviewedCoreCapabilityRegistry(
            RuntimeCapabilityAdmission(ScopedCapabilityRegistry()),
        ),
    )
    runtime = _AcceptedTurnRuntime(sessions)
    runner = _Runner(runtime)
    composition.bind_runtime(runtime)
    composition.bind_runner(runner)
    organization = AgentOrganizationRuntime(
        coordinator=composition.coordinator,
        dispatch_store=composition.dispatch_store,
        run_store=composition.store,
        request_loader=composition.request_loader,
        start_pair_scanner=lambda limit: composition.store.list_organization_start_pairs(limit=limit),
        recovery_plan_scanner=lambda limit: composition.dispatch_store.list_recovery_plans(limit=limit),
    )
    composition.bind_organization_runtime(organization)
    return composition, runner, organization


def _cluster_proposal() -> dict[str, object]:
    return {
        "mode": "cluster", "plan_id": "failure-gate-cluster", "cluster_id": "failure-gate-cluster",
        "assignments": [{
            "assignment_id": "failure-gate-assignment-1",
            "profile_id": "subagent.explorer", "profile_revision": 1,
            "task": "private task text must remain in immutable payload storage",
            "budget": {"model_calls": 1, "tool_calls": 1, "input_tokens": 100, "output_tokens": 100, "wall_time_ms": 1_000},
            "capability_ids": ["memory.recall"], "expert": None, "skill": None,
        }],
    }


def test_restart_after_consumed_permit_never_recreates_child_and_can_converge_fan_in(tmp_path: Path) -> None:
    first, first_runner, first_organization = _compose(tmp_path)
    started = first_organization.start(_request(), agent_turn_mode=True)
    steward_turn = started["steward"]["turn_id"]
    steward_request = first.request_loader(steward_turn)
    first.coordinator.plan(
        parent_turn_id=steward_turn, operation_id="operation-failure-gate-plan",
        project_id="project-alpha", scope=steward_request["scope"],
        privacy=steward_request["privacy"], arguments=_cluster_proposal(),
    )
    steward, _ = first.store.get_run_by_turn_id(steward_turn, project_id="project-alpha")
    first.store.converge_terminal_child(
        replace(
            steward, status="completed", model_routing_snapshot_ref="crp://failure-gate/steward/model",
            capability_manifest_ref="crp://failure-gate/steward/capability",
            context_manifest_ref="crp://failure-gate/steward/context",
            terminal_receipt_ref="crp://failure-gate/steward/receipt",
        ), usage=_ZERO, operation_id="operation-failure-gate-steward-terminal",
    )
    first_organization.on_terminal(steward_turn)
    main_run_id = started["main"]["run_id"]
    children = first.store.list_runs(project_id="project-alpha", parent_run_id=main_run_id)
    experts = [item for item in children if item.profile_id != "steward.scheduler"]
    assert len(experts) == 1
    plan = first.dispatch_store.list_plans_for_main(project_id="project-alpha", main_run_id=main_run_id)[0]
    permits = first.dispatch_store.list_permits(project_id="project-alpha", plan_id=plan.plan_id)
    assert plan.status == "dispatched" and [item.status for item in permits] == ["consumed"]
    accepted_child_turn = experts[0].turn_id
    assert accepted_child_turn in first_runner.submitted

    # Process boundary: compose a new runtime over the exact same SQLite file.
    second, second_runner, second_organization = _compose(tmp_path)
    recovery = second_organization.recover()
    recovered_children = second.store.list_runs(project_id="project-alpha", parent_run_id=main_run_id)
    recovered_experts = [item for item in recovered_children if item.profile_id != "steward.scheduler"]
    assert len(recovered_experts) == 1 and recovered_experts[0].turn_id == accepted_child_turn
    assert second_runner.submitted == [started["main"]["turn_id"]]
    assert recovery["replayed_turn_ids"] == (started["main"]["turn_id"],)

    expert = recovered_experts[0]
    terminal_expert, _link, _reservation, _changed = second.store.converge_terminal_child(
        replace(
            expert, status="completed", model_routing_snapshot_ref="crp://failure-gate/expert/model",
            capability_manifest_ref="crp://failure-gate/expert/capability",
            context_manifest_ref="crp://failure-gate/expert/context",
            terminal_receipt_ref="crp://failure-gate/expert/receipt",
        ), usage=_ZERO, operation_id="operation-failure-gate-expert-terminal",
    )
    fan_in = second.store.list_fan_ins(project_id="project-alpha", parent_run_id=main_run_id)[0]
    second.store.complete_fan_in(
        AgentFanInResult(
            "failure-gate-fan-in-result", fan_in.fan_in_id, "project-alpha",
            main_run_id, "completed", (
                AgentTerminalChildSummary(
                    terminal_expert.run_id, "project-alpha", "completed",
                    terminal_expert.terminal_receipt_ref, "crp://failure-gate/expert/summary", (), _ZERO,
                ),
            ), "crp://failure-gate/fan-in/receipt", "crp://failure-gate/fan-in/result",
        ), operation_id="operation-failure-gate-fan-in", expected_cancel_epoch=0,
    )
    second_organization.on_terminal(expert.turn_id)
    final_plan = second.dispatch_store.list_plans_for_main(project_id="project-alpha", main_run_id=main_run_id)[0]
    assert final_plan.status == "completed"
