from __future__ import annotations

import ast
import json
from pathlib import Path

from backend.security.turn_frozen_authorization import TurnFrozenAuthorizationAuthority
from core.ai_kernel import (
    CapabilityDefinition,
    CodexHookHost,
    HookHandlerManifest,
    HookPolicyCatalog,
    HookPolicySnapshot,
    RevisionPinnedHookRunner,
    SQLiteAITurnStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
    ToolExecutionBoundaryDecision,
)
from core.ai_kernel.codex_hook_parity import HookEvent, HookRun
from core.ai_kernel.dispatcher import SynchronousToolDispatcher
from core.effect_log import EffectLog, EffectRunner


ROOT = Path(__file__).resolve().parents[2]


class _Planner:
    def __init__(self, trace: list[str], *, use_tool: bool = True) -> None:
        self._trace = trace
        self._use_tool = use_tool

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        self._trace.append("turn_driver")
        if not self._use_tool or any(item["type"] == "tool.completed" for item in events):
            return {"type": "complete", "summary": "done"}
        return {
            "type": "tool",
            "capability_id": "calendar.read",
            "arguments": {},
        }


class _Provider:
    def __init__(self, trace: list[str]) -> None:
        self._trace = trace

    def invoke(self, request):
        self._trace.append("provider")
        return {"summary": "read", "result": {"events": []}, "evidence_refs": []}


class _Boundary:
    def __init__(self) -> None:
        self.evaluations = 0

    def evaluate(self, request, capability, decision):
        self.evaluations += 1
        return ToolExecutionBoundaryDecision(
            "allow", ("frozen-control-plane",), (), 1, False, False,
            dict(decision.get("arguments", {})),
        )

    def sanitize_candidate_arguments(self, capability, arguments, *, turn_id):
        return dict(arguments)

    def dispatch_fence(self, request):
        raise AssertionError("Hook-enabled hot path must not enter dynamic Boundary fence")


class _TracingRunner(EffectRunner):
    def __init__(self, log: EffectLog, trace: list[str]) -> None:
        super().__init__(log, owner_id="four-layer-gate", lease_seconds=30)
        self._trace = trace
        self.claimed_operation_ids: list[str] = []

    def claim_planned(self, *args, **kwargs):
        self._trace.append("effect_runner")
        self.claimed_operation_ids.append(str(args[0]))
        return super().claim_planned(*args, **kwargs)


class _TracingDispatcher(SynchronousToolDispatcher):
    def __init__(self, trace: list[str]) -> None:
        super().__init__()
        self._trace = trace

    def dispatch(self, provider, request, observer):
        self._trace.append("handler")
        return super().dispatch(provider, request, observer)


def _runtime(
    tmp_path: Path, trace: list[str], *, use_tool: bool,
) -> tuple[SynchronousAIRuntime, _Boundary, _TracingRunner]:
    database = tmp_path / ("tool.sqlite3" if use_tool else "question.sqlite3")
    log = EffectLog(database)
    runner = _TracingRunner(log, trace)
    store = SQLiteAITurnStore(database, effect_runner=runner)
    registry = ScopedCapabilityRegistry()
    registry.register(
        CapabilityDefinition(
            "calendar.read", 1, "read", False, "read_only",
            "crp://default/in", "crp://default/out",
        ),
        _Provider(trace),
    )
    snapshot = HookPolicySnapshot(
        "hook-policy-r1",
        (HookHandlerManifest("trace", "trace-r1", HookEvent.PRE_TOOL_USE, 0),),
    )
    hook_host = CodexHookHost(
        catalog=HookPolicyCatalog(snapshot),
        runner=RevisionPinnedHookRunner({
            ("trace", "trace-r1"): lambda manifest, payload: (
                trace.append("gate")
                or HookRun(0, 0, True, hook_id=manifest.hook_id)
            ),
        }),
    )
    boundary = _Boundary()
    frozen = TurnFrozenAuthorizationAuthority(
        payloads=store,
        execution_boundary=boundary,
    )
    runtime = SynchronousAIRuntime(
        planner=_Planner(trace, use_tool=use_tool),
        registry=registry,
        events=store,
        payloads=store,
        state=store,
        execution_boundary=boundary,
        dispatcher=_TracingDispatcher(trace),
        hook_host=hook_host,
        frozen_authorization=frozen,
        effect_runner=runner,
    )
    return runtime, boundary, runner


def _request() -> dict[str, object]:
    fixture = ROOT / "core-contracts/ai/fixtures/turn-request/valid-project-answer.json"
    request = json.loads(fixture.read_text(encoding="utf-8"))
    request["capability_policy"] = {
        "allowed": ["calendar.read"],
        "denied": [],
        "require_approval": [],
    }
    return request


def test_real_tool_turn_traces_exact_four_layer_order(tmp_path) -> None:
    trace: list[str] = []
    runtime, boundary, runner = _runtime(tmp_path, trace, use_tool=True)

    result = runtime.submit_turn(_request())

    assert result.status == "completed"
    first = {name: trace.index(name) for name in (
        "turn_driver", "gate", "effect_runner", "handler", "provider",
    )}
    assert first["turn_driver"] < first["gate"] < first["effect_runner"] < first["handler"]
    assert first["handler"] < first["provider"]
    assert boundary.evaluations == 1
    assert len(runner.claimed_operation_ids) == 1
    effect = runner.log.get(runner.claimed_operation_ids[0])
    assert effect.state.value == "SETTLED_OK"
    assert effect.lease_owner is None and effect.result_ref is not None


