from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.agent_capabilities import AGENT_CAPABILITY_IDS
from backend.api.ai_runtime import build_ai_runtime
from backend.api.agent_runtime_composition import (
    AgentRuntimeCompositionError,
    build_agent_runtime_composition,
)
from backend.api.recursive_evolution_composition import RecursiveEvolutionComposition
from backend.api.capability_admission import (
    ReviewedCoreCapabilityRegistry,
    RuntimeCapabilityAdmission,
)
from core.ai_kernel import SQLiteAITurnStore, ScopedCapabilityRegistry, TurnReceipt


ROOT = Path(__file__).resolve().parents[4]


class _Runtime:
    def accept_turn(self, _request):
        raise AssertionError("not exercised")

    def receipt_for(self, _turn_id, *, replayed=False):
        raise AssertionError("not exercised")


class _Runner:
    def __init__(self) -> None:
        self.observers = []

    def subscribe_terminal(self, observer):
        self.observers.append(observer)
        return lambda: self.observers.remove(observer)

    def accept_and_submit(self, _request):
        raise AssertionError("not exercised")

    def request_turn_cancel(self, _turn_id, *, reason):
        raise AssertionError("not exercised")

    def wait_for_terminal(self, _turn_id, *, timeout_seconds=None, poll_interval_seconds=0.05):
        raise AssertionError("not exercised")

    def terminal_receipt(self, _turn_id):
        raise AssertionError("not exercised")


class _Organization:
    def __init__(self, calls, *, fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail

    def on_terminal(self, turn_id):
        self.calls.append(("organization", turn_id))
        if self.fail:
            raise RuntimeError("organization payload must not leak")
        return {"status": "progressed"}


class _SupervisionObserver:
    def __init__(self, calls, *, fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail

    def observe(self, turn_id):
        self.calls.append(("supervision", turn_id))
        if self.fail:
            raise RuntimeError("supervision payload must not leak")
        return "recorded"


def test_composition_uses_existing_turn_database_and_one_late_bound_runner(tmp_path) -> None:
    database = tmp_path / ".rebuild-data" / "ai-turns.sqlite3"
    session_store = SQLiteAITurnStore(database)
    dispatch_registry = ScopedCapabilityRegistry()
    registry = ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(dispatch_registry))

    composition = build_agent_runtime_composition(
        runtime_root=tmp_path, session_store=session_store, registry=registry,
    )

    assert composition.profiles.get("main.orchestrator") is not None
    assert composition.dispatch_store._path == composition.store._path
    assert composition.coordinator._dispatch_runtime is composition.dispatch_runtime
    assert composition.coordinator._permit_store is composition.dispatch_store
    assert {
        definition.capability_id
        for definition in dispatch_registry.snapshot().definitions
    } == set(AGENT_CAPABILITY_IDS)
    with pytest.raises(AgentRuntimeCompositionError, match="not bound"):
        composition._runtime_port.accept_turn({})

    composition.bind_runtime(_Runtime())
    runner = _Runner()
    composition.bind_runner(runner)
    composition.bind_runner(runner)

    assert len(runner.observers) == 1
    with pytest.raises(AgentRuntimeCompositionError, match="already exists"):
        composition.bind_runner(_Runner())


def test_dispatch_runtime_resolvers_are_explicit_and_optional(tmp_path) -> None:
    database = tmp_path / ".rebuild-data" / "ai-turns.sqlite3"
    session_store = SQLiteAITurnStore(database)
    registry = ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(ScopedCapabilityRegistry()))
    expert = lambda _profile_id, _assignment, _project_id: ("crp://assignments/expert/frozen", 1)
    skill = lambda _profile_id, _assignment, _project_id: ("crp://assignments/skill/frozen", 1)

    composition = build_agent_runtime_composition(
        runtime_root=tmp_path, session_store=session_store, registry=registry,
        expert_assignment_resolver=expert, skill_assignment_resolver=skill,
    )

    assert composition.dispatch_runtime._expert_resolver is expert
    assert composition.dispatch_runtime._skill_resolver is skill


def test_composition_optionally_wires_agent_policy_snapshot_authority(tmp_path) -> None:
    database = tmp_path / ".rebuild-data" / "ai-turns.sqlite3"
    session_store = SQLiteAITurnStore(database)
    registry = ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(ScopedCapabilityRegistry()))
    policy = object()

    composition = build_agent_runtime_composition(
        runtime_root=tmp_path, session_store=session_store, registry=registry,
        agent_policy_snapshots=policy,  # type: ignore[arg-type]
    )

    assert composition.coordinator._agent_policy_snapshots is policy


