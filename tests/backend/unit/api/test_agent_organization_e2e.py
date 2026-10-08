from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from backend.api.ai_runtime import get_or_build_ai_runtime
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
    """Deterministic canonical-Turn seam; it never invokes a model."""

    def __init__(self, store: SQLiteAITurnStore) -> None:
        self._store = store
        self._receipts: dict[str, TurnReceipt] = {}

    def accept_turn(self, request: dict[str, object]) -> TurnReceipt:
        _turn_id, replayed = self._store.claim_turn(request)
        receipt = TurnReceipt(
            str(request["turn_id"]), str(request["session_id"]),
            str(request["operation_id"]), "accepted", 1, replayed,
        )
        self._receipts[receipt.turn_id] = receipt
        return receipt

    def receipt_for(self, turn_id: str, *, replayed: bool = False) -> TurnReceipt:
        receipt = self._receipts[turn_id]
        return replace(receipt, replayed=replayed)


class _ObserverRunner:
    """Small runner seam preserving the composition's one observer chain."""

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

    def emit_terminal(self, turn_id: str) -> None:
        receipt = TurnReceipt(turn_id, "session-e2e", "operation-e2e", "completed", 2, False)
        for observer in tuple(self._observers):
            observer(receipt)


def _request(*, suffix: str) -> dict[str, object]:
    value = json.loads(
        (ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8")
    )
    value.update({
        "turn_id": f"turn-organization-e2e-{suffix}",
        "session_id": f"session-organization-e2e-{suffix}",
        "operation_id": f"operation-organization-e2e-{suffix}",
        "idempotency_key": f"idempotency-organization-e2e-{suffix}",
    })
    return value


def _organization(tmp_path: Path):
    database = tmp_path / ".rebuild-data" / "ai-turns.sqlite3"
    sessions = SQLiteAITurnStore(database)
    composition = build_agent_runtime_composition(
        runtime_root=tmp_path,
        session_store=sessions,
        registry=ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(ScopedCapabilityRegistry())),
    )
    runtime = _AcceptedTurnRuntime(sessions)
    runner = _ObserverRunner(runtime)
    composition.bind_runtime(runtime)
    composition.bind_runner(runner)
    organization = AgentOrganizationRuntime(
        coordinator=composition.coordinator,
        dispatch_store=composition.dispatch_store,
        run_store=composition.store,
        request_loader=composition.request_loader,
    )
    composition.bind_organization_runtime(organization)
    return composition, runner, organization


def _converge_child(composition, run_id: str) -> None:
    run, _revision = composition.store.get_run_with_revision(run_id, project_id="project-alpha")
    terminal = replace(
        run, status="completed",
        model_routing_snapshot_ref="crp://e2e/model",
        capability_manifest_ref="crp://e2e/capabilities",
        context_manifest_ref="crp://e2e/context",
        terminal_receipt_ref=f"crp://{run.turn_id}/terminal-receipt",
    )
    composition.store.converge_terminal_child(
        terminal, usage=_ZERO, operation_id=f"e2e-converge-{run.turn_id}",
    )


def _converge_main(composition, run_id: str) -> None:
    run, _revision = composition.store.get_run_with_revision(
        run_id, project_id="project-alpha",
    )
    terminal = replace(
        run, status="completed",
        model_routing_snapshot_ref="crp://e2e/main/model",
        capability_manifest_ref="crp://e2e/main/capabilities",
        context_manifest_ref="crp://e2e/main/context",
        terminal_receipt_ref=f"crp://{run.turn_id}/terminal-receipt",
    )
    composition.store.converge_terminal_main(
        terminal, operation_id=f"e2e-converge-{run.turn_id}",
    )


def _bypass_already_converged_observer(composition) -> None:
    # The store convergence above is the durable portion normally completed by
    # coordinator.reconcile_terminal_turn.  Retain the same composition
    # observer ordering while avoiding model/event fixtures in this focused test.
    composition.coordinator.reconcile_terminal_turn = lambda turn_id: {"turn_id": turn_id}  # type: ignore[method-assign]


