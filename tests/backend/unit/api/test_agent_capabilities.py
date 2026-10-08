from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from backend.api.agent_capabilities import (
    AGENT_CAPABILITY_IDS,
    AGENT_FAN_IN_CAPABILITY,
    AGENT_LIST_CAPABILITY,
    AGENT_MESSAGE_CAPABILITY,
    AGENT_PLAN_CAPABILITY,
    AGENT_SPAWN_CAPABILITY,
    AgentCapabilityProvider,
    agent_capability_definitions,
)
from core.ai_kernel.dispatcher import ToolProviderFailure
from core.ai_tooling import tool_from_capability
from backend.api.capability_admission import reviewed_core_capability_ids


class _Coordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _call(self, name: str, **request: object) -> Mapping[str, object]:
        self.calls.append((name, dict(request)))
        return {
            "summary": f"{name} safely completed",
            "run_id": "child-run-1",
            "hidden_context_ref": "crp://session/hidden/context",
            "local_path": "C:\\Users\\secret.txt",
            "nested": {"secret": "do-not-leak", "status": "safe"},
            "evidence_refs": ["crp://agent/runs/child-run-1", "C:\\secret"],
        }

    def spawn(self, **request: object) -> Mapping[str, object]: return self._call("spawn", **request)
    def message(self, **request: object) -> Mapping[str, object]: return self._call("message", **request)
    def interrupt(self, **request: object) -> Mapping[str, object]: return self._call("interrupt", **request)
    def wait(self, **request: object) -> Mapping[str, object]: return self._call("wait", **request)
    def fan_in(self, **request: object) -> Mapping[str, object]: return self._call("fan_in", **request)
    def list(self, **request: object) -> Mapping[str, object]: return self._call("list", **request)
    def plan(self, **request: object) -> Mapping[str, object]: return self._call("plan", **request)


class _BrokenCoordinator:
    def spawn(self, **request: object) -> Mapping[str, object]:
        del request
        raise RuntimeError("the durable operation may have started")


def _request(capability_id: str, arguments: Mapping[str, object]) -> dict[str, object]:
    return {
        "turn_id": "turn-0123456789abcdef0123456789abcdef",
        "operation_id": "op-agent-operation-0001",
        "tool_call_id": "tool-call-01",
        "capability_id": capability_id,
        "scope": {"kind": "project", "project_id": "project-1", "series_id": None},
        "privacy": {"mode": "local_first", "allow_remote": False, "pii": "none", "consent_refs": [], "retention": "session"},
        "arguments": dict(arguments),
    }


def _arguments(capability_id: str) -> dict[str, object]:
    return {
        AGENT_SPAWN_CAPABILITY: {"profile_id": "explorer", "task": "Inspect immutable state"},
        AGENT_MESSAGE_CAPABILITY: {"recipient_run_id": "child-run-1", "kind": "progress", "payload_ref": "crp://agent/messages/message-1"},
        "agent.interrupt": {"child_run_id": "child-run-1", "reason": "parent cancelled"},
        "agent.wait": {"child_run_ids": ["child-run-1"], "timeout_ms": 1000},
        AGENT_FAN_IN_CAPABILITY: {"child_run_ids": ["child-run-1"], "policy": "all"},
        AGENT_LIST_CAPABILITY: {"include_messages": False},
        AGENT_PLAN_CAPABILITY: {"mode": "main_only", "plan_id": "plan-1"},
    }[capability_id]


def test_definitions_are_native_and_tool_contracts_are_valid() -> None:
    definitions = agent_capability_definitions()
    assert tuple(item.capability_id for item in definitions) == AGENT_CAPABILITY_IDS
    assert {item.capability_id for item in definitions if item.requires_approval} == {
        "agent.spawn", "agent.message", "agent.interrupt", "agent.fan_in",
    }
    assert AGENT_PLAN_CAPABILITY in reviewed_core_capability_ids()
    plan = next(item for item in definitions if item.capability_id == AGENT_PLAN_CAPABILITY)
    assert plan.mode == "platform" and plan.requires_approval is False
    assert tool_from_capability(plan).operation_semantics == "receipt_required"
    for definition in definitions:
        tool = tool_from_capability(definition)
        assert tool.owner_id == "agent-coordinator"
        assert tool.destination == "platform"


