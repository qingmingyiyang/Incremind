"""T11.7 model boundary guard, including legacy protocol forwarding.

This is a syntactic guard, not a proof of dynamic Python data flow. It covers
gateway methods, the memory facade, imported generation helpers and aliases,
including callbacks handed to thread pools. Counts prevent adding another
call inside an already approved function; line numbers may move freely.
"""
from __future__ import annotations

import ast
from collections import Counter
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODEL_METHODS = {
    "complete_text", "complete_text_with_usage", "complete_structured", "complete_vision",
    "complete_structured_with_usage", "stream_structured_with_usage",
    "complete_stream", "complete_governed", "complete_json", "embed", "rerank",
    "stream_text", "stream_text_with_metadata", "test_connection", "post_json",
    "create_text_completion", "create_text_completion_stream",
    "create_text_completion_stream_with_metadata", "create_structured_completion",
}
HELPERS = {"generate_structured"}
MODEL_TYPES = {"ModelConfiguration", "ModelGatewayPort", "LiteLLMCompletionGateway"}
MODEL_OBJECT = "<model-object>"

# Only explicit probes remain in the migration allowlist.
# Kernel/transport implementations require separate live evidence below.
ALLOWED_CALLS: dict[str, tuple[int, str]] = {
    "src/backend/memory_app/app.py:_router.settings_test:embed": (1, "explicit probe"),
    "src/backend/memory_app/app.py:_router.settings_test:rerank": (1, "explicit probe"),
    "src/backend/memory_app/app.py:_router.settings_test:complete": (1, "explicit probe"),
    "src/backend/shared/llm/connection_diagnostic.py:run_model_connection_diagnostic:test_connection": (1, "settings probe"),
}



# These are implementation boundaries, not migration exemptions. Every entry is
# an exact caller with a call-count ceiling and executable AST evidence below.
# A new caller, including one in the same module/class, remains forbidden.
# A boundary loses its classification when its registration/forwarding evidence
# disappears. In particular, merely living under kernel/ is not sufficient.
BOUNDARIES = {}
BOUNDARY_EVIDENCE = {}


def boundary(kind, path, callers, *evidence):
    if any(expression.startswith("bind:ProductPolicyRuntime.") for _, expression in evidence):
        runtime_path = "backend/memory_app/kernel/policy_runtime.py"
        evidence = (*evidence,
            (runtime_path, "import:core.ai_kernel:SynchronousAIRuntime"),
            (runtime_path, "base:ProductPolicyRuntime:SynchronousAIRuntime"),
            (runtime_path, "call:super().run_accepted_turn"))
    for caller in callers:
        key = f"src/{path}:{caller}"
        assert key not in BOUNDARIES, f"Duplicate boundary classification: {key}"
        BOUNDARIES[key] = (1, kind)
        BOUNDARY_EVIDENCE[key] = evidence


# Evidence is (source path relative to src, exact AST expression/keyword).
# ast.unparse normalizes quotes and whitespace; comments cannot satisfy it.
boundary("kernel", "backend/memory_app/kernel/memory_turn.py", [
    "MemoryTurn.generate.Planner.plan:generate_structured",
    "embedding_request.invoke.wire:post_json",
], ("backend/memory_app/kernel/memory_turn.py", "bind:ProductPolicyRuntime.planner:Planner"),
   ("backend/memory_app/kernel/memory_turn.py", "attempt.invoke_wire(wire)"),
   ("backend/memory_app/kernel/memory_turn.py", "invoke=invoke"))
boundary("kernel", "backend/memory_app/kernel/answer_turns.py", [
    "generate_answer:complete_governed",
], ("backend/memory_app/kernel/answer_turns.py", "ACTIVE_ANSWER.get()"),
   ("backend/memory_app/kernel/answer_turns.py", "wire_attempt_sink=handle"))
boundary("kernel", "backend/memory_app/kernel/image_read.py", [
    "read_images.invoke:complete_vision",
], ("backend/memory_app/kernel/image_read.py", "call:MemoryTurn"),
   ("backend/memory_app/kernel/image_read.py", "kind='media.image_read'"),
   ("backend/memory_app/kernel/image_read.py", "purpose='vision'"),
   ("backend/memory_app/kernel/image_read.py", "bind:turn.generate.invoke:invoke"),
   ("backend/memory_app/kernel/image_read.py", "wire_attempt_sink=control"),
   ("backend/memory_app/kernel/memory_turn.py", "attempt.invoke_wire(wire)"))
boundary("kernel", "backend/memory_app/kernel/task_planner.py", [
    "ProductTaskPlanner.plan.Gateway.invoke:complete_governed",
], ("backend/memory_app/kernel/task_planner.py", "ModelGatewayAgentPlanner(gateway)"),
   ("backend/memory_app/kernel/task_planner.py", "wire_attempt_sink=execution_control"),
   ("backend/memory_app/kernel/ai_runtime.py", "assign:task_planner:ProductTaskPlanner"),
   ("backend/memory_app/kernel/ai_runtime.py", "assign:planner:task_planner"),
   ("backend/memory_app/kernel/ai_runtime.py", "bind:ProductPolicyRuntime.planner:planner"))
