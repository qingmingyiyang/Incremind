from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from core.ai_kernel import (
    AIKernelRuntimeError,
    CapabilityDefinition,
    CapabilityRegistryError,
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    MemoryRecallCapability,
    NestedModelHandleUnavailable,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
    ToolExecutionBoundaryDecision,
    ToolProviderFailure,
)
from core.ai_kernel.codex_hook_parity import HookEvent, HookRun
from core.ai_kernel.codex_hook_runtime import (
    CodexHookHost,
    HookHandlerManifest,
    HookPolicyCatalog,
    HookPolicySnapshot,
    RevisionPinnedHookRunner,
)
from core.ai_kernel.runtime import _RuntimeToolDispatchObserver
from core.ai_kernel.event_store import TurnEventConflict
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.ai_kernel.tool_invocation import ToolInvocationIntent, build_intent, intent_to_payload
from core.search_and_recall import RecallHit
from core.ai_tooling import ToolConnectionIdentity, ToolDefinition, ToolRetryPolicy, tool_contract_identity
from core.effect_log import EffectClass, EffectIntent, EffectLog, EffectPurpose, EffectReaper, EffectRunner, EffectState


ROOT = Path(__file__).resolve().parents[2]


class _Recall:
    def __init__(self) -> None:
        self.calls = 0

    def recall(self, query):
        self.calls += 1
        assert query.project_id == "project-alpha"
        return (RecallHit("atom-1", "atom", "evidence", ("source-1",), "verified", 0.9),)


class _Planner:
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        if any(event["type"] == "tool.completed" for event in events):
            completed = next(event for event in reversed(events) if event["type"] == "tool.completed")
            recalled = payloads.get(completed["data"]["payload_ref"])
            assert recalled[0]["content"] == "evidence"
            return {"type": "complete", "summary": "answer ready", "evidence_refs": ["crp://default/memory/atom-1"]}
        assert [item.capability_id for item in capabilities] == ["memory.recall"]
        return {"type": "tool", "capability_id": "memory.recall", "arguments": {"query": request["input"]["text"]}}


class _WritePlanner:
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        if any(event["type"] == "tool.completed" for event in events):
            return {"type": "complete", "summary": "write complete"}
        return {"type": "tool", "capability_id": "document.draft", "arguments": {}}


class _WriteProvider:
    def invoke(self, request):
        return {"summary": "draft created", "receipt_ref": "crp://default/operations/op-write-00000001", "evidence_refs": []}


class _FailingPlanner:
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        raise ValueError("provider output is invalid")


class _CooperativePlanner:
    def __init__(self) -> None:
        self.started = Event()

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        assert execution_control is not None
        execution_control.model_call_started(provider="openai", model="test-model")
        self.started.set()
        while True:
            execution_control.checkpoint()
            time.sleep(0.005)


class _DeadlinePlanner:
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        assert execution_control is not None
        execution_control.model_call_started(provider="openai", model="test-model")
        time.sleep(0.02)
        execution_control.checkpoint()
        raise AssertionError("deadline checkpoint must stop the planner")


class _RecoveredProviderFailurePlanner:
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        assert execution_control is not None
        execution_control.model_call_started(provider="openai", model="fallback-model")
        execution_control.model_call_failed()
        return {"type": "complete", "summary": "local fallback completed"}


class _PostprocessingFailurePlanner:
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        assert execution_control is not None
        execution_control.model_call_started(provider="openai", model="completed-model")
        execution_control.model_call_completed(usage={
            "input_tokens": 9, "output_tokens": 4, "total_tokens": 13,
        })
        raise ValueError("planner output validation failed")