def test_agent_operations_share_resource_lock_without_exclusive_gate() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext
    from threading import Lock
    from time import sleep
    from types import SimpleNamespace
    from core.ai_kernel.dispatcher import SynchronousToolDispatcher, ToolDispatchRequest

    for definition in agent_capability_definitions():
        tool = tool_from_capability(definition)
        assert tool.execution_mode == "parallel"
        assert tool.resource_locks == (("agent:coordination",) if definition.mode == "platform" else ())

    class Coordinator(_Coordinator):
        active = 0
        peak = 0
        def __init__(self):
            super().__init__()
            self.lock = Lock()
        def plan(self, **request):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                sleep(.02)
                return self._call("plan", **request)
            finally:
                with self.lock:
                    self.active -= 1

    coordinator = Coordinator()
    dispatcher = SynchronousToolDispatcher()
    provider = AgentCapabilityProvider(coordinator=coordinator, capability_id=AGENT_PLAN_CAPABILITY)
    tool = tool_from_capability(next(item for item in agent_capability_definitions() if item.capability_id == AGENT_PLAN_CAPABILITY))
    observer = SimpleNamespace(claimed=lambda: None, started=lambda: None, fence=nullcontext)
    def invoke(number):
        request = _request(AGENT_PLAN_CAPABILITY, _arguments(AGENT_PLAN_CAPABILITY))
        request["tool_call_id"] = f"tool-call-{number}"
        return dispatcher.dispatch(provider, ToolDispatchRequest(request, tool.execution_mode,
            tool.resource_locks, request["tool_call_id"], 1, 1000), observer)
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert len(list(pool.map(invoke, range(3)))) == 3
    assert coordinator.peak == 1
    assert len(coordinator.calls) == 3


@pytest.mark.parametrize("capability_id", AGENT_CAPABILITY_IDS)
def test_provider_passes_only_frozen_authority_and_returns_safe_projection(capability_id: str) -> None:
    coordinator = _Coordinator()
    result = AgentCapabilityProvider(coordinator=coordinator, capability_id=capability_id).invoke(
        _request(capability_id, _arguments(capability_id))
    )
    assert coordinator.calls[0][0] == capability_id.removeprefix("agent.")
    forwarded = coordinator.calls[0][1]
    assert forwarded["parent_turn_id"] == "turn-0123456789abcdef0123456789abcdef"
    assert forwarded["project_id"] == "project-1"
    assert "hidden_context_ref" not in result["result"]
    assert "local_path" not in result["result"]
    assert result["result"]["nested"] == {"status": "safe"}
    assert result["evidence_refs"] == ["crp://agent/runs/child-run-1"]
    if capability_id in {"agent.spawn", "agent.message", "agent.interrupt", "agent.fan_in", "agent.plan"}:
        assert result["operation_receipt"]["operation_id"] == "op-agent-operation-0001"
    else:
        assert "operation_receipt" not in result


def test_provider_rejects_spoofed_scope_and_does_not_call_coordinator() -> None:
    coordinator = _Coordinator()
    request = _request(AGENT_SPAWN_CAPABILITY, _arguments(AGENT_SPAWN_CAPABILITY))
    request["scope"] = {"kind": "global", "project_id": None, "series_id": None}
    with pytest.raises(ToolProviderFailure) as error:
        AgentCapabilityProvider(coordinator=coordinator, capability_id=AGENT_SPAWN_CAPABILITY).invoke(request)
    assert error.value.error_code == "agent.request_invalid"
    assert error.value.effect_certainty == "confirmed_none"
    assert coordinator.calls == []


def test_provider_accepts_the_existing_world_action_identity_family() -> None:
    coordinator = _Coordinator()
    request = _request(AGENT_LIST_CAPABILITY, _arguments(AGENT_LIST_CAPABILITY))
    request["turn_id"] = "world-turn-12345678-1234-1234-1234-123456789abc"
    request["operation_id"] = "world-action-12345678-1234-1234-1234-123456789abc"

    AgentCapabilityProvider(
        coordinator=coordinator,
        capability_id=AGENT_LIST_CAPABILITY,
    ).invoke(request)

    forwarded = coordinator.calls[0][1]
    assert forwarded["parent_turn_id"] == request["turn_id"]
    assert forwarded["operation_id"] == request["operation_id"]


@pytest.mark.parametrize(
    ("turn_id", "operation_id"),
    [
        ("world-turn-", "world-action-valid-0001"),
        ("world-turn-valid-0001", "world-action-"),
        ("other-turn-valid-0001", "world-action-valid-0001"),
        ("world-turn-valid-0001", "other-action-valid-0001"),
    ],
)
def test_provider_keeps_unknown_identity_families_closed(
    turn_id: str,
    operation_id: str,
) -> None:
    coordinator = _Coordinator()
    request = _request(AGENT_LIST_CAPABILITY, _arguments(AGENT_LIST_CAPABILITY))
    request["turn_id"] = turn_id
    request["operation_id"] = operation_id

    with pytest.raises(ToolProviderFailure) as error:
        AgentCapabilityProvider(
            coordinator=coordinator,
            capability_id=AGENT_LIST_CAPABILITY,
        ).invoke(request)

    assert error.value.error_code == "agent.request_invalid"
    assert coordinator.calls == []


def test_provider_rejects_extra_and_sensitive_arguments() -> None:
    coordinator = _Coordinator()
    arguments = _arguments(AGENT_SPAWN_CAPABILITY)
    arguments["secret"] = "nope"
    with pytest.raises(ToolProviderFailure):
        AgentCapabilityProvider(coordinator=coordinator, capability_id=AGENT_SPAWN_CAPABILITY).invoke(
            _request(AGENT_SPAWN_CAPABILITY, arguments)
        )
    assert coordinator.calls == []


