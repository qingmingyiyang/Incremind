from __future__ import annotations

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.api.ai_profile_resolvers import (
    ProjectAwareCapabilityManifestResolver,
    ProjectAwareContextManifestResolver,
    ProjectProfileResolutionError,
    TurnProjectProfileSnapshotAuthority,
)
from backend.api.model_routing_snapshot_authority import (
    TurnModelRoutingSnapshotAuthority,
    TurnModelRoutingSnapshotAuthorityError,
)
from backend.model_route_context import model_route_provider_context_from_record
from backend.model_route_context import provider_egress_manifest
from backend.model_provider_health import (
    ModelProviderHealthStore,
    ProviderFailureClass,
    ProviderHealthScope,
)
from backend.model_routing_profile import ModelRoutingProfileStore
from backend.model_routing_snapshot import (
    TurnModelRoutingSnapshotError,
    decode_turn_model_routing_snapshot,
    encode_turn_model_routing_snapshot,
    project_turn_model_routing_snapshot,
    turn_model_routing_snapshot_revision,
)
from backend.api.context_benchmark_observation import (
    ContextBenchmarkObservationError,
    context_benchmark_observation_from_turn,
)
from backend.providers import ProviderRegistry
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.provider_egress import ProviderEgressPolicyStore
from core.ai_kernel import CapabilityDefinition, InMemoryTurnPayloadStore
from core.ai_kernel import ModelGatewayAgentPlanner
from core.model_gateway import ModelResult
from core.product_core.model_route_registry import ModelRouteRegistry
from core.product_core.model_route_runtime import ModelRouteRuntimeService


class _Secrets:
    def get(self, _key: str) -> str:
        return "unused"


def _container(root):
    return SimpleNamespace(root_dir=root, secret_store=_Secrets())


def _activate(
    root, *, base_url: str = "http://127.0.0.1:8317", image_generation: bool = False,
) -> None:
    registry, contexts = ModelRouteRegistry(root), []
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
    for revision, (key, provider_id, model, adapter) in enumerate(route_specs):
        provider = ProviderRegistry(root).create({"provider_id": provider_id, "name": provider_id, "llm_provider": "openai", "base_url": base_url, "api_path": "/chat/completions", "model": model, "models": [model], "enabled": True}, fallback={})
        if base_url.startswith("https://"):
            policy = ProviderEgressPolicyStore(root)
            manifest = provider_egress_manifest(provider, policy)
            policy.grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
        context = model_route_provider_context_from_record(root, provider)
        contexts.append(context)
        registry.update(key, {"provider_id": provider_id, "model_name": model, "adapter_kind": adapter, "enabled": True, "reason": "snapshot unit test"}, expected_registry_revision=revision, provider=provider, egress_consented=True)
    runtime = ModelRouteRuntimeService(root)
    compatibility = {
        key: context for (key, *_rest), context in zip(route_specs, contexts, strict=True)
    }
    shadow = runtime.preview(route_keys=list(compatibility), compatibility=compatibility, providers=contexts)
    runtime.activate(shadow_token=shadow["shadow_token"], route_keys=shadow["route_keys"], expected_runtime_revision=0, confirm=True, compatibility=compatibility, providers=contexts)
    ModelRoutingProfileStore(root).update(expected_revision=1, rules_version=1, text_default_tier="standard", tier_routes={"fast": None, "standard": "search.answer", "deep": "tier.deep", "vision": "companion.vision", "image_generation": "tier.image_generation" if image_generation else None})
    ProjectCapabilityProfileStore(root).update("project-a", expected_revision=0, boundary_profile_id="project-boundary-project-a", boundary_profile_revision=1, preferred_model_tier="deep")


def _snapshot(root, *, turn_id: str, required_capability: str = "structured", **kwargs):
    requirement = {
        "structured": ("text", "json_object"),
        "vision": ("image_input", "text"),
        "image_generation": ("image_generation", "image_asset"),
    }[required_capability]
    return project_turn_model_routing_snapshot(_container(root), turn_id=turn_id, project_id="project-a", required_capability=required_capability, modality=requirement[0], output_contract=requirement[1], egress_purpose="workbench_answer", egress_categories=("instructions",), privacy_scope="remote_allowed", **kwargs)


def _benchmark_request(turn_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "turn_id": turn_id,
        "session_id": "session-benchmark-observation",
        "operation_id": "op-lm-suite-test-a-research_turn-r0-linemap",
        "idempotency_key": "idem-lm-suite-test-a-research_turn-r0-linemap",
        "scope": {"kind": "project", "project_id": "project-a", "series_id": None},
        "input": {
            "kind": "text",
            "text": "Evaluate the frozen research fixture.",
            "refs": [{
                "kind": "context_binding",
                "object_id": "binding-research-r1",
                "uri": "crp://context-bindings/project-a/binding-research-r1",
            }],
        },
        "desired_outcome": "context.evaluate",
        "privacy": {
            "mode": "remote_allowed",
            "allow_remote": True,
            "pii": "possible",
            "consent_refs": ["crp://consents/project-a/benchmark-r1"],
            "retention": "local_durable",
        },
        "capability_policy": {"allowed": [], "denied": [], "require_approval": []},
        "context_policy": {
            "include_project_skill": False,
            "include_memory": False,
            "include_session_history": False,
            "max_context_bytes": 262144,
        },
        "approval_policy": {"mode": "risk_based", "auto_approve_read_only": True},
        "created_at": "2026-08-29T10:00:00Z",
    }


