from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import base64
from contextlib import ExitStack
from dataclasses import dataclass, replace
import json
from pathlib import Path
from urllib.parse import urlsplit

from backend.model_route_context import model_route_provider_context
from backend.model_route_context import list_model_route_provider_contexts, provider_egress_manifest
from backend.model_provider_health import (
    ModelProviderHealthError,
    ModelProviderHealthStore,
    ProviderHealthScope,
)
from backend.model_routing_profile import ModelRoutingProfileStore
from backend.model_routing_snapshot import (
    TurnModelRoutingSnapshotError,
    turn_model_routing_snapshot_revision,
    validate_turn_model_routing_snapshot,
)
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.secret_egress import SecretEgressBroker
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from backend.security.ai_model_egress_boundary import (
    AIModelEgressBoundary,
    AIModelEgressBoundaryError,
)
from backend.security.provider_egress import ProviderEgressPolicyStore, build_provider_egress_guard
from backend.shared.llm.base_url import resolve_openai_compatible_api_base_url
from backend.shared.llm.connection_diagnostic import (
    normalize_litellm_provider,
    run_model_connection_diagnostic,
)
from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
from backend.shared.llm.image_generation_gateway import ImageGenerationProviderAdapter
from core.model_gateway import (
    ImageGenerationGatewayPort,
    ImageGenerationRequest,
    ImageGenerationResult,
    ModelExecutionControlPort,
    ModelGatewayPort,
    ModelRequest,
    ModelResult,
)
from core.ai_kernel.ports import is_durable_model_wire_commit_witness
from core.ai_tooling import (
    ModelRouteCandidate,
)
from core.product_core.model_route_registry import ModelRouteRegistry, ModelRouteRegistryError, ModelRouteRegistryNotFound
from core.product_core.model_dispatch_authority import model_dispatch_authority_fence
from core.product_core.model_route_runtime import ModelRouteRuntimeService


class ModelRuntimeError(RuntimeError):
    pass


class ModelRoutingDrift(ModelRuntimeError):
    pass


@dataclass(slots=True)
class _CommitReleasingWireAttemptSink:
    """Release authority locks only after the durable wire commit succeeds."""

    delegate: object
    release_authority: Callable[[], None]
    _committed: bool = False

    def begin_model_wire_attempt(self):
        if self._committed:
            raise ModelRuntimeError("model wire attempt is already committed")
        begin = getattr(self.delegate, "begin_model_wire_attempt", None)
        if not callable(begin):
            raise ModelRuntimeError("model wire attempt recorder is unavailable")
        handle = begin()
        if not is_durable_model_wire_commit_witness(
            getattr(handle, "durable_commit_witness", None)
        ):
            # Compatibility and direct diagnostic sinks do not prove an atomic
            # Session reservation.  Keep the conservative full-I/O lock.
            return handle
        # The immutable dispatch payload, Session Event and durable reservation
        # are now committed.  This is the egress linearization point.  Release
        # both the root authority fence and project Boundary snapshot before
        # the transport blocks on remote I/O.
        self._committed = True
        self.release_authority()
        _after_durable_model_wire_commit()
        return handle


def _after_durable_model_wire_commit() -> None:
    """Fault-injection seam for the committed-before-transport crash window."""


class LiteLLMModelGatewayAdapter(ModelGatewayPort):
    """The only production adapter from the domain model port to LiteLLM."""

    def __init__(
        self, gateway: LiteLLMCompletionGateway, *, provider: str, model: str,
        allow_local_only: bool = False,
    ) -> None:
        self._gateway = gateway
        self._provider = provider
        self._model = model
        self._allow_local_only = allow_local_only

    @property
    def model_name(self) -> str:
        return self._model

    def invoke(self, request: ModelRequest) -> ModelResult:
        if request.privacy_scope != "remote_allowed" and not (
            request.privacy_scope == "local_only" and self._allow_local_only
        ):
            raise ModelRuntimeError("model gateway route requires remote_allowed privacy scope")
        control = request.execution_control
        if control is not None:
            control.checkpoint()
        messages = request.parameters.get("messages")
        if messages is None:
            messages = [{"role": "user", "content": request.input}]
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            raise ModelRuntimeError("model messages must be an array")
        structured = [dict(item) for item in messages if isinstance(item, Mapping)]
        if len(structured) != len(messages) or not structured:
            raise ModelRuntimeError("model messages are invalid")
        if request.capability == "vision":
            structured = _vision_messages(structured, request.parameters.get("image_payload"))
        metadata_sink = request.metadata_sink
        if metadata_sink is not None:
            metadata_sink.model_call_started(provider=self._provider, model=self._model)
        try:
            call_parameters = {
                "temperature": float(request.parameters.get("temperature", 0)),
                "response_format": request.parameters.get("response_format"),
                "max_tokens": _optional_int(request.parameters.get("max_tokens")),
                "timeout": _effective_timeout(request.parameters.get("timeout"), control),
            }
            complete_with_usage = getattr(self._gateway, "complete_text_with_usage", None)
            if callable(complete_with_usage):
                text, usage, cache_observation = complete_with_usage(
                    structured,
                    **call_parameters,
                    wire_attempt_sink=request.wire_attempt_sink,
                )
            else:
                text = self._gateway.complete_text(
                    structured,
                    **call_parameters,
                    wire_attempt_sink=request.wire_attempt_sink,
                )
                usage, cache_observation = {}, None
        except Exception:
            if metadata_sink is not None:
                try:
                    metadata_sink.model_call_failed()
                except Exception:
                    pass
            raise
        if control is not None:
            control.checkpoint()
        if metadata_sink is not None:
            cache_observer = getattr(metadata_sink, "model_call_cache_observed", None)
            if cache_observation is not None and callable(cache_observer):
                try:
                    cache_observer(observation=cache_observation)
                except Exception:
                    pass
            try:
                metadata_sink.model_call_completed(usage=usage)
            except Exception:
                pass
        output: object = text
        if request.capability == "structured":
            try:
                output = json.loads(text)
            except json.JSONDecodeError as error:
                raise ModelRuntimeError("structured model response is not JSON") from error
        return ModelResult(output=output, provider=self._provider, model=self._model, usage=usage)