boundary("kernel", "backend/memory_app/v2/organize_turns.py", [
    "OrganizeTurns.complete.Planner.execute:complete_governed",
    "OrganizeTurns.complete.Planner.execute:complete",
], ("backend/memory_app/v2/organize_turns.py", "bind:ProductPolicyRuntime.planner:Planner"),
   ("backend/memory_app/v2/organize_turns.py", "runtime.submit_turn(request)"),
   ("backend/memory_app/v2/organize_turns.py", "wire_attempt_sink=execution_control"))
boundary("kernel", "backend/memory_app/v2/route.py", [
    "RouteService.route_model.Planner.plan:complete_governed",
], ("backend/memory_app/v2/route.py", "bind:ProductPolicyRuntime.planner:Planner"),
   ("backend/memory_app/v2/route.py", "wire_attempt_sink=execution_control"))
boundary("kernel", "backend/memory_app/turn_capability.py", [
    "RecognitionTaskCapability.invoke:complete_governed",
], ("backend/memory_app/turn_capability.py", "execution_control_from(request)"),
   ("backend/memory_app/turn_capability.py", "wire_attempt_sink=nested"),
   ("backend/memory_app/turn_installation.py", "assign:recognition_task_tool:RecognitionTaskCapability"),
   ("backend/memory_app/turn_installation.py", "register:recognition_task_tool"))
boundary("kernel", "core/ai_kernel/model_planner.py", [
    "ModelGatewayAgentPlanner.plan:invoke",
], ("backend/memory_app/kernel/task_planner.py", "ModelGatewayAgentPlanner(gateway)"))

# Configuration/output adapters dispatch through the single gateway. Calls to
# these methods/helpers in business code are still found by repository_calls.
boundary("transport", "backend/memory_app/model_config.py", [
    "ModelConfiguration.complete:complete_text_with_usage",
    "ModelConfiguration.complete:complete_structured_with_usage",
    "ModelConfiguration.complete_stream:stream_structured_with_usage",
    "ModelConfiguration.complete_governed:stream_structured_with_usage",
    "ModelConfiguration.complete_governed:complete_structured_with_usage",
    "ModelConfiguration.complete_governed:complete_text_with_usage",
], ("backend/memory_app/model_config.py", "LiteLLMCompletionGateway"))
boundary("transport", "backend/memory_app/model_config.py", [
    "ModelConfiguration.complete_vision:complete_structured_with_usage",
], ("backend/memory_app/model_config.py", "call:self._gateway_factory"),
   ("backend/memory_app/model_config.py", "egress_purpose='media_image_read'"),
   ("backend/memory_app/model_config.py", "call:self.price_wire_sink"),
   ("backend/memory_app/kernel/image_read.py", "call:MemoryTurn"),
   ("backend/memory_app/kernel/image_read.py", "call:models.complete_vision"),
   ("backend/memory_app/kernel/image_read.py", "bind:turn.generate.invoke:invoke"),
   ("backend/memory_app/kernel/image_read.py", "wire_attempt_sink=control"),
   ("backend/memory_app/kernel/memory_turn.py", "attempt.invoke_wire(wire)"))
boundary("transport", "backend/memory_app/structured_generation.py", [
    "generate_structured:complete_stream", "generate_structured:complete_structured",
    "generate_structured:complete",
], ("backend/memory_app/kernel/memory_turn.py", "wire_attempt_sink=control if observable else None"))
boundary("transport", "backend/shared/llm/litellm_gateway.py", [
    "LiteLLMCompletionGateway.complete_text:complete_text_with_usage",
    "LiteLLMCompletionGateway.test_connection:complete_text",
    "LiteLLMCompletionGateway.stream_structured_with_usage.consume:stream_text_with_metadata",
    "LiteLLMCompletionGateway.complete_structured:complete_structured_with_usage",
    "LiteLLMCompletionGateway.complete_structured_with_usage:complete_text_with_usage",
], ("backend/memory_app/model_config.py", "LiteLLMCompletionGateway"))
boundary("transport", "backend/model_runtime.py", [
    "LiteLLMModelGatewayAdapter.invoke:complete_text_with_usage",
    "LiteLLMModelGatewayAdapter.invoke:complete_text",
    "FrozenImageGenerationGateway.generate:generate",
    "TieredModelGatewayAdapter.invoke:invoke",
], ("backend/model_runtime.py", "ModelGatewayPort"),
   ("backend/model_runtime.py", "ImageGenerationGatewayPort"))
boundary("transport", "backend/memory_app/local_model.py", ["_generate:generate"],
   ("backend/memory_app/local_model.py", "local_files_only=True"),
   ("backend/memory_app/local_model.py", "torch.inference_mode()"))