def test_snapshot_uses_project_preference_and_is_deterministic(tmp_path) -> None:
    _activate(tmp_path)
    first = _snapshot(tmp_path, turn_id="turn-a", capability_ids=("answer",), skill_snapshot_revision="skill-1", context_policy={"max_context_bytes": 4096}, input_refs=("crp://default/source/one",))
    second = _snapshot(tmp_path, turn_id="turn-b", capability_ids=("answer",), skill_snapshot_revision="skill-1", context_policy={"max_context_bytes": 4096}, input_refs=("crp://default/source/one",))
    assert first["selected"] == {"tier": "deep", "route_key": "tier.deep", "route_revision": 1, "provider_id": "deep-provider", "provider_revision": first["selected"]["provider_revision"], "model_name": "deep-model", "adapter_kind": "openai-compatible", "execution_location": "local_loopback", "reason": "project_preferred_tier"}
    assert first["catalog_revision"] == second["catalog_revision"]
    assert first["prompt_cache_scope"]["identity"] == second["prompt_cache_scope"]["identity"]
    assert decode_turn_model_routing_snapshot(encode_turn_model_routing_snapshot(first)) == first


def test_auxiliary_snapshot_is_fast_only_and_primary_keeps_project_preference(tmp_path) -> None:
    _activate(tmp_path)
    common = {
        "capability_ids": (), "skill_snapshot_revision": None,
        "context_policy": {"max_context_bytes": 4096}, "input_refs": (),
    }
    primary = _snapshot(tmp_path, turn_id="turn-purpose-primary", **common)
    auxiliary = _snapshot(
        tmp_path, turn_id="turn-purpose-aux", model_call_purpose="aux", **common,
    )

    assert primary["requirement"]["model_call_purpose"] == "primary"
    assert primary["selected"]["tier"] == "deep"
    assert auxiliary["requirement"]["model_call_purpose"] == "aux"
    # The fixture deliberately has no fast route: an aux call fails closed
    # instead of silently consuming the project's deep/standard budget.
    assert auxiliary["selected"] is None
    assert auxiliary["prompt_cache_scope"]["identity"] != primary["prompt_cache_scope"]["identity"]


def test_snapshot_rejects_unknown_model_call_purpose(tmp_path) -> None:
    _activate(tmp_path)
    with pytest.raises(TurnModelRoutingSnapshotError, match="purpose"):
        _snapshot(
            tmp_path, turn_id="turn-purpose-invalid", model_call_purpose="summary",
            capability_ids=(), skill_snapshot_revision=None,
            context_policy={"max_context_bytes": 4096}, input_refs=(),
        )