def test_plain_question_stops_in_turn_driver_without_execution_backend(tmp_path) -> None:
    trace: list[str] = []
    runtime, boundary, runner = _runtime(tmp_path, trace, use_tool=False)

    result = runtime.submit_turn(_request())

    assert result.status == "completed"
    assert trace == ["turn_driver"]
    assert boundary.evaluations == 1
    assert runner.claimed_operation_ids == []


def test_production_ai_composition_always_injects_core_effect_runner() -> None:
    source = (ROOT / "src/backend/memory_app/kernel/ai_runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    builder = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_ai_runtime"
    )
    rendered = ast.unparse(builder)

    assert "effect_runner=session_store.effect_runner" in rendered
    assert "SQLiteAITurnStore(runtime_root / '.rebuild-data' / 'ai-turns.sqlite3', effect_runner=shared_effect_runner)" in rendered


def test_planner_is_a_validated_decision_adapter_not_an_execution_dispatcher() -> None:
    source = (ROOT / "src/core/ai_kernel/model_planner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    planner = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ModelGatewayAgentPlanner"
    )
    rendered = ast.unparse(planner)

    assert not {"EffectRunner", "SynchronousToolDispatcher", "CapabilityProviderPort"}.intersection(imports)
    assert "_validate_decision" in rendered
    assert ".dispatch(" not in rendered
    assert ".invoke(" in rendered


def test_legacy_agent_graph_cannot_remain_a_second_production_turn_runtime() -> None:
    source = (ROOT / "src/backend/api/bootstrap.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    provider = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "LazyAgentRuntimeProvider"
    )
    getter = next(
        node for node in provider.body
        if isinstance(node, ast.FunctionDef) and node.name == "get_agent_graph_service"
    )
    rendered = ast.unparse(getter)

    assert "Legacy Agent API is retired" in rendered
    assert "AgentGraphService(" not in rendered


def test_transports_are_handlers_and_do_not_own_core_execution_authority() -> None:
    transport_paths = (
        ROOT / "src/core/mcp_host/stdio_transport.py",
        ROOT / "src/core/mcp_host/streamable_http_transport.py",
        ROOT / "src/core/plugin_hands/stdio_runner.py",
        ROOT / "src/core/media_hands/handler.py",
    )
    forbidden = ("EffectRunner(", "EffectReaper(", "build_effect_runtime(")
    for path in transport_paths:
        source = path.read_text(encoding="utf-8")
        assert all(token not in source for token in forbidden), path


def test_non_expert_runtime_has_no_dynamic_selection_layer() -> None:
    production_runtime_paths = (
        ROOT / "src/backend/memory_app/kernel/ai_runtime.py",
        ROOT / "src/core/ai_kernel/runtime.py",
        ROOT / "src/core/ai_kernel/model_planner.py",
    )
    for path in production_runtime_paths:
        source = path.read_text(encoding="utf-8")
        assert "CapabilitySelector" not in source
        assert "DynamicSelector" not in source


def test_production_planner_has_no_expert_execution_layer() -> None:
    source = (ROOT / "src/backend/memory_app/kernel/ai_runtime.py").read_text(encoding="utf-8")
    assert "ExpertAwarePlanner" not in source
    assert "expert_media_job_wait_bridge" not in source


def test_expert_is_configuration_without_affinity_or_auto_selection() -> None:
    source = (ROOT / "src/core/product_core/expert_catalog.py").read_text(
        encoding="utf-8"
    )
    resolver = (ROOT / "src/backend/api/expert_turn_binding_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "class ExpertConfigurationResolver" in source
    assert "def _select_auto" not in source
    assert '"selection_mode"] = "affinity"' not in source
    assert "ExpertSelectionService" not in source
    assert "ExpertConfigurationResolver" in resolver


def test_model_wire_enforces_token_budget_before_secret_and_egress() -> None:
    path = ROOT / "src/backend/shared/llm/litellm_gateway.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    gateway = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LiteLLMCompletionGateway"
    )
    for name in (
        "complete_text_with_usage", "acomplete_text", "stream_text",
        "stream_text_with_metadata", "astream_text",
    ):
        method = next(
            node for node in gateway.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        )
        rendered = ast.unparse(method)
        assert rendered.index("_enforce_input_token_budget") < rendered.index("_wire_api_key")
        assert rendered.index("_enforce_input_token_budget") < rendered.index("_authorize_egress")


def test_ai_turn_recovery_worker_is_coordination_not_effect_recovery_authority() -> None:
    source = (ROOT / "src/backend/api/ai_turn_recovery_worker.py").read_text(
        encoding="utf-8"
    )
    forbidden = (
        "EffectReaper", "EffectRunner(", "recover_expired(",
        "probe(", "mark_unknown(", "settle_ok(", "settle_error(",
    )
    assert all(token not in source for token in forbidden)
    assert "recover_accepted_turn" in source