boundary("transport", "backend/recognition_retrieval/service.py", [
    "HttpEmbeddingProvider.embed:post_json", "HttpReranker.rerank:post_json",
    "retrieve:rerank", "_vector_scores:embed",
], ("backend/memory_app/v2/contextual_chunk_vectors.py", "replace(provider, client=ObservedTransport())"),
   ("backend/memory_app/v2/links.py", "replace(provider, client=ObservedTransport())"))

boundary("transport", "backend/agent/infrastructure/chat_gateway.py", [
    "LiteLLMChatGateway.create_text_completion:complete_text",
    "LiteLLMChatGateway.create_text_completion_stream:stream_text",
    "LiteLLMChatGateway.create_text_completion_stream_with_metadata:stream_text_with_metadata",
    "LiteLLMChatGateway.create_structured_completion:complete_structured",
], ("backend/agent/infrastructure/chat_gateway.py", "ChatGateway"),
   ("backend/agent/infrastructure/chat_gateway.py", "call:self._gateway.complete_text"))
boundary("registered kernel port", "core/product_core/workbench_input_classifier.py", [
    "EnhanceWorkbenchInputClassification.execute:complete_json",
], ("backend/api/workbench_input_classifier_ai_runtime.py", "call:EnhanceWorkbenchInputClassification"),
   ("backend/api/workbench_input_classifier_ai_runtime.py", "call:_GatewayJsonProvider"),
   ("backend/api/workbench_input_classifier_ai_runtime.py", "call:begin_nested_model_call"),
   ("backend/memory_app/kernel/ai_runtime.py", "register:ScopedWorkbenchInputClassificationEnhanceCapability"))

# Local numeric providers are protocol adapters. Their independently tested
# methods only invoke the model and convert numeric output; no domain actions,
# storage or business orchestration are admitted by these two exact entries.
boundary("local numeric transport", "backend/video_summary/infrastructure/agent_memory/fastembed_adapter.py", [
    "FastEmbedEmbedding._get_text_embeddings:embed",
], ("backend/video_summary/infrastructure/agent_memory/fastembed_adapter.py", "base:FastEmbedEmbedding:BaseEmbedding"),
   ("backend/video_summary/infrastructure/agent_memory/fastembed_adapter.py", "import:fastembed:TextEmbedding"),
   ("backend/video_summary/infrastructure/agent_memory/fastembed_adapter.py", "assign:self._embedding:_create_text_embedding"))
boundary("local numeric transport", "backend/video_summary/infrastructure/agent_memory/pinpoint.py", [
    "BGEReranker.score:rerank",
], ("backend/video_summary/infrastructure/agent_memory/pinpoint.py", "base:BGEReranker:SemanticScorer"),
   ("backend/video_summary/infrastructure/agent_memory/pinpoint.py", "import:fastembed.rerank.cross_encoder:TextCrossEncoder"),
   ("backend/video_summary/infrastructure/agent_memory/pinpoint.py", "call:TextCrossEncoder"))

# These are not kernel entrypoints: the first is an injected numeric algorithm
# consumed only by the packaged local CPU/BGE command, and the second replays
# fictional frozen judgments without a model. Consumer-closure tests below
# reject new wiring, provider replacements and remote implementations.
boundary("local deterministic numeric algorithm", "backend/video_summary/infrastructure/local_semantic_summary.py", [
    "build_semantic_extractive_summary:embed", "_semantic_keywords:embed",
], ("backend/video_summary/infrastructure/local_semantic_summary.py", "assign:embedding:build_fastembed_embedding"),
   ("backend/video_summary/infrastructure/local_semantic_summary.py", "embed=embedding.get_text_embedding_batch"),
   ("backend/video_summary/infrastructure/local_semantic_summary.py", "device='cpu'"))
BOUNDARIES["src/backend/video_summary/infrastructure/local_semantic_summary.py:build_semantic_extractive_summary:embed"] = (2, "local deterministic numeric algorithm")
for path, caller in [
    ("core/product_core/ai_project_route_reranker.py", "AIProjectRouteReranker.rerank:complete_json"),
    ("core/product_core/global_project_series_router.py", "GlobalProjectSeriesRouter._rerank_if_allowed:rerank"),
]:
    boundary("offline frozen judgment replay", path, [caller],
        ("core/product_core/project_route_evaluation.py", "assign:provider:_ReplayProvider"),
        ("core/product_core/project_route_evaluation.py", "bind:AIProjectRouteReranker.provider:provider"),
        ("core/product_core/project_route_evaluation.py", "reranker_factory=reranker_factory"))

# AGENTS §5 freezes the companion backend. Keep precise counts, not a package
# exclusion: newly added companion calls still require an explicit decision.
boundary("frozen companion", "backend/api/companion_chat_ai_runtime.py", [
    "CompanionChatMessageWriteCapability.invoke:invoke",
], ("backend/api/companion_chat_ai_runtime.py", "execution_control_from(request)"))
boundary("frozen companion", "backend/api/companion_vision_ai_runtime.py", [
    "CompanionVisionAnalyzeCapability.invoke:invoke",
], ("backend/api/companion_vision_ai_runtime.py", "execution_checkpoint(request)"))
boundary("frozen companion", "backend/companion_provider_runtime.py", [
    "CompanionLiteLLMProvider.generate:invoke",
], ("backend/companion_provider_runtime.py", "ModelGatewayPort"))
boundary("frozen companion", "core/companion_core/model_routes.py", [
    "_invoke_provider_bounded.run:generate",
], ("core/companion_core/model_routes.py", "provider.generate(request)"))
boundary("frozen companion", "core/companion_core/memory_vector_evaluation.py", [
    "evaluate_vector_embeddings:embed", "evaluate_vector_embeddings.vector_search:embed",
], ("core/companion_core/memory_vector_evaluation.py", "embed"))