@dataclass(frozen=True, slots=True)
class ModelGatewayResolution:
    gateway: LiteLLMModelGatewayAdapter | None
    model_name: str
    adapter_kind: str
    egress_consented: bool
    enabled: bool
    routing_evidence: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ImageGenerationGatewayResolution:
    """A frozen image route; its output is still ephemeral provider data."""

    gateway: ImageGenerationGatewayPort | None
    model_name: str
    adapter_kind: str
    egress_consented: bool
    enabled: bool
    routing_evidence: Mapping[str, object] | None = None


class FrozenImageGenerationGateway(ImageGenerationGatewayPort):
    """Run one image request against an already-frozen Turn decision.

    The constructor takes no mutable routing hints. Each invocation rechecks
    the current registry/runtime/provider facts without reentering the Project
    Boundary lock already held by the Tool data plane. This keeps a resumed
    Turn from silently changing model, secret source, egress location, or
    route revision.
    """

    def __init__(
        self, container: object, *, project_id: str, snapshot: Mapping[str, object],
        privacy_scope: str, egress_purpose: str, egress_categories: tuple[str, ...],
    ) -> None:
        self._container = container
        self._project_id = project_id
        self._snapshot = dict(snapshot)
        self._privacy_scope = privacy_scope
        self._egress_purpose = egress_purpose
        self._egress_categories = egress_categories

    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        if request.privacy_scope != self._privacy_scope:
            raise ModelRuntimeError("image generation privacy scope drifted")
        selected = self._snapshot["selected"]
        if not isinstance(selected, Mapping):
            raise ModelRuntimeError("image generation route is unavailable")
        root_dir = Path(getattr(self._container, "root_dir")).resolve()
        provider_record = _revalidate_frozen_image_route(
            self._container, snapshot=self._snapshot, selected=selected,
        )
        execution_location = _provider_execution_location(root_dir, provider_record)
        if selected.get("execution_location") != execution_location:
            raise ModelRoutingDrift("image generation execution location drifted")
        if request.privacy_scope == "local_only" and execution_location != "local_loopback":
            raise ModelRuntimeError("image generation local-only route is unavailable")
        local_anonymous = execution_location == "local_loopback"
        api_key_provider = None
        if not local_anonymous:
            api_key_provider = _secret_api_key_provider(
                self._container, project_id=self._project_id,
                provider_id=str(selected["provider_id"]),
                base_url=resolve_openai_compatible_api_base_url(str(provider_record.get("base_url") or "")),
                purpose=self._egress_purpose,
            )
        provider = normalize_litellm_provider(provider_record.get("llm_provider"))
        adapter = ImageGenerationProviderAdapter(
            provider=provider, model=str(selected["model_name"]),
            base_url=resolve_openai_compatible_api_base_url(str(provider_record.get("base_url") or "")),
            api_key=None, api_key_provider=api_key_provider, anonymous=local_anonymous,
            egress_guard=build_provider_egress_guard(
                root_dir, provider_id=str(selected["provider_id"]),
                endpoint=resolve_openai_compatible_api_base_url(str(provider_record.get("base_url") or "")),
            ),
            egress_purpose=self._egress_purpose,
            egress_categories=self._egress_categories,
        )
        # The surrounding Tool dispatch fence already owns and linearizes the
        # project Boundary snapshot. Re-entering it here deadlocks on the
        # non-reentrant profile lock. The adapter receives those same,
        # previously authorized tool arguments; its egress guard remains the
        # canonical provider manifest/consent check for this one wire call.
        return adapter.generate(request)