def test_context_benchmark_observation_uses_terminal_and_routing_evidence(tmp_path) -> None:
    _activate(tmp_path, base_url="https://api.example.test")
    turn_id = "turn-benchmark-observation"
    snapshot = _snapshot(
        tmp_path, turn_id=turn_id, capability_ids=(),
        skill_snapshot_revision=None,
        context_policy={"max_context_bytes": 262144},
        input_refs=({
            "kind": "context_binding",
            "object_id": "binding-research-r1",
            "uri": "crp://context-bindings/project-a/binding-research-r1",
        },),
    )
    routing_ref = f"crp://session/{turn_id}/model-routing/ref"
    receipt_ref = f"crp://session/{turn_id}/model-receipt/ref"
    attempt_ref = f"crp://session/{turn_id}/model-wire-attempt-receipt/ref"
    selected = snapshot["selected"]
    routing_revision = turn_model_routing_snapshot_revision(snapshot)
    receipt = {
        "schema_version": "1.0.0",
        "receipt_id": "model-receipt-benchmark-observation",
        "turn_id": turn_id,
        "model_request_id": "model-request-benchmark-observation",
        "status": "completed",
        "requested_at": "2026-08-29T10:00:00+00:00",
        "completed_at": "2026-08-29T10:00:01+00:00",
        "duration_ms": 1000,
        "provider_id": selected["provider_id"],
        "model_id": selected["model_name"],
        "usage_status": "recorded",
        "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        "input_recorded": False,
        "output_recorded": False,
        "error_code": None,
    }
    attempt = {
        "schema_version": "1.0.0",
        "attempt_id": "model-wire-attempt-benchmark-observation",
        "turn_id": turn_id,
        "model_request_id": "model-request-benchmark-observation",
        "attempt_number": 1,
        "routing_snapshot_revision": routing_revision,
        "provider_id": selected["provider_id"],
        "model_id": selected["model_name"],
        "execution_location": selected["execution_location"],
        "status": "succeeded",
        "started_at": "2026-08-29T10:00:00+00:00",
        "completed_at": "2026-08-29T10:00:01+00:00",
        "duration_ms": 1000,
        "usage_status": "reported",
        "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        "cache_status": "unavailable",
        "cache_metadata": None,
        "input_stored": False,
        "output_stored": False,
        "error_code": None,
    }
    payloads = {routing_ref: snapshot, receipt_ref: receipt, attempt_ref: attempt}
    events = (
        {"event_id": "event-model-completed", "turn_id": turn_id, "type": "model.completed", "correlation": {"step_id": "step-benchmark-observation", "model_request_id": "model-request-benchmark-observation"}, "data": {"receipt_ref": receipt_ref, "evidence_refs": [routing_ref, attempt_ref]}},
        {"event_id": "event-turn-completed", "turn_id": turn_id, "type": "turn.completed", "correlation": {"step_id": "step-benchmark-observation", "model_request_id": "model-request-benchmark-observation"}, "data": {"summary": "source:evidence-a supports A"}},
    )

    observation = context_benchmark_observation_from_turn(
        request=_benchmark_request(turn_id),
        events=events, payload_loader=payloads.__getitem__,
        capability_revision="2.9.0", compiler_revision="1.0.0",
        decoding_revision="model-planner-json-t0-v1",
    )

    assert observation.total_tokens == 150
    assert observation.suite_run_id == "suite-test-a"
    assert observation.case_id == "research_turn"
    assert observation.variant == "linemap"
    assert observation.operation_id == "op-lm-suite-test-a-research_turn-r0-linemap"
    assert observation.model_receipt_ref == receipt_ref
    assert observation.routing_snapshot_ref == routing_ref
    assert observation.model_request_id == receipt["model_request_id"]
    assert observation.model_attempt_id == attempt["attempt_id"]
    assert observation.routing_snapshot_revision == routing_revision
    assert observation.execution_location == attempt["execution_location"] == "remote"
    assert observation.output_text == "source:evidence-a supports A"

    drifted_payloads = {
        **payloads,
        receipt_ref: {**receipt, "provider_id": "provider-drift"},
    }
    with pytest.raises(ContextBenchmarkObservationError, match="routing evidence drifted"):
        context_benchmark_observation_from_turn(
            request=_benchmark_request(turn_id),
            events=events, payload_loader=drifted_payloads.__getitem__,
            capability_revision="2.9.0", compiler_revision="1.0.0",
            decoding_revision="model-planner-json-t0-v1",
        )

    request_drift_payloads = {
        **payloads,
        receipt_ref: {**receipt, "model_request_id": "model-request-drift"},
    }
    with pytest.raises(ContextBenchmarkObservationError, match="routing evidence drifted"):
        context_benchmark_observation_from_turn(
            request=_benchmark_request(turn_id),
            events=events, payload_loader=request_drift_payloads.__getitem__,
            capability_revision="2.9.0", compiler_revision="1.0.0",
            decoding_revision="model-planner-json-t0-v1",
        )

    correlation_drift_events = tuple(
        {
            **event,
            "correlation": {
                **event["correlation"],
                "step_id": "step-drift",
            },
        }
        if event["type"] == "turn.completed" else event
        for event in events
    )
    with pytest.raises(ContextBenchmarkObservationError, match="terminal correlation drifted"):
        context_benchmark_observation_from_turn(
            request=_benchmark_request(turn_id),
            events=correlation_drift_events, payload_loader=payloads.__getitem__,
            capability_revision="2.9.0", compiler_revision="1.0.0",
            decoding_revision="model-planner-json-t0-v1",
        )

    attempt_drift_payloads = {
        **payloads,
        attempt_ref: {**attempt, "model_request_id": "model-request-drift"},
    }
    with pytest.raises(ContextBenchmarkObservationError, match="attempt Receipt drifted"):
        context_benchmark_observation_from_turn(
            request=_benchmark_request(turn_id),
            events=events, payload_loader=attempt_drift_payloads.__getitem__,
            capability_revision="2.9.0", compiler_revision="1.0.0",
            decoding_revision="model-planner-json-t0-v1",
        )

    revision_drift_payloads = {
        **payloads,
        attempt_ref: {**attempt, "routing_snapshot_revision": "b" * 64},
    }
    with pytest.raises(ContextBenchmarkObservationError, match="attempt Receipt drifted"):
        context_benchmark_observation_from_turn(
            request=_benchmark_request(turn_id),
            events=events, payload_loader=revision_drift_payloads.__getitem__,
            capability_revision="2.9.0", compiler_revision="1.0.0",
            decoding_revision="model-planner-json-t0-v1",
        )

    for location_payload in (
        {key: value for key, value in attempt.items() if key != "execution_location"},
        {**attempt, "execution_location": "local_loopback"},
    ):
        location_drift_payloads = {
            **payloads,
            attempt_ref: location_payload,
        }
        with pytest.raises(ContextBenchmarkObservationError, match="attempt Receipt drifted"):
            context_benchmark_observation_from_turn(
                request=_benchmark_request(turn_id),
                events=events, payload_loader=location_drift_payloads.__getitem__,
                capability_revision="2.9.0", compiler_revision="1.0.0",
                decoding_revision="model-planner-json-t0-v1",
            )

    invalid_request = {
        **_benchmark_request(turn_id),
        "operation_id": "op-lm-suite-test-a-research_turn-r0-linear",
    }
    with pytest.raises(ContextBenchmarkObservationError, match="must not use refs"):
        context_benchmark_observation_from_turn(
            request=invalid_request,
            events=events,
            payload_loader=payloads.__getitem__,
            capability_revision="2.9.0",
            compiler_revision="1.0.0",
            decoding_revision="model-planner-json-t0-v1",
        )