class _DurableWireAttemptPlanner:
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        assert execution_control is not None
        execution_control.model_call_routed(
            snapshot_ref=f"crp://session/{request['turn_id']}/turn-model-routing-snapshot-v1/frozen",
            snapshot_revision="a" * 64,
            prompt_cache_scope_identity="b" * 64,
            provider="deepseek",
            model="deepseek-chat",
            execution_location="remote",
        )
        attempt = execution_control.begin_model_wire_attempt()
        attempt.succeeded(
            usage={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            cache_observation={"cache_read_input_tokens": 5, "cache_miss_input_tokens": 2},
        )
        execution_control.model_call_started(provider="deepseek", model="deepseek-chat")
        execution_control.model_call_cache_observed(
            observation={"cache_read_input_tokens": 5, "cache_miss_input_tokens": 2}
        )
        execution_control.model_call_completed(
            usage={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
        )
        return {"type": "complete", "summary": "wire audited"}


class _AttemptFailurePlanner:
    def __init__(self) -> None:
        self.provider_calls = 0
        self.after_terminal = False

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        assert execution_control is not None
        execution_control.model_call_routed(
            snapshot_ref=f"crp://session/{request['turn_id']}/turn-model-routing-snapshot-v1/frozen",
            snapshot_revision="a" * 64,
            prompt_cache_scope_identity="b" * 64,
            provider="deepseek",
            model="deepseek-chat",
            execution_location="remote",
        )
        attempt = execution_control.begin_model_wire_attempt()
        self.provider_calls += 1
        attempt.succeeded(usage={}, cache_observation=None)
        self.after_terminal = True
        return {"type": "complete", "summary": "must not complete"}


class _FailingAttemptPayloadStore(InMemoryTurnPayloadStore):
    def __init__(self, failed_kind: str) -> None:
        super().__init__()
        self.failed_kind = failed_kind

    def put(self, turn_id, kind, payload):
        if kind == self.failed_kind:
            raise OSError(f"{kind} persistence failed")
        return super().put(turn_id, kind, payload)


class _FailingAttemptTerminalEventStore(InMemoryTurnEventStore):
    def append(self, event, *, expected_sequence, run_lease=None):
        if event.get("type") == "model.attempt.terminal":
            raise OSError("model attempt terminal event persistence failed")
        return super().append(
            event,
            expected_sequence=expected_sequence,
            run_lease=run_lease,
        )


class _NativeToolPlanner:
    def plan(self, request, events, capabilities, payloads, execution_control=None):
        if any(event["type"] == "tool.completed" for event in events):
            return {"type": "complete", "summary": "native tool completed"}
        return {"type": "tool", "capability_id": "calendar.read", "arguments": {}}


class _NativeReadProvider:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        return {"summary": "calendar read", "result": {"events": []}, "evidence_refs": []}


class _FailingWriteProvider:
    def invoke(self, request):
        raise ValueError("generation baseline is stale")


class _CooperativeReadProvider:
    def __init__(self) -> None:
        self.started = Event()

    def invoke(self, request):
        context = request["execution_context"]
        self.started.set()
        while True:
            context.checkpoint()
            time.sleep(0.005)


class _UnconfirmedWriteProvider:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def invoke(self, request):
        self.started.set()
        assert self.release.wait(timeout=2)
        raise OSError("connection lost after request submission")


class _TransientReadProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def invoke(self, request):
        self.calls.append(dict(request))
        if len(self.calls) == 1:
            raise TimeoutError("temporary read timeout")
        return {
            "summary": "read recovered",
            "result": [{"content": "evidence"}],
            "evidence_refs": ["crp://default/memory/atom-1"],
        }


class _ClassifiedFailureProvider:
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        self.calls = 0

    def invoke(self, _request):
        self.calls += 1
        raise ToolProviderFailure(
            self.error_code,
            effect_certainty="confirmed_none",
        )


class _OrdinaryWriteFailureProvider:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, _request):
        self.calls += 1
        raise RuntimeError("write transport failed after dispatch")


def test_turn_executes_memory_recall_once_and_replays_idempotently() -> None:
    recalls = _Recall()
    registry = ScopedCapabilityRegistry()
    registry.register(_definition("memory.recall"), MemoryRecallCapability(recalls))
    payloads = InMemoryTurnPayloadStore()
    runtime = SynchronousAIRuntime(planner=_Planner(), registry=registry, events=InMemoryTurnEventStore(), payloads=payloads)
    request = _request()

    receipt = runtime.submit_turn(request)
    replay = runtime.submit_turn(request)

    assert receipt.status == "completed"
    assert replay.turn_id == receipt.turn_id and replay.replayed is True
    assert recalls.calls == 1
    events = tuple(runtime.events_after(receipt.turn_id))
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert [event["type"] for event in events][-2:] == ["model.completed", "turn.completed"]
    assert events[-1]["data"]["evidence_refs"] == ["crp://default/memory/atom-1"]
    model_event = events[-2]
    assert model_event["data"]["receipt_ref"] is None


def test_runtime_persists_wire_attempt_before_logical_model_terminal() -> None:
    payloads = InMemoryTurnPayloadStore()
    runtime = SynchronousAIRuntime(
        planner=_DurableWireAttemptPlanner(),
        registry=ScopedCapabilityRegistry(),
        events=InMemoryTurnEventStore(),
        payloads=payloads,
    )

    receipt = runtime.submit_turn(_request())
    events = tuple(runtime.events_after(receipt.turn_id))
    types = [event["type"] for event in events]

    assert receipt.status == "completed"
    assert types.index("model.routed") < types.index("model.attempt.dispatched")
    assert types.index("model.attempt.dispatched") < types.index("model.attempt.terminal")
    assert types.index("model.attempt.terminal") < types.index("model.completed")
    dispatched = next(event for event in events if event["type"] == "model.attempt.dispatched")
    terminal = next(event for event in events if event["type"] == "model.attempt.terminal")
    dispatch_payload = payloads.get(dispatched["data"]["payload_ref"])
    attempt_receipt = payloads.get(terminal["data"]["receipt_ref"])
    assert dispatch_payload["attempt_id"] == attempt_receipt["attempt_id"]
    assert dispatch_payload["execution_location"] == "remote"
    assert attempt_receipt["execution_location"] == "remote"
    assert attempt_receipt["attempt_number"] == 1
    assert attempt_receipt["status"] == "succeeded"
    assert attempt_receipt["usage"] == {
        "input_tokens": 7, "output_tokens": 3, "total_tokens": 10,
    }
    assert attempt_receipt["cache_metadata"] == {
        "source_format": "provider_usage",
        "cache_read_input_tokens": 5,
        "cache_write_input_tokens": None,
        "uncached_input_tokens": 2,
    }
    logical = next(event for event in events if event["type"] == "model.completed")
    assert terminal["data"]["receipt_ref"] in logical["data"]["evidence_refs"]


def test_runtime_nested_model_factory_creates_distinct_handles_for_distinct_keys() -> None:
    runtime = SynchronousAIRuntime(
        planner=_Planner(),
        registry=ScopedCapabilityRegistry(),
        events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(),
    )
    accepted = runtime.accept_turn(_request())
    tool = _native_mcp_definition("nested-model-host").tool_definition
    assert tool is not None
    intent = build_intent(
        invocation_id="tool-call-nested-handles",
        turn_id=accepted.turn_id,
        step_id="step-nested-handles",
        operation_id="op-nested-handles",
        tool=tool,
        arguments={},
    )
    observer = _RuntimeToolDispatchObserver(
        runtime,
        intent,
        "crp://session/test/tool-intent",
        1,
        _request(),
    )

    first = observer.nested_model_handle_factory(
        invocation_key="video-chunk-1", purpose="aux"
    )
    second = observer.nested_model_handle_factory(
        invocation_key="video-chunk-2", purpose="probe"
    )

    assert first is not second
    assert first.control.model_request_id != second.control.model_request_id
    requested = [
        event for event in runtime.events_after(accepted.turn_id)
        if event["type"] == "model.requested"
        and event["correlation"]["tool_call_id"] == intent.invocation_id
    ]
    assert [event["correlation"]["model_request_id"] for event in requested] == [
        first.control.model_request_id,
        second.control.model_request_id,
    ]
    assert [event["data"]["model_call_purpose"] for event in requested] == ["aux", "probe"]


@pytest.mark.parametrize(
    ("failed_kind", "provider_calls", "has_dispatch"),
    [
        ("model-wire-attempt-dispatch", 0, False),
        ("model-wire-attempt-receipt", 1, True),
    ],
)
def test_runtime_attempt_persistence_failure_never_returns_business_success(
    failed_kind: str,
    provider_calls: int,
    has_dispatch: bool,
) -> None:
    planner = _AttemptFailurePlanner()
    runtime = SynchronousAIRuntime(
        planner=planner,
        registry=ScopedCapabilityRegistry(),
        events=InMemoryTurnEventStore(),
        payloads=_FailingAttemptPayloadStore(failed_kind),
    )

    receipt = runtime.submit_turn(_request())
    types = [event["type"] for event in runtime.events_after(receipt.turn_id)]

    assert receipt.status == "failed"
    assert planner.provider_calls == provider_calls
    assert planner.after_terminal is False
    assert ("model.attempt.dispatched" in types) is has_dispatch
    assert "model.attempt.terminal" not in types
    assert types[-2:] == ["model.failed", "turn.failed"]


def test_runtime_terminal_event_append_failure_never_returns_business_success() -> None:
    planner = _AttemptFailurePlanner()
    events = _FailingAttemptTerminalEventStore()
    runtime = SynchronousAIRuntime(
        planner=planner,
        registry=ScopedCapabilityRegistry(),
        events=events,
        payloads=InMemoryTurnPayloadStore(),
    )

    receipt = runtime.submit_turn(_request())
    types = [event["type"] for event in events.events_after(receipt.turn_id)]

    assert receipt.status == "failed"
    assert planner.provider_calls == 1
    assert planner.after_terminal is False
    assert "model.attempt.dispatched" in types
    assert "model.attempt.terminal" not in types
    assert types[-2:] == ["model.failed", "turn.failed"]


def test_completed_turn_rejects_late_cancel_without_second_terminal_event() -> None:
    registry = ScopedCapabilityRegistry()
    registry.register(_definition("memory.recall"), MemoryRecallCapability(_Recall()))
    runtime = SynchronousAIRuntime(
        planner=_Planner(),
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(),
    )
    completed = runtime.submit_turn(_request())
    cancel = _cancel_action(completed.turn_id, completed.current_sequence, "cancel-completed-turn-0001")

    with pytest.raises(AIKernelRuntimeError, match="terminal turn cannot cancel"):
        runtime.apply_action(cancel)

    events = tuple(runtime.events_after(completed.turn_id))
    assert sum(event["type"].startswith("turn.") and event["data"]["status"] in {"completed", "failed", "cancelled"} for event in events) == 1


def test_runtime_intent_freezes_native_mcp_execution_contract() -> None:
    tool = ToolDefinition(
        "calendar.read", 2, "Read calendar", "Read events from MCP",
        "mcp", "calendar-server", "read", ("calendar_event",), "mcp",
        "crp://input", "crp://output", None, "read_only", "parallel",
        ("mcp:calendar-server",), "never_retry", ToolRetryPolicy(1, 0, ()),
        None, None, "read_only", "remote", ("calendar-server",),
        ("calendar_event",), 12_000, ("calendar.read",), ("mcp_server_enabled",),
        connection_identity=ToolConnectionIdentity(
            "mcp", "calendar-server", "2025-11-25", 2,
            "calendar-local", "personal-calendar", 1, 1, 1,
        ),
    )
    definition = CapabilityDefinition(
        "calendar.read", 2, "read", False, "read_only",
        "crp://input", "crp://output", tool,
    )
    registry = ScopedCapabilityRegistry()
    registry.register(definition, _NativeReadProvider())
    payloads = InMemoryTurnPayloadStore()
    runtime = SynchronousAIRuntime(
        planner=_NativeToolPlanner(), registry=registry,
        events=InMemoryTurnEventStore(), payloads=payloads,
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["calendar.read"], "denied": [], "require_approval": [],
    }

    completed = runtime.submit_turn(request)

    assert completed.status == "completed"
    intent_event = next(
        event for event in runtime.events_after(completed.turn_id)
        if event["type"] == "tool.intent.recorded"
    )
    intent = payloads.get(intent_event["data"]["payload_ref"])
    assert intent["timeout_ms"] == 12_000
    assert intent["idempotency"] == "never_retry"
    assert intent["max_attempts"] == 1
    assert intent["resource_locks"] == ["mcp:calendar-server"]
    assert intent["tool_contract"]["source"] == "mcp"
    assert intent["tool_contract"]["owner_id"] == "calendar-server"
    assert intent["tool_contract"]["destination"] == "mcp"
    assert intent["tool_contract"]["execution_mode"] == "parallel"
    assert intent["tool_contract"]["resource_locks"] == ["mcp:calendar-server"]
    assert intent["tool_contract"]["idempotency"] == "never_retry"
    assert intent["tool_contract"]["retry_policy"] == {
        "max_attempts": 1,
        "backoff_ms": 0,
        "retryable_error_codes": [],
    }
    assert intent["tool_contract"]["timeout_ms"] == 12_000
    assert intent["requires_approval"] is False
    schema = json.loads(
        (ROOT / "core-contracts" / "ai" / "tool-invocation-intent.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert not tuple(Draft202012Validator(schema).iter_errors(intent))


def test_runtime_enforces_contract_bound_nested_model_handle_budget() -> None:
    class _TwoHandleProvider:
        def __init__(self) -> None:
            self.handles = 0

        def invoke(self, request):
            control = request["execution_context"]
            control.take_nested_model_handle(invocation_key="stage-1", purpose="aux")
            control.take_nested_model_handle(invocation_key="stage-2", purpose="probe")
            self.handles = 2
            return {"summary": "two handles allocated", "receipt_ref": "crp://default/operations/two-handles", "evidence_refs": []}

    class _TwoHandlePlanner:
        def plan(self, request, events, capabilities, payloads, execution_control=None):
            if any(event["type"] == "tool.completed" for event in events):
                return {"type": "complete", "summary": "done"}
            return {"type": "tool", "capability_id": "model.pipeline", "arguments": {}}

    base = CapabilityDefinition(
        "model.pipeline", 1, "external", False, "receipt_required",
        "crp://input", "crp://output",
    )
    from core.ai_tooling import tool_from_capability
    definition = replace(
        base,
        tool_definition=replace(
            tool_from_capability(base), nested_model_handle_budget=2,
        ),
    )
    provider = _TwoHandleProvider()
    registry = ScopedCapabilityRegistry()
    registry.register(definition, provider)
    runtime = SynchronousAIRuntime(
        planner=_TwoHandlePlanner(), registry=registry,
        events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore(),
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["model.pipeline"], "denied": [], "require_approval": [],
    }

    completed = runtime.submit_turn(request)

    assert completed.status == "completed"
    assert provider.handles == 2


def test_runtime_orders_multiple_tools_as_distinct_durable_planner_steps() -> None:
    calls: list[str] = []

    class _OrderedProvider:
        def __init__(self, capability_id: str) -> None:
            self.capability_id = capability_id

        def invoke(self, _request):
            calls.append(self.capability_id)
            return {
                "summary": f"{self.capability_id} completed",
                "result": {"capability_id": self.capability_id},
                "evidence_refs": [],
            }

    class _OrderedPlanner:
        def plan(self, _request, events, _capabilities, _payloads, execution_control=None):
            completed = [
                event["data"]["capability_id"]
                for event in events
                if event["type"] == "tool.completed"
            ]
            if completed == []:
                return {"type": "tool", "capability_id": "ordered.first", "arguments": {}}
            if completed == ["ordered.first"]:
                return {"type": "tool", "capability_id": "ordered.second", "arguments": {}}
            assert completed == ["ordered.first", "ordered.second"]
            return {"type": "complete", "summary": "ordered work completed"}

    registry = ScopedCapabilityRegistry()
    for capability_id in ("ordered.first", "ordered.second"):
        registry.register(_definition(capability_id), _OrderedProvider(capability_id))
    runtime = SynchronousAIRuntime(
        planner=_OrderedPlanner(), registry=registry,
        events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore(),
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["ordered.first", "ordered.second"],
        "denied": [], "require_approval": [],
    }

    completed = runtime.submit_turn(request)

    assert completed.status == "completed"
    assert calls == ["ordered.first", "ordered.second"]
    events = tuple(runtime.events_after(completed.turn_id))
    intents = [event for event in events if event["type"] == "tool.intent.recorded"]
    outcomes = [event for event in events if event["type"] == "tool.outcome.recorded"]
    terminals = [event for event in events if event["type"] == "tool.completed"]
    assert [event["data"]["capability_id"] for event in intents] == calls
    assert [event["data"]["capability_id"] for event in outcomes] == calls
    assert [event["data"]["capability_id"] for event in terminals] == calls
    assert len({event["correlation"]["step_id"] for event in intents}) == 2
    assert intents[0]["sequence"] < terminals[0]["sequence"] < intents[1]["sequence"] < terminals[1]["sequence"]


@pytest.mark.parametrize("third_party_source", ["plugin", "mcp"])
def test_runtime_denies_nested_model_handle_to_third_party_tool_sources(
    third_party_source: str,
) -> None:
    class _DeniedHandleProvider:
        def __init__(self) -> None:
            self.denied = False

        def invoke(self, request):
            with pytest.raises(
                NestedModelHandleUnavailable,
                match="nested model handle is unavailable",
            ):
                request["execution_context"].take_nested_model_handle()
            self.denied = True
            return {"summary": "third-party handle denied", "result": {}, "evidence_refs": []}

    if third_party_source == "mcp":
        definition = _native_mcp_definition("calendar-server")
    else:
        base = _definition("calendar.read")
        from core.ai_tooling import tool_from_capability
        definition = replace(
            base,
            tool_definition=replace(
                tool_from_capability(base),
                source="plugin",
                owner_id="plugin.test",
            ),
        )
    provider = _DeniedHandleProvider()
    registry = ScopedCapabilityRegistry()
    registry.register(definition, provider)
    runtime = SynchronousAIRuntime(
        planner=_NativeToolPlanner(), registry=registry,
        events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore(),
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["calendar.read"], "denied": [], "require_approval": [],
    }

    completed = runtime.submit_turn(request)

    assert completed.status == "completed"
    assert provider.denied is True


def test_exact_capability_request_uses_governed_tool_path_without_planner_events() -> None:
    class _NeverPlanner:
        def plan(self, *_args, **_kwargs):
            raise AssertionError("exact capability request must not invoke the planner")

    provider = _NativeReadProvider()
    registry = ScopedCapabilityRegistry()
    registry.register(_native_mcp_definition("calendar-server"), provider)
    runtime = SynchronousAIRuntime(
        planner=_NeverPlanner(),
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(),
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["calendar.read"], "denied": [], "require_approval": [],
    }
    request["capability_request"] = {
        "mode": "execute_exact_v1",
        "capability_id": "calendar.read",
        "arguments": {"calendar_id": "personal"},
    }

    completed = runtime.submit_turn(request)
    events = tuple(runtime.events_after(completed.turn_id))

    assert completed.status == "completed"
    assert provider.calls == 1
    assert [event["type"] for event in events if event["type"].startswith("model.")] == []
    assert [event["type"] for event in events if event["type"] == "tool.requested"] == ["tool.requested"]
    assert events[-1]["type"] == "turn.completed"


def test_exact_capability_request_preserves_approval_before_provider_execution() -> None:
    class _NeverPlanner:
        def plan(self, *_args, **_kwargs):
            raise AssertionError("exact capability request must not invoke the planner")

    provider = _NativeReadProvider()
    registry = ScopedCapabilityRegistry()
    registry.register(_native_mcp_definition("calendar-server", requires_approval=True), provider)
    runtime = SynchronousAIRuntime(
        planner=_NeverPlanner(),
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(),
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["calendar.read"], "denied": [], "require_approval": ["calendar.read"],
    }
    request["capability_request"] = {
        "mode": "execute_exact_v1",
        "capability_id": "calendar.read",
        "arguments": {},
    }

    waiting = runtime.submit_turn(request)
    approval = tuple(runtime.events_after(waiting.turn_id))[-1]
    assert waiting.status == "waiting_approval"
    assert provider.calls == 0
    completed = runtime.apply_action({
        "schema_version": "1.0.0",
        "action_id": "action-exact-capability-0000000000001",
        "turn_id": waiting.turn_id,
        "type": "approve",
        "target_event_id": approval["event_id"],
        "reason": "approved",
        "actor": "user",
        "expected_sequence": waiting.current_sequence,
        "idempotency_key": "approve-exact-capability-0001",
        "created_at": "2026-08-26T00:00:00Z",
    })

    assert completed.status == "completed"
    assert provider.calls == 1
    assert not any(event["type"].startswith("model.") for event in runtime.events_after(waiting.turn_id))


@pytest.mark.parametrize("exact", [False, True])
def test_planner_and_exact_requests_share_boundary_deny_path_without_provider(exact: bool) -> None:
    class _DenyBoundary:
        def __init__(self) -> None:
            self.calls = 0

        def evaluate(self, _request, _capability, _decision):
            self.calls += 1
            return ToolExecutionBoundaryDecision(
                "deny", ("test_deny",), (), 1, False, False, {},
            )

    class _RecordingRuntime(SynchronousAIRuntime):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.shared_tool_path_calls = 0

        def _execute_tool_decision(self, *args, **kwargs):
            self.shared_tool_path_calls += 1
            return super()._execute_tool_decision(*args, **kwargs)

    provider = _NativeReadProvider()
    boundary = _DenyBoundary()
    registry = ScopedCapabilityRegistry()
    registry.register(_native_mcp_definition("calendar-server"), provider)
    runtime = _RecordingRuntime(
        planner=_NativeToolPlanner(), registry=registry,
        events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore(),
        execution_boundary=boundary,
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["calendar.read"], "denied": [], "require_approval": [],
    }
    if exact:
        request["capability_request"] = {
            "mode": "execute_exact_v1", "capability_id": "calendar.read", "arguments": {},
        }

    receipt = runtime.submit_turn(request)

    assert receipt.status == "failed"
    assert runtime.shared_tool_path_calls == 1
    assert boundary.calls == 1
    assert provider.calls == 0


@pytest.mark.parametrize("exact", [False, True])
def test_shared_tool_path_pre_tool_hook_deny_never_reaches_provider(exact: bool) -> None:
    snapshot = HookPolicySnapshot(
        "hook-policy-v1",
        (HookHandlerManifest("pre-tool", "handler-v1", HookEvent.PRE_TOOL_USE, 0),),
    )
    denied = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "local policy",
        }
    }
    host = CodexHookHost(
        catalog=HookPolicyCatalog(snapshot),
        runner=RevisionPinnedHookRunner({
            ("pre-tool", "handler-v1"): lambda manifest, _payload: HookRun(
                manifest.config_order, 0, True, stdout=json.dumps(denied), hook_id=manifest.hook_id,
            ),
        }),
    )
    provider = _NativeReadProvider()
    registry = ScopedCapabilityRegistry()
    registry.register(_native_mcp_definition("calendar-server"), provider)
    authorization_checks: list[object] = []
    runtime = SynchronousAIRuntime(
        planner=_NativeToolPlanner(), registry=registry,
        events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore(),
        hook_host=host,
        frozen_hook_authorization_check=lambda *args: authorization_checks.append(args) or True,
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["calendar.read"], "denied": [], "require_approval": [],
    }
    if exact:
        request["capability_request"] = {
            "mode": "execute_exact_v1", "capability_id": "calendar.read", "arguments": {},
        }

    receipt = runtime.submit_turn(request)

    assert receipt.status == "failed"
    assert authorization_checks == []
    assert provider.calls == 0


def test_approval_rejects_same_version_mcp_server_identity_drift() -> None:
    original_provider = _NativeReadProvider()
    replacement_provider = _NativeReadProvider()
    registry = ScopedCapabilityRegistry()
    lease = registry.register(
        _native_mcp_definition("calendar-server"), original_provider,
    )
    runtime = SynchronousAIRuntime(
        planner=_NativeToolPlanner(), registry=registry,
        events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore(),
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["calendar.read"], "denied": [],
        "require_approval": ["calendar.read"],
    }
    waiting = runtime.submit_turn(request)
    approval_event = tuple(runtime.events_after(waiting.turn_id))[-1]
    lease.close()
    registry.register(
        _native_mcp_definition("different-calendar-server"), replacement_provider,
    )
    approval = {
        "schema_version": "1.0.0",
        "action_id": "action-dddddddddddddddddddddddddddddddd",
        "turn_id": waiting.turn_id,
        "type": "approve",
        "target_event_id": approval_event["event_id"],
        "reason": "approved",
        "actor": "user",
        "expected_sequence": waiting.current_sequence,
        "idempotency_key": "approve-native-mcp-drift-0001",
        "created_at": "2026-08-24T05:00:02Z",
    }

    failed = runtime.apply_action(approval)

    assert failed.status == "failed"
    assert original_provider.calls == replacement_provider.calls == 0
    assert tuple(runtime.events_after(waiting.turn_id))[-1]["data"]["error_code"] == "ai.execution_failed"


def test_approval_rejects_same_identity_execution_and_approval_drift() -> None:
    original_provider = _NativeReadProvider()
    replacement_provider = _NativeReadProvider()
    registry = ScopedCapabilityRegistry()
    lease = registry.register(
        _native_mcp_definition(
            "calendar-server",
            timeout_ms=12_000,
            resource_lock="mcp:calendar-server",
            requires_approval=True,
        ),
        original_provider,
    )
    runtime = SynchronousAIRuntime(
        planner=_NativeToolPlanner(), registry=registry,
        events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore(),
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["calendar.read"], "denied": [], "require_approval": [],
    }
    waiting = runtime.submit_turn(request)
    approval_event = tuple(runtime.events_after(waiting.turn_id))[-1]
    lease.close()
    registry.register(
        _native_mcp_definition(
            "calendar-server",
            timeout_ms=90_000,
            resource_lock="mcp:replacement-lock",
            requires_approval=False,
        ),
        replacement_provider,
    )

    failed = runtime.apply_action({
        "schema_version": "1.0.0",
        "action_id": "action-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "turn_id": waiting.turn_id,
        "type": "approve",
        "target_event_id": approval_event["event_id"],
        "reason": "approved",
        "actor": "user",
        "expected_sequence": waiting.current_sequence,
        "idempotency_key": "approve-native-execution-drift-0001",
        "created_at": "2026-08-24T05:00:02Z",
    })

    assert failed.status == "failed"
    assert original_provider.calls == replacement_provider.calls == 0
    assert tuple(runtime.events_after(waiting.turn_id))[-1]["data"]["error_code"] == "ai.execution_failed"


def test_approval_rejects_native_tool_version_drift() -> None:
    original_provider = _NativeReadProvider()
    replacement_provider = _NativeReadProvider()
    registry = ScopedCapabilityRegistry()
    lease = registry.register(
        _native_mcp_definition("calendar-server", version=2), original_provider,
    )
    runtime = SynchronousAIRuntime(
        planner=_NativeToolPlanner(), registry=registry,
        events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore(),
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["calendar.read"], "denied": [],
        "require_approval": ["calendar.read"],
    }
    waiting = runtime.submit_turn(request)
    approval_event = tuple(runtime.events_after(waiting.turn_id))[-1]
    lease.close()
    registry.register(
        _native_mcp_definition("calendar-server", version=3), replacement_provider,
    )

    failed = runtime.apply_action({
        "schema_version": "1.0.0",
        "action_id": "action-ffffffffffffffffffffffffffffffff",
        "turn_id": waiting.turn_id,
        "type": "approve",
        "target_event_id": approval_event["event_id"],
        "reason": "approved",
        "actor": "user",
        "expected_sequence": waiting.current_sequence,
        "idempotency_key": "approve-native-version-drift-0001",
        "created_at": "2026-08-24T05:00:02Z",
    })

    assert failed.status == "failed"
    assert original_provider.calls == replacement_provider.calls == 0


def test_approval_rejects_same_owner_mcp_connection_identity_drift() -> None:
    original_provider = _NativeReadProvider()
    replacement_provider = _NativeReadProvider()
    registry = ScopedCapabilityRegistry()
    lease = registry.register(
        _native_mcp_definition(
            "calendar-server", endpoint_identity="calendar-local",
        ),
        original_provider,
    )
    runtime = SynchronousAIRuntime(
        planner=_NativeToolPlanner(), registry=registry,
        events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore(),
    )
    request = _request()
    request["capability_policy"] = {
        "allowed": ["calendar.read"], "denied": [],
        "require_approval": ["calendar.read"],
    }
    waiting = runtime.submit_turn(request)
    approval_event = tuple(runtime.events_after(waiting.turn_id))[-1]
    lease.close()
    registry.register(
        _native_mcp_definition(
            "calendar-server", endpoint_identity="calendar-reconfigured",
        ),
        replacement_provider,
    )

    failed = runtime.apply_action({
        "schema_version": "1.0.0",
        "action_id": "action-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "turn_id": waiting.turn_id,
        "type": "approve",
        "target_event_id": approval_event["event_id"],
        "reason": "approved",
        "actor": "user",
        "expected_sequence": waiting.current_sequence,
        "idempotency_key": "approve-native-connection-drift-0001",
        "created_at": "2026-08-24T05:00:02Z",
    })

    assert failed.status == "failed"
    assert original_provider.calls == replacement_provider.calls == 0


def test_registry_lease_revokes_capability_and_rejects_duplicate_identity() -> None:
    registry = ScopedCapabilityRegistry()
    lease = registry.register(_definition("memory.recall"), MemoryRecallCapability(_Recall()))
    with pytest.raises(CapabilityRegistryError, match="already registered"):
        registry.register(_definition("memory.recall"), MemoryRecallCapability(_Recall()))
    lease.close()
    assert registry.resolve("memory.recall") is None


def test_write_tool_waits_for_matching_human_approval_and_requires_receipt() -> None:
    registry = ScopedCapabilityRegistry()
    registry.register(
        CapabilityDefinition("document.draft", 1, "write", True, "receipt_required", "crp://default/contracts/in.schema.json", "crp://default/contracts/out.schema.json"),
        _WriteProvider(),
    )
    runtime = SynchronousAIRuntime(planner=_WritePlanner(), registry=registry, events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore())
    request = _request()
    request["capability_policy"] = {"allowed": ["document.draft"], "denied": [], "require_approval": ["document.draft"]}
    waiting = runtime.submit_turn(request)
    assert waiting.status == "waiting_approval"
    last = tuple(runtime.events_after(waiting.turn_id))[-1]
    action = {"schema_version": "1.0.0", "action_id": "action-0123456789abcdef0123456789abcdef", "turn_id": waiting.turn_id, "type": "approve", "target_event_id": last["event_id"], "reason": "approved", "actor": "user", "expected_sequence": waiting.current_sequence, "idempotency_key": "approve-document-0001", "created_at": "2026-08-23T05:00:00Z"}
    assert runtime.apply_action(action).status == "completed"

    replay = runtime.apply_action(action)
    assert replay.status == "completed" and replay.replayed is True

    conflicting = dict(action)
    conflicting["idempotency_key"] = "approve-document-0002"
    with pytest.raises(AIKernelRuntimeError, match="sequence conflict"):
        runtime.apply_action(conflicting)


def test_planner_failure_is_durable_terminal_and_replays_without_running_zombie() -> None:
    payloads = InMemoryTurnPayloadStore()
    runtime = SynchronousAIRuntime(
        planner=_FailingPlanner(),
        registry=ScopedCapabilityRegistry(),
        events=InMemoryTurnEventStore(),
        payloads=payloads,
    )
    request = _request()

    failed = runtime.submit_turn(request)
    replay = runtime.submit_turn(request)

    assert failed.status == "failed"
    assert replay.status == "failed" and replay.replayed is True
    events = tuple(runtime.events_after(failed.turn_id))
    assert events[-2]["type"] == "model.failed"
    assert events[-2]["data"]["receipt_ref"] is None
    assert events[-1]["type"] == "turn.failed"
    assert events[-1]["data"]["error_code"] == "ai.execution_failed"


def test_provider_failure_can_fallback_while_model_receipt_remains_failed() -> None:
    payloads = InMemoryTurnPayloadStore()
    runtime = SynchronousAIRuntime(
        planner=_RecoveredProviderFailurePlanner(),
        registry=ScopedCapabilityRegistry(),
        events=InMemoryTurnEventStore(),
        payloads=payloads,
    )

    completed = runtime.submit_turn(_request())

    assert completed.status == "completed"
    events = tuple(runtime.events_after(completed.turn_id))
    assert [event["type"] for event in events][-2:] == ["model.failed", "turn.completed"]
    receipt = payloads.get(events[-2]["data"]["receipt_ref"])
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "ai.model_call_failed"


def test_completed_model_call_is_not_relabelled_when_planner_postprocessing_fails() -> None:
    payloads = InMemoryTurnPayloadStore()
    runtime = SynchronousAIRuntime(
        planner=_PostprocessingFailurePlanner(),
        registry=ScopedCapabilityRegistry(),
        events=InMemoryTurnEventStore(),
        payloads=payloads,
    )

    failed = runtime.submit_turn(_request())

    assert failed.status == "failed"
    events = tuple(runtime.events_after(failed.turn_id))
    assert [event["type"] for event in events][-2:] == ["model.completed", "turn.failed"]
    receipt = payloads.get(events[-2]["data"]["receipt_ref"])
    assert receipt["status"] == "completed"
    assert receipt["usage"] == {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13}


def test_running_planner_cancel_is_requested_and_blocks_model_completion() -> None:
    planner = _CooperativePlanner()
    payloads = InMemoryTurnPayloadStore()
    runtime = SynchronousAIRuntime(
        planner=planner,
        registry=ScopedCapabilityRegistry(),
        events=InMemoryTurnEventStore(),
        payloads=payloads,
    )
    request = _request()

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(runtime.submit_turn, request)
        assert planner.started.wait(timeout=1)
        events = tuple(runtime.events_after(str(request["turn_id"])))
        cancel = _cancel_action(str(request["turn_id"]), len(events), "cancel-running-planner-0001")
        requested = runtime.apply_action(cancel)
        assert requested.status == "running"
        completed = running.result(timeout=2)

    assert completed.status == "cancelled"
    events = tuple(runtime.events_after(str(request["turn_id"])))
    types = [event["type"] for event in events]
    assert types[-3:] == ["turn.cancel.requested", "model.cancelled", "turn.cancelled"]
    assert "model.completed" not in types
    assert payloads.get(events[-2]["data"]["receipt_ref"])["status"] == "cancelled"
    assert events[-1]["data"]["error_code"] == "ai.planner_cancelled"


def test_planner_deadline_is_terminal_and_blocks_model_completion() -> None:
    payloads = InMemoryTurnPayloadStore()
    runtime = SynchronousAIRuntime(
        planner=_DeadlinePlanner(),
        registry=ScopedCapabilityRegistry(),
        events=InMemoryTurnEventStore(),
        payloads=payloads,
        planner_timeout_ms=5,
    )

    failed = runtime.submit_turn(_request())

    assert failed.status == "failed"
    events = tuple(runtime.events_after(failed.turn_id))
    assert "model.completed" not in [event["type"] for event in events]
    assert events[-2]["type"] == "model.timed_out"
    assert payloads.get(events[-2]["data"]["receipt_ref"])["status"] == "timed_out"
    assert events[-1]["type"] == "turn.failed"
    assert events[-1]["data"]["error_code"] == "ai.planner_timeout"


def test_approved_capability_failure_converges_to_failed_action_receipt() -> None:
    registry = ScopedCapabilityRegistry()
    registry.register(
        CapabilityDefinition("document.draft", 1, "write", True, "receipt_required", "crp://default/contracts/in.schema.json", "crp://default/contracts/out.schema.json"),
        _FailingWriteProvider(),
    )
    runtime = SynchronousAIRuntime(planner=_WritePlanner(), registry=registry, events=InMemoryTurnEventStore(), payloads=InMemoryTurnPayloadStore())
    request = _request()
    request["capability_policy"] = {"allowed": ["document.draft"], "denied": [], "require_approval": ["document.draft"]}
    waiting = runtime.submit_turn(request)
    approval = tuple(runtime.events_after(waiting.turn_id))[-1]
    action = {"schema_version": "1.0.0", "action_id": "action-ffffffffffffffffffffffffffffffff", "turn_id": waiting.turn_id, "type": "approve", "target_event_id": approval["event_id"], "reason": "approved", "actor": "user", "expected_sequence": waiting.current_sequence, "idempotency_key": "approve-document-failure-0001", "created_at": "2026-08-23T05:00:00Z"}

    failed = runtime.apply_action(action)
    replay = runtime.apply_action(action)

    assert failed.status == "failed"
    assert replay.status == "failed" and replay.replayed is True
    assert tuple(runtime.events_after(waiting.turn_id))[-1]["data"]["error_code"] == "ai.stale_baseline"


def test_running_read_cancel_is_requested_then_cooperatively_converges() -> None:
    provider = _CooperativeReadProvider()
    registry = ScopedCapabilityRegistry()
    registry.register(_definition("memory.recall"), provider)
    runtime = SynchronousAIRuntime(
        planner=_Planner(),
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(),
    )
    request = _request()

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(runtime.submit_turn, request)
        assert provider.started.wait(timeout=1)
        events = tuple(runtime.events_after(request["turn_id"]))
        cancel = _cancel_action(str(request["turn_id"]), len(events), "cancel-running-read-0001")
        requested = runtime.apply_action(cancel)
        assert requested.status == "running"
        completed = running.result(timeout=2)

    assert completed.status == "cancelled"
    types = [event["type"] for event in runtime.events_after(str(request["turn_id"]))]
    assert "turn.cancel.requested" in types
    assert types[-3:] == ["tool.outcome.recorded", "tool.cancelled", "turn.cancelled"]
    replay = runtime.apply_action(cancel)
    assert replay.status == "cancelled" and replay.replayed is True


def test_unconfirmed_write_cancel_records_unknown_effect_and_never_claims_cancelled() -> None:
    provider = _UnconfirmedWriteProvider()
    registry = ScopedCapabilityRegistry()
    registry.register(
        CapabilityDefinition("document.draft", 1, "write", True, "receipt_required", "crp://default/contracts/in.schema.json", "crp://default/contracts/out.schema.json"),
        provider,
    )
    runtime = SynchronousAIRuntime(
        planner=_WritePlanner(),
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(),
    )
    request = _request()
    request["capability_policy"] = {"allowed": ["document.draft"], "denied": [], "require_approval": ["document.draft"]}
    waiting = runtime.submit_turn(request)
    approval_event = tuple(runtime.events_after(waiting.turn_id))[-1]
    approval = {
        "schema_version": "1.0.0",
        "action_id": "action-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "turn_id": waiting.turn_id,
        "type": "approve",
        "target_event_id": approval_event["event_id"],
        "reason": "approved",
        "actor": "user",
        "expected_sequence": waiting.current_sequence,
        "idempotency_key": "approve-unknown-write-0001",
        "created_at": "2026-08-24T05:00:00Z",
    }

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(runtime.apply_action, approval)
        assert provider.started.wait(timeout=1)
        events = tuple(runtime.events_after(waiting.turn_id))
        cancel = _cancel_action(waiting.turn_id, len(events), "cancel-unknown-write-0001")
        assert runtime.apply_action(cancel).status == "running"
        provider.release.set()
        failed = running.result(timeout=2)

    assert failed.status == "failed"
    events = tuple(runtime.events_after(waiting.turn_id))
    assert events[-1]["data"]["error_code"] == "ai.tool_cancel_unconfirmed"
    outcome_event = next(event for event in events if event["type"] == "tool.outcome.recorded")
    outcome = runtime._payloads.get(outcome_event["data"]["payload_ref"])
    assert outcome["status"] == "unknown_effect"
    assert not any(event["type"] == "turn.cancelled" for event in events)


def test_transient_read_retry_is_scheduled_only_after_core_reaper(tmp_path: Path) -> None:
    provider = _TransientReadProvider()
    registry = ScopedCapabilityRegistry()
    registry.register(_definition("memory.recall"), provider)
    effects = EffectLog(tmp_path / "tool-effects.sqlite3")
    runtime = SynchronousAIRuntime(
        planner=_Planner(),
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(),
        effect_runner=EffectRunner(effects, owner_id="tool-runner-a", lease_seconds=1),
    )

    running = runtime.submit_turn(_request())
    assert running.status == "running"
    assert len(provider.calls) == 1
    assert EffectReaper(effects).recover_expired(now=2_000_000_000)[0].state.value == "PLANNED"
    completed = runtime.apply_action(
        _resume_action(running.turn_id, running.current_sequence, "resume-tool-retry-0001")
    )

    assert completed.status == "completed"
    assert len(provider.calls) == 2
    assert len({call["tool_call_id"] for call in provider.calls}) == 1
    assert len({call["idempotency_key"] for call in provider.calls}) == 1
    assert [call["attempt"] for call in provider.calls] == [1, 2]
    events = tuple(runtime.events_after(completed.turn_id))
    assert sum(event["type"] == "tool.attempt.failed" for event in events) == 1
    assert sum(event["type"] == "tool.outcome.recorded" for event in events) == 1
    attempt_failure = next(event for event in events if event["type"] == "tool.attempt.failed")
    assert attempt_failure["data"]["error_code"] == "timeout"
    assert attempt_failure["data"]["retryable"] is True
    failure_payload = runtime._payloads.get(attempt_failure["data"]["payload_ref"])
    assert failure_payload["attempt"] == 1
    assert failure_payload["effect_certainty"] == "confirmed_none"
    assert failure_payload["backoff_ms"] == 250


def test_external_fact_projection_commits_result_and_settles_parent_without_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    effects = EffectLog(tmp_path / "effects.sqlite3")
    runner = EffectRunner(effects, owner_id="tool-runner-a", lease_seconds=30)
    projected_calls = 0

    def project(intent, effect):
        nonlocal projected_calls
        projected_calls += 1
        assert intent.invocation_id == effect.operation_id
        return {
            "result": {"value": "recovered"},
            "operation_receipt": {"operation": "plugin_hand", "status": "completed"},
            "evidence_refs": ("plugin-hands-outcome:call-projected-0001:r1",),
        }

    runtime = SynchronousAIRuntime(
        planner=_Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store,
        external_tool_outcome_projector=project, effect_runner=runner,
    )
    request = _request()
    accepted = runtime.accept_turn(request)
    frozen_tool = _native_mcp_definition("example").tool_definition
    assert frozen_tool is not None
    intent = ToolInvocationIntent(
        invocation_id="call-projected-0001", turn_id=accepted.turn_id,
        step_id="step-projected-0001", capability_id="calendar.read",
        capability_version=2, operation_id=str(request["operation_id"]),
        idempotency_key="idem-projected-0001", execution_mode="exclusive",
        resource_locks=(), idempotency="never_retry", max_attempts=1,
        retry_backoff_ms=0, retryable_error_codes=(), timeout_ms=1_000,
        tool_contract=tool_contract_identity(frozen_tool),
        requires_approval=True, arguments={},
    )
    intent_event = runtime._new_event(
        accepted.turn_id, "tool.intent.recorded", "running", "intent",
        capability_id=intent.capability_id, step_id=intent.step_id,
        tool_call_id=intent.invocation_id,
    )
    committed = store.append_intent_bundle(
        intent_event, expected_sequence=1,
        intent_kind="tool-invocation-intent", intent_payload=intent_to_payload(intent),
    )
    planned = runtime._ensure_tool_effect(intent, committed.intent_payload_ref)
    assert planned is not None
    runner.begin_planned(planned.operation_id, now=1, lease_expires_at=2)
    original_settle_ok = runner.settle_ok
    monkeypatch.setattr(
        runner, "settle_ok",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("after outer bundle")),
    )

    with pytest.raises(SystemExit, match="after outer bundle"):
        runtime._resume_incomplete_tool(accepted.turn_id)
    assert projected_calls == 1
    assert effects.get(intent.invocation_id).state is EffectState.INFLIGHT
    events = tuple(store.events_after(accepted.turn_id))
    outcome_event = next(event for event in events if event["type"] == "tool.outcome.recorded")
    outcome = store.get(outcome_event["data"]["payload_ref"])
    assert store.get(outcome["payload_ref"]) == {"value": "recovered"}
    assert not any(event["type"] == "tool.completed" for event in events)

    monkeypatch.setattr(runner, "settle_ok", original_settle_ok)
    recovered = EffectReaper(effects).recover_expired(
        now=3,
        verifiers={effects.get(intent.invocation_id).kind: store.verify_tool_call_effect},
    )
    assert recovered[0].state is EffectState.SETTLED_OK

    def forbidden_projection(_intent, _effect):
        raise AssertionError("restart must consume the outer outcome first")

    restarted = SynchronousAIRuntime(
        planner=_Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store,
        external_tool_outcome_projector=forbidden_projection, effect_runner=runner,
    )
    handled, terminal = restarted._resume_incomplete_tool(accepted.turn_id)
    assert handled is True and terminal is None
    assert sum(
        event["type"] == "tool.completed"
        for event in store.events_after(accepted.turn_id)
    ) == 1

    # A concurrent loser can observe a stale sequence after the winner commits.
    # It must converge on the winner's outcome instead of failing or dispatching.
    loser = SynchronousAIRuntime(
        planner=_Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store,
        external_tool_outcome_projector=project, effect_runner=runner,
    )
    monkeypatch.setattr(
        loser, "_append_tool_outcome_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TurnEventConflict("lost race")),
    )
    stale = replace(effects.get(intent.invocation_id), state=EffectState.INFLIGHT)
    assert loser._project_external_tool_completion(intent, stale) is True
    assert sum(
        event["type"] == "tool.outcome.recorded"
        for event in store.events_after(accepted.turn_id)
    ) == 1