# Historical module locations are still used by the product kernel composition.
# Both their constructor registration and their own routing/lifecycle boundary
# must remain present; an orphaned legacy class cannot satisfy this contract.
for module, caller, registration, lifecycle in [
    ("agent_steward_decomposition", "StewardDecompositionPlanner.plan:invoke", "bind:StewardPlanningPlanner.remote:StewardDecompositionPlanner", "turn_routing_parameters"),
    ("developer_studio_test_lab_ai_runtime", "DeveloperStudioTestLabCapability._invoke_model:invoke", "register:DeveloperStudioTestLabCapability", "begin_nested_model_call"),
    ("image_generation_ai_runtime", "ImageGenerationCapability.invoke:generate", "register:ImageGenerationCapability", "load_turn_model_routing_binding"),
    ("project_skill_ai_runtime", "ProjectSkillDraftPlanner.plan:invoke", "outcome:PROJECT_SKILL_DRAFT_OUTCOME:ProjectSkillDraftPlanner", "load_turn_model_routing_binding"),
    ("series_intake_ai_runtime", "SeriesIntakeOrganizeCommitCapability.invoke:invoke", "register:SeriesIntakeOrganizeCommitCapability", "begin_nested_model_call"),
    ("source_document_ai_runtime", "SourceDocumentDraftPlanner.plan:invoke", "outcome:SOURCE_DOCUMENT_DRAFT_OUTCOME:SourceDocumentDraftPlanner", "load_turn_model_routing_binding"),
    ("workbench_ai_runtime", "WorkbenchQuestionPlanner.plan:invoke", "outcome:WORKBENCH_QUESTION_OUTCOME:WorkbenchQuestionPlanner", "execution_control.checkpoint"),
]:
    boundary("registered kernel adapter", f"backend/api/{module}.py", [caller],
        ("backend/memory_app/kernel/ai_runtime.py", registration),
        ("backend/memory_app/kernel/ai_runtime.py", "bind:ProductPolicyRuntime.planner:planner"),
        (f"backend/api/{module}.py", "call:" + lifecycle))
boundary("registered kernel adapter", "backend/api/four_layer_memory_candidate_ai_runtime.py", [
    "_GatewayProposalGenerator.__call__:invoke", "_BoundModelGateway.invoke:invoke",
], ("backend/memory_app/kernel/ai_runtime.py", "register:ScopedFourLayerMemoryCandidateProposalCapability"),
   ("backend/api/four_layer_memory_candidate_ai_runtime.py", "call:begin_nested_model_call"),
   ("backend/api/four_layer_memory_candidate_ai_runtime.py", "call:_BoundModelGateway"))
boundary("registered kernel adapter", "backend/api/workbench_input_classifier_ai_runtime.py", [
    "_GatewayJsonProvider.complete_json:invoke",
], ("backend/memory_app/kernel/ai_runtime.py", "register:ScopedWorkbenchInputClassificationEnhanceCapability"),
   ("backend/api/workbench_input_classifier_ai_runtime.py", "call:begin_nested_model_call"),
   ("backend/api/workbench_input_classifier_ai_runtime.py", "call:_GatewayJsonProvider"))
boundary("non-model tool provider", "backend/api/plugin_runtime.py", [
    "_DurablePluginToolProvider.invoke:invoke",
], ("backend/api/plugin_runtime.py", "current.provider.invoke(request)"),
   ("backend/api/plugin_runtime.py", "call:self._activation.active_contributions"))


def evidence_is_present(source, expression):
    return expression in evidence_expressions(source)