def resolve_frozen_image_generation_gateway(
    container: object, *, project_id: str, snapshot: Mapping[str, object],
    privacy_scope: str, egress_purpose: str = "image_generation",
    egress_categories: tuple[str, ...] = ("instructions",),
) -> ImageGenerationGatewayResolution:
    """Create an explicit image gateway from a Turn's immutable snapshot.

    Callers must obtain ``project_id`` and ``snapshot`` from the immutable
    Turn payload; this function never consults a current-tier default.
    """
    request = ModelRequest(
        capability="image_generation", input="[image-generation-routing-check]",
        parameters={"_routing_project_id": project_id, "_model_routing_snapshot": snapshot},
        privacy_scope=privacy_scope,
    )
    frozen = _frozen_tiered_routing_snapshot(
        request, required_capability="image_generation", egress_purpose=egress_purpose,
        egress_categories=egress_categories,
    )
    selected = frozen["selected"]
    assert isinstance(selected, Mapping)
    if str(selected.get("adapter_kind")) != "openai-compatible-image-generation":
        raise ModelRuntimeError("image generation requires its dedicated adapter")
    return ImageGenerationGatewayResolution(
        FrozenImageGenerationGateway(
            container, project_id=project_id, snapshot=frozen,
            privacy_scope=privacy_scope, egress_purpose=egress_purpose,
            egress_categories=egress_categories,
        ),
        str(selected["model_name"]), str(selected["adapter_kind"]), True, True,
    )


class TieredModelGatewayAdapter(ModelGatewayPort):
    """Resolve one project-scoped tier against the activated route authority per call."""

    def __init__(
        self, container: object, *, required_capability: str,
        egress_purpose: str, egress_categories: tuple[str, ...],
    ) -> None:
        self._container = container
        self._required_capability = required_capability
        self._egress_purpose = egress_purpose
        self._egress_categories = egress_categories

    def invoke(self, request: ModelRequest) -> ModelResult:
        snapshot = _frozen_tiered_routing_snapshot(
            request,
            required_capability=self._required_capability,
            egress_purpose=self._egress_purpose,
            egress_categories=self._egress_categories,
        )
        project_id = str(snapshot["project"]["project_id"])
        snapshot_ref = request.parameters.get("_model_routing_snapshot_ref")
        snapshot_revision = request.parameters.get("_model_routing_snapshot_revision")
        expected_revision = turn_model_routing_snapshot_revision(snapshot)
        turn_id = str(snapshot["turn"]["turn_id"])
        has_snapshot_binding = snapshot_ref is not None or snapshot_revision is not None
        if request.metadata_sink is not None or has_snapshot_binding:
            if (
                not isinstance(snapshot_ref, str)
                or not snapshot_ref.startswith(
                    f"crp://session/{turn_id}/turn-model-routing-snapshot-v1/"
                )
                or snapshot_revision != expected_revision
            ):
                raise ModelRuntimeError("tiered model routing snapshot reference drifted")
        selected = snapshot["selected"]
        assert isinstance(selected, Mapping)
        parameters = dict(request.parameters)
        parameters.pop("_routing_project_id", None)
        parameters.pop("_model_routing_snapshot", None)
        parameters.pop("_model_routing_snapshot_ref", None)
        parameters.pop("_model_routing_snapshot_revision", None)
        root_dir = Path(getattr(self._container, "root_dir")).resolve()
        measurement_recorder = getattr(
            request.metadata_sink,
            "model_dispatch_authority_observed",
            None,
        )
        if measurement_recorder is not None and not callable(measurement_recorder):
            raise ModelRuntimeError("model dispatch authority recorder is unavailable")
        measurement_sink = (
            (lambda measurement: measurement_recorder(**measurement))
            if measurement_recorder is not None
            else None
        )
        try:
            with ExitStack() as authority_stack:
                authority_stack.enter_context(model_dispatch_authority_fence(
                    root_dir,
                    measurement_sink=measurement_sink,
                ))
                resolution = _resolve_tiered_model_gateway(
                    self._container,
                    project_id=project_id.strip(),
                    required_capability=self._required_capability,
                    egress_purpose=self._egress_purpose,
                    egress_categories=self._egress_categories,
                    snapshot=snapshot,
                )
                if resolution.gateway is None:
                    raise ModelRuntimeError("tiered model route is unavailable")
                _require_current_authority_matches_snapshot(
                    self._container,
                    project_id=project_id,
                    snapshot=snapshot,
                    resolution=resolution,
                )
                authorized = authority_stack.enter_context(
                    AIModelEgressBoundary(root_dir).dispatch_fence(
                    turn_id=turn_id,
                    project_id=project_id,
                    route_key=str(selected["route_key"]),
                    provider_id=str(selected["provider_id"]),
                    egress_categories=self._egress_categories,
                    privacy_scope=request.privacy_scope,
                    execution_location=str(selected["execution_location"]),
                    input_text=request.input,
                    parameters=parameters,
                    expected_profile_id=str(snapshot["boundary"]["profile_id"]),
                    expected_profile_revision=int(snapshot["boundary"]["profile_revision"]),
                    )
                )
                # The current Provider was re-resolved above and its canonical
                # egress manifest was required to match the frozen snapshot.
                # Record only that verified non-sensitive wire location.
                execution_location = str(selected["execution_location"])
                route_recorder = getattr(request.metadata_sink, "model_call_routed", None)
                if request.metadata_sink is not None:
                    if not callable(route_recorder):
                        raise ModelRuntimeError("model routing recorder is unavailable")
                    route_recorder(
                        snapshot_ref=str(snapshot_ref),
                        snapshot_revision=expected_revision,
                        prompt_cache_scope_identity=str(snapshot["prompt_cache_scope"]["identity"]),
                        provider=str(selected["provider_id"]),
                        model=str(selected["model_name"]),
                        execution_location=execution_location,
                        purpose=request.purpose,
                    )
                attempt_sink = (
                    request.metadata_sink
                    if callable(getattr(request.metadata_sink, "begin_model_wire_attempt", None))
                    else request.wire_attempt_sink
                )
                committed_sink = (
                    _CommitReleasingWireAttemptSink(attempt_sink, authority_stack.close)
                    if attempt_sink is not None else None
                )
                result = resolution.gateway.invoke(replace(
                    request,
                    input=authorized.input_text,
                    parameters=authorized.parameters,
                    wire_attempt_sink=committed_sink,
                ))
        except AIModelEgressBoundaryError as error:
            raise ModelRuntimeError("model Provider egress is not authorized") from error
        evidence = dict(resolution.routing_evidence or {})
        evidence.update({
            "model_routing_snapshot_revision": turn_model_routing_snapshot_revision(snapshot),
            "model_routing_catalog_revision": snapshot["catalog_revision"],
            "prompt_cache_scope_identity": snapshot["prompt_cache_scope"]["identity"],
        })
        return replace(result, routing_evidence=evidence)