def _cluster_proposal() -> dict[str, object]:
    return {
        "mode": "cluster", "plan_id": "e2e-cluster", "cluster_id": "e2e-cluster",
        "assignments": [{
            "assignment_id": "e2e-assignment-1",
            "profile_id": "subagent.explorer", "profile_revision": 1,
            "task": "Inspect the supplied project context.",
            "budget": {"model_calls": 1, "tool_calls": 1, "input_tokens": 100, "output_tokens": 100, "wall_time_ms": 1_000},
            "capability_ids": ["memory.recall"], "expert": None, "skill": None,
        }],
    }


def test_cluster_organization_observer_chain_converges_plan_and_fan_in_idempotently(tmp_path) -> None:
    composition, runner, organization = _organization(tmp_path)
    _bypass_already_converged_observer(composition)
    started = organization.start(_request(suffix="cluster"), agent_turn_mode=True)
    main_turn = started["main"]["turn_id"]
    steward_turn = started["steward"]["turn_id"]
    steward_request = composition.request_loader(steward_turn)

    composition.coordinator.plan(
        parent_turn_id=steward_turn,
        operation_id="operation-e2e-steward-plan",
        project_id="project-alpha", scope=steward_request["scope"],
        privacy=steward_request["privacy"], arguments=_cluster_proposal(),
    )
    steward = composition.store.get_run_by_turn_id(steward_turn, project_id="project-alpha")[0]
    _converge_child(composition, steward.run_id)
    runner.emit_terminal(steward_turn)

    children = composition.store.list_runs(project_id="project-alpha", parent_run_id=started["main"]["run_id"])
    experts = [item for item in children if item.profile_id != "steward.scheduler"]
    assert len(experts) == 1
    _converge_child(composition, experts[0].run_id)
    fan_in = composition.store.list_fan_ins(project_id="project-alpha", parent_run_id=started["main"]["run_id"])[0]
    summary = AgentTerminalChildSummary(
        experts[0].run_id, "project-alpha", "completed",
        f"crp://{experts[0].turn_id}/terminal-receipt",
        f"crp://{experts[0].turn_id}/summary", (), _ZERO,
    )
    composition.store.complete_fan_in(
        AgentFanInResult(
            "fan-in-result-e2e", fan_in.fan_in_id, "project-alpha",
            started["main"]["run_id"], "completed", (summary,),
            "crp://fan-in/e2e/receipt", "crp://fan-in/e2e/result",
        ),
        operation_id="operation-e2e-fan-in", expected_cancel_epoch=0,
    )
    runner.emit_terminal(experts[0].turn_id)
    runner.emit_terminal(experts[0].turn_id)

    plan = composition.dispatch_store.list_plans_for_main(
        project_id="project-alpha", main_run_id=started["main"]["run_id"],
    )[0]
    _converge_main(composition, started["main"]["run_id"])
    runner.emit_terminal(main_turn)
    main = composition.store.get_run(started["main"]["run_id"])
    assert plan.status == "completed"
    assert main is not None and main.status == "completed"
    assert main_turn in runner.submitted and steward_turn in runner.submitted
    assert composition.last_organization_progress_status == "progressed"