def test_snapshot_allows_only_text_default_fallback_and_hard_vision(tmp_path) -> None:
    _activate(tmp_path)
    ProviderRegistry(tmp_path).update("deep-provider", {"api_path": "/v2/chat/completions"}, fallback={})
    text = _snapshot(tmp_path, turn_id="turn-text")
    vision = _snapshot(tmp_path, turn_id="turn-vision", required_capability="vision")
    assert text["selected"]["tier"] == "standard"
    assert text["selected"]["reason"] == "text_default_fallback"
    assert vision["selected"]["tier"] == "vision"
    assert "tier_not_applicable" in next(item for item in vision["tiers"] if item["tier"] == "deep")["exclusion_reasons"]


def test_new_turn_uses_text_fallback_when_primary_health_is_open(tmp_path) -> None:
    _activate(tmp_path)
    baseline = _snapshot(tmp_path, turn_id="turn-health-baseline")
    selected = baseline["selected"]
    boundary = baseline["boundary"]
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

    fallback = _snapshot(tmp_path, turn_id="turn-health-fallback")

    assert fallback["selected"]["tier"] == "standard"
    assert fallback["selected"]["reason"] == "text_default_fallback"
    deep = next(item for item in fallback["tiers"] if item["tier"] == "deep")
    assert deep["exclusion_reasons"] == ["provider_health_open"]


def test_health_never_converts_hard_vision_requirement_to_text(tmp_path) -> None:
    _activate(tmp_path)
    baseline = _snapshot(
        tmp_path, turn_id="turn-vision-health-baseline", required_capability="vision",
    )
    selected = baseline["selected"]
    boundary = baseline["boundary"]
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
        ProviderFailureClass.SERVER_ERROR,
    )

    unavailable = _snapshot(
        tmp_path, turn_id="turn-vision-health-open", required_capability="vision",
    )

    assert unavailable["selected"] is None
    vision = next(item for item in unavailable["tiers"] if item["tier"] == "vision")
    assert vision["exclusion_reasons"] == ["provider_health_open"]


def test_image_generation_requires_dedicated_adapter_and_freezes_execution_location(tmp_path) -> None:
    _activate(tmp_path, image_generation=True)

    snapshot = _snapshot(
        tmp_path, turn_id="turn-image-generation", required_capability="image_generation",
    )

    assert snapshot["requirement"]["required_capability"] == "image_generation"
    assert snapshot["requirement"]["modality"] == "image_generation"
    assert snapshot["requirement"]["output_contract"] == "image_asset"
    assert snapshot["selected"] is not None
    assert snapshot["selected"]["tier"] == "image_generation"
    assert snapshot["selected"]["adapter_kind"] == "openai-compatible-image-generation"
    assert snapshot["selected"]["execution_location"] == "local_loopback"
    assert all(
        "tier_not_applicable" in tier["exclusion_reasons"]
        for tier in snapshot["tiers"] if tier["tier"] != "image_generation"
    )
    assert decode_turn_model_routing_snapshot(encode_turn_model_routing_snapshot(snapshot)) == snapshot


def test_image_generation_fails_closed_without_dedicated_route(tmp_path) -> None:
    _activate(tmp_path)

    snapshot = _snapshot(
        tmp_path, turn_id="turn-image-generation-unconfigured", required_capability="image_generation",
    )

    assert snapshot["selected"] is None
    image = next(item for item in snapshot["tiers"] if item["tier"] == "image_generation")
    assert image["exclusion_reasons"] == ["tier_unconfigured"]


def test_image_generation_rejects_a_reused_text_route(tmp_path) -> None:
    _activate(tmp_path)
    ModelRoutingProfileStore(tmp_path).update(
        expected_revision=2,
        rules_version=1,
        text_default_tier="standard",
        tier_routes={
            "fast": None,
            "standard": "search.answer",
            "deep": "tier.deep",
            "vision": "companion.vision",
            "image_generation": "search.answer",
        },
    )

    snapshot = _snapshot(
        tmp_path, turn_id="turn-image-generation-text-route", required_capability="image_generation",
    )

    assert snapshot["selected"] is None
    image = next(item for item in snapshot["tiers"] if item["tier"] == "image_generation")
    assert "required_capability_unsupported" in image["exclusion_reasons"]


def test_project_boundary_can_remove_every_provider_candidate(tmp_path) -> None:
    _activate(tmp_path, base_url="https://api.example.invalid")
    ProjectBoundaryProfileStore(tmp_path).update(
        "project-a", mode="sealed", remote_default="deny", expected_revision=0,
    )

    snapshot = _snapshot(tmp_path, turn_id="turn-sealed-model-egress")

    assert snapshot["selected"] is None
    assert "boundary_denied" in next(
        item for item in snapshot["tiers"] if item["tier"] == "deep"
    )["exclusion_reasons"]