def test_fan_in_requires_quorum_and_list_is_exact() -> None:
    coordinator = _Coordinator()
    with pytest.raises(ToolProviderFailure):
        AgentCapabilityProvider(coordinator=coordinator, capability_id=AGENT_FAN_IN_CAPABILITY).invoke(
            _request(AGENT_FAN_IN_CAPABILITY, {"child_run_ids": ["child-run-1"], "policy": "quorum"})
        )
    with pytest.raises(ToolProviderFailure):
        AgentCapabilityProvider(coordinator=coordinator, capability_id=AGENT_LIST_CAPABILITY).invoke(
            _request(AGENT_LIST_CAPABILITY, {"include_messages": False, "extra": True})
        )
    assert coordinator.calls == []


@pytest.mark.parametrize("budget", [
    {"model_calls": 1, "tool_calls": 1, "input_tokens": 1, "output_tokens": 1},
    {"model_calls": True, "tool_calls": 1, "input_tokens": 1, "output_tokens": 1, "wall_time_ms": 1},
    {"model_calls": 1, "tool_calls": 1, "input_tokens": 1, "output_tokens": 1, "wall_time_ms": 86_400_001},
    {"model_calls": 1, "tool_calls": 1, "input_tokens": 1, "output_tokens": 1, "wall_time_ms": 1, "extra": 0},
])
def test_spawn_budget_is_exact_and_rejected_before_coordinator(budget: Mapping[str, object]) -> None:
    coordinator = _Coordinator()
    arguments = _arguments(AGENT_SPAWN_CAPABILITY)
    arguments["budget"] = dict(budget)
    with pytest.raises(ToolProviderFailure) as error:
        AgentCapabilityProvider(coordinator=coordinator, capability_id=AGENT_SPAWN_CAPABILITY).invoke(
            _request(AGENT_SPAWN_CAPABILITY, arguments)
        )
    assert error.value.effect_certainty == "confirmed_none"
    assert coordinator.calls == []


def test_coordinator_failure_is_unknown_after_dispatch_starts() -> None:
    with pytest.raises(ToolProviderFailure) as error:
        AgentCapabilityProvider(coordinator=_BrokenCoordinator(), capability_id=AGENT_SPAWN_CAPABILITY).invoke(
            _request(AGENT_SPAWN_CAPABILITY, _arguments(AGENT_SPAWN_CAPABILITY))
        )
    assert error.value.error_code == "agent.coordinator_failed"
    assert error.value.effect_certainty == "unknown"


def test_agent_schemas_parse_and_budget_schema_rejects_extra_fields() -> None:
    root = Path(__file__).resolve().parents[4]
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in (root / "core-contracts" / "ai").glob("agent-*.schema.json")]
    for schema in schemas:
        Draft202012Validator.check_schema(schema)
    spawn = next(schema for schema in schemas if schema["$id"].endswith("agent-spawn-request.schema.json"))
    validator = Draft202012Validator(spawn)
    invalid = {"profile_id": "explorer", "task": "inspect", "budget": {"model_calls": 1, "tool_calls": 1, "input_tokens": 1, "output_tokens": 1, "wall_time_ms": 1, "extra": 0}}
    assert list(validator.iter_errors(invalid))


def test_cluster_plan_is_forwarded_without_creating_execution_limits() -> None:
    coordinator = _Coordinator()
    assignment = {
        "assignment_id": "assignment-1", "profile_id": "subagent.explorer",
        "profile_revision": 1, "task": "inspect bounded facts",
        "budget": {"model_calls": 1, "tool_calls": 1, "input_tokens": 1, "output_tokens": 1, "wall_time_ms": 1},
        "capability_ids": ["agent.list"],
        "expert": {"expert_id": "expert-1", "task_intents": ["review"], "budget": "small"},
        "skill": {"skill_ids": ["skill-1"]},
    }
    result = AgentCapabilityProvider(coordinator=coordinator, capability_id=AGENT_PLAN_CAPABILITY).invoke(
        _request(AGENT_PLAN_CAPABILITY, {"mode": "cluster", "plan_id": "plan-1", "cluster_id": "cluster-1", "assignments": [assignment]})
    )
    assert coordinator.calls[-1][0] == "plan"
    assert coordinator.calls[-1][1]["arguments"]["assignments"] == [assignment]
    assert result["operation_receipt"]["capability_id"] == AGENT_PLAN_CAPABILITY


@pytest.mark.parametrize("arguments", [
    {"mode": "main_only", "plan_id": "plan-1", "cluster_id": "no"},
    {"mode": "cluster", "plan_id": "plan-1", "cluster_id": "cluster-1", "assignments": []},
    {"mode": "cluster", "plan_id": "plan-1", "cluster_id": "cluster-1", "assignments": [{"provider": "x"}]},
])
def test_plan_rejects_invalid_or_sensitive_shapes_before_dispatch(arguments: Mapping[str, object]) -> None:
    coordinator = _Coordinator()
    with pytest.raises(ToolProviderFailure) as error:
        AgentCapabilityProvider(coordinator=coordinator, capability_id=AGENT_PLAN_CAPABILITY).invoke(_request(AGENT_PLAN_CAPABILITY, arguments))
    assert error.value.effect_certainty == "confirmed_none"
    assert coordinator.calls == []