def _frozen_tiered_routing_snapshot(
    request: ModelRequest, *, required_capability: str, egress_purpose: str,
    egress_categories: tuple[str, ...],
) -> dict[str, object]:
    """Accept only the immutable decision prepared before this model call."""
    try:
        snapshot = validate_turn_model_routing_snapshot(
            request.parameters.get("_model_routing_snapshot")
        )
    except TurnModelRoutingSnapshotError as error:
        raise ModelRuntimeError("tiered model routing snapshot is invalid") from error
    project_id = request.parameters.get("_routing_project_id")
    requirement = snapshot["requirement"]
    if (
        not isinstance(project_id, str)
        or project_id.strip() != snapshot["project"]["project_id"]
        or request.capability != required_capability
        or requirement["required_capability"] != required_capability
        or request.privacy_scope != requirement["privacy_scope"]
        or request.privacy_scope not in {"remote_allowed", "local_only"}
        or (
            request.privacy_scope == "local_only"
            and (
                not isinstance(snapshot["selected"], Mapping)
                or snapshot["selected"].get("execution_location") != "local_loopback"
            )
        )
        or requirement["egress_purpose"] != egress_purpose
        or tuple(requirement["egress_categories"]) != tuple(sorted(egress_categories))
        or request.purpose != requirement.get("model_call_purpose", "primary")
    ):
        raise ModelRuntimeError("tiered model routing snapshot request does not match")
    if snapshot["selected"] is None:
        raise ModelRuntimeError("tiered model route is unavailable")
    return snapshot


def _require_current_authority_matches_snapshot(
    container: object, *, project_id: str, snapshot: Mapping[str, object],
    resolution: ModelGatewayResolution,
) -> None:
    """A Turn never silently switches to a newly resolved route."""
    evidence = resolution.routing_evidence
    if not isinstance(evidence, Mapping):
        raise ModelRuntimeError("tiered model routing snapshot drifted")
    selected = snapshot["selected"]
    if not isinstance(selected, Mapping):
        raise ModelRuntimeError("tiered model route is unavailable")
    root_dir = Path(getattr(container, "root_dir")).resolve()
    boundary = ProjectBoundaryProfileStore(root_dir).get(project_id).profile
    expected = {
        "project_id": snapshot["project"]["project_id"],
        "project_profile_id": snapshot["profile"]["profile_id"],
        "project_profile_revision": snapshot["profile"]["profile_revision"],
        "boundary_profile_id": snapshot["boundary"]["profile_id"],
        "boundary_profile_revision": snapshot["boundary"]["profile_revision"],
        "routing_profile_revision": snapshot["routing"]["profile_revision"],
        "routing_rules_version": snapshot["routing"]["rules_version"],
        "registry_revision": snapshot["registry"]["registry_revision"],
        "runtime_revision": snapshot["runtime"]["runtime_revision"],
        "runtime_mode": snapshot["runtime"]["mode"],
        "runtime_activation": snapshot["runtime"]["runtime_activation"],
        "activation_fingerprint": snapshot["activation"]["activation_fingerprint"],
        "route_key": selected["route_key"],
        "route_revision": selected["route_revision"],
        "provider_id": selected["provider_id"],
        "provider_revision": selected["provider_revision"],
        "model_name": selected["model_name"],
        "adapter_kind": selected["adapter_kind"],
        "selected_tier": selected["tier"],
    }
    if (
        boundary.profile_id != expected["boundary_profile_id"]
        or boundary.revision != expected["boundary_profile_revision"]
        or any(evidence.get(key) != value for key, value in expected.items())
    ):
        raise ModelRuntimeError("tiered model routing snapshot drifted")


