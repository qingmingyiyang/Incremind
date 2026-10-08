"""Frozen product turns retain purpose, policy and execution limits on replay."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from core.ai_kernel import validate_turn_request, AIKernelContractError
from core.ai_kernel.turn_kinds import TURN_KINDS, freeze_turn_request, turn_purpose
from tests.memory_app.v2.test_workbench_ask import env

ROOT = Path(__file__).resolve().parents[2]


def request(kind="memory.organize", **overrides):
    values = dict(turn_id="turn-" + "a" * 32, session_id="session-project-alpha",
                  operation_id="op-product-turn-0001", idempotency_key="product-turn-request-0001",
                  project_id="project-alpha", created_at="2026-10-02T00:00:00Z", text="synthetic input",
                  privacy={"mode": "local_only", "allow_remote": False, "pii": "possible",
                           "consent_refs": [], "retention": "session"})
    values.update(overrides)
    if kind == 'external.context':
        values.setdefault('capability_request', {'mode': 'execute_exact_v1',
            'capability_id': 'external.context.execute', 'arguments': {'client':'codex', 'tool':'read',
                'query':values['text'], 'scope':{'user_id':'local-user', 'project_id':values['project_id']}, 'budget':3000}})
        from backend.memory_app.v2.policies.pipelines import versions_for_turn
        result = freeze_turn_request(kind, **values)
        result['policy_versions'] = versions_for_turn(kind)
        return result
    return freeze_turn_request(kind, **values)


@pytest.mark.parametrize("kind", TURN_KINDS)
def test_every_kind_is_deterministic_schema_valid_and_detached(kind):
    first, second = request(kind), request(kind)
    assert first == second
    assert validate_turn_request(first) == first
    schema = json.loads((ROOT / "core-contracts/ai/turn-request.schema.json").read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(first))
    assert turn_purpose(first) == ("aux" if kind.startswith("memory.") or kind in {"workbench.route", "media.image_read", "external.context", "web.search"} else "primary")
    first["execution_policy"]["budget"]["max_steps"] = 999
    first["capability_policy"]["allowed"].clear()
    assert second == request(kind)


def test_legacy_request_is_not_rewritten():
    old = json.loads((ROOT / "core-contracts/ai/fixtures/turn-request/valid-project-answer.json").read_text(encoding="utf-8"))
    before = copy.deepcopy(old)
    assert validate_turn_request(old) == before
    assert "execution_policy" not in old
    assert turn_purpose(old) == "primary"


@pytest.mark.parametrize("change", ["purpose", "capability", "budget", "version", "history"])
def test_frozen_kind_rejects_policy_expansion_or_mismatch(change):
    value = request()
    if change == "purpose":
        value["execution_policy"]["purpose"] = "primary"
    elif change == "capability":
        value["capability_policy"]["allowed"].append("document.draft.propose")
    elif change == "budget":
        value["execution_policy"]["budget"]["max_steps"] = 0
    elif change == "history":
        value["context_policy"]["include_session_history"] = True
    else:
        value["execution_policy"]["template_version"] = 99
    with pytest.raises(AIKernelContractError):
        validate_turn_request(value)
    schema = json.loads((ROOT / "core-contracts/ai/turn-request.schema.json").read_text(encoding="utf-8"))
    assert not Draft202012Validator(schema).is_valid(value)


def test_version_one_validation_does_not_reconsult_live_builtin_profiles(monkeypatch):
    from core.ai_kernel import agent_profiles
    value = request("project.task")
    monkeypatch.setattr(agent_profiles, "builtin_agent_profiles", lambda: ())
    assert validate_turn_request(value) == value


def test_budget_and_capabilities_can_only_be_narrowed():
    value = request(capabilities=[], budget={"max_steps": 1, "planner_timeout_ms": 5})
    assert value["capability_policy"]["allowed"] == []
    assert value["execution_policy"]["budget"] == {"max_steps": 1, "planner_timeout_ms": 5}
    with pytest.raises(AIKernelContractError):
        request(budget={"max_steps": 999, "planner_timeout_ms": 5})


def test_answer_version_two_adds_exact_answer_tool_without_changing_version_one():
    value = request("project.answer", template_version=2, capabilities=["workbench.answer.execute"])
    assert validate_turn_request(value) == value
    schema = json.loads((ROOT / "core-contracts/ai/turn-request.schema.json").read_text(encoding="utf-8"))
    assert Draft202012Validator(schema).is_valid(value)
    with pytest.raises(AIKernelContractError):
        request("project.answer", capabilities=["workbench.answer.execute"])
    with pytest.raises(AIKernelContractError):
        request("memory.organize", template_version=2)


@pytest.mark.parametrize("atomic", [False, True])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "timed_out", "no_receipt"])
def test_aux_runtime_propagates_purpose_to_events_and_receipts(tmp_path, atomic, outcome):
    import time
    from core.ai_kernel import SynchronousAIRuntime, ScopedCapabilityRegistry, InMemoryTurnEventStore, InMemoryTurnPayloadStore
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore

    class Planner:
        def plan(self, value, events, capabilities, payloads, execution_control):
            control = execution_control
            assert control.purpose == "aux"
            assert control.timeout_ms <= 25 if outcome == "timed_out" else control.timeout_ms == 120000
            if outcome == "no_receipt":
                raise ValueError("synthetic pre-dispatch failure")
            control.model_call_routed(snapshot_ref=f"crp://session/{value['turn_id']}/route/frozen",
                snapshot_revision="a" * 64, prompt_cache_scope_identity="b" * 64,
                provider="synthetic", model="local-model", execution_location="local_loopback", purpose="aux")
            control.model_call_started(provider="synthetic", model="local-model")
            if outcome == "failed":
                control.model_call_failed()
                raise ValueError("synthetic provider failure")
            if outcome == "cancelled":
                control.request_cancel()
                control.checkpoint()
            if outcome == "timed_out":
                time.sleep(.035)
                control.checkpoint()
            control.model_call_completed(usage={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
            return {"type": "complete", "summary": "synthetic completed"}

    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3") if atomic else None
    events = store if atomic else InMemoryTurnEventStore()
    payloads = store if atomic else InMemoryTurnPayloadStore()
    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=events, payloads=payloads, state=store)
    value = request(capabilities=[], budget={"max_steps": 1, "planner_timeout_ms": 25 if outcome == "timed_out" else 120000})
    result = runtime.submit_turn(value)
    model_events = [e for e in events.events_after(result.turn_id) if e["type"].startswith("model.")]
    assert model_events
    assert all(e["data"]["model_call_purpose"] == "aux" for e in model_events)
    terminal = model_events[-1]
    assert terminal["type"] == "model." + ("failed" if outcome == "no_receipt" else outcome)
    if outcome != "no_receipt":
        receipt = payloads.get(terminal["data"]["receipt_ref"])
        assert receipt["model_call_purpose"] == "aux"


def test_gateway_receives_frozen_aux_purpose():
    from core.ai_kernel import ModelGatewayAgentPlanner, InMemoryTurnPayloadStore
    from core.model_gateway import ModelResult
    calls = []

    class Gateway:
        def invoke(self, value):
            calls.append(value)
            return ModelResult({"type": "complete", "summary": "done"}, "synthetic", "local", {})

    ModelGatewayAgentPlanner(Gateway()).plan(request(capabilities=[]), [], [], InMemoryTurnPayloadStore())
    assert len(calls) == 1
    assert calls[0].purpose == "aux"


def test_restored_planner_budget_counts_started_calls_but_not_nested_calls():
    from core.ai_kernel.turn_kinds import planner_limits
    value = request(budget={"max_steps": 2, "planner_timeout_ms": 123})
    events = [{"type": "model.requested", "correlation": {"model_request_id": "model-1"}},
              {"type": "model.requested", "correlation": {"model_request_id": "model-1"}},
              {"type": "model.requested", "correlation": {"model_request_id": "nested-1", "tool_call_id": "tool-1"}}]
    assert planner_limits(value, events, max_steps=8, timeout_ms=500) == (1, 123)
    assert planner_limits(value, events, max_steps=1, timeout_ms=100) == (0, 100)


@pytest.mark.parametrize("event_purpose,receipt_purpose,broken,expected", [
    ("aux", "aux", False, 0), ("aux", None, False, 0),
    (None, "aux", False, 0), (None, None, False, 1),
    ("primary", "aux", False, 1), ("aux", "primary", False, 1),
    ("aux", "aux", True, 1),
])
def test_aux_usage_is_exempt_only_with_consistent_trusted_evidence(event_purpose, receipt_purpose, broken, expected):
    from backend.memory_app.kernel.agent_coordinator import _observed_usage
    from core.ai_kernel.agent_contracts import AgentBudget
    receipt = {"schema_version": "1.0.0", "receipt_id": "model-receipt-001", "turn_id": "turn-usage-001",
        "model_request_id": "request-model-001", "status": "completed",
        "requested_at": "2026-10-02T00:00:00+00:00", "completed_at": "2026-10-02T00:00:01+00:00",
        "duration_ms": 1000, "provider_id": "synthetic", "model_id": "synthetic",
        "usage_status": "recorded", "usage": {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13},
        "input_recorded": False, "output_recorded": False, "error_code": None}
    data = {}
    if event_purpose is not None:
        data["model_call_purpose"] = event_purpose
    if receipt_purpose is not None:
        receipt["model_call_purpose"] = receipt_purpose
        data["receipt_ref"] = "crp://receipts/model/001"
    event = {"type": "model.completed", "correlation": {"model_request_id": "request-model-001"}, "data": data}
    usage = _observed_usage([event], lambda ref: {} if broken else receipt, AgentBudget(4, 4, 100, 100, 100))
    assert usage.model_calls == expected
    if expected == 0:
        assert usage.input_tokens == usage.output_tokens == 0


def test_frozen_step_limit_is_executed_and_completed_turn_replay_does_not_run_again(tmp_path):
    from core.ai_kernel import SynchronousAIRuntime, ScopedCapabilityRegistry, CapabilityDefinition
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore
    calls = []

    class Planner:
        def plan(self, value, events, capabilities, payloads, execution_control):
            calls.append(value["turn_id"])
            return {"type": "tool", "capability_id": "source.evidence.read", "arguments": {}}

    class Reader:
        def invoke(self, value):
            return {"evidence": "synthetic public material"}

    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    registry = ScopedCapabilityRegistry()
    registry.register(CapabilityDefinition("source.evidence.read", 1, "read", False, "read_only", "crp://input", "crp://output"), Reader())
    runtime = SynchronousAIRuntime(planner=Planner(), registry=registry, events=store, payloads=store, state=store)
    value = request(budget={"max_steps": 2, "planner_timeout_ms": 1000})
    result = runtime.submit_turn(value)
    assert len(calls) == 2
    assert store.events_after(result.turn_id)[-1]["data"]["error_code"] == "ai.step_limit"
    restored = SynchronousAIRuntime(planner=Planner(), registry=registry, events=store, payloads=store, state=store)
    restored.submit_turn(value)
    assert len(calls) == 2


def test_atomic_receipt_failure_retains_aux_terminal_identity(tmp_path):
    from core.ai_kernel import SynchronousAIRuntime, ScopedCapabilityRegistry
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore

    class BrokenReceiptStore(SQLiteAITurnStore):
        def append_model_terminal_bundle(self, *args, **kwargs):
            raise OSError("synthetic receipt persistence failure")

    class Planner:
        def plan(self, value, events, capabilities, payloads, execution_control):
            return {"type": "complete", "summary": "synthetic"}

    store = BrokenReceiptStore(tmp_path / "turns.sqlite3")
    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(), events=store, payloads=store, state=store)
    result = runtime.submit_turn(request(capabilities=[]))
    terminal = next(e for e in store.events_after(result.turn_id) if e["type"] == "model.failed")
    assert terminal["data"]["model_call_purpose"] == "aux"
    assert terminal["data"]["error_code"] == "ai.model_receipt_failed"


@pytest.mark.parametrize("kind", TURN_KINDS)
def test_each_kind_executes_with_its_frozen_purpose(kind, env):
    from core.ai_kernel import SynchronousAIRuntime, ScopedCapabilityRegistry, InMemoryTurnEventStore, InMemoryTurnPayloadStore
    calls = []

    class Planner:
        def plan(self, value, events, capabilities, payloads, execution_control):
            calls.append(execution_control.purpose)
            return {"type": "complete", "summary": "synthetic"}

    if kind == 'external.context':
        from tests.memory_app.v2.test_external_context import prepare, TURN
        from core.effect_log import EffectState
        service, product_runtime, runner, frozen = prepare(env)
        result = service.execute(TURN, runtime=product_runtime, runner=runner)
        store = env.http.app.state.ai_turn_store
        product_events = store.events_after(TURN)
        assert turn_purpose(frozen) == 'aux' and result['version'] == 'handoff@1'
        assert calls == [] and env.model.calls == 0
        assert product_events[-1]['type'] == 'turn.completed'
        assert not any(event['type'].startswith('model.') for event in product_events)
        outcome = next(event for event in product_events if event['type'] == 'tool.outcome.recorded')
        assert store.effect_runner.log.get(outcome['correlation']['tool_call_id']).state is EffectState.SETTLED_OK
        return
    events = InMemoryTurnEventStore()
    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(), events=events, payloads=InMemoryTurnPayloadStore())
    result = runtime.submit_turn(request(kind, capabilities=[]))
    assert calls == ["aux" if kind.startswith("memory.") or kind in {"workbench.route", "media.image_read", "web.search"} else "primary"]
    assert events.events_after(result.turn_id)[-1]["type"] == "turn.completed"


def test_workbench_route_is_registered_as_a_single_auxiliary_call():
    from core.ai_kernel.turn_kinds import is_user_turn, planner_limits

    assert "workbench.route" in TURN_KINDS
    value = request("workbench.route")
    assert value["capability_policy"]["allowed"] == []
    assert value["execution_policy"] == {
        "template_version": 1, "purpose": "aux",
        "budget": {"max_steps": 1, "planner_timeout_ms": 4000},
    }
    assert value["context_policy"] == {
        "include_project_skill": False, "include_memory": False,
        "include_session_history": False, "max_context_bytes": 262144,
    }
    assert not is_user_turn(value)
    assert planner_limits(value, [], max_steps=8, timeout_ms=120000) == (1, 4000)
    assert planner_limits(value, [{"type": "model.requested", "correlation": {
        "model_request_id": "route-call-1"}}], max_steps=8, timeout_ms=120000) == (0, 4000)


@pytest.mark.parametrize("change", ["purpose", "tool", "steps", "timeout", "memory", "history", "skill"])
def test_workbench_route_rejects_expanded_policy_in_python_and_json_contract(change):
    value = request("workbench.route")
    if change == "purpose":
        value["execution_policy"]["purpose"] = "primary"
    elif change == "tool":
        value["capability_policy"]["allowed"] = ["source.evidence.read"]
    elif change == "steps":
        value["execution_policy"]["budget"]["max_steps"] = 2
    elif change == "timeout":
        value["execution_policy"]["budget"]["planner_timeout_ms"] = 4001
    else:
        field = {"memory": "include_memory", "history": "include_session_history", "skill": "include_project_skill"}[change]
        value["context_policy"][field] = True
    with pytest.raises(AIKernelContractError):
        validate_turn_request(value)
    schema = json.loads((ROOT / "core-contracts/ai/turn-request.schema.json").read_text(encoding="utf-8"))
    assert not Draft202012Validator(schema).is_valid(value)


def test_workbench_route_runtime_receives_frozen_deadline_without_tools():
    from core.ai_kernel import SynchronousAIRuntime, ScopedCapabilityRegistry, InMemoryTurnEventStore, InMemoryTurnPayloadStore
    observed = []

    class Planner:
        def plan(self, value, events, capabilities, payloads, execution_control):
            observed.append((execution_control.purpose, execution_control.timeout_ms, list(capabilities)))
            return {"type": "complete", "summary": "synthetic route"}

    events = InMemoryTurnEventStore()
    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=events, payloads=InMemoryTurnPayloadStore())
    result = runtime.submit_turn(request("workbench.route"))
    assert observed == [("aux", 4000, [])]
    assert events.events_after(result.turn_id)[-1]["type"] == "turn.completed"