def test_non_allowlisted_read_failure_never_retries() -> None:
    provider = _ClassifiedFailureProvider("authentication_failed")
    registry = ScopedCapabilityRegistry()
    registry.register(_definition("memory.recall"), provider)
    runtime = SynchronousAIRuntime(
        planner=_Planner(),
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(),
    )

    failed = runtime.submit_turn(_request())

    assert failed.status == "failed"
    assert provider.calls == 1
    events = tuple(runtime.events_after(failed.turn_id))
    assert not any(event["type"] == "tool.attempt.failed" for event in events)
    assert events[-1]["data"]["error_code"] == "authentication_failed"


def test_retryable_read_stops_at_frozen_attempt_limit_after_reaper(tmp_path: Path) -> None:
    provider = _ClassifiedFailureProvider("timeout")
    registry = ScopedCapabilityRegistry()
    registry.register(_definition("memory.recall"), provider)
    effects = EffectLog(tmp_path / "tool-effects.sqlite3")
    runtime = SynchronousAIRuntime(
        planner=_Planner(),
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(),
        effect_runner=EffectRunner(effects, owner_id="tool-runner-a", lease_seconds=1),
    )

    running = runtime.submit_turn(_request())
    assert running.status == "running"
    EffectReaper(effects).recover_expired(now=2_000_000_000)
    failed = runtime.apply_action(
        _resume_action(running.turn_id, running.current_sequence, "resume-tool-retry-0002")
    )

    assert failed.status == "failed"
    assert provider.calls == 2
    events = tuple(runtime.events_after(failed.turn_id))
    assert sum(event["type"] == "tool.attempt.failed" for event in events) == 1
    assert events[-1]["data"]["error_code"] == "ai.tool_retry_exhausted"
    outcome_event = next(event for event in events if event["type"] == "tool.outcome.recorded")
    outcome = runtime._payloads.get(outcome_event["data"]["payload_ref"])
    assert outcome["effect_certainty"] == "confirmed_none"