def resolve_tiered_model_gateway_runtime(
    container: object, *, required_capability: str,
    egress_purpose: str, egress_categories: tuple[str, ...],
) -> ModelGatewayResolution:
    """Build a fail-closed gateway whose route is selected for every invocation."""
    if required_capability not in {"text", "structured", "vision"}:
        raise ModelRuntimeError("tiered model capability is unsupported")
    adapter_kind = (
        "openai-compatible-vision" if required_capability == "vision"
        else "openai-compatible"
    )
    return ModelGatewayResolution(
        TieredModelGatewayAdapter(
            container,
            required_capability=required_capability,
            egress_purpose=egress_purpose,
            egress_categories=egress_categories,
        ),
        "tiered-runtime",
        adapter_kind,
        True,
        True,
    )


def validate_tiered_model_routing_profile(
    container: object, profile: object,
) -> dict[str, object]:
    """Require every configured tier to bind to the current activated authority."""
    root_dir = Path(getattr(container, "root_dir")).resolve()
    runtime_state = ModelRouteRuntimeService(root_dir).status()
    registry = ModelRouteRegistry(root_dir)
    registry_state = registry.list()
    providers = list_model_route_provider_contexts(container)
    provider_by_id = {
        str(item.record.get("provider_id") or ""): item for item in providers
    }
    candidates = _active_route_candidates(
        routing_profile=profile,
        runtime_state=runtime_state,
        registry=registry,
        registry_state=registry_state,
        provider_by_id=provider_by_id,
    )
    candidate_by_key = {candidate.route_key: candidate for candidate in candidates}
    requirements = {
        "fast": "structured", "standard": "structured", "deep": "structured",
        "vision": "vision", "image_generation": "image_generation",
    }
    for tier, route_key in getattr(profile, "tier_routes", ()):
        if route_key is None:
            continue
        candidate = candidate_by_key.get(route_key)
        if candidate is None or requirements[tier] not in candidate.capabilities:
            raise ModelRuntimeError(
                f"model tier route is not active for required capability: {tier}"
            )
    fingerprint = runtime_state.get("activation_fingerprint")
    if not isinstance(fingerprint, str):
        raise ModelRuntimeError("model route activation fingerprint is unavailable")
    return {
        "registry_revision": registry_state["registry_revision"],
        "runtime_revision": runtime_state["runtime_revision"],
        "activation_fingerprint": fingerprint,
    }


def resolve_model_gateway_runtime(
    container: object,
    route_key: str,
    *,
    egress_purpose: str,
    egress_categories: tuple[str, ...],
    tiered_capability: str | None = None,
) -> ModelGatewayResolution:
    if tiered_capability is not None:
        return resolve_tiered_model_gateway_runtime(
            container,
            required_capability=tiered_capability,
            egress_purpose=egress_purpose,
            egress_categories=egress_categories,
        )
    # Direct route-key resolution predates immutable Turn routing snapshots.
    # It cannot prove project, Boundary, route or durable attempt identity, so
    # it must remain unavailable instead of silently creating a remote gateway.
    return ModelGatewayResolution(None, "unconfigured", "openai-compatible", False, False)


def _resolve_tiered_model_gateway(
    container: object, *, project_id: str, required_capability: str,
    egress_purpose: str, egress_categories: tuple[str, ...],
    snapshot: Mapping[str, object],
) -> ModelGatewayResolution:
    root_dir = Path(getattr(container, "root_dir")).resolve()
    try:
        routing_profile = ModelRoutingProfileStore(root_dir).get().profile
        project_profile = ProjectCapabilityProfileStore(root_dir).get(project_id).profile
        runtime = ModelRouteRuntimeService(root_dir)
        runtime_state = runtime.status()
        registry = ModelRouteRegistry(root_dir)
        registry_state = registry.list()
        binding = routing_profile.authority_binding
        if binding is not None and (
            binding.registry_revision != registry_state.get("registry_revision")
            or binding.runtime_revision != runtime_state.get("runtime_revision")
            or binding.activation_fingerprint
            != runtime_state.get("activation_fingerprint")
        ):
            raise ModelRoutingDrift("model routing authority binding drifted")
        providers = list_model_route_provider_contexts(container)
        provider_by_id = {
            str(item.record.get("provider_id") or ""): item for item in providers
        }
        frozen = snapshot.get("selected")
        if not isinstance(frozen, Mapping):
            raise ModelRuntimeError("tiered model route is unavailable")
        route_result = registry.get(str(frozen["route_key"]))
        route = route_result["route"]
        if (
            route_result.get("runtime_activation") is not True
            or route.get("enabled") is not True
            or int(route["revision"]) != frozen["route_revision"]
            or any(
                str(route[key]) != str(frozen[key])
                for key in (
                    "route_key", "provider_id", "provider_revision",
                    "model_name", "adapter_kind",
                )
            )
        ):
            raise ModelRoutingDrift("tiered model routing snapshot drifted")
        provider_id = str(route["provider_id"])
        compatibility = provider_by_id.get(provider_id)
        if compatibility is None:
            raise ModelRuntimeError("tiered model route provider is unavailable")
        try:
            registry.validate_provider_reference(
                route,
                provider=getattr(compatibility, "record"),
                egress_consented=bool(getattr(compatibility, "egress_consented", False)),
            )
        except (AttributeError, ModelRouteRegistryError) as error:
            raise ModelRoutingDrift("tiered model routing snapshot drifted") from error
        selected = runtime.resolve(
            str(frozen["route_key"]),
            compatibility=compatibility,
            providers=providers,
        )
        if selected.get("source") != "registry":
            raise ModelRuntimeError("tiered model route is not runtime activated")
        resolved = _gateway_from_runtime_selection(
            container,
            selected,
            snapshot=snapshot,
            adapter_kind=str(route["adapter_kind"]),
            egress_purpose=egress_purpose,
            egress_categories=egress_categories,
        )
        boundary_profile = ProjectBoundaryProfileStore(root_dir).get(project_id).profile
        return replace(resolved, routing_evidence={
            "schema_version": "1.0.0",
            "project_id": project_id,
            "project_profile_id": project_profile.profile_id,
            "project_profile_revision": project_profile.revision,
            "boundary_profile_id": boundary_profile.profile_id,
            "boundary_profile_revision": boundary_profile.revision,
            "routing_profile_revision": routing_profile.revision,
            "routing_rules_version": routing_profile.rules_version,
            "required_capability": required_capability,
            "selected_tier": frozen["tier"],
            "selection_reason": frozen["reason"],
            "route_key": frozen["route_key"],
            "route_revision": frozen["route_revision"],
            "provider_id": provider_id,
            "provider_revision": str(route["provider_revision"]),
            "model_name": str(route["model_name"]),
            "adapter_kind": str(route["adapter_kind"]),
            "registry_revision": selected["registry_revision"],
            "runtime_revision": selected["runtime_revision"],
            "runtime_mode": runtime_state["mode"],
            "runtime_activation": runtime_state["runtime_activation"],
            "activation_fingerprint": runtime_state["activation_fingerprint"],
        })
    except ModelRoutingDrift:
        raise
    except (
        AttributeError, KeyError, ModelRouteRegistryError, ModelRouteRegistryNotFound,
        OSError, RuntimeError, TypeError, ValueError,
    ):
        return ModelGatewayResolution(
            None, "unconfigured", "openai-compatible", False, False,
        )