def test_snapshot_reports_exclusions_and_rejects_sensitive_data(tmp_path) -> None:
    _activate(tmp_path)
    snapshot = _snapshot(tmp_path, turn_id="turn-model-name-tamper")
    snapshot["selected"]["model_name"] = "https://user@example.invalid/v1"
    selected_tier = next(item for item in snapshot["tiers"] if item["eligible"])
    selected_tier["route"]["model_name"] = "https://user@example.invalid/v1"
    with pytest.raises(TurnModelRoutingSnapshotError, match="model name"):
        encode_turn_model_routing_snapshot(snapshot)
    runtime = ModelRouteRuntimeService(tmp_path)
    runtime.deactivate(expected_runtime_revision=1, confirm=True)
    snapshot = _snapshot(tmp_path, turn_id="turn-off")
    standard = next(item for item in snapshot["tiers"] if item["tier"] == "standard")
    assert "runtime_inactive" in standard["exclusion_reasons"]
    assert "tier_unconfigured" in next(item for item in snapshot["tiers"] if item["tier"] == "fast")["exclusion_reasons"]
    assert "tier_not_applicable" in next(
        item for item in snapshot["tiers"] if item["tier"] == "image_generation"
    )["exclusion_reasons"]
    snapshot["runtime"]["endpoint"] = "http://secret.invalid"
    try:
        encode_turn_model_routing_snapshot(snapshot)
    except TurnModelRoutingSnapshotError:
        pass
    else:
        raise AssertionError("sensitive snapshot field was accepted")
    snapshot = _snapshot(tmp_path, turn_id="turn-tamper")
    snapshot["prompt_cache_scope"]["project_id"] = "other-project"
    try:
        encode_turn_model_routing_snapshot(snapshot)
    except TurnModelRoutingSnapshotError:
        pass
    else:
        raise AssertionError("cross identity tamper was accepted")


@pytest.mark.parametrize(
    "unsafe_ref",
    (
        "crp://default/email=user@example.com",
        "crp://default/source/one?token=secret",
        "crp://default/source/one#user@example.com",
    ),
)
def test_snapshot_rejects_non_opaque_input_reference(tmp_path, unsafe_ref) -> None:
    _activate(tmp_path)

    with pytest.raises(TurnModelRoutingSnapshotError, match="opaque crp identity"):
        _snapshot(tmp_path, turn_id="turn-unsafe-ref", input_refs=(unsafe_ref,))


def test_project_manifests_share_one_model_routing_snapshot_reference(tmp_path) -> None:
    _activate(tmp_path)
    request = _authority_request()
    payloads = InMemoryTurnPayloadStore()
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    boundary_store = ProjectBoundaryProfileStore(tmp_path)
    profiles = TurnProjectProfileSnapshotAuthority(capability_store, boundary_store)
    routing = TurnModelRoutingSnapshotAuthority(_container(tmp_path), payloads)
    capability = CapabilityDefinition(
        "workbench.question.answer", 1, "read", False, "read_only",
        "crp://default/contracts/in.schema.json", "crp://default/contracts/out.schema.json",
    )

    manifest = ProjectAwareCapabilityManifestResolver(
        profiles, model_routing=routing,
    ).resolve(request, (capability,))
    context = ProjectAwareContextManifestResolver(
        profiles, model_routing=routing,
    ).resolve(
        request, f"crp://session/{request['turn_id']}/capability-manifest/ref", manifest,
    )

    assert manifest.model_routing_snapshot_ref is not None
    assert manifest.model_routing_snapshot_revision is not None
    entry = next(item for item in context.entries if item.kind == "model_routing_snapshot")
    assert entry.payload_ref == manifest.model_routing_snapshot_ref
    assert entry.revision_identity == manifest.model_routing_snapshot_revision
    assert entry.disclosure == "audit_only"
    assert entry.content_bytes == 0


def test_authority_replays_existing_immutable_snapshot_without_reprojection(tmp_path, monkeypatch) -> None:
    _activate(tmp_path)
    payloads = InMemoryTurnPayloadStore()
    authority = TurnModelRoutingSnapshotAuthority(_container(tmp_path), payloads)
    request, identity = _authority_request_and_identity(tmp_path)
    first = authority.acquire(request, **identity)
    assert first is not None

    def _unexpected_projection(*_args, **_kwargs):
        raise AssertionError("existing Turn must not project a new model routing snapshot")

    monkeypatch.setattr(
        "backend.api.model_routing_snapshot_authority.project_turn_model_routing_snapshot",
        _unexpected_projection,
    )
    replay = authority.acquire(request, **identity)

    assert replay == first