def evidence_expressions(source):
    nodes = tuple(ast.walk(ast.parse(source)))
    expressions = {ast.unparse(node) for node in nodes
                   if isinstance(node, (ast.Call, ast.Name, ast.keyword))}
    for node in nodes:
        if isinstance(node, ast.ClassDef):
            expressions.update(f"base:{node.name}:{ast.unparse(base)}" for base in node.bases)
        if isinstance(node, ast.ImportFrom):
            expressions.update(f"import:{node.module}:{alias.name}" for alias in node.names)
        if isinstance(node, ast.Call):
            expressions.add("call:" + ast.unparse(node.func))
            for keyword in node.keywords:
                value = keyword.value
                if isinstance(value, (ast.Call, ast.Name)):
                    target = ast.unparse(value.func if isinstance(value, ast.Call) else value)
                    expressions.add(f"bind:{ast.unparse(node.func)}.{keyword.arg}:{target}")
                if ast.unparse(node.func) == "OutcomeDispatchPlanner" and keyword.arg == "outcomes" and isinstance(value, ast.Dict):
                    for key, planner in zip(value.keys, value.values):
                        if key is not None and isinstance(planner, ast.Call):
                            expressions.add(f"outcome:{ast.unparse(key)}:{ast.unparse(planner.func)}")
            if isinstance(node.func, ast.Attribute) and node.func.attr == "register":
                for arg in node.args:
                    if isinstance(arg, (ast.Call, ast.Name)):
                        expressions.add("register:" + ast.unparse(arg.func if isinstance(arg, ast.Call) else arg))
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Call, ast.Name)):
            value = ast.unparse(node.value.func if isinstance(node.value, ast.Call) else node.value)
            expressions.update(f"assign:{ast.unparse(target)}:{value}" for target in node.targets)
    return expressions


def verified_boundaries(read_source):
    evidence = {path: evidence_expressions(read_source(path))
                for path in {path for entries in BOUNDARY_EVIDENCE.values() for path, _ in entries}}
    return {key: value for key, value in BOUNDARIES.items()
            if BOUNDARY_EVIDENCE[key] and all(
                expression in evidence[path]
                for path, expression in BOUNDARY_EVIDENCE[key])}


def unexpected_calls(found, allowed):
    return {key: count for key, count in found.items() if key not in allowed or count > allowed[key][0]}


def model_references(expression: ast.AST, aliases: dict[str, str]) -> set[str]:
    """Resolve only model-bearing expressions, not arbitrary complete/invoke."""
    if isinstance(expression, ast.Name):
        if expression.id in aliases:
            return set(aliases[expression.id].split("|"))
        if expression.id == "models":
            return {MODEL_OBJECT}
        return {expression.id} if expression.id in MODEL_METHODS | HELPERS else set()
    if isinstance(expression, ast.Attribute):
        receiver_node = expression.value.func if isinstance(expression.value, ast.Call) else expression.value
        receiver = ast.unparse(receiver_node).lower()
        name = expression.attr
        if name in MODEL_METHODS | HELPERS:
            return {name}
        if name == "complete" and ("models" in receiver or
                                   MODEL_OBJECT in model_references(expression.value, aliases)):
            return {name}
        if name in {"invoke", "generate"} and any(
            part in receiver for part in ("gateway", "provider", "model", "adapter")
        ):
            return {name}
    if isinstance(expression, ast.Call) and ast.unparse(expression.func).split(".")[-1] in MODEL_TYPES:
        return {MODEL_OBJECT}
    if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name) and expression.func.id == "getattr":
        if len(expression.args) >= 2 and isinstance(expression.args[1], ast.Constant):
            method = expression.args[1].value
            if method in MODEL_METHODS | {"complete"}:
                return {method}
    if isinstance(expression, (ast.BoolOp, ast.IfExp)):
        children = expression.values if isinstance(expression, ast.BoolOp) else [expression.body, expression.orelse]
        return set().union(*(model_references(child, aliases) for child in children))
    return set()