def test_terminal_observer_reconciles_safely_and_terminal_receipts_are_immutable(tmp_path) -> None:
    database = tmp_path / ".rebuild-data" / "ai-turns.sqlite3"
    session_store = SQLiteAITurnStore(database)
    dispatch_registry = ScopedCapabilityRegistry()
    composition = build_agent_runtime_composition(
        runtime_root=tmp_path,
        session_store=session_store,
        registry=ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(dispatch_registry)),
    )
    composition.bind_runtime(_Runtime())
    runner = _Runner()
    composition.bind_runner(runner)
    receipt = TurnReceipt(
        "turn-0123456789abcdef0123456789abcdef", "session-terminal-001",
        "op-terminal-receipt-001", "completed", 9, False,
    )
    request = json.loads(
        (ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(
            encoding="utf-8"
        )
    )
    request["turn_id"] = receipt.turn_id
    request["session_id"] = receipt.session_id
    request["operation_id"] = receipt.operation_id
    request["idempotency_key"] = "terminal-receipt-key-001"
    session_store.claim_turn(request)

    calls = []
    organization = _Organization(calls)
    supervision = _SupervisionObserver(calls)
    composition.bind_organization_runtime(organization)
    composition.bind_organization_runtime(organization)
    composition.bind_supervision_observer(supervision)
    composition.bind_supervision_observer(supervision)

    def reconcile(turn_id):
        calls.append(("coordinator", turn_id))
        return {"turn_id": turn_id}

    composition.coordinator.reconcile_terminal_turn = reconcile  # type: ignore[method-assign]
    runner.observers[0](receipt)
    assert calls == [
        ("coordinator", receipt.turn_id),
        ("organization", receipt.turn_id),
        ("supervision", receipt.turn_id),
    ]
    assert composition.last_terminal_reconciliation_status == "reconciled"
    assert composition.last_organization_progress_status == "progressed"
    assert composition.last_supervision_observation_status == "recorded"
    composition.coordinator.reconcile_terminal_turn = lambda _turn_id: None  # type: ignore[method-assign]
    runner.observers[0](receipt)
    assert composition.last_terminal_reconciliation_status == "noop"
    assert calls == [
        ("coordinator", receipt.turn_id),
        ("organization", receipt.turn_id),
        ("supervision", receipt.turn_id),
        ("supervision", receipt.turn_id),
    ]
    composition.coordinator.reconcile_terminal_turn = lambda _turn_id: (_ for _ in ()).throw(RuntimeError("payload must not leak"))  # type: ignore[method-assign]
    runner.observers[0](receipt)
    assert composition.last_terminal_reconciliation_status == "failed"
    assert composition.last_terminal_reconciliation_error == "RuntimeError"
    assert calls[-1] == ("supervision", receipt.turn_id)
    assert len(calls) == 4

    composition.coordinator.reconcile_terminal_turn = reconcile  # type: ignore[method-assign]
    organization.fail = True
    runner.observers[0](receipt)
    assert composition.last_terminal_reconciliation_status == "reconciled"
    assert composition.last_organization_progress_status == "failed"
    assert composition.last_organization_progress_error == "RuntimeError"
    assert calls[-1] == ("organization", receipt.turn_id)
    assert len(calls) == 6
    organization.fail = False
    supervision.fail = True
    runner.observers[0](receipt)
    assert composition.last_supervision_observation_status == "failed"
    assert composition.last_supervision_observation_error == "RuntimeError"
    with pytest.raises(AgentRuntimeCompositionError, match="already exists"):
        composition.bind_organization_runtime(_Organization(calls))
    with pytest.raises(AgentRuntimeCompositionError, match="already exists"):
        composition.bind_supervision_observer(_SupervisionObserver(calls))

    terminal_ref = composition.coordinator._persist_terminal_receipt(
        receipt, status="completed",
    )
    payload = session_store.get(terminal_ref)
    assert payload["kind"] == "agent.turn-terminal-receipt.v1"
    with pytest.raises(ValueError):
        session_store.get_or_create_immutable_payload(
            receipt.turn_id, "agent-turn-terminal-receipt-v1", {"tampered": True},
        )


def test_production_runtime_state_aliases_share_one_agent_composition(tmp_path) -> None:
    application = SimpleNamespace(state=SimpleNamespace())

    runtime = build_ai_runtime(SimpleNamespace(root_dir=tmp_path), application=application)

    composition = application.state.agent_runtime_composition
    assert application.state.agent_profile_registry is composition.profiles
    assert application.state.agent_run_coordinator is composition.coordinator
    assert application.state.agent_turn_request_loader is composition.request_loader
    assert application.state.agent_organization_runtime is composition._organization_runtime
    assert application.state.world_supervision_agent_observer is composition._supervision_observer
    assert application.state.agent_organization_runtime._freshness_gate.__self__ is application.state.world_supervision_runtime
    assert application.state.agent_expert_binding_runtime is runtime._expert_binding
    evolution = application.state.recursive_evolution_composition
    assert application.state.recursive_evolution_runtime is evolution.runtime
    assert application.state.recursive_evolution_authority is evolution.authority
    assert evolution.turns is application.state.ai_turn_store
    assert evolution.world is application.state.personal_world_model_runtime
    assert evolution.world is application.state.world_supervision_runtime._world
    assert evolution.targets._profiles is composition.profiles  # noqa: SLF001 - composition identity gate
    assert composition.coordinator._agent_policy_snapshots is evolution.policy  # noqa: SLF001 - construction-order gate
    assert composition.dispatch_runtime._expert_resolver is not None
    assert composition.dispatch_runtime._skill_resolver is not None
    assert type(runtime._planner).__name__ == "AgentRoleDispatchPlanner"
    skill_authority = runtime._manifest_resolver._application_skills
    assert skill_authority._agent_binding_verifier.__self__ is composition.coordinator


def test_production_runtime_runs_bounded_recursive_evolution_recovery(
    tmp_path, monkeypatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    original = RecursiveEvolutionComposition.recover

    def tracking_recover(self, project_ids):
        calls.append(tuple(project_ids))
        return original(self, project_ids)

    monkeypatch.setattr(RecursiveEvolutionComposition, "recover", tracking_recover)
    application = SimpleNamespace(state=SimpleNamespace())
    build_ai_runtime(SimpleNamespace(root_dir=tmp_path), application=application)

    assert calls == [()]
    assert application.state.recursive_evolution_verified_workflow.runtime is (
        application.state.recursive_evolution_runtime
    )


def test_startup_upgrades_all_untouched_old_limits_once(tmp_path):
    from dataclasses import replace
    from core.ai_kernel import AgentProfileRegistry, AgentBudget, SQLiteAgentStore
    from core.ai_kernel.agent_profiles import _PREVIOUS_BUILTIN_LIMITS
    SQLiteAITurnStore(tmp_path / ".rebuild-data" / "ai-turns.sqlite3")
    store = SQLiteAgentStore(tmp_path / ".rebuild-data" / "ai-turns.sqlite3")
    registry = AgentProfileRegistry(store)
    expected = {
        "main.orchestrator": (AgentBudget(12, 24, 64000, 12000, 600000), 64, 600000),
        "steward.scheduler": (AgentBudget(2, 3, 8000, 2000, 60000), 6, 60000),
        "subagent.explorer": (AgentBudget(4, 8, 16000, 3000, 240000), 12, 240000),
        "subagent.worker": (AgentBudget(6, 12, 20000, 5000, 300000), 16, 300000),
        "subagent.reviewer": (AgentBudget(4, 8, 16000, 3000, 240000), 12, 240000),
    }
    for identity, (budget, steps, timeout) in _PREVIOUS_BUILTIN_LIMITS.items():
        profile = registry.get(identity)
        registry.update(replace(profile, revision=2, budget_limit=budget, max_steps=steps, timeout_ms=timeout), expected_revision=1)
    app = SimpleNamespace(state=SimpleNamespace())
    runtime = build_ai_runtime(SimpleNamespace(root_dir=tmp_path), application=app)
    for identity, limits in expected.items():
        profile = app.state.agent_profile_registry.get(identity)
        assert (profile.budget_limit, profile.max_steps, profile.timeout_ms) == limits
        assert profile.revision == 3
    assert runtime._max_steps == 64
    build_ai_runtime(SimpleNamespace(root_dir=tmp_path), application=SimpleNamespace(state=SimpleNamespace()))
    assert all(store.get(identity).revision == 3 for identity in expected)


def test_startup_preserves_customized_old_builtin_limits(tmp_path):
    from dataclasses import replace
    from core.ai_kernel import AgentProfileRegistry, SQLiteAgentStore
    from core.ai_kernel.agent_profiles import _PREVIOUS_BUILTIN_LIMITS
    SQLiteAITurnStore(tmp_path / ".rebuild-data" / "ai-turns.sqlite3")
    store = SQLiteAgentStore(tmp_path / ".rebuild-data" / "ai-turns.sqlite3")
    registry = AgentProfileRegistry(store)
    snapshots = {}
    for index, (identity, (budget, steps, timeout)) in enumerate(_PREVIOUS_BUILTIN_LIMITS.items()):
        field = index % 3
        if field == 0: budget = replace(budget, input_tokens=budget.input_tokens + 1)
        if field == 1: steps += 1
        if field == 2: timeout += 1
        profile = replace(registry.get(identity), revision=2, budget_limit=budget, max_steps=steps, timeout_ms=timeout)
        registry.update(profile, expected_revision=1)
        snapshots[identity] = profile
    build_ai_runtime(SimpleNamespace(root_dir=tmp_path), application=SimpleNamespace(state=SimpleNamespace()))
    assert {identity: store.get(identity) for identity in snapshots} == snapshots


def test_two_experts_receive_concrete_new_default_budget_slices():
    from backend.api.agent_steward_proposal import _slice_budget
    from core.ai_kernel import AgentBudget, AgentProfileRegistry
    registry = AgentProfileRegistry()
    budgets = _slice_budget(registry.get("main.orchestrator").budget_limit,
        (registry.get("subagent.explorer").budget_limit, registry.get("subagent.worker").budget_limit), 2)
    assert budgets == (AgentBudget(4, 8, 16000, 3000, 240000), AgentBudget(6, 12, 20000, 5000, 300000))