def test_child_agent_binding_requires_a_verifier_and_freezes_the_profile_tier(tmp_path) -> None:
    _activate(tmp_path)
    payloads = InMemoryTurnPayloadStore()
    request, identity = _authority_request_and_identity(tmp_path)
    request["desired_outcome"] = "agent.child.execute"
    binding = {
        "schema_version": "1.0.0",
        "kind": "internal_agent_run_v1",
        "run_id": "agent-run-child-001",
        "role": "subagent",
        "profile_id": "subagent.researcher",
        "profile_revision": 3,
        "model_tier": "standard",
        "parent_run_id": "agent-run-parent-001",
        "link_id": "agent-link-child-001",
        "reservation_id": "agent-reservation-001",
        "spawn_operation_id": "op-agent-spawn-child-001",
        "depth": 1,
        "cancel_epoch": 0,
        "budget_snapshot_ref": "crp://agent-runs/agent-run-child-001/budget-snapshot",
    }
    request["agent_binding"] = binding

    with pytest.raises(TurnModelRoutingSnapshotAuthorityError, match="not trusted"):
        TurnModelRoutingSnapshotAuthority(_container(tmp_path), payloads).acquire(request, **identity)

    observed_verifier_inputs = []

    def verify(turn_request, candidate):
        observed_verifier_inputs.append((turn_request, candidate))
        if (
            turn_request.get("turn_id") != "turn-routing-snapshot-unit"
            or turn_request.get("scope", {}).get("project_id") != "project-a"
        ):
            raise ValueError("binding does not belong to this Turn")
        return candidate

    authority = TurnModelRoutingSnapshotAuthority(
        _container(tmp_path), payloads, agent_binding_verifier=verify,
    )
    frozen = authority.acquire(request, **identity)

    assert frozen is not None
    assert observed_verifier_inputs[-1] == (request, binding)
    assert frozen.payload["agent"] == binding
    assert frozen.payload["selected"]["tier"] == "standard"

    drifted = copy.deepcopy(request)
    drifted["agent_binding"]["model_tier"] = "deep"
    with pytest.raises(TurnModelRoutingSnapshotAuthorityError, match="drifted"):
        authority.acquire(drifted, **identity)

    forged_turn = copy.deepcopy(request)
    forged_turn["turn_id"] = "turn-forged-agent-binding"
    with pytest.raises(TurnModelRoutingSnapshotAuthorityError, match="not trusted"):
        authority.acquire(forged_turn, **identity)

    forged_project = copy.deepcopy(request)
    forged_project["scope"]["project_id"] = "project-forged"
    with pytest.raises(TurnModelRoutingSnapshotAuthorityError, match="not trusted"):
        authority.acquire(forged_project, **identity)

    malformed_snapshot = copy.deepcopy(frozen.payload)
    malformed_snapshot["agent"] = None
    with pytest.raises(TurnModelRoutingSnapshotError, match="agent binding"):
        encode_turn_model_routing_snapshot(malformed_snapshot)
    malformed_snapshot = copy.deepcopy(frozen.payload)
    malformed_snapshot["unexpected"] = True
    with pytest.raises(TurnModelRoutingSnapshotError, match="shape"):
        encode_turn_model_routing_snapshot(malformed_snapshot)


def test_main_agent_binding_freezes_the_main_profile_tier_for_primary_route(tmp_path) -> None:
    _activate(tmp_path)
    payloads = InMemoryTurnPayloadStore()
    request, identity = _authority_request_and_identity(tmp_path)
    request["turn_id"] = "turn-main-agent-routing-unit"
    request["desired_outcome"] = "agent.child.execute"
    binding = {
        "schema_version": "1.0.0",
        "kind": "internal_agent_run_v1",
        "run_id": "agent-run-main-001",
        "role": "main",
        "profile_id": "main.orchestrator",
        "profile_revision": 4,
        "model_tier": "standard",
        "depth": 0,
        "cancel_epoch": 0,
        "budget_snapshot_ref": "crp://agent-runs/agent-run-main-001/budget-snapshot",
    }
    request["agent_binding"] = binding

    def verify(turn_request, candidate):
        if (
            turn_request.get("turn_id") != "turn-main-agent-routing-unit"
            or turn_request.get("scope", {}).get("project_id") != "project-a"
        ):
            raise ValueError("binding does not belong to this Turn")
        return candidate

    frozen = TurnModelRoutingSnapshotAuthority(
        _container(tmp_path), payloads, agent_binding_verifier=verify,
    ).acquire(request, **identity)

    assert frozen is not None
    assert frozen.payload["agent"] == binding
    # The project defaults to deep; this proves the verified main profile's
    # configured tier controls the primary request instead.
    assert frozen.payload["selected"]["tier"] == "standard"


def test_agent_route_binding_selects_exact_synthetic_route_and_fails_closed_on_drift(tmp_path) -> None:
    _activate(tmp_path)
    binding = {
        "schema_version": "1.0.0", "kind": "internal_agent_run_v1",
        "run_id": "agent-run-route-binding-001", "role": "main",
        "profile_id": "main.orchestrator", "profile_revision": 2,
        "model_tier": "standard", "model_route_key": "tier.deep",
        "model_route_revision": 1, "depth": 0, "cancel_epoch": 0,
        "budget_snapshot_ref": "crp://agent-runs/agent-run-route-binding-001/budget-snapshot",
    }
    snapshot = _snapshot(
        tmp_path, turn_id="turn-agent-route-binding-001", agent_binding=binding,
    )
    assert snapshot["selected"]["route_key"] == "tier.deep"
    assert snapshot["selected"]["route_revision"] == 1
    drifted = dict(binding, model_route_revision=2)
    with pytest.raises(TurnModelRoutingSnapshotError, match="binding is unavailable or drifted"):
        _snapshot(
            tmp_path, turn_id="turn-agent-route-binding-drift-001", agent_binding=drifted,
        )
    with pytest.raises(TurnModelRoutingSnapshotError, match="binding is unavailable or drifted"):
        _snapshot(
            tmp_path,
            turn_id="turn-agent-route-binding-missing-001",
            agent_binding=dict(binding, model_route_key="synthetic.missing", model_route_revision=1),
        )


