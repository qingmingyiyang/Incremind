from __future__ import annotations

from types import SimpleNamespace
from threading import Event, Thread

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.ai import router as ai_router
from backend.model_route_context import model_route_provider_context_from_record
from backend.model_provider_health import (
    ModelProviderHealthStore,
    ProviderFailureClass,
    ProviderHealthScope,
    ProviderHealthState,
)
from backend.model_routing_profile import ModelRoutingProfileStore
from backend.model_routing_snapshot import (
    project_turn_model_routing_snapshot,
    turn_model_routing_snapshot_revision,
)
from backend.model_runtime import (
    ImageGenerationProviderAdapter,
    LiteLLMModelGatewayAdapter,
    ModelRuntimeError,
    resolve_frozen_image_generation_gateway,
    resolve_tiered_model_gateway_runtime,
    validate_tiered_model_routing_profile,
)
from backend.providers import ProviderRegistry
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from core.model_gateway import ImageGenerationRequest, ImageGenerationResult, ModelRequest, ModelResult
from core.product_core.model_route_registry import ModelRouteRegistry
from core.product_core.model_route_runtime import ModelRouteRuntimeService
import backend.shared.llm.litellm_gateway as litellm_gateway
import backend.shared.llm.connection_diagnostic as connection_diagnostic
import backend.model_runtime as model_runtime


def test_connection_diagnostic_uses_leaf_transport_composer_and_runtime_compatibility_export(monkeypatch) -> None:
    captured = {}

    class DiagnosticGateway:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def test_connection(self):
            return "reachable"

    monkeypatch.setattr(connection_diagnostic, "LiteLLMCompletionGateway", DiagnosticGateway)
    guard = object()
    result = model_runtime.run_model_connection_diagnostic(
        provider="custom_openai",
        model_name="diagnostic-model",
        base_url="https://provider.example/v1",
        api_key_provider=lambda: "candidate-secret",
        anonymous=False,
        reasoning_effort="medium",
        egress_guard=guard,
    )

    assert result == "reachable"
    assert model_runtime.run_model_connection_diagnostic is connection_diagnostic.run_model_connection_diagnostic
    assert captured == {
        "provider": "openai",
        "model": "diagnostic-model",
        "base_url": "https://provider.example/v1",
        "api_key": None,
        "api_key_provider": captured["api_key_provider"],
        "anonymous": False,
        "reasoning_effort": "medium",
        "egress_guard": guard,
        "egress_purpose": "connection_test",
    }


class _Secrets:
    def get(self, _key: str) -> str:
        return "test-key"


class _NoSecrets:
    def get(self, _key: str) -> str:
        raise AssertionError("loopback image route must not read a secret")


def _container(root):
    return SimpleNamespace(root_dir=root, secret_store=_Secrets())


def _provider(root, provider_id: str, model: str):
    return ProviderRegistry(root).create(
        {
            "provider_id": provider_id,
            "name": provider_id,
            "llm_provider": "openai",
            "base_url": "http://127.0.0.1:8317",
            "api_path": "/chat/completions",
            "model": model,
            "models": [model],
            "enabled": True,
        },
        fallback={},
    )