def _provider_attempt_observer(
    root_dir: Path,
    snapshot: Mapping[str, object],
) -> Callable[[str, BaseException | None], None]:
    def observe(status: str, failure: BaseException | None) -> None:
        _observe(status, failure)

    def _observe(status: str, failure: BaseException | None) -> None:
        selected = snapshot.get("selected")
        project = snapshot.get("project")
        boundary = snapshot.get("boundary")
        if not all(isinstance(item, Mapping) for item in (selected, project, boundary)):
            return
        try:
            scope = ProviderHealthScope(
                project_id=str(project["project_id"]),  # type: ignore[index]
                boundary_profile_id=str(boundary["profile_id"]),  # type: ignore[index]
                boundary_revision=int(boundary["profile_revision"]),  # type: ignore[index]
                route_key=str(selected["route_key"]),  # type: ignore[index]
                provider_id=str(selected["provider_id"]),  # type: ignore[index]
                provider_revision=str(selected["provider_revision"]),  # type: ignore[index]
                model_name=str(selected["model_name"]),  # type: ignore[index]
            )
            store = ModelProviderHealthStore(root_dir)
            if status == "succeeded" and failure is None:
                store.observe_success(scope)
            elif status == "failed" and failure is not None:
                store.observe_failure(scope, failure)
        except (ModelProviderHealthError, OSError, RuntimeError, TypeError, ValueError):
            # Health is advisory. It cannot replace the Provider result or
            # widen the independent authorization decision.
            return

    return observe


def _active_route_candidates(
    *, routing_profile: object, runtime_state: Mapping[str, object],
    registry: ModelRouteRegistry, registry_state: Mapping[str, object],
    provider_by_id: Mapping[str, object],
) -> tuple[ModelRouteCandidate, ...]:
    if (
        runtime_state.get("runtime_activation") is not True
        or runtime_state.get("mode") != "active"
        or runtime_state.get("activated_registry_revision")
        != registry_state.get("registry_revision")
    ):
        return ()
    assignments = runtime_state.get("assignments")
    if not isinstance(assignments, list):
        return ()
    assignment_by_key = {
        str(item.get("route_key") or ""): item
        for item in assignments if isinstance(item, Mapping)
    }
    candidates: list[ModelRouteCandidate] = []
    tier_routes = getattr(routing_profile, "tier_routes", ())
    for _tier, route_key in tier_routes:
        if route_key is None or route_key in {item.route_key for item in candidates}:
            continue
        assignment = assignment_by_key.get(route_key)
        if assignment is None:
            continue
        route = registry.get(route_key)["route"]
        expected = {
            "route_key": str(route["route_key"]),
            "provider_id": str(route["provider_id"]),
            "provider_revision": str(route["provider_revision"]),
            "model_name": str(route["model_name"]),
            "route_revision": int(route["revision"]),
        }
        if dict(assignment) != expected or route.get("enabled") is not True:
            continue
        provider = provider_by_id.get(str(route["provider_id"]))
        if provider is None or not bool(getattr(provider, "egress_consented", False)):
            continue
        try:
            registry.validate_provider_reference(
                route,
                provider=getattr(provider, "record"),
                egress_consented=True,
            )
        except (AttributeError, ModelRouteRegistryError):
            continue
        adapter_kind = str(route.get("adapter_kind") or "")
        capabilities = (
            ("vision",) if adapter_kind == "openai-compatible-vision"
            else ("image_generation",) if adapter_kind == "openai-compatible-image-generation"
            else ("text", "structured") if adapter_kind == "openai-compatible"
            else ()
        )
        if not capabilities:
            continue
        candidates.append(ModelRouteCandidate(
            route_key=route_key,
            capabilities=capabilities,  # type: ignore[arg-type]
            revision=int(route["revision"]),
            revision_evidence=(
                f"registry:{registry_state['registry_revision']}:"
                f"runtime:{runtime_state['runtime_revision']}:route:{route['revision']}"
            ),
        ))
    return tuple(candidates)


