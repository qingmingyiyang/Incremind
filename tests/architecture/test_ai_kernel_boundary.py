from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]
KERNEL_ROOT = ROOT / "src" / "core" / "ai_kernel"


def _kernel_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(KERNEL_ROOT.glob("*.py"))
    )


def test_ai_kernel_is_platform_provider_and_framework_neutral() -> None:
    text = _kernel_text()
    for forbidden in (
        "fastapi",
        "backend.api",
        "electron",
        "frontend",
        "litellm",
        "httpx",
        "urllib",
        "ProviderRegistry",
        "JsonObjectStore",
        "product_core._exports",
    ):
        assert forbidden not in text


def test_ai_kernel_owns_orchestration_but_not_domain_authority() -> None:
    ports = (KERNEL_ROOT / "ports.py").read_text(encoding="utf-8")
    assert "class AIRuntimePort" in ports
    assert "class CapabilityRegistryPort" in ports
    assert "class TurnEventStorePort" in ports
    assert "expected_sequence" in ports
    for forbidden_authority in (
        "save_memory",
        "publish_memory",
        "write_project_skill",
        "create_document",
        "write_vault",
    ):
        assert forbidden_authority not in ports


def test_ai_kernel_persists_and_reuses_a_turn_capability_manifest() -> None:
    runtime = (KERNEL_ROOT / "runtime.py").read_text(encoding="utf-8")
    manifest = (KERNEL_ROOT / "capability_manifest.py").read_text(encoding="utf-8")
    assert 'self._payloads.put(turn_id, "capability-manifest"' in runtime
    assert "self._registry.list(), self._payloads" not in runtime
    assert "manifest.capability_ids" in runtime
    assert "capability manifest expanded the Turn policy" in manifest
    schema = json.loads(
        (ROOT / "core-contracts" / "ai" / "turn-capability-manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False

    context_schema = json.loads(
        (ROOT / "core-contracts" / "ai" / "turn-context-manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(context_schema)
    assert context_schema["additionalProperties"] is False


def test_tool_execution_records_intent_before_provider_and_outcome_after_provider() -> None:
    runtime = (KERNEL_ROOT / "runtime.py").read_text(encoding="utf-8")
    intent = runtime.index('"tool.intent.recorded"')
    provider = runtime.index("result = self._dispatcher.dispatch(")
    outcome = runtime.index('"tool.outcome.recorded"')
    assert intent < provider < outcome
    assert '"tool_call_id": intent.invocation_id' in runtime
    assert '"idempotency_key": intent.idempotency_key' in runtime
    assert "result = invoke(" not in runtime

    dispatcher = (KERNEL_ROOT / "dispatcher.py").read_text(encoding="utf-8")
    assert "class SynchronousToolDispatcher" in dispatcher
    assert "self._gate.acquire(request.execution_mode, context.checkpoint)" in dispatcher
    assert "def request_cancel" in dispatcher
    assert 'provider_request["execution_context"] = context' in dispatcher
    assert "class ToolProviderFailure" in dispatcher
    assert "class ToolDispatchFailure" in dispatcher
    assert 'error.error_code in intent.retryable_error_codes' in runtime
    assert 'intent.idempotency == "idempotent"' in runtime
    assert 'effect_certainty == "confirmed_none"' in runtime
    assert '"tool.attempt.failed"' in runtime
    assert "sorted(item.strip() for item in identities)" in dispatcher

    for name in (
        "tool-attempt-failure.schema.json",
        "tool-invocation-intent.schema.json",
        "tool-invocation-outcome.schema.json",
    ):
        schema = json.loads((ROOT / "core-contracts" / "ai" / name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False


def test_planner_receives_a_turn_scoped_payload_view() -> None:
    runtime = (KERNEL_ROOT / "runtime.py").read_text(encoding="utf-8")
    scoped = (KERNEL_ROOT / "scoped_payloads.py").read_text(encoding="utf-8")
    ports = (KERNEL_ROOT / "ports.py").read_text(encoding="utf-8")
    assert "ScopedTurnPayloadView(" in runtime
    assert "allowed_refs=planner_context_payload_refs(planner_events, self._payloads, turn_id=turn_id)" in runtime
    assert "planner_payloads,\n                        planner_control," in runtime
    assert "execution_control: ModelExecutionControlPort | None = None" in ports
    assert "_request_planner_cancel(" in runtime
    assert "Planner payload write crossed Turn identity" in scoped
    assert "outside Planner context manifest" in scoped


def test_production_ai_runtime_injects_project_profile_resolvers() -> None:
    composition = (ROOT / "src" / "backend" / "memory_app" / "kernel" / "ai_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "TurnProjectProfileSnapshotAuthority(" in composition
    assert "manifest_resolver=ProjectAwareCapabilityManifestResolver(" in composition
    assert "context_manifest_resolver=ProjectAwareContextManifestResolver(" in composition
    assert "application_skills=skill_snapshots" in composition
    assert "execution_boundary = AIToolExecutionBoundary(" in composition
    assert composition.count("execution_boundary=execution_boundary") >= 2
    assert "binding_guard=TurnCapabilityBindingGuard(" in composition


def test_production_context_composition_is_project_aware_and_budgeted() -> None:
    composition = (ROOT / "src" / "backend" / "memory_app" / "kernel" / "ai_runtime.py").read_text(
        encoding="utf-8"
    )
    resolver = composition.split(
        "context_manifest_resolver=ProjectAwareContextManifestResolver(", 1
    )[1].split(")\n", 1)[0]
    for required in (
        "profile_snapshots",
        "application_skills=skill_snapshots",
        "model_routing=model_snapshots",
        "published_memory=published_memory_snapshots",
        "context_bindings=context_binding_snapshots",
        "payloads=session_store",
    ):
        assert required in resolver
    assert "fallback=" not in resolver

    resolver_source = (ROOT / "src" / "backend" / "api" / "ai_profile_resolvers.py").read_text(
        encoding="utf-8"
    )
    assert "selected_bytes + content_bytes > baseline.max_context_bytes" in resolver_source
    assert "remaining_bytes = baseline.max_context_bytes - selected_bytes" in resolver_source

    kernel_runtime = (KERNEL_ROOT / "runtime.py").read_text(encoding="utf-8")
    assert "context_manifest_resolver or V1TurnContextManifestResolver()" in kernel_runtime


def test_ai_contracts_do_not_accept_provider_secrets_or_local_paths() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "core-contracts" / "ai").glob("*.schema.json"))
    )
    # Opaque authorization fact references and non-secret revision metadata are
    # valid Session facts. Reject fields which can carry the credential or a
    # host path rather than substring-matching those safe identities.
    for forbidden in (
        '"api_key"', '"provider_api_key"', '"authorization"',
        '"authorization_header"', '"cookie"', '"local_path"', '"windows_path"',
    ):
        assert forbidden not in text


def test_production_model_wire_commit_requires_atomic_reservation_witness() -> None:
    model_runtime = (ROOT / "src" / "backend" / "model_runtime.py").read_text(encoding="utf-8")
    kernel_runtime = (ROOT / "src" / "core" / "ai_kernel" / "runtime.py").read_text(encoding="utf-8")
    sqlite_store = (ROOT / "src" / "core" / "ai_kernel" / "sqlite_store.py").read_text(encoding="utf-8")
    assert "is_durable_model_wire_commit_witness" in model_runtime
    assert "authority_stack.close" in model_runtime
    assert "_issue_durable_model_wire_commit_witness" in kernel_runtime
    assert "commit_model_attempt_dispatch_bundle" in kernel_runtime
    assert "append_model_attempt_terminal_bundle" in kernel_runtime
    assert "CREATE TABLE IF NOT EXISTS ai_model_attempt_reservations" in sqlite_store
    reservation_schema = next(
        line for line in sqlite_store.splitlines()
        if "CREATE TABLE IF NOT EXISTS ai_model_attempt_reservations" in line
    )
    for forbidden in ("prompt", "output", "endpoint", "credential", "secret", "local_path"):
        assert forbidden not in reservation_schema


def test_companion_consumes_unified_model_gateway_without_direct_litellm_composition() -> None:
    companion = (ROOT / "src" / "backend" / "companion_provider_runtime.py").read_text(encoding="utf-8")
    assert "from core.model_gateway import ModelGatewayPort, ModelRequest" in companion
    for forbidden in ("LiteLLMCompletionGateway", "build_active_provider_egress_guard", "model_route_provider_context", "secret_store.get"):
        assert forbidden not in companion

    composition = (ROOT / "src" / "backend" / "model_runtime.py").read_text(encoding="utf-8")
    assert "class LiteLLMModelGatewayAdapter" in composition
    assert "remote_allowed" in composition


def test_durable_turn_store_keeps_session_authority_and_domain_writes_separate() -> None:
    ports = (KERNEL_ROOT / "ports.py").read_text(encoding="utf-8")
    store = (KERNEL_ROOT / "sqlite_store.py").read_text(encoding="utf-8")
    assert "class TurnStateStorePort" in ports
    for required in ("BEGIN IMMEDIATE", "PRAGMA journal_mode=WAL", "PRAGMA foreign_keys=ON", "expected_sequence"):
        assert required in store
    for forbidden in ("library_items", "memory_publication", "job_store", "provider_secret"):
        assert forbidden not in store.lower()
    # Closed change-type labels are Session projection metadata, not domain
    # repositories or publication writers.  The durable store must still stay
    # independent from product-core implementations.
    assert "from core.product_core" not in store


def test_external_agent_context_composition_does_not_provision_hands_or_secrets() -> None:
    composition = (
        ROOT / "src" / "backend" / "api" / "external_agent_context_runtime.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "get_or_build_ai_runtime", "model_gateway", "mcp", "plugin", "media_hands",
        "secret_store", "vault",
    ):
        assert forbidden not in composition.lower()
    assert "SQLiteAITurnStore" in composition
    assert "ProjectCapabilityProfileStore" in composition
    assert "ProjectBoundaryProfileStore" in composition


def test_ai_http_router_is_a_thin_adapter_over_ai_runtime() -> None:
    route = (ROOT / "src" / "backend" / "api" / "routes" / "ai.py").read_text(encoding="utf-8")
    assert 'APIRouter(prefix="/api/ai"' in route
    assert "get_or_build_ai_runtime" in route
    assert "await asyncio.to_thread(" in route
    for forbidden in ("SQLiteAITurnStore", "ModelRouteRegistry", "LiteLLM", "MemoryRecallCapability", "JsonObjectStore"):
        assert forbidden not in route


def test_default_workbench_question_uses_turn_api_without_provider_bypass() -> None:
    frontend = (ROOT / "src" / "frontend" / "src" / "features" / "rebuild" / "workbenchIntakeApi.js").read_text(encoding="utf-8")
    legacy = (ROOT / "src" / "backend" / "api" / "routes" / "product" / "workbench_compat.py").read_text(encoding="utf-8")
    composition = (ROOT / "src" / "backend" / "api" / "workbench_ai_runtime.py").read_text(encoding="utf-8")
    assert 'AI_TURN_ENDPOINT = "/api/ai/turns"' in frontend
    assert 'desired_outcome: "workbench.question.answer"' in frontend
    assert 'WORKBENCH_DIRECT_QUESTION_ENDPOINT' not in frontend
    assert "_resolve_workbench_answer_provider" not in legacy
    assert "_resolve_workbench_project_reranker" not in legacy
    assert "Legacy DTO adapter" in legacy
    assert "WorkbenchQuestionCapability" in composition
    assert "ModelGatewayPort" in composition