def _activate(root, *, image_generation: bool = False) -> None:
    registry = ModelRouteRegistry(root)
    route_specs = [
        ("search.answer", "standard-provider", "standard-model", "openai-compatible"),
        ("tier.deep", "deep-provider", "deep-model", "openai-compatible"),
        ("companion.vision", "vision-provider", "vision-model", "openai-compatible-vision"),
    ]
    if image_generation:
        route_specs.append((
            "tier.image_generation", "image-provider", "image-model",
            "openai-compatible-image-generation",
        ))
    contexts = []
    compatibility = {}
    for revision, (route_key, provider_id, model, adapter_kind) in enumerate(route_specs):
        record = _provider(root, provider_id, model)
        context = model_route_provider_context_from_record(root, record)
        contexts.append(context)
        compatibility[route_key] = context
        registry.update(
            route_key,
            {
                "provider_id": provider_id,
                "model_name": model,
                "adapter_kind": adapter_kind,
                "enabled": True,
                "reason": "tier runtime test",
            },
            expected_registry_revision=revision,
            provider=record,
            egress_consented=True,
        )
    runtime = ModelRouteRuntimeService(root)
    shadow = runtime.preview(
        route_keys=[item[0] for item in route_specs],
        compatibility=compatibility,
        providers=contexts,
    )
    runtime.activate(
        shadow_token=str(shadow["shadow_token"]),
        route_keys=shadow["route_keys"],
        expected_runtime_revision=0,
        confirm=True,
        compatibility=compatibility,
        providers=contexts,
    )
    ModelRoutingProfileStore(root).update(
        expected_revision=1,
        rules_version=1,
        text_default_tier="standard",
        tier_routes={
            "fast": None,
            "standard": "search.answer",
            "deep": "tier.deep",
            "vision": "companion.vision",
            "image_generation": "tier.image_generation" if image_generation else None,
        },
    )
    ProjectCapabilityProfileStore(root).update(
        "project-a",
        expected_revision=0,
        boundary_profile_id="project-boundary-project-a",
        boundary_profile_revision=1,
        preferred_model_tier="deep",
    )


def _snapshot(
    root, *, turn_id: str, required_capability: str = "structured",
    egress_purpose: str, egress_categories: tuple[str, ...],
):
    return project_turn_model_routing_snapshot(
        _container(root),
        turn_id=turn_id,
        project_id="project-a",
        required_capability=required_capability,
        modality="text" if required_capability == "structured" else "image_input",
        output_contract="json_object" if required_capability == "structured" else "text",
        egress_purpose=egress_purpose,
        egress_categories=egress_categories,
        privacy_scope="remote_allowed",
    )


def _image_snapshot(root, *, turn_id: str, privacy_scope: str = "remote_allowed"):
    return project_turn_model_routing_snapshot(
        _container(root), turn_id=turn_id, project_id="project-a",
        required_capability="image_generation", modality="image_generation",
        output_contract="image_asset", egress_purpose="image_generation",
        egress_categories=("instructions",), privacy_scope=privacy_scope,
    )


def _health_scope(snapshot) -> ProviderHealthScope:
    selected = snapshot["selected"]
    boundary = snapshot["boundary"]
    return ProviderHealthScope(
        project_id=snapshot["project"]["project_id"],
        boundary_profile_id=boundary["profile_id"],
        boundary_revision=boundary["profile_revision"],
        route_key=selected["route_key"],
        provider_id=selected["provider_id"],
        provider_revision=selected["provider_revision"],
        model_name=selected["model_name"],
    )


def test_frozen_image_gateway_revalidates_dedicated_route_and_uses_local_anonymous_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate(tmp_path, image_generation=True)
    snapshot = _image_snapshot(tmp_path, turn_id="turn-image")
    calls: list[ImageGenerationRequest] = []

    def generate(_self, request: ImageGenerationRequest) -> ImageGenerationResult:
        calls.append(request)
        return ImageGenerationResult(
            capability="image_generation", image_bytes=b"\x89PNG\r\n\x1a\nimage",
            media_type="image/png", provider="openai", model="image-model",
        )

    monkeypatch.setattr(ImageGenerationProviderAdapter, "generate", generate)
    resolution = resolve_frozen_image_generation_gateway(
        SimpleNamespace(root_dir=tmp_path, secret_store=_NoSecrets()),
        project_id="project-a", snapshot=snapshot,
        privacy_scope="remote_allowed",
    )
    assert resolution.gateway is not None
    result = resolution.gateway.generate(ImageGenerationRequest(
        capability="image_generation", prompt="make a lake", parameters={"size": "1024x1024"},
        privacy_scope="remote_allowed",
    ))
    assert result.image_bytes.startswith(b"\x89PNG")
    assert len(calls) == 1 and calls[0].prompt == "make a lake"