@pytest.mark.parametrize(
    ("outcome", "binding"),
    (
        (
            "project.answer",
            {
                "schema_version": "1.0.0",
                "kind": "internal_agent_run_v1",
                "run_id": "agent-run-main-project-answer-001",
                "role": "main",
                "profile_id": "main.orchestrator",
                "profile_revision": 1,
                "model_tier": "standard",
                "depth": 0,
                "cancel_epoch": 0,
                "budget_snapshot_ref": "crp://agent-runs/agent-run-main-project-answer-001/budget-snapshot",
            },
        ),
        (
            "agent.steward.plan",
            {
                "schema_version": "1.0.0",
                "kind": "internal_agent_run_v1",
                "run_id": "agent-run-steward-plan-001",
                "role": "subagent",
                "profile_id": "steward.scheduler",
                "profile_revision": 1,
                "model_tier": "standard",
                "parent_run_id": "agent-run-main-project-answer-001",
                "link_id": "agent-link-steward-plan-001",
                "reservation_id": "agent-reservation-steward-plan-001",
                "spawn_operation_id": "op-agent-spawn-steward-plan-001",
                "depth": 1,
                "cancel_epoch": 0,
                "budget_snapshot_ref": "crp://agent-runs/agent-run-steward-plan-001/budget-snapshot",
            },
        ),
    ),
)
def test_organization_outcomes_freeze_a_real_snapshot_when_no_route_is_eligible(
    tmp_path, outcome, binding,
) -> None:
    _activate(tmp_path)
    ModelRouteRuntimeService(tmp_path).deactivate(
        expected_runtime_revision=1, confirm=True,
    )
    payloads = InMemoryTurnPayloadStore()
    request, identity = _authority_request_and_identity(tmp_path)
    request["desired_outcome"] = outcome
    request["agent_binding"] = binding
    authority = TurnModelRoutingSnapshotAuthority(
        _container(tmp_path), payloads,
        agent_binding_verifier=lambda _request, candidate: candidate,
    )

    frozen = authority.acquire(request, **identity)

    assert frozen is not None
    assert frozen.payload["selected"] is None
    assert frozen.payload["agent"] == binding
    assert payloads.get_immutable_payload(
        str(request["turn_id"]), authority.snapshot_kind,
    ) == (frozen.payload_ref, frozen.payload)


def test_unknown_agent_outcome_does_not_gain_an_implicit_model_route(tmp_path) -> None:
    _activate(tmp_path)
    payloads = InMemoryTurnPayloadStore()
    request, identity = _authority_request_and_identity(tmp_path)
    request["desired_outcome"] = "agent.unknown"
    request["agent_binding"] = {
        "schema_version": "1.0.0",
        "kind": "internal_agent_run_v1",
        "run_id": "agent-run-main-unknown-001",
        "role": "main",
        "profile_id": "main.orchestrator",
        "profile_revision": 1,
        "model_tier": "standard",
        "depth": 0,
        "cancel_epoch": 0,
        "budget_snapshot_ref": "crp://agent-runs/agent-run-main-unknown-001/budget-snapshot",
    }
    authority = TurnModelRoutingSnapshotAuthority(
        _container(tmp_path), payloads,
        agent_binding_verifier=lambda _request, candidate: candidate,
    )

    assert authority.acquire(request, **identity) is None
    assert payloads.get_immutable_payload(
        str(request["turn_id"]), authority.snapshot_kind,
    ) is None


def test_agent_primary_route_never_falls_back_when_its_frozen_tier_is_unavailable(
    tmp_path,
) -> None:
    _activate(tmp_path)
    binding = {
        "schema_version": "1.0.0",
        "kind": "internal_agent_run_v1",
        "run_id": "agent-run-fast-unavailable-001",
        "role": "subagent",
        "profile_id": "subagent.explorer",
        "profile_revision": 1,
        "model_tier": "fast",
        "parent_run_id": "agent-run-parent-001",
        "link_id": "agent-link-fast-unavailable-001",
        "reservation_id": "agent-reservation-fast-unavailable-001",
        "spawn_operation_id": "op-agent-spawn-fast-unavailable-001",
        "depth": 1,
        "cancel_epoch": 0,
        "budget_snapshot_ref": "crp://agent-runs/agent-run-fast-unavailable-001/budget-snapshot",
    }

    frozen = _snapshot(
        tmp_path,
        turn_id="turn-agent-fast-unavailable",
        agent_binding=binding,
    )

    assert frozen["agent"] == binding
    assert frozen["selected"] is None
    tier_state = {item["tier"]: item for item in frozen["tiers"]}
    assert "tier_unconfigured" in tier_state["fast"]["exclusion_reasons"]
    assert tier_state["standard"]["exclusion_reasons"] == ["tier_not_selected"]
    assert tier_state["deep"]["exclusion_reasons"] == ["tier_not_selected"]


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("project_profile_revision", 2),
        ("boundary_profile_revision", 2),
        ("capability_ids", ("workbench.question.answer", "memory.recall")),
        ("skill_snapshot_revision", "skill-other"),
    ),
)
def test_authority_fails_closed_when_frozen_identity_drifts(tmp_path, field, changed) -> None:
    _activate(tmp_path)
    payloads = InMemoryTurnPayloadStore()
    authority = TurnModelRoutingSnapshotAuthority(_container(tmp_path), payloads)
    request, identity = _authority_request_and_identity(tmp_path)
    assert authority.acquire(request, **identity) is not None
    drifted = dict(identity)
    drifted[field] = changed

    with pytest.raises(TurnModelRoutingSnapshotAuthorityError, match="authority drifted"):
        authority.acquire(request, **drifted)