def test_cancel_while_reaper_recovery_is_pending_prevents_next_attempt(tmp_path: Path) -> None:
    provider = _TransientReadProvider()
    registry = ScopedCapabilityRegistry()
    registry.register(_definition("memory.recall"), provider)
    effects = EffectLog(tmp_path / "tool-effects.sqlite3")
    runtime = SynchronousAIRuntime(
        planner=_Planner(),
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(),
        effect_runner=EffectRunner(effects, owner_id="tool-runner-a", lease_seconds=1),
    )
    request = _request()

    result = runtime.submit_turn(request)
    events = tuple(runtime.events_after(str(request["turn_id"])))
    assert any(event["type"] == "tool.attempt.failed" for event in events)
    cancel = _cancel_action(
        str(request["turn_id"]), len(events), "cancel-retry-backoff-0001",
    )
    assert runtime.apply_action(cancel).status == "cancelled"

    assert result.status == "running"
    assert len(provider.calls) == 1


def test_ordinary_write_failure_is_unknown_effect_and_never_retried() -> None:
    provider = _OrdinaryWriteFailureProvider()
    registry = ScopedCapabilityRegistry()
    registry.register(
        CapabilityDefinition("document.draft", 1, "write", True, "receipt_required", "crp://default/contracts/in.schema.json", "crp://default/contracts/out.schema.json"),
        provider,
    )
    runtime = SynchronousAIRuntime(
        planner=_WritePlanner(),
        registry=registry,
        events=InMemoryTurnEventStore(),
        payloads=InMemoryTurnPayloadStore(),
    )
    request = _request()
    request["capability_policy"] = {"allowed": ["document.draft"], "denied": [], "require_approval": ["document.draft"]}
    waiting = runtime.submit_turn(request)
    approval_event = tuple(runtime.events_after(waiting.turn_id))[-1]
    approval = {
        "schema_version": "1.0.0",
        "action_id": "action-cccccccccccccccccccccccccccccccc",
        "turn_id": waiting.turn_id,
        "type": "approve",
        "target_event_id": approval_event["event_id"],
        "reason": "approved",
        "actor": "user",
        "expected_sequence": waiting.current_sequence,
        "idempotency_key": "approve-ordinary-write-failure-0001",
        "created_at": "2026-08-24T05:00:02Z",
    }

    failed = runtime.apply_action(approval)

    assert failed.status == "failed"
    assert provider.calls == 1
    events = tuple(runtime.events_after(waiting.turn_id))
    outcome_event = next(event for event in events if event["type"] == "tool.outcome.recorded")
    outcome = runtime._payloads.get(outcome_event["data"]["payload_ref"])
    assert outcome["status"] == "unknown_effect"
    assert outcome["effect_certainty"] == "unknown"