def calls_in_source(source: str) -> Counter[str]:
    tree = ast.parse(source)
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in HELPERS:
                    imported[alias.asname or alias.name] = alias.name
    found: Counter[str] = Counter()

    def visit(node: ast.AST, scope: tuple[str, ...], aliases: dict[str, str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
            aliases = dict(aliases)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                    if argument.annotation and ast.unparse(argument.annotation).split(".")[-1] in MODEL_TYPES:
                        aliases[argument.arg] = MODEL_OBJECT
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            refs = model_references(node.value, aliases)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and refs:
                    aliases[target.id] = "|".join(sorted(refs))
        if isinstance(node, ast.Call):
            refs = model_references(node.func, aliases)
            if isinstance(node.func, ast.Name) and node.func.id in aliases:
                refs.update(aliases[node.func.id].split("|"))
            # Deferred calls are actual gateway use too (threadpool/to_thread).
            if isinstance(node.func, (ast.Name, ast.Attribute)) and ast.unparse(node.func).split(".")[-1] in {"run_in_threadpool", "to_thread"} and node.args:
                refs.update(model_references(node.args[0], aliases))
            refs.discard(MODEL_OBJECT)
            if refs:
                found[f"{'.'.join(scope) or '<module>'}:{'|'.join(sorted(refs))}"] += 1
        for child in ast.iter_child_nodes(node):
            visit(child, scope, aliases)

    visit(tree, (), imported)
    return found


@cache
def repository_calls() -> Counter[str]:
    found: Counter[str] = Counter()
    for path in sorted((ROOT / "src").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        for caller, count in calls_in_source(path.read_text(encoding="utf-8-sig")).items():
            found[f"{relative}:{caller}"] = count
    return found


def test_model_calls_match_frozen_inventory() -> None:
    classified = verified_boundaries(lambda path: (ROOT / "src" / path).read_text(encoding="utf-8-sig"))
    unexpected = unexpected_calls(repository_calls(), {**ALLOWED_CALLS, **classified})
    assert not unexpected, f"New model calls must go through kernel turns: {unexpected}"
    assert all(reason for _, reason in ALLOWED_CALLS.values())


@pytest.mark.parametrize("source, expected", [
    ("def f(models):\n models.complete([])", {"f:complete": 1}),
    ("def f(models):\n fn = getattr(models, 'complete_structured', None) or models.complete\n fn([])", {"f:complete|complete_structured": 1}),
    ("from x import generate_structured as g\ndef f():\n g([])", {"f:generate_structured": 1}),
    ("async def f():\n await run_in_threadpool(generate_structured, models, [])", {"f:generate_structured": 1}),
    ("def f(gateway):\n call = gateway.complete_text_with_usage\n call([])\n call([])", {"f:complete_text_with_usage": 2}),
    ("def f(store, request, graph):\n store.complete('x')\n request.stream()\n graph.invoke({})", {}),
    ("class C:\n def f(self):\n  self._gateway.invoke(request)", {"C.f:invoke": 1}),
    ("def f(models):\n fn = getattr(models, 'complete_structured', None) or models.complete\n other = fn\n other([])", {"f:complete|complete_structured": 1}),
    ("def f():\n generation.generate_structured(models, [])", {"f:generate_structured": 1}),
])
def test_inventory_detects_direct_aliased_and_deferred_calls(source, expected) -> None:
    assert calls_in_source(source) == expected


def test_inventory_rejects_new_caller_and_extra_call_in_existing_caller() -> None:
    allowed = {"old.py:f:complete": (1, "T11.4")}
    assert unexpected_calls({"old.py:f:complete": 2, "new.py:f:complete": 1}, allowed) == {
        "old.py:f:complete": 2, "new.py:f:complete": 1,
    }
    assert unexpected_calls({}, allowed) == {}  # Migration can remove calls.


def test_migration_allowlist_contains_only_explicit_probes():
    assert all('probe' in reason.lower() for _, reason in ALLOWED_CALLS.values())


def forbidden_gateway_imports(source):
    forbidden = []
    for node in ast.walk(ast.parse(source)):
        modules = ([node.module or ""] if isinstance(node, ast.ImportFrom) else
                   [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
        forbidden.extend(module for module in modules
                         if module.startswith(("backend.shared.llm.litellm_gateway", "core.model_gateway")))
        if isinstance(node, ast.Import) and "backend.shared.llm" in modules:
            forbidden.append("backend.shared.llm")
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("shared.llm"):
            forbidden.extend(f"{node.module}.{alias.name}" for alias in node.names
                             if alias.name in {"LiteLLMCompletionGateway", "ImageGenerationProviderAdapter", "*"})
    return forbidden


def test_product_orchestration_does_not_import_model_gateway():
    paths = list((ROOT / 'src/backend/memory_app/v2').glob('*.py'))
    paths += list((ROOT / 'src/backend/memory_app').glob('workspace_*.py'))
    forbidden = [f"{path.relative_to(ROOT)}:{module}" for path in paths
                 for module in forbidden_gateway_imports(path.read_text(encoding="utf-8-sig"))]
    assert not forbidden, forbidden


@pytest.mark.parametrize("source", [
    "from backend.shared.llm import LiteLLMCompletionGateway",
    "from backend.shared.llm import LiteLLMCompletionGateway as Gateway",
    "from backend.shared.llm import *",
    "import backend.shared.llm as llm; gateway = llm.LiteLLMCompletionGateway()",
])
def test_gateway_import_guard_rejects_public_reexports(source):
    assert forbidden_gateway_imports(source)


def test_message_metadata_helpers_remain_the_same_public_functions():
    from backend.shared.llm import message_metadata, litellm_gateway
    for name in ('_estimate_input_tokens', '_extract_normalized_usage',
                 '_build_prompt_fallback_messages', '_build_json_mode_messages'):
        assert getattr(message_metadata, name) is getattr(litellm_gateway, name)


def test_chunked_generation_requires_kernel_organizer():
    import inspect
    from backend.memory_app.workspace_generation import _complete_chunked_video_draft
    parameters = inspect.signature(_complete_chunked_video_draft).parameters
    assert parameters['organize'].default is inspect.Parameter.empty
    assert 'models' not in parameters


@pytest.mark.parametrize('source', [
    "def f(models):\n client = models\n client.complete([])",
    "def f(cfg: ModelConfiguration):\n cfg.complete([])",
    "def f():\n client = ModelConfiguration()\n client.complete([])",
])
def test_inventory_tracks_model_object_aliases(source):
    assert calls_in_source(source) == {'f:complete': 1}


@pytest.mark.parametrize('source', [
    "def f(models):\n client = models\n client.complete_vision([])",
    "def f(cfg: ModelConfiguration):\n cfg.complete_vision([])",
    "def f():\n client = ModelConfiguration()\n client.complete_vision([])",
])
def test_inventory_tracks_vision_model_object_aliases(source):
    assert calls_in_source(source) == {'f:complete_vision': 1}


@pytest.mark.parametrize('old,new', [
    ('MemoryTurn(', 'UnregisteredTurn('),
    ('wire_attempt_sink=control', 'wire_attempt_sink=None'),
    ('invoke=invoke', 'invoke=None'),
])
def test_vision_boundaries_require_registered_turn_and_wire_evidence(old, new):
    path = 'backend/memory_app/kernel/image_read.py'
    original = (ROOT / 'src' / path).read_text(encoding='utf-8-sig')
    assert old in original
    changed = original.replace(old, new)
    verified = verified_boundaries(lambda current: changed if current == path else
        (ROOT / 'src' / current).read_text(encoding='utf-8-sig'))
    for key in (
        'src/backend/memory_app/kernel/image_read.py:read_images.invoke:complete_vision',
        'src/backend/memory_app/model_config.py:ModelConfiguration.complete_vision:complete_structured_with_usage',
    ):
        assert key not in verified


def test_boundary_evidence_is_required_and_comments_cannot_supply_it():
    assert evidence_is_present("runtime = SynchronousAIRuntime(planner=Planner())", "bind:SynchronousAIRuntime.planner:Planner")
    assert not evidence_is_present("# SynchronousAIRuntime(planner=Planner())\nruntime = Runtime()", "bind:SynchronousAIRuntime.planner:Planner")
    assert verified_boundaries(lambda path: "pass") == {}


def test_boundaries_do_not_exempt_new_functions_or_additional_calls():
    for key, (limit, kind) in BOUNDARIES.items():
        path, caller, method = key.split(":")
        new = f"{path}:new_business_function:{method}"
        assert unexpected_calls({key: limit + 1, new: 1}, BOUNDARIES) == {key: limit + 1, new: 1}


def test_all_boundary_classifications_have_live_ast_evidence():
    verified = verified_boundaries(lambda path: (ROOT / "src" / path).read_text(encoding="utf-8-sig"))
    assert set(verified) == set(BOUNDARIES), sorted(set(BOUNDARIES) - set(verified))


@pytest.mark.parametrize("method", [
    "create_text_completion", "create_text_completion_stream",
    "create_text_completion_stream_with_metadata", "create_structured_completion",
])
def test_inventory_detects_chat_gateway_protocol_calls(method):
    assert calls_in_source(f"def f(gateway):\n gateway.{method}([])") == {f"f:{method}": 1}


def test_registration_evidence_requires_binding_not_construction():
    assert not evidence_is_present("Handler()", "register:Handler")
    assert evidence_is_present("registry.register(definition, Handler())", "register:Handler")
    assert not evidence_is_present("Planner()", "outcome:OUTCOME:Planner")
    assert evidence_is_present("OutcomeDispatchPlanner(outcomes={OUTCOME: Planner()})", "outcome:OUTCOME:Planner")
    assert not evidence_is_present("Planner()", "bind:SynchronousAIRuntime.planner:Planner")


def test_structured_generation_helper_has_only_the_kernel_consumer():
    found = repository_calls()
    helpers = {key: count for key, count in found.items() if key.endswith(":generate_structured")}
    assert helpers == {"src/backend/memory_app/kernel/memory_turn.py:MemoryTurn.generate.Planner.plan:generate_structured": 1}


@pytest.mark.parametrize("path, owner, method, allowed_calls", [
    ("backend/video_summary/infrastructure/agent_memory/fastembed_adapter.py",
     "FastEmbedEmbedding", "_get_text_embeddings", {"float", "self._embedding.embed"}),
    ("backend/video_summary/infrastructure/agent_memory/pinpoint.py",
     "BGEReranker", "score", {"list", "float", "_sigmoid", "self._model.rerank"}),
])
def test_local_numeric_ports_have_no_business_or_storage_calls(path, owner, method, allowed_calls):
    tree = ast.parse((ROOT / "src" / path).read_text(encoding="utf-8-sig"))
    adapter = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner)
    entry = next(node for node in adapter.body if isinstance(node, ast.FunctionDef) and node.name == method)
    calls = {ast.unparse(node.func) for node in ast.walk(entry) if isinstance(node, ast.Call)}
    assert calls == allowed_calls
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(entry))


def named_calls(source, names):
    """Enumerate imported/assigned aliases as well as module-qualified calls."""
    tree = ast.parse(source)
    aliases = {name: name for name in names}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in names:
                    aliases[alias.asname or alias.name] = alias.name
    # Follow chained local constructor aliases without relying on AST walk order.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) and node.value.id in aliases:
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in aliases:
                        aliases[target.id] = aliases[node.value.id]
                        changed = True
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            symbol = aliases.get(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            symbol = node.func.attr if node.func.attr in names else None
        else:
            symbol = None
        if symbol:
            calls.append((symbol, node))
    return calls


@cache
def special_port_consumers():
    names = {"AIProjectRouteReranker", "GlobalProjectSeriesRouter", "build_semantic_extractive_summary"}
    found = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        source = path.read_text(encoding="utf-8-sig")
        if any(name in source for name in names):
            found.extend((path.relative_to(ROOT).as_posix(), symbol, call)
                         for symbol, call in named_calls(source, names))
    return found


def single_factory_assignment(source, variable, factory):
    assignments = [node for node in ast.walk(ast.parse(source))
                   if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == variable for target in node.targets)]
    return len(assignments) == 1 and isinstance(assignments[0].value, ast.Call) and ast.unparse(assignments[0].value.func) == factory


def test_named_port_scan_tracks_imported_and_assigned_aliases():
    calls = named_calls("from port import AIProjectRouteReranker as R\nfactory = R\nfactory(provider=remote)", {"AIProjectRouteReranker"})
    assert len(calls) == 1 and calls[0][0] == "AIProjectRouteReranker"
    assert not single_factory_assignment("provider = RemoteProvider()", "provider", "_ReplayProvider")
    assert not single_factory_assignment("provider = _ReplayProvider()\nprovider = RemoteProvider()", "provider", "_ReplayProvider")
    assert single_factory_assignment("provider = _ReplayProvider(cases)", "provider", "_ReplayProvider")


def known_port_binding(path, name, call):
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in call.keywords}
    if None in keywords:
        return False  # No opaque **kwargs may inject another provider.
    if path == "src/core/product_core/project_route_evaluation.py":
        if name == "AIProjectRouteReranker":
            return keywords.get("provider") == "provider"
        if name == "GlobalProjectSeriesRouter":
            return keywords.get("reranker_factory") == "reranker_factory"
    if path == "src/backend/api/workbench_ai_runtime.py" and name == "GlobalProjectSeriesRouter":
        return "reranker_factory" not in keywords
    if path == "src/backend/video_summary/infrastructure/local_semantic_summary.py" and name == "build_semantic_extractive_summary":
        return keywords.get("embed") == "embedding.get_text_embedding_batch"
    return False


@pytest.mark.parametrize("path, source", [
    ("src/core/product_core/project_route_evaluation.py", "AIProjectRouteReranker(provider=RemoteProvider())"),
    ("src/backend/api/workbench_ai_runtime.py", "GlobalProjectSeriesRouter(catalog=c, reranker_factory=remote)"),
    ("src/backend/api/workbench_ai_runtime.py", "GlobalProjectSeriesRouter(catalog=c, **settings)"),
    ("src/backend/video_summary/infrastructure/local_semantic_summary.py", "build_semantic_extractive_summary(payload, embed=remote.embed)"),
    ("src/backend/memory_app/v2/new_business.py", "AIProjectRouteReranker(provider=provider)"),
])
def test_special_port_bindings_reject_remote_replacements_and_new_consumers(path, source):
    [(name, call)] = named_calls(source, {"AIProjectRouteReranker", "GlobalProjectSeriesRouter", "build_semantic_extractive_summary"})
    assert not known_port_binding(path, name, call)


def test_numeric_and_replay_ports_have_only_the_verified_consumers():
    found = special_port_consumers()
    assert Counter((path, name) for path, name, _ in found) == {
        ("src/backend/video_summary/infrastructure/local_semantic_summary.py", "build_semantic_extractive_summary"): 1,
        ("src/core/product_core/project_route_evaluation.py", "AIProjectRouteReranker"): 1,
        ("src/core/product_core/project_route_evaluation.py", "GlobalProjectSeriesRouter"): 1,
        ("src/backend/api/workbench_ai_runtime.py", "GlobalProjectSeriesRouter"): 1,
    }
    assert all(known_port_binding(path, name, call) for path, name, call in found)
    source = (ROOT / "src/core/product_core/project_route_evaluation.py").read_text(encoding="utf-8-sig")
    assert single_factory_assignment(source, "provider", "_ReplayProvider")
    local = (ROOT / "src/backend/video_summary/infrastructure/local_semantic_summary.py").read_text(encoding="utf-8-sig")
    assert single_factory_assignment(local, "embedding", "build_fastembed_embedding")
    [(_, factory)] = named_calls(local, {"build_fastembed_embedding"})
    options = {keyword.arg: ast.unparse(keyword.value) for keyword in factory.keywords}
    assert options["device"] == "'cpu'" and options["model_name"] == "MODEL_NAME"
    assert None not in options
    model = [node.value for node in ast.parse(local).body if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == "MODEL_NAME" for target in node.targets)]
    assert len(model) == 1 and ast.literal_eval(model[0]) == "BAAI/bge-small-zh-v1.5"


def test_replay_provider_uses_only_frozen_dictionary_judgments():
    source = (ROOT / "src/core/product_core/project_route_evaluation.py").read_text(encoding="utf-8-sig")
    provider = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == "_ReplayProvider")
    calls = {ast.unparse(node.func) for node in ast.walk(provider) if isinstance(node, ast.Call)}
    assert calls == {"dict", "isinstance", "ProjectRouteEvaluationError", "tuple", "str",
                     "user_payload.get", "self._cases.get", "candidate.get", "scores.get"}
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(provider))