@pytest.mark.parametrize(
    "request_change",
    (
        {"desired_outcome": "companion.vision.analyze"},
        {"privacy": {"mode": "local_only", "allow_remote": False}},
    ),
)
def test_authority_replay_rejects_requirement_or_privacy_drift(
    tmp_path, request_change,
) -> None:
    _activate(tmp_path)
    payloads = InMemoryTurnPayloadStore()
    authority = TurnModelRoutingSnapshotAuthority(_container(tmp_path), payloads)
    request, identity = _authority_request_and_identity(tmp_path)
    assert authority.acquire(request, **identity) is not None
    drifted = {**request, **request_change}

    with pytest.raises(TurnModelRoutingSnapshotAuthorityError, match="authority drifted"):
        authority.acquire(drifted, **identity)


def test_authority_accepts_opaque_uri_only_input_reference(tmp_path) -> None:
    _activate(tmp_path)
    payloads = InMemoryTurnPayloadStore()
    authority = TurnModelRoutingSnapshotAuthority(_container(tmp_path), payloads)
    request, identity = _authority_request_and_identity(tmp_path)
    request["input"] = {"refs": [{"uri": "crp://default/source/source-1"}]}

    snapshot = authority.acquire(request, **identity)

    assert snapshot is not None
    assert snapshot.payload["requirement"]["input_refs"] == [
        {"ref": "crp://default/source/source-1"},
    ]


def test_context_evaluate_freezes_structured_json_routing_and_planner_parameters(
    tmp_path,
) -> None:
    _activate(tmp_path)
    payloads = InMemoryTurnPayloadStore()
    authority = TurnModelRoutingSnapshotAuthority(_container(tmp_path), payloads)
    request, identity = _authority_request_and_identity(tmp_path)
    request["desired_outcome"] = "context.evaluate"
    frozen = authority.acquire(request, **identity)
    assert frozen is not None
    assert frozen.payload["requirement"] == {
        **frozen.payload["requirement"],
        "required_capability": "structured",
        "modality": "text",
        "output_contract": "json_object",
        "egress_purpose": "search_answer",
        "egress_categories": ["instructions", "source_excerpt"],
    }

    class RecordingGateway:
        def __init__(self) -> None:
            self.request = None

        def invoke(self, model_request):
            self.request = model_request
            return ModelResult({"type": "complete", "summary": "ok", "evidence_refs": []}, "test", "model", {})

    gateway = RecordingGateway()
    decision = ModelGatewayAgentPlanner(gateway).plan(
        request, [], (), payloads,
    )
    assert decision["type"] == "complete"
    assert gateway.request is not None
    parameters = gateway.request.parameters
    assert parameters["_routing_project_id"] == "project-a"
    assert parameters["_model_routing_snapshot_ref"] == frozen.payload_ref
    assert parameters["_model_routing_snapshot_revision"] == frozen.revision
    assert parameters["_model_routing_snapshot"]["selected"] == frozen.payload["selected"]

    # The planner uses a detached copy of the immutable payload; mutating a
    # caller-held authority view cannot change the already-planned gateway call.
    frozen.payload["selected"]["model_name"] = "caller-drift"
    assert parameters["_model_routing_snapshot"]["selected"]["model_name"] != "caller-drift"


def test_context_rejects_tampered_model_routing_manifest_binding(tmp_path) -> None:
    _activate(tmp_path)
    request = _authority_request()
    payloads = InMemoryTurnPayloadStore()
    profiles = TurnProjectProfileSnapshotAuthority(
        ProjectCapabilityProfileStore(tmp_path), ProjectBoundaryProfileStore(tmp_path),
    )
    routing = TurnModelRoutingSnapshotAuthority(_container(tmp_path), payloads)
    capability = CapabilityDefinition(
        "workbench.question.answer", 1, "read", False, "read_only",
        "crp://default/contracts/in.schema.json", "crp://default/contracts/out.schema.json",
    )
    manifest = ProjectAwareCapabilityManifestResolver(
        profiles, model_routing=routing,
    ).resolve(request, (capability,))

    with pytest.raises(ProjectProfileResolutionError, match="model routing bindings drifted"):
        ProjectAwareContextManifestResolver(profiles, model_routing=routing).resolve(
            request,
            f"crp://session/{request['turn_id']}/capability-manifest/ref",
            replace(manifest, model_routing_snapshot_revision="tampered"),
        )


def _authority_request() -> dict[str, object]:
    return {
        "turn_id": "turn-routing-snapshot-unit",
        "desired_outcome": "workbench.question.answer",
        "scope": {"kind": "project", "project_id": "project-a", "series_id": None},
        "privacy": {"mode": "remote_allowed", "allow_remote": True},
        "context_policy": {"max_context_bytes": 4096},
        "input": {"refs": []},
        "capability_policy": {
            "allowed": ["workbench.question.answer"], "denied": [], "require_approval": [],
        },
    }


def _authority_request_and_identity(root):
    capability = ProjectCapabilityProfileStore(root).get("project-a").profile
    boundary = ProjectBoundaryProfileStore(root).get("project-a").profile
    return _authority_request(), {
        "project_id": "project-a",
        "project_profile_id": capability.profile_id,
        "project_profile_revision": capability.revision,
        "boundary_profile_id": boundary.profile_id,
        "boundary_profile_revision": boundary.revision,
        "capability_ids": ("workbench.question.answer",),
        "skill_snapshot_revision": None,
    }