def _definition(capability_id: str) -> CapabilityDefinition:
    return CapabilityDefinition(capability_id, 1, "read", False, "read_only", "crp://default/contracts/in.schema.json", "crp://default/contracts/out.schema.json")


def _native_mcp_definition(
    owner_id: str,
    *,
    timeout_ms: int = 12_000,
    resource_lock: str | None = None,
    requires_approval: bool = False,
    version: int = 2,
    endpoint_identity: str = "calendar-local",
) -> CapabilityDefinition:
    tool = ToolDefinition(
        "calendar.read", version, "Read calendar", "Read events from MCP",
        "mcp", owner_id, "read", ("calendar_event",), "mcp",
        "crp://input", "crp://output", None, "read_only", "parallel",
        (resource_lock or f"mcp:{owner_id}",), "never_retry", ToolRetryPolicy(1, 0, ()),
        None, None, "read_only", "remote", (owner_id,),
        ("calendar_event",), timeout_ms, ("calendar.read",), ("mcp_server_enabled",),
        connection_identity=ToolConnectionIdentity(
            "mcp", owner_id, "2025-11-25", version,
            endpoint_identity, "personal-calendar", 1, 1, 1,
        ),
    )
    return CapabilityDefinition(
        "calendar.read", version, "read", requires_approval, "read_only",
        "crp://input", "crp://output", tool,
    )


def _request() -> dict[str, object]:
    return json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))


def _cancel_action(turn_id: str, sequence: int, idempotency_key: str) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "action_id": f"action-{uuid4().hex}",
        "turn_id": turn_id,
        "type": "cancel",
        "target_event_id": None,
        "reason": "user requested cancellation",
        "actor": "user",
        "expected_sequence": sequence,
        "idempotency_key": idempotency_key,
        "created_at": "2026-08-24T05:00:01Z",
    }


def _resume_action(turn_id: str, sequence: int, idempotency_key: str) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "action_id": f"action-{uuid4().hex}",
        "turn_id": turn_id,
        "type": "resume",
        "target_event_id": None,
        "reason": "core reaper returned the Effect to planned",
        "actor": "user",
        "expected_sequence": sequence,
        "idempotency_key": idempotency_key,
        "created_at": "2026-08-24T05:00:01Z",
    }