def _gateway_from_runtime_selection(
    container: object, selected: Mapping[str, object], *, snapshot: Mapping[str, object], adapter_kind: str,
    egress_purpose: str, egress_categories: tuple[str, ...],
) -> ModelGatewayResolution:
    provider_record = selected.get("provider")
    if not isinstance(provider_record, Mapping):
        raise ModelRuntimeError("tiered model provider projection is invalid")
    provider_id = str(selected.get("provider_id") or "")
    model_name = str(selected.get("model_name") or "")
    execution_location = _provider_execution_location(
        Path(getattr(container, "root_dir")).resolve(), provider_record,
    )
    frozen = snapshot.get("selected")
    if not isinstance(frozen, Mapping) or frozen.get("execution_location") != execution_location:
        raise ModelRoutingDrift("tiered model execution location drifted")
    # remote_allowed is an upper egress bound, not a requirement to treat a
    # canonical loopback endpoint as remote.  The manifest's external=false
    # identity alone selects the keyless local transport.
    local_anonymous = execution_location == "local_loopback"
    if not local_anonymous:
        if not getattr(container, "secret_store").has_secret(f"provider:{provider_id}"):
            return ModelGatewayResolution(None, model_name, adapter_kind, True, True)
    base_url = resolve_openai_compatible_api_base_url(
        str(provider_record.get("base_url") or "")
    )
    provider = normalize_litellm_provider(provider_record.get("llm_provider"))
    context_window_tokens, reserved_output_tokens = _model_token_budget(container)
    gateway = LiteLLMCompletionGateway(
        provider=provider,
        model=model_name,
        base_url=base_url,
        api_key=None,
        api_key_provider=_secret_api_key_provider(
            container, project_id=str(snapshot["project"]["project_id"]),
            provider_id=provider_id, base_url=base_url, purpose=egress_purpose,
        ) if not local_anonymous else None,
        anonymous=local_anonymous,
        egress_guard=build_provider_egress_guard(
            Path(getattr(container, "root_dir")).resolve(),
            provider_id=provider_id,
            endpoint=base_url,
        ),
        egress_purpose=egress_purpose,
        egress_categories=egress_categories,
        provider_attempt_observer=_provider_attempt_observer(
            Path(getattr(container, "root_dir")).resolve(),
            snapshot,
        ),
        context_window_tokens=context_window_tokens,
        reserved_output_tokens=reserved_output_tokens,
    )
    return ModelGatewayResolution(
        LiteLLMModelGatewayAdapter(
            gateway, provider=provider, model=model_name,
            allow_local_only=local_anonymous,
        ),
        model_name,
        adapter_kind,
        True,
        True,
    )


def _model_token_budget(container: object) -> tuple[int, int]:
    from backend.agent.context.budget import (
        DEFAULT_CONTEXT_WINDOW_TOKENS,
        DEFAULT_RESERVED_OUTPUT_TOKENS,
    )
    from backend.video_summary.infrastructure.settings import load_settings

    root = Path(getattr(container, "root_dir")).resolve()
    config_path = Path(getattr(container, "config_path", root / "config" / "settings.toml"))
    if not config_path.is_file():
        return DEFAULT_CONTEXT_WINDOW_TOKENS, DEFAULT_RESERVED_OUTPUT_TOKENS
    budget = load_settings(config_path, root).agent_context
    return budget.window_tokens, budget.reserved_output_tokens


def _secret_api_key_provider(
    container: object, *, project_id: str, provider_id: str, base_url: str, purpose: str,
) -> Callable[[], str]:
    root_dir = Path(getattr(container, "root_dir")).resolve()
    profiles = ProjectBoundaryProfileStore(root_dir)
    def boundary_revision(project: str) -> str:
        return f"boundary:{profiles.get(project).profile.revision}"
    broker = SecretEgressBroker(
        getattr(container, "secret_store"), boundary_revision_reader=boundary_revision,
    )
    host = urlsplit(base_url).hostname
    if not host:
        raise ModelRuntimeError("model provider endpoint host is unavailable")
    def provide() -> str:
        revision = boundary_revision(project_id)
        lease = broker.grant(
            project_id=project_id, secret_ref=f"provider:{provider_id}", purpose=purpose,
            allowed_hosts=(host,), boundary_revision=revision, ttl_seconds=30,
        )
        try:
            return broker.materialize_for_sdk(
                lease, project_id=project_id, purpose=purpose,
                boundary_revision=revision, url=base_url,
            )
        finally:
            broker.revoke(lease.lease_id)
    return provide