def test_frozen_image_gateway_rejects_execution_location_drift(tmp_path) -> None:
    _activate(tmp_path, image_generation=True)
    snapshot = _image_snapshot(tmp_path, turn_id="turn-image-drift")
    ProviderRegistry(tmp_path).update(
        "image-provider", {"base_url": "https://images.example.invalid/v1"}, fallback={},
    )
    resolution = resolve_frozen_image_generation_gateway(
        _container(tmp_path), project_id="project-a", snapshot=snapshot,
        privacy_scope="remote_allowed",
    )
    assert resolution.gateway is not None
    with pytest.raises(ModelRuntimeError, match="drifted"):
        resolution.gateway.generate(ImageGenerationRequest(
            capability="image_generation", prompt="make a lake", parameters={},
            privacy_scope="remote_allowed",
        ))


def test_frozen_image_gateway_allows_local_only_loopback_without_secret(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate(tmp_path, image_generation=True)
    snapshot = _image_snapshot(tmp_path, turn_id="turn-image-local", privacy_scope="local_only")
    monkeypatch.setattr(
        ImageGenerationProviderAdapter, "generate",
        lambda _self, request: ImageGenerationResult(
            capability="image_generation", image_bytes=b"\x89PNG\r\n\x1a\nimage",
            media_type="image/png", provider="openai", model="image-model",
        ),
    )
    resolution = resolve_frozen_image_generation_gateway(
        SimpleNamespace(root_dir=tmp_path, secret_store=_NoSecrets()),
        project_id="project-a", snapshot=snapshot, privacy_scope="local_only",
    )
    assert resolution.gateway is not None
    assert resolution.gateway.generate(ImageGenerationRequest(
        capability="image_generation", prompt="make a lake", parameters={}, privacy_scope="local_only",
    )).media_type == "image/png"


def test_frozen_image_gateway_does_not_reenter_project_boundary_during_tool_dispatch(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate(tmp_path, image_generation=True)
    snapshot = _image_snapshot(tmp_path, turn_id="turn-image-no-boundary-reentry")
    monkeypatch.setattr(
        ImageGenerationProviderAdapter, "generate",
        lambda _self, _request: ImageGenerationResult(
            capability="image_generation", image_bytes=b"\x89PNG\r\n\x1a\nimage",
            media_type="image/png", provider="openai", model="image-model",
        ),
    )
    class ForbiddenBoundaryStore:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("image tool data-plane must not reenter Project Boundary")
    class ForbiddenModelEgressBoundary:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("frozen image tool must not run a second Boundary evaluator")
    monkeypatch.setattr(model_runtime, "ProjectBoundaryProfileStore", ForbiddenBoundaryStore)
    monkeypatch.setattr(model_runtime, "AIModelEgressBoundary", ForbiddenModelEgressBoundary)
    gateway = resolve_frozen_image_generation_gateway(
        _container(tmp_path), project_id="project-a", snapshot=snapshot,
        privacy_scope="remote_allowed",
    ).gateway
    assert gateway is not None
    assert gateway.generate(ImageGenerationRequest(
        capability="image_generation", prompt="make a lake", parameters={},
        privacy_scope="remote_allowed",
    )).media_type == "image/png"


def _completion_response(content: str) -> dict[str, object]:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {},
    }


def test_project_tier_and_hard_vision_route_use_activated_runtime(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _container(tmp_path)
    text = resolve_tiered_model_gateway_runtime(
        container,
        required_capability="structured",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    )
    vision = resolve_tiered_model_gateway_runtime(
        container,
        required_capability="vision",
        egress_purpose="companion_vision",
        egress_categories=("image_frame", "instructions"),
    )
    assert text.gateway is not None and vision.gateway is not None
    _activate(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []

    def invoke(self, request):
        calls.append((self.model_name, dict(request.parameters)))
        return ModelResult({"answer": "ok"}, "openai", self.model_name, {})

    monkeypatch.setattr(LiteLLMModelGatewayAdapter, "invoke", invoke)
    text_snapshot = _snapshot(
        tmp_path, turn_id="turn-text", egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    )
    result = text.gateway.invoke(ModelRequest(
        capability="structured",
        input="question",
        parameters={
            "_routing_project_id": "project-a",
            "_model_routing_snapshot": text_snapshot,
        },
        privacy_scope="remote_allowed",
    ))
    assert result.model == "deep-model"
    assert result.routing_evidence is not None
    assert result.routing_evidence["route_key"] == "tier.deep"
    assert result.routing_evidence["model_routing_catalog_revision"] == text_snapshot["catalog_revision"]
    assert result.routing_evidence["prompt_cache_scope_identity"] == text_snapshot["prompt_cache_scope"]["identity"]

    vision_snapshot = _snapshot(
        tmp_path, turn_id="turn-vision", required_capability="vision",
        egress_purpose="companion_vision",
        egress_categories=("image_frame", "instructions"),
    )
    result = vision.gateway.invoke(ModelRequest(
        capability="vision",
        input="image",
        parameters={
            "_routing_project_id": "project-a",
            "_model_routing_snapshot": vision_snapshot,
            "image_payload": {"media_type": "image/png", "pixels": b"pixels"},
        },
        privacy_scope="remote_allowed",
    ))
    assert result.model == "vision-model"
    assert [item[0] for item in calls] == ["deep-model", "vision-model"]
    assert all(
        "_routing_project_id" not in parameters
        and "_model_routing_snapshot" not in parameters
        for _, parameters in calls
    )


def test_explicit_text_default_fallback_is_frozen_before_invoke(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate(tmp_path)
    ProviderRegistry(tmp_path).update(
        "deep-provider", {"api_path": "/v2/chat/completions"}, fallback={},
    )
    snapshot = _snapshot(
        tmp_path, turn_id="turn-fallback", egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    )
    assert snapshot["selected"]["tier"] == "standard"
    monkeypatch.setattr(
        LiteLLMModelGatewayAdapter,
        "invoke",
        lambda self, _request: ModelResult(
            {"answer": "fallback"}, "openai", self.model_name, {},
        ),
    )
    gateway = resolve_tiered_model_gateway_runtime(
        _container(tmp_path),
        required_capability="structured",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    ).gateway
    assert gateway is not None
    result = gateway.invoke(ModelRequest(
        capability="structured",
        input="question",
        parameters={
            "_routing_project_id": "project-a",
            "_model_routing_snapshot": snapshot,
        },
        privacy_scope="remote_allowed",
    ))
    assert result.model == "standard-model"


def test_tiered_gateway_route_recorder_failure_blocks_provider_egress(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate(tmp_path)
    snapshot = _snapshot(
        tmp_path, turn_id="turn-route-recorder", egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    )
    provider_calls = 0

    def invoke(_self, _request):
        nonlocal provider_calls
        provider_calls += 1
        return ModelResult({"answer": "must-not-run"}, "openai", "model", {})

    class Sink:
        def model_call_routed(self, **_metadata):
            raise OSError("route event persistence failed")

    monkeypatch.setattr(LiteLLMModelGatewayAdapter, "invoke", invoke)
    gateway = resolve_tiered_model_gateway_runtime(
        _container(tmp_path), required_capability="structured",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    ).gateway
    assert gateway is not None

    with pytest.raises(OSError, match="persistence failed"):
        gateway.invoke(ModelRequest(
            capability="structured",
            input="question",
            parameters={
                "_routing_project_id": "project-a",
                "_model_routing_snapshot": snapshot,
                "_model_routing_snapshot_ref": (
                    "crp://session/turn-route-recorder/"
                    "turn-model-routing-snapshot-v1/ref"
                ),
                "_model_routing_snapshot_revision": (
                    turn_model_routing_snapshot_revision(snapshot)
                ),
            },
            privacy_scope="remote_allowed",
            metadata_sink=Sink(),
        ))

    assert provider_calls == 0


def test_authority_drift_after_snapshot_fails_closed_without_dynamic_fallback(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate(tmp_path)
    snapshot = _snapshot(
        tmp_path, turn_id="turn-before-drift", egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        LiteLLMModelGatewayAdapter,
        "invoke",
        lambda self, _request: calls.append(self.model_name) or ModelResult(
            {"answer": "unexpected"}, "openai", self.model_name, {},
        ),
    )
    ProviderRegistry(tmp_path).update(
        "deep-provider", {"api_path": "/v2/chat/completions"}, fallback={},
    )
    gateway = resolve_tiered_model_gateway_runtime(
        _container(tmp_path), required_capability="structured",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    ).gateway
    assert gateway is not None
    with pytest.raises(ModelRuntimeError, match="drifted"):
        gateway.invoke(ModelRequest(
            capability="structured",
            input="question",
            parameters={
                "_routing_project_id": "project-a",
                "_model_routing_snapshot": snapshot,
            },
            privacy_scope="remote_allowed",
        ))
    assert calls == []


def test_health_change_after_snapshot_does_not_reroute_current_turn(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate(tmp_path)
    snapshot = _snapshot(
        tmp_path, turn_id="turn-frozen-before-health", egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    )
    selected = snapshot["selected"]
    boundary = snapshot["boundary"]
    ModelProviderHealthStore(tmp_path).observe_failure(
        ProviderHealthScope(
            project_id="project-a",
            boundary_profile_id=boundary["profile_id"],
            boundary_revision=boundary["profile_revision"],
            route_key=selected["route_key"],
            provider_id=selected["provider_id"],
            provider_revision=selected["provider_revision"],
            model_name=selected["model_name"],
        ),
        ProviderFailureClass.TIMEOUT,
    )
    calls: list[str] = []
    monkeypatch.setattr(
        LiteLLMModelGatewayAdapter,
        "invoke",
        lambda self, _request: calls.append(self.model_name) or ModelResult(
            {"answer": "frozen"}, "openai", self.model_name, {},
        ),
    )
    gateway = resolve_tiered_model_gateway_runtime(
        _container(tmp_path), required_capability="structured",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    ).gateway
    assert gateway is not None

    result = gateway.invoke(ModelRequest(
        capability="structured", input="question",
        parameters={
            "_routing_project_id": "project-a",
            "_model_routing_snapshot": snapshot,
        },
        privacy_scope="remote_allowed",
    ))

    assert result.model == "deep-model"
    assert calls == ["deep-model"]


def test_provider_timeout_is_not_retried_and_only_next_turn_falls_back(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate(tmp_path)
    snapshot = _snapshot(
        tmp_path, turn_id="turn-provider-timeout", egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    )
    calls: list[str] = []

    def timeout(**request):
        calls.append(str(request["model"]))
        raise TimeoutError("ambiguous delivery")

    monkeypatch.setattr(litellm_gateway, "_anonymous_openai_compatible_completion", timeout)
    gateway = resolve_tiered_model_gateway_runtime(
        _container(tmp_path), required_capability="structured",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    ).gateway
    assert gateway is not None

    with pytest.raises(TimeoutError, match="ambiguous delivery"):
        gateway.invoke(ModelRequest(
            capability="structured", input="question",
            parameters={
                "_routing_project_id": "project-a",
                "_model_routing_snapshot": snapshot,
            },
            privacy_scope="remote_allowed",
        ))

    assert len(calls) == 1 and calls[0].endswith("deep-model")
    next_turn = _snapshot(
        tmp_path, turn_id="turn-after-provider-timeout",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    )
    assert next_turn["selected"]["tier"] == "standard"
    assert next_turn["selected"]["reason"] == "text_default_fallback"


def test_boundary_denial_before_transport_does_not_change_provider_health(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate(tmp_path)
    ProjectBoundaryProfileStore(tmp_path).update(
        "project-a", mode="guarded", remote_default="review",
        denied_effects=("read",), expected_revision=0,
    )
    snapshot = _snapshot(
        tmp_path, turn_id="turn-pre-egress-deny", egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    )
    assert snapshot["selected"] is None
    calls = 0

    def completion(**_request):
        nonlocal calls
        calls += 1
        return _completion_response('{"answer":"unexpected"}')

    monkeypatch.setattr(litellm_gateway, "_anonymous_openai_compatible_completion", completion)
    gateway = resolve_tiered_model_gateway_runtime(
        _container(tmp_path), required_capability="structured",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    ).gateway
    assert gateway is not None

    with pytest.raises(ModelRuntimeError, match="route is unavailable"):
        gateway.invoke(ModelRequest(
            capability="structured", input="secret sk-1234567890abcdef",
            parameters={
                "_routing_project_id": "project-a",
                "_model_routing_snapshot": snapshot,
            },
            privacy_scope="remote_allowed",
        ))

    assert calls == 0
    deep = next(item for item in snapshot["tiers"] if item["tier"] == "deep")
    route = deep["route"]
    assert isinstance(route, dict)
    boundary = snapshot["boundary"]
    assert ModelProviderHealthStore(tmp_path).get(ProviderHealthScope(
        project_id="project-a", boundary_profile_id=boundary["profile_id"],
        boundary_revision=boundary["profile_revision"], route_key=route["route_key"],
        provider_id=route["provider_id"], provider_revision=route["provider_revision"],
        model_name=route["model_name"],
    )).state is ProviderHealthState.UNKNOWN


def test_local_timeout_before_transport_does_not_change_provider_health(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate(tmp_path)
    snapshot = _snapshot(
        tmp_path, turn_id="turn-local-timeout", egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    )
    calls = 0

    def completion(**_request):
        nonlocal calls
        calls += 1
        return _completion_response('{"answer":"unexpected"}')

    class ExpiredControl:
        remaining_timeout_ms = 0

        def checkpoint(self) -> None:
            raise TimeoutError("local deadline")

    monkeypatch.setattr(litellm_gateway, "_anonymous_openai_compatible_completion", completion)
    gateway = resolve_tiered_model_gateway_runtime(
        _container(tmp_path), required_capability="structured",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    ).gateway
    assert gateway is not None

    with pytest.raises(TimeoutError, match="local deadline"):
        gateway.invoke(ModelRequest(
            capability="structured", input="question",
            parameters={
                "_routing_project_id": "project-a",
                "_model_routing_snapshot": snapshot,
            },
            privacy_scope="remote_allowed",
            execution_control=ExpiredControl(),
        ))

    assert calls == 0
    assert ModelProviderHealthStore(tmp_path).get(_health_scope(snapshot)).state is ProviderHealthState.UNKNOWN


def test_successful_transport_with_invalid_json_keeps_provider_healthy(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate(tmp_path)
    snapshot = _snapshot(
        tmp_path, turn_id="turn-invalid-json", egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    )
    monkeypatch.setattr(
        litellm_gateway, "_anonymous_openai_compatible_completion",
        lambda **_request: _completion_response("not-json"),
    )
    gateway = resolve_tiered_model_gateway_runtime(
        _container(tmp_path), required_capability="structured",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    ).gateway
    assert gateway is not None

    with pytest.raises(ModelRuntimeError, match="not JSON"):
        gateway.invoke(ModelRequest(
            capability="structured", input="question",
            parameters={
                "_routing_project_id": "project-a",
                "_model_routing_snapshot": snapshot,
            },
            privacy_scope="remote_allowed",
        ))

    assert ModelProviderHealthStore(tmp_path).get(_health_scope(snapshot)).state is ProviderHealthState.HEALTHY


def test_provider_authority_mutation_waits_for_inflight_dispatch(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate(tmp_path)
    snapshot = _snapshot(
        tmp_path, turn_id="turn-authority-fence", egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    )
    transport_started = Event()
    release_transport = Event()
    mutation_finished = Event()
    errors: list[BaseException] = []

    def completion(**_request):
        transport_started.set()
        assert release_transport.wait(2)
        return _completion_response('{"answer":"ok"}')

    monkeypatch.setattr(litellm_gateway, "_anonymous_openai_compatible_completion", completion)
    gateway = resolve_tiered_model_gateway_runtime(
        _container(tmp_path), required_capability="structured",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    ).gateway
    assert gateway is not None

    def invoke() -> None:
        try:
            gateway.invoke(ModelRequest(
                capability="structured", input="question",
                parameters={
                    "_routing_project_id": "project-a",
                    "_model_routing_snapshot": snapshot,
                },
                privacy_scope="remote_allowed",
            ))
        except BaseException as error:
            errors.append(error)

    def mutate() -> None:
        ProviderRegistry(tmp_path).update(
            "deep-provider", {"api_path": "/v2/chat/completions"}, fallback={},
        )
        mutation_finished.set()

    invoke_thread = Thread(target=invoke)
    invoke_thread.start()
    assert transport_started.wait(2)
    mutation_thread = Thread(target=mutate)
    mutation_thread.start()
    assert not mutation_finished.wait(0.1)
    release_transport.set()
    invoke_thread.join(2)
    mutation_thread.join(2)

    assert errors == []
    assert mutation_finished.is_set()


def test_persisted_authority_binding_rejects_runtime_drift(tmp_path) -> None:
    _activate(tmp_path)
    container = _container(tmp_path)
    store = ModelRoutingProfileStore(tmp_path)
    current = store.get().profile
    binding = validate_tiered_model_routing_profile(container, current)
    store.update(
        expected_revision=current.revision,
        rules_version=current.rules_version,
        text_default_tier=current.text_default_tier,
        tier_routes=dict(current.tier_routes),
        authority_binding=binding,
    )
    snapshot = _snapshot(
        tmp_path, turn_id="turn-binding", egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    )
    runtime = ModelRouteRuntimeService(tmp_path)
    runtime.deactivate(
        expected_runtime_revision=int(runtime.status()["runtime_revision"]),
        confirm=True,
    )
    gateway = resolve_tiered_model_gateway_runtime(
        container,
        required_capability="structured",
        egress_purpose="search_answer",
        egress_categories=("instructions", "source_excerpt"),
    ).gateway
    assert gateway is not None
    with pytest.raises(ModelRuntimeError, match="drifted"):
        gateway.invoke(ModelRequest(
            capability="structured",
            input="question",
            parameters={
                "_routing_project_id": "project-a",
                "_model_routing_snapshot": snapshot,
            },
            privacy_scope="remote_allowed",
        ))


def test_profile_http_update_persists_current_active_authority_binding(tmp_path) -> None:
    _activate(tmp_path)
    app = FastAPI()
    app.state.container = _container(tmp_path)
    app.include_router(ai_router)
    with TestClient(app) as client:
        response = client.put("/api/ai/model-routing-profile", json={
            "expected_revision": 2,
            "rules_version": 2,
            "text_default_tier": "standard",
            "tier_routes": {
                "fast": None,
                "standard": "search.answer",
                "deep": "tier.deep",
                "vision": "companion.vision",
                "image_generation": None,
            },
            "confirm": True,
        })
    assert response.status_code == 200
    binding = response.json()["authority_binding"]
    assert binding["registry_revision"] == 3
    assert binding["runtime_revision"] == 1
    assert len(binding["activation_fingerprint"]) == 64