def test_main_only_organization_observer_chain_is_idempotent(tmp_path) -> None:
    composition, runner, organization = _organization(tmp_path)
    _bypass_already_converged_observer(composition)
    started = organization.start(_request(suffix="main-only"), agent_turn_mode=True)
    steward_turn = started["steward"]["turn_id"]
    steward_request = composition.request_loader(steward_turn)
    composition.coordinator.plan(
        parent_turn_id=steward_turn,
        operation_id="operation-e2e-main-only-plan",
        project_id="project-alpha", scope=steward_request["scope"],
        privacy=steward_request["privacy"],
        arguments={"mode": "main_only", "plan_id": "e2e-main-only"},
    )
    steward = composition.store.get_run_by_turn_id(steward_turn, project_id="project-alpha")[0]
    _converge_child(composition, steward.run_id)
    runner.emit_terminal(steward_turn)
    runner.emit_terminal(steward_turn)
    plan = composition.dispatch_store.list_plans_for_main(
        project_id="project-alpha", main_run_id=started["main"]["run_id"],
    )[0]
    _converge_main(composition, started["main"]["run_id"])
    runner.emit_terminal(started["main"]["turn_id"])
    main = composition.store.get_run(started["main"]["run_id"])
    assert plan.status == "completed"
    assert main is not None and main.status == "completed"
    assert composition.last_organization_progress_status == "progressed"


def test_production_organization_completes_with_store_backed_routing_refs(tmp_path) -> None:
    application = SimpleNamespace(state=SimpleNamespace())
    container = SimpleNamespace(root_dir=tmp_path)
    get_or_build_ai_runtime(SimpleNamespace(app=application), container)
    runner = application.state.ai_turn_runner
    composition = application.state.agent_runtime_composition
    organization = application.state.agent_organization_runtime
    request = json.loads(
        (
            ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request"
            / "valid-project-answer.json"
        ).read_text(encoding="utf-8")
    )
    request["input"]["text"] = (
        "Analyze the project evidence, compare the relevant constraints, and "
        "produce a concise governed recommendation."
    )

    try:
        started = organization.start(request, agent_turn_mode=True)
        terminal = runner.wait_for_terminal(
            started["main"]["turn_id"], timeout_seconds=30,
        )
        observed_runs = composition.store.list_runs(project_id="project-alpha")
        observed_plans = composition.dispatch_store.list_plans_for_main(
            project_id="project-alpha", main_run_id=started["main"]["run_id"],
        )
        diagnostics = {
            "terminal": getattr(terminal, "status", None),
            "runs": tuple(
                (run.profile_id, run.status, run.turn_id)
                for run in observed_runs
            ),
            "plans": tuple((plan.mode, plan.status) for plan in observed_plans),
            "reconcile": getattr(
                composition, "last_terminal_reconciliation_error", None,
            ),
            "organization": getattr(
                composition, "last_organization_progress_error", None,
            ),
            "events": {
                run.profile_id: tuple(
                    (
                        event.get("type"),
                        event.get("data", {}).get("error_code")
                        if isinstance(event.get("data"), dict) else None,
                        event.get("data", {}).get("status")
                        if isinstance(event.get("data"), dict) else None,
                    )
                    for event in application.state.ai_turn_store.events_after(
                        run.turn_id,
                    )[-12:]
                )
                for run in observed_runs
            },
        }
        assert terminal is not None and terminal.status == "completed", diagnostics

        main = composition.store.get_run(started["main"]["run_id"])
        steward = composition.store.get_run(started["steward"]["run_id"])
        assert main is not None and main.status == "completed"
        assert steward is not None and steward.status == "completed"
        plans = composition.dispatch_store.list_plans_for_main(
            project_id=main.project_id, main_run_id=main.run_id,
        )
        assert len(plans) == 1
        assert plans[0].mode == "cluster" and plans[0].status == "completed"
        children = composition.store.list_runs(
            project_id=main.project_id, parent_run_id=main.run_id,
        )
        experts = [
            child for child in children
            if child.profile_id != "steward.scheduler"
        ]
        assert len(experts) == 2
        assert all(child.is_terminal for child in children)
        for run in (main, *children):
            assert run.model_routing_snapshot_ref is not None
            assert application.state.ai_turn_store.get(
                run.model_routing_snapshot_ref,
            )["turn"]["turn_id"] == run.turn_id
    finally:
        assert runner.shutdown(timeout_seconds=5) == ()