def _revalidate_frozen_image_route(
    container: object, *, snapshot: Mapping[str, object], selected: Mapping[str, object],
) -> Mapping[str, object]:
    """Check non-Boundary authority facts while Tool dispatch owns its lock.

    This intentionally excludes ProjectBoundaryProfileStore and
    AIModelEgressBoundary. The outer TurnCapabilityBindingGuard has already
    frozen and locked that authority for the whole Tool invocation. Registry,
    runtime and Provider records use independent local authorities and are
    safe to compare here without recursive acquisition of the Boundary lock.
    """
    root_dir = Path(getattr(container, "root_dir")).resolve()
    registry = ModelRouteRegistry(root_dir)
    route_result = registry.get(str(selected["route_key"]))
    route = route_result["route"]
    runtime = ModelRouteRuntimeService(root_dir)
    runtime_state = runtime.status()
    registry_state = registry.list()
    expected = {
        "route_key": selected["route_key"],
        "provider_id": selected["provider_id"],
        "provider_revision": selected["provider_revision"],
        "model_name": selected["model_name"],
        "adapter_kind": "openai-compatible-image-generation",
        "revision": selected["route_revision"],
    }
    if (
        route_result.get("runtime_activation") is not True
        or route.get("enabled") is not True
        or any(str(route.get(key)) != str(value) for key, value in expected.items())
        or registry_state.get("registry_revision") != snapshot["registry"]["registry_revision"]
        or runtime_state.get("runtime_revision") != snapshot["runtime"]["runtime_revision"]
        or runtime_state.get("runtime_activation") is not True
        or runtime_state.get("mode") != "active"
        or runtime_state.get("activation_fingerprint")
        != snapshot["activation"]["activation_fingerprint"]
    ):
        raise ModelRoutingDrift("image generation routing snapshot drifted")
    contexts = list_model_route_provider_contexts(container)
    context = next(
        (
            item for item in contexts
            if str(getattr(item, "record", {}).get("provider_id") or "")
            == str(selected["provider_id"])
        ),
        None,
    )
    record = getattr(context, "record", None) if context is not None else None
    if not isinstance(record, Mapping):
        raise ModelRuntimeError("image generation route provider is unavailable")
    try:
        registry.validate_provider_reference(
            route, provider=record,
            egress_consented=bool(getattr(context, "egress_consented", False)),
        )
    except (AttributeError, ModelRouteRegistryError) as error:
        raise ModelRoutingDrift("image generation provider revision drifted") from error
    resolved = runtime.resolve(
        str(selected["route_key"]), compatibility=context, providers=contexts,
    )
    if (
        resolved.get("source") != "registry"
        or str(resolved.get("provider_id") or "") != str(selected["provider_id"])
        or str(resolved.get("model_name") or "") != str(selected["model_name"])
    ):
        raise ModelRoutingDrift("image generation runtime selection drifted")
    return record


def _provider_execution_location(root_dir: Path, record: Mapping[str, object]) -> str:
    """Project an execution location from the canonical egress manifest only.

    The returned label is safe to freeze into a Turn. Endpoint and path remain
    inside the Provider record/egress authority and never enter session state.
    """
    manifest = provider_egress_manifest(
        record, ProviderEgressPolicyStore(root_dir),
    )
    return "remote" if manifest.external else "local_loopback"


def _vision_messages(
    messages: list[dict[str, object]], image_payload: object,
) -> list[dict[str, object]]:
    if not isinstance(image_payload, Mapping):
        raise ModelRuntimeError("vision model request requires image payload")
    media_type = image_payload.get("media_type")
    pixels = image_payload.get("pixels")
    if media_type not in {"image/jpeg", "image/png"} or not isinstance(pixels, bytes) or not pixels:
        raise ModelRuntimeError("vision model image payload is invalid")
    final = messages[-1]
    if final.get("role") != "user" or not isinstance(final.get("content"), str):
        raise ModelRuntimeError("vision model requires a final user message")
    encoded = base64.b64encode(pixels).decode("ascii")
    messages[-1] = {
        "role": "user",
        "content": [
            {"type": "text", "text": final["content"]},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{encoded}", "detail": "low"},
            },
        ],
    }
    return messages


def _optional_int(value: object) -> int | None:
    return int(value) if value is not None else None


def _optional_float(value: object) -> float | None:
    return float(value) if value is not None else None


def _effective_timeout(
    value: object,
    control: ModelExecutionControlPort | None,
) -> float | None:
    configured = _optional_float(value)
    if configured is not None and configured <= 0:
        raise ModelRuntimeError("model timeout must be positive")
    if control is None:
        return configured
    remaining_ms = control.remaining_timeout_ms
    remaining = max(remaining_ms / 1000, 0.001)
    return min(configured, remaining) if configured is not None else remaining
