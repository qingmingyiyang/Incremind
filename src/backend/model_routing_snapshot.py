"""Pure, non-secret projection of a turn's model-routing authority.

This module is deliberately not a gateway, persistence layer, or API handler.
Its input references are identities only; prompts, endpoint material, and
credentials are rejected before they can become part of a snapshot.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import re

from backend.model_route_context import list_model_route_provider_contexts, provider_egress_manifest
from backend.model_provider_health import (
    ModelProviderHealthError,
    ModelProviderHealthStore,
    ProviderHealthScope,
    ProviderHealthState,
)
from backend.model_routing_profile import ModelRoutingProfileStore
from backend.security.ai_model_egress_boundary import AIModelEgressBoundary
from backend.security.provider_egress import ProviderEgressPolicyStore
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.ai_tooling import (
    ModelRouteCandidate,
    ModelRoutingDecision,
    ModelRoutingRouter,
    ModelSelectionRequest,
)
from core.product_core.model_route_registry import ModelRouteRegistry, ModelRouteRegistryError, ModelRouteRegistryNotFound
from core.product_core.model_route_runtime import ModelRouteRuntimeService


SCHEMA_VERSION = "1.0.0"
CANONICAL_TIERS = ("fast", "standard", "deep", "vision", "image_generation")
_CAPABILITIES = frozenset({"text", "structured", "vision", "image_generation"})
_SENSITIVE = frozenset({"api_key", "apikey", "authorization", "cookie", "cookies", "endpoint", "base_url", "password", "prompt", "secret", "token", "path", "local_path", "windows_path"})
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_OPAQUE_CRP_REF = re.compile(
    r"^crp://[a-z0-9][a-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$"
)
_LOCAL_ABSOLUTE_PATH = re.compile(
    r"(?i)(?:\b[A-Z]:[\\/]|\\\\[^\\\s]+\\[^\\\s]+|\bfile:/+[^\s<>\'\"`]+|(?<![:/A-Za-z0-9])/(?!/)[^\s<>\'\"`]+)"
)
_REASONS = frozenset({
    "tier_unconfigured", "runtime_inactive", "assignment_missing", "assignment_drift",
    "route_missing", "disabled", "provider_missing", "unconsented", "reference_invalid",
    "required_capability_unsupported", "tier_not_applicable", "image_generation_unavailable",
    "authority_binding_drift", "tier_not_selected", "privacy_scope_ineligible",
    "boundary_approval_required", "boundary_denied", "provider_health_open",
    "provider_health_blocked", "agent_route_revision_drift",
})
_MODALITIES = {
    "text": "text",
    "structured": "text",
    "vision": "image_input",
    "image_generation": "image_generation",
}
_OUTPUT_CONTRACTS = {
    "text": "text",
    "structured": "json_object",
    "vision": "text",
    "image_generation": "image_asset",
}
_PRIVACY_SCOPES = frozenset({"local_only", "remote_allowed"})
_RETENTION_POLICIES = frozenset({"turn_only", "session", "local_durable"})
_ROUTE_FIELDS = {"route_key", "provider_id", "provider_revision", "model_name", "adapter_kind", "enabled", "revision"}
_EXECUTION_LOCATIONS = frozenset({"local_loopback", "remote"})


class TurnModelRoutingSnapshotError(ValueError):
    pass


def project_turn_model_routing_snapshot(
    container: object, *, turn_id: str, project_id: str, required_capability: str,
    capability_ids: Sequence[str] = (), skill_snapshot_revision: str | None = None,
    context_policy: Mapping[str, object] | None = None, input_refs: Sequence[object] = (),
    modality: str, output_contract: str, egress_purpose: str,
    egress_categories: Sequence[str], privacy_scope: str,
    retention_policy: str = "turn_only", protocol_version: str = SCHEMA_VERSION,
    model_call_purpose: str = "primary",
    agent_binding: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Read current authorities and return a deterministic, safe routing projection."""
    turn_id, project_id = _identity(turn_id, "turn id"), _identity(project_id, "project id")
    if required_capability not in _CAPABILITIES:
        raise TurnModelRoutingSnapshotError("required capability is unsupported")
    _require_requirement_contract(
        required_capability, modality, output_contract, egress_purpose,
        egress_categories, privacy_scope, retention_policy, protocol_version,
    )
    if model_call_purpose not in {"primary", "aux", "probe"}:
        raise TurnModelRoutingSnapshotError("model call purpose is invalid")
    agent = _agent_binding(agent_binding)
    root = Path(getattr(container, "root_dir")).resolve()
    profile = ModelRoutingProfileStore(root).get().profile
    project = ProjectCapabilityProfileStore(root).get(project_id).profile
    boundary = ProjectBoundaryProfileStore(root).get(project_id).profile
    registry = ModelRouteRegistry(root)
    registry_state = registry.list()
    runtime = ModelRouteRuntimeService(root).status()
    providers = {str(item.record.get("provider_id") or ""): item for item in list_model_route_provider_contexts(container)}
    model_boundary = AIModelEgressBoundary(root)
    provider_health = ModelProviderHealthStore(root)
    policy = _context_policy(context_policy)
    refs = _input_refs(input_refs)
    capabilities = sorted({_identity(item, "capability id") for item in capability_ids})
    if skill_snapshot_revision is not None:
        skill_snapshot_revision = _identity(skill_snapshot_revision, "skill snapshot revision")

    binding_drift = _binding_drift(profile, registry_state, runtime)
    tiers: list[dict[str, object]] = []
    candidates: list[ModelRouteCandidate] = []
    bound_route_key = agent.get("model_route_key") if agent is not None else None
    bound_route_revision = agent.get("model_route_revision") if agent is not None else None
    for tier, route_key in profile.tier_routes:
        if (
            bound_route_key is not None
            and model_call_purpose == "primary"
            and required_capability in {"text", "structured"}
            and tier == agent["model_tier"]
        ):
            route_key = bound_route_key
        item, candidate = _tier_projection(
            tier=tier, route_key=route_key, required_capability=required_capability,
            registry=registry, registry_state=registry_state, runtime=runtime,
            providers=providers, binding_drift=binding_drift, privacy_scope=privacy_scope,
            turn_id=turn_id, project_id=project_id, boundary=boundary,
            egress_categories=egress_categories, model_boundary=model_boundary,
            provider_health=provider_health, egress_policy=ProviderEgressPolicyStore(root),
        )
        tiers.append(item)
        if candidate is not None:
            candidates.append(candidate)
    purpose_preferred_tier = (
        "fast" if model_call_purpose in {"aux", "probe"}
        else agent["model_tier"] if agent is not None else project.preferred_model_tier
    )
    purpose_candidates = tuple(
        candidate for candidate in candidates
        if model_call_purpose == "primary"
        or candidate.route_key == profile.route_key_for("fast")
    )
    if (
        agent is not None
        and model_call_purpose == "primary"
        and required_capability in {"text", "structured"}
    ):
        # A configured Agent tier is a frozen execution choice. Falling back
        # to the project/default tier would silently run a different model.
        route_key = bound_route_key or profile.route_key_for(purpose_preferred_tier)  # type: ignore[arg-type]
        route = next(
            (
                candidate for candidate in purpose_candidates
                if candidate.route_key == route_key
                and (bound_route_revision is None or candidate.revision == bound_route_revision)
            ),
            None,
        )
        if bound_route_key is not None and route is None:
            selected_tier = next(item for item in tiers if item["tier"] == purpose_preferred_tier)
            if (
                isinstance(selected_tier.get("route"), Mapping)
                and selected_tier["route"].get("route_key") == bound_route_key
                and selected_tier["route"].get("revision") != bound_route_revision
            ):
                selected_tier["eligible"] = False
                selected_tier["exclusion_reasons"] = sorted(set(selected_tier["exclusion_reasons"]) | {"agent_route_revision_drift"})
            for item in tiers:
                if item is not selected_tier and item["eligible"]:
                    item["eligible"] = False
                    item["exclusion_reasons"] = ["tier_not_selected"]
        decision = ModelRoutingDecision(
            route is not None,
            purpose_preferred_tier,  # type: ignore[arg-type]
            route,
            (
                "agent_profile_route_binding" if bound_route_key is not None and route is not None
                else "agent_profile_route_binding_unavailable" if bound_route_key is not None
                else "agent_profile_tier" if route is not None else "agent_profile_tier_unavailable"
            ),
            profile.revision,
        )
    else:
        decision = ModelRoutingRouter().resolve(
            profile, purpose_candidates, ModelSelectionRequest(
                required_capability=required_capability,  # type: ignore[arg-type]
                project_preferred_tier=purpose_preferred_tier,
            ),
        )
    selected = _select_unique_eligible(tiers, decision.route, decision.tier, decision.reason)
    if bound_route_key is not None and selected is None:
        # A concrete Profile choice is an explicit execution contract.  It
        # must never degrade into the tier/default fallback used by legacy
        # profiles, and must be rejected before a model execution is planned.
        raise TurnModelRoutingSnapshotError("agent model route binding is unavailable or drifted")
    catalog_identity: dict[str, object] = {
        "profile": [profile.revision, profile.rules_version, list(profile.tier_routes)],
        "registry_revision": registry_state["registry_revision"],
        "runtime": [runtime["runtime_revision"], runtime["activation_fingerprint"]],
        "tiers": tiers,
    }
    if agent is not None:
        catalog_identity["agent"] = agent
    catalog_revision = _revision(catalog_identity)
    prompt_scope_identity: dict[str, object] = {
        "project_id": project_id, "profile": [project.profile_id, project.revision],
        "boundary": [boundary.profile_id, boundary.revision],
        "skill_snapshot_revision": skill_snapshot_revision,
        "protocol_version": protocol_version,
        "requirement": payload_requirement_identity(
            required_capability, modality, output_contract, egress_purpose,
            egress_categories, privacy_scope, retention_policy, capabilities, policy,
            refs, model_call_purpose=model_call_purpose,
        ),
        "selected": selected,
    }
    if agent is not None:
        prompt_scope_identity["agent"] = agent
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "turn": {"turn_id": turn_id},
        "project": {"project_id": project_id},
        "profile": {"profile_id": project.profile_id, "profile_revision": project.revision, "preferred_model_tier": project.preferred_model_tier},
        **({"agent": agent} if agent is not None else {}),
        "boundary": {"profile_id": boundary.profile_id, "profile_revision": boundary.revision},
        "requirement": {"required_capability": required_capability, "modality": modality, "output_contract": output_contract, "egress_purpose": egress_purpose, "egress_categories": sorted({_identity(item, "egress category") for item in egress_categories}), "privacy_scope": privacy_scope, "retention_policy": retention_policy, "protocol_version": protocol_version, "model_call_purpose": model_call_purpose, "capability_ids": capabilities, "skill_snapshot_revision": skill_snapshot_revision, "context_policy": policy, "input_refs": refs},
        "routing": {"profile_revision": profile.revision, "rules_version": profile.rules_version, "text_default_tier": profile.text_default_tier, "authority_binding": _binding_identity(profile)},
        "registry": {"registry_revision": registry_state["registry_revision"]},
        "runtime": {"runtime_revision": runtime["runtime_revision"], "mode": runtime["mode"], "runtime_activation": runtime["runtime_activation"], "activation_fingerprint": runtime["activation_fingerprint"] or None},
        "activation": {"activation_fingerprint": runtime["activation_fingerprint"] or None, "binding_drift": binding_drift},
        "tiers": tiers,
        "selected": selected,
        "catalog_revision": catalog_revision,
        "prompt_cache_scope": {
            "identity": _revision(prompt_scope_identity),
            "project_id": project_id, "profile_id": project.profile_id, "profile_revision": project.revision,
            "boundary_profile_id": boundary.profile_id, "boundary_profile_revision": boundary.revision,
            "skill_snapshot_revision": skill_snapshot_revision, "protocol_version": protocol_version,
        },
    }
    return validate_turn_model_routing_snapshot(payload)


# Concise alias for future turn composition consumers.
build_turn_model_routing_snapshot = project_turn_model_routing_snapshot


def encode_turn_model_routing_snapshot(value: Mapping[str, object]) -> str:
    return json.dumps(validate_turn_model_routing_snapshot(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def turn_model_routing_snapshot_revision(value: Mapping[str, object]) -> str:
    return hashlib.sha256(encode_turn_model_routing_snapshot(value).encode("utf-8")).hexdigest()


def decode_turn_model_routing_snapshot(value: str | bytes | bytearray) -> dict[str, object]:
    try:
        return validate_turn_model_routing_snapshot(json.loads(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, TurnModelRoutingSnapshotError):
            raise
        raise TurnModelRoutingSnapshotError("turn model routing snapshot is invalid") from error


def validate_turn_model_routing_snapshot(value: object) -> dict[str, object]:
    required = {"schema_version", "turn", "project", "profile", "boundary", "requirement", "routing", "registry", "runtime", "activation", "tiers", "selected", "catalog_revision", "prompt_cache_scope"}
    if not isinstance(value, Mapping) or set(value) - {"agent"} != required:
        raise TurnModelRoutingSnapshotError("turn model routing snapshot shape is invalid")
    _reject_sensitive(value)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise TurnModelRoutingSnapshotError("turn model routing snapshot schema is unsupported")
    payload = json.loads(json.dumps(value))
    _shape(payload["turn"], {"turn_id"}, "turn identity")
    _shape(payload["project"], {"project_id"}, "project identity")
    _shape(payload["profile"], {"profile_id", "profile_revision", "preferred_model_tier"}, "profile identity")
    agent = None
    if "agent" in payload:
        agent = _agent_binding(payload["agent"])
        if agent is None:
            raise TurnModelRoutingSnapshotError("agent binding is invalid")
    _shape(payload["boundary"], {"profile_id", "profile_revision"}, "boundary identity")
    requirement_fields = {"required_capability", "modality", "output_contract", "egress_purpose", "egress_categories", "privacy_scope", "retention_policy", "protocol_version", "capability_ids", "skill_snapshot_revision", "context_policy", "input_refs"}
    if isinstance(payload["requirement"], Mapping) and "model_call_purpose" in payload["requirement"]:
        requirement_fields.add("model_call_purpose")
    _shape(payload["requirement"], requirement_fields, "requirement")
    _shape(payload["routing"], {"profile_revision", "rules_version", "text_default_tier", "authority_binding"}, "routing identity")
    _shape(payload["registry"], {"registry_revision"}, "registry identity")
    _shape(payload["runtime"], {"runtime_revision", "mode", "runtime_activation", "activation_fingerprint"}, "runtime identity")
    _shape(payload["activation"], {"activation_fingerprint", "binding_drift"}, "activation identity")
    _shape(payload["prompt_cache_scope"], {"identity", "project_id", "profile_id", "profile_revision", "boundary_profile_id", "boundary_profile_revision", "skill_snapshot_revision", "protocol_version"}, "prompt cache scope")
    _identity(payload["turn"]["turn_id"], "turn id")
    _identity(payload["project"]["project_id"], "project id")
    _identity(payload["profile"]["profile_id"], "profile id")
    _positive(payload["profile"]["profile_revision"], "profile revision")
    _identity(payload["boundary"]["profile_id"], "boundary profile id")
    _positive(payload["boundary"]["profile_revision"], "boundary profile revision")
    _positive(payload["routing"]["profile_revision"], "routing profile revision")
    _positive(payload["routing"]["rules_version"], "routing rules version")
    _nonnegative(payload["registry"]["registry_revision"], "registry revision")
    _nonnegative(payload["runtime"]["runtime_revision"], "runtime revision")
    _require_requirement_contract(**{key: payload["requirement"][key] for key in ("required_capability", "modality", "output_contract", "egress_purpose", "egress_categories", "privacy_scope", "retention_policy", "protocol_version")})
    if payload["requirement"].get("model_call_purpose", "primary") not in {"primary", "aux", "probe"}:
        raise TurnModelRoutingSnapshotError("model call purpose is invalid")
    _validate_context_policy(payload["requirement"]["context_policy"])
    _validate_refs(payload["requirement"]["input_refs"])
    if payload["prompt_cache_scope"]["project_id"] != payload["project"]["project_id"] or payload["prompt_cache_scope"]["profile_id"] != payload["profile"]["profile_id"] or payload["prompt_cache_scope"]["profile_revision"] != payload["profile"]["profile_revision"] or payload["prompt_cache_scope"]["boundary_profile_id"] != payload["boundary"]["profile_id"] or payload["prompt_cache_scope"]["boundary_profile_revision"] != payload["boundary"]["profile_revision"] or payload["prompt_cache_scope"]["skill_snapshot_revision"] != payload["requirement"]["skill_snapshot_revision"] or payload["prompt_cache_scope"]["protocol_version"] != payload["requirement"]["protocol_version"]:
        raise TurnModelRoutingSnapshotError("prompt cache scope identity drifted")
    if not isinstance(payload["tiers"], list) or [item.get("tier") for item in payload["tiers"] if isinstance(item, Mapping)] != list(CANONICAL_TIERS):
        raise TurnModelRoutingSnapshotError("turn model routing snapshot tiers are invalid")
    if any(not isinstance(item, Mapping) or set(item) != {"tier", "route", "capabilities", "execution_location", "eligible", "exclusion_reasons"} for item in payload["tiers"]):
        raise TurnModelRoutingSnapshotError("turn model routing tier shape is invalid")
    for item in payload["tiers"]:
        if not isinstance(item["eligible"], bool) or not isinstance(item["exclusion_reasons"], list) or any(reason not in _REASONS for reason in item["exclusion_reasons"]):
            raise TurnModelRoutingSnapshotError("turn model routing tier eligibility is invalid")
        if item["eligible"] != (not item["exclusion_reasons"]):
            raise TurnModelRoutingSnapshotError("turn model routing tier eligibility drifted")
        if item["exclusion_reasons"] != sorted(set(item["exclusion_reasons"])):
            raise TurnModelRoutingSnapshotError("turn model routing exclusion reasons are not canonical")
        if not isinstance(item["capabilities"], list) or tuple(item["capabilities"]) not in {("text", "structured"), ("vision",), ("image_generation",), ()}:
            raise TurnModelRoutingSnapshotError("turn model routing capabilities are invalid")
        if item["route"] is not None:
            if item["execution_location"] not in _EXECUTION_LOCATIONS:
                raise TurnModelRoutingSnapshotError("turn model routing tier execution location is invalid")
            _shape(item["route"], _ROUTE_FIELDS, "route")
            _model_name(item["route"]["model_name"])
            if tuple(item["capabilities"]) != _adapter_capabilities(item["route"]["adapter_kind"]):
                raise TurnModelRoutingSnapshotError("turn model routing adapter capabilities drifted")
            _positive(item["route"]["revision"], "route revision")
        elif item["execution_location"] is not None:
            raise TurnModelRoutingSnapshotError("unrouted model tier cannot have an execution location")
    if not isinstance(payload["catalog_revision"], str) or not re.fullmatch(r"[a-f0-9]{64}", payload["catalog_revision"]):
        raise TurnModelRoutingSnapshotError("turn model routing catalog revision is invalid")
    if not isinstance(payload["selected"], Mapping) and payload["selected"] is not None:
        raise TurnModelRoutingSnapshotError("turn model routing selection is invalid")
    if isinstance(payload["selected"], Mapping):
        _shape(payload["selected"], {"tier", "route_key", "route_revision", "provider_id", "provider_revision", "model_name", "adapter_kind", "execution_location", "reason"}, "selection")
        if payload["selected"]["execution_location"] not in _EXECUTION_LOCATIONS:
            raise TurnModelRoutingSnapshotError("turn model routing execution location is invalid")
    eligible = [item for item in payload["tiers"] if item["eligible"]]
    if (payload["selected"] is None) != (not eligible) or len(eligible) > 1:
        raise TurnModelRoutingSnapshotError("turn model routing selection eligibility drifted")
    if eligible:
        route, selected = eligible[0]["route"], payload["selected"]
        if route is None or not isinstance(selected, Mapping) or selected["tier"] != eligible[0]["tier"] or any(selected[key] != route[key] for key in ("route_key", "provider_id", "provider_revision", "model_name", "adapter_kind")) or selected["route_revision"] != route["revision"] or selected["execution_location"] != eligible[0].get("execution_location"):
            raise TurnModelRoutingSnapshotError("turn model routing selection drifted")
    requirement = payload["requirement"]
    expected_scope_input: dict[str, object] = {
        "project_id": payload["project"]["project_id"],
        "profile": [payload["profile"]["profile_id"], payload["profile"]["profile_revision"]],
        "boundary": [payload["boundary"]["profile_id"], payload["boundary"]["profile_revision"]],
        "skill_snapshot_revision": requirement["skill_snapshot_revision"],
        "protocol_version": requirement["protocol_version"],
        "requirement": payload_requirement_identity(requirement["required_capability"], requirement["modality"], requirement["output_contract"], requirement["egress_purpose"], requirement["egress_categories"], requirement["privacy_scope"], requirement["retention_policy"], requirement["capability_ids"], requirement["context_policy"], requirement["input_refs"], model_call_purpose=requirement.get("model_call_purpose") if "model_call_purpose" in requirement else None),
        "selected": payload["selected"],
    }
    if agent is not None:
        expected_scope_input["agent"] = agent
    expected_scope = _revision(expected_scope_input)
    if payload["prompt_cache_scope"]["identity"] != expected_scope:
        raise TurnModelRoutingSnapshotError("prompt cache scope fingerprint drifted")
    return payload


def _agent_binding(value: Mapping[str, object] | object | None) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TurnModelRoutingSnapshotError("agent binding is invalid")
    binding = dict(value)
    common_fields = {"schema_version", "kind", "run_id", "role", "profile_id", "profile_revision", "model_tier", "depth", "cancel_epoch", "budget_snapshot_ref"}
    route_fields = {"model_route_key", "model_route_revision"}
    child_fields = {"parent_run_id", "link_id", "reservation_id", "spawn_operation_id"}
    role = binding.get("role")
    expected = common_fields | (child_fields if role == "subagent" else set())
    if set(binding) != expected and set(binding) != expected | route_fields:
        raise TurnModelRoutingSnapshotError("agent binding is invalid")
    if binding.get("schema_version") != SCHEMA_VERSION or binding.get("kind") != "internal_agent_run_v1" or role not in {"main", "subagent"}:
        raise TurnModelRoutingSnapshotError("agent binding is invalid")
    for field in ("run_id", "profile_id"):
        _identity(binding.get(field), f"agent binding {field}")
    _positive(binding.get("profile_revision"), "agent binding profile revision")
    if binding.get("model_tier") not in {"fast", "standard", "deep"}:
        raise TurnModelRoutingSnapshotError("agent binding model tier is invalid")
    if ("model_route_key" in binding) != ("model_route_revision" in binding):
        raise TurnModelRoutingSnapshotError("agent binding model route is invalid")
    if "model_route_key" in binding:
        _identity(binding.get("model_route_key"), "agent binding model route key")
        _positive(binding.get("model_route_revision"), "agent binding model route revision")
    depth = binding.get("depth")
    if not isinstance(depth, int) or isinstance(depth, bool) or not 0 <= depth <= 4:
        raise TurnModelRoutingSnapshotError("agent binding depth is invalid")
    if role == "main":
        if binding.get("profile_id") != "main.orchestrator" or depth != 0:
            raise TurnModelRoutingSnapshotError("main agent binding is invalid")
    else:
        if depth < 1:
            raise TurnModelRoutingSnapshotError("subagent binding depth is invalid")
        for field in child_fields:
            _identity(binding.get(field), f"agent binding {field}")
    epoch = binding.get("cancel_epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise TurnModelRoutingSnapshotError("agent binding cancel epoch is invalid")
    ref = binding.get("budget_snapshot_ref")
    if not isinstance(ref, str) or not _OPAQUE_CRP_REF.fullmatch(ref):
        raise TurnModelRoutingSnapshotError("agent binding budget snapshot ref is invalid")
    return binding


def _tier_projection(
    *, tier: str, route_key: str | None, required_capability: str,
    registry: ModelRouteRegistry, registry_state: Mapping[str, object],
    runtime: Mapping[str, object], providers: Mapping[str, object],
    binding_drift: bool, privacy_scope: str, turn_id: str, project_id: str,
    boundary: object, egress_categories: Sequence[str],
    model_boundary: AIModelEgressBoundary,
    provider_health: ModelProviderHealthStore,
    egress_policy: ProviderEgressPolicyStore,
) -> tuple[dict[str, object], ModelRouteCandidate | None]:
    reasons: list[str] = []
    route: dict[str, object] | None = None
    capabilities = _adapter_capabilities(None)
    if required_capability == "image_generation" and tier != "image_generation":
        reasons.append("tier_not_applicable")
    elif required_capability == "vision" and tier != "vision":
        reasons.append("tier_not_applicable")
    elif required_capability in {"text", "structured"} and tier not in {"fast", "standard", "deep"}:
        reasons.append("tier_not_applicable")
    execution_location: str | None = None
    if route_key is None:
        reasons.append("tier_unconfigured")
    else:
        try:
            route = dict(registry.get(route_key)["route"])
        except ModelRouteRegistryNotFound:
            reasons.append("route_missing")
        if route is not None:
            capabilities = _adapter_capabilities(route.get("adapter_kind"))
            if route.get("enabled") is not True:
                reasons.append("disabled")
            provider = providers.get(str(route.get("provider_id") or ""))
            if provider is None:
                reasons.append("provider_missing")
            else:
                try:
                    manifest = provider_egress_manifest(
                        getattr(provider, "record"), egress_policy,
                    )
                    execution_location = "remote" if manifest.external else "local_loopback"
                except (AttributeError, KeyError, TypeError, ValueError):
                    reasons.append("reference_invalid")
                if privacy_scope == "local_only" and execution_location != "local_loopback":
                    reasons.append("privacy_scope_ineligible")
                if not bool(getattr(provider, "egress_consented", False)):
                    reasons.append("unconsented")
                try:
                    registry.validate_provider_reference(route, provider=getattr(provider, "record"), egress_consented=bool(getattr(provider, "egress_consented", False)))
                except (AttributeError, ModelRouteRegistryError):
                    reasons.append("reference_invalid")
            if required_capability not in capabilities:
                reasons.append("required_capability_unsupported")
            _runtime_reasons(route, runtime, registry_state, reasons)
            if provider is not None and not reasons:
                try:
                    boundary_decision = model_boundary.preflight(
                        turn_id=turn_id,
                        project_id=project_id,
                        route_key=str(route["route_key"]),
                        provider_id=str(route["provider_id"]),
                        egress_categories=egress_categories,
                        privacy_scope=privacy_scope,
                        execution_location=execution_location or "remote",
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    reasons.append("boundary_denied")
                else:
                    if boundary_decision.outcome == "ask":
                        reasons.append("boundary_approval_required")
                    elif boundary_decision.outcome == "deny":
                        reasons.append("boundary_denied")
            if provider is not None and not reasons:
                try:
                    health = provider_health.get(ProviderHealthScope(
                        project_id=project_id,
                        boundary_profile_id=str(getattr(boundary, "profile_id")),
                        boundary_revision=int(getattr(boundary, "revision")),
                        route_key=str(route["route_key"]),
                        provider_id=str(route["provider_id"]),
                        provider_revision=str(route["provider_revision"]),
                        model_name=str(route["model_name"]),
                    ))
                except ModelProviderHealthError:
                    # Health is advisory and can only narrow availability. A
                    # broken projection never grants authority and never
                    # overrides the independent Boundary/consent gates.
                    health = None
                if health is not None and health.state is ProviderHealthState.OPEN:
                    reasons.append("provider_health_open")
                elif health is not None and health.state is ProviderHealthState.BLOCKED:
                    reasons.append("provider_health_blocked")
    if binding_drift:
        reasons.append("authority_binding_drift")
    reasons = sorted(set(reasons))
    if route is not None:
        _model_name(route.get("model_name"))
    route_metadata = None if route is None else {key: route[key] for key in ("route_key", "provider_id", "provider_revision", "model_name", "adapter_kind", "enabled", "revision")}
    item = {"tier": tier, "route": route_metadata, "capabilities": list(capabilities), "execution_location": execution_location, "eligible": not reasons, "exclusion_reasons": reasons}
    candidate = None
    if not reasons and route is not None:
        candidate = ModelRouteCandidate(route_key=str(route["route_key"]), capabilities=capabilities, revision=int(route["revision"]), revision_evidence=f"registry:{registry_state['registry_revision']}:runtime:{runtime['runtime_revision']}:route:{route['revision']}")
    return item, candidate


def _select_unique_eligible(tiers: list[dict[str, object]], route: ModelRouteCandidate | None, tier: str, reason: str) -> dict[str, object] | None:
    for item in tiers:
        if item["eligible"] and (route is None or item["tier"] != tier):
            item["eligible"] = False
            item["exclusion_reasons"] = ["tier_not_selected"]
    if route is None:
        return None
    selected_tier = next((item for item in tiers if item["tier"] == tier), None)
    if selected_tier is None or selected_tier["eligible"] is not True or not isinstance(selected_tier["route"], Mapping):
        raise TurnModelRoutingSnapshotError("selected route is not uniquely eligible")
    selected = selected_tier["route"]
    location = selected_tier.get("execution_location")
    if location not in _EXECUTION_LOCATIONS:
        raise TurnModelRoutingSnapshotError("selected route execution location is invalid")
    return {"tier": tier, "route_key": selected["route_key"], "route_revision": selected["revision"], "provider_id": selected["provider_id"], "provider_revision": selected["provider_revision"], "model_name": selected["model_name"], "adapter_kind": selected["adapter_kind"], "execution_location": location, "reason": reason}


def _runtime_reasons(route: Mapping[str, object], runtime: Mapping[str, object], registry_state: Mapping[str, object], reasons: list[str]) -> None:
    if runtime.get("runtime_activation") is not True or runtime.get("mode") != "active" or runtime.get("activated_registry_revision") != registry_state.get("registry_revision"):
        reasons.append("runtime_inactive")
        return
    assignment = next((item for item in runtime.get("assignments", []) if isinstance(item, Mapping) and item.get("route_key") == route.get("route_key")), None)
    if assignment is None:
        reasons.append("assignment_missing")
    elif dict(assignment) != {"route_key": route["route_key"], "provider_id": route["provider_id"], "provider_revision": route["provider_revision"], "model_name": route["model_name"], "route_revision": route["revision"]}:
        reasons.append("assignment_drift")


def _adapter_capabilities(adapter_kind: object) -> tuple[str, ...]:
    # This is intentionally the sole capability mapping; model names and
    # provider self-description never infer capabilities.
    if adapter_kind == "openai-compatible-vision":
        return ("vision",)
    if adapter_kind == "openai-compatible-image-generation":
        return ("image_generation",)
    return ("text", "structured") if adapter_kind == "openai-compatible" else ()


def _binding_drift(profile: object, registry: Mapping[str, object], runtime: Mapping[str, object]) -> bool:
    binding = getattr(profile, "authority_binding", None)
    return binding is not None and (binding.registry_revision != registry.get("registry_revision") or binding.runtime_revision != runtime.get("runtime_revision") or binding.activation_fingerprint != runtime.get("activation_fingerprint"))


def _binding_identity(profile: object) -> dict[str, object] | None:
    binding = getattr(profile, "authority_binding", None)
    return None if binding is None else {"registry_revision": binding.registry_revision, "runtime_revision": binding.runtime_revision, "activation_fingerprint": binding.activation_fingerprint}


def _context_policy(value: Mapping[str, object] | None) -> dict[str, object]:
    value = {} if value is None else value
    if not isinstance(value, Mapping):
        raise TurnModelRoutingSnapshotError("context policy is invalid")
    allowed = {"include_project_skill", "include_memory", "include_session_history", "max_context_bytes"}
    if set(value) - allowed:
        raise TurnModelRoutingSnapshotError("context policy contains unsupported fields")
    result = {key: value[key] for key in sorted(value)}
    _reject_sensitive(result)
    return result


def _validate_context_policy(value: object) -> None:
    if not isinstance(value, Mapping):
        raise TurnModelRoutingSnapshotError("context policy is invalid")
    _context_policy(value)
    for key in ("include_project_skill", "include_memory", "include_session_history"):
        if key in value and not isinstance(value[key], bool):
            raise TurnModelRoutingSnapshotError("context policy flag is invalid")
    if "max_context_bytes" in value:
        _positive(value["max_context_bytes"], "maximum context bytes")


def _input_refs(values: Sequence[object]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for value in values:
        if isinstance(value, str):
            if not _OPAQUE_CRP_REF.fullmatch(value):
                raise TurnModelRoutingSnapshotError("input reference must be an opaque crp identity")
            result.append({"ref": value})
        elif isinstance(value, Mapping) and set(value) <= {"kind", "object_id", "uri"}:
            object_id = value.get("object_id")
            uri = value.get("uri")
            if object_id is not None:
                result.append({
                    key: _identity(str(value[key]), f"input ref {key}")
                    for key in sorted(value) if key != "uri"
                })
            elif isinstance(uri, str) and _OPAQUE_CRP_REF.fullmatch(uri):
                result.append({"ref": uri})
            else:
                raise TurnModelRoutingSnapshotError("input reference is invalid")
        else:
            raise TurnModelRoutingSnapshotError("input reference is invalid")
    return sorted(result, key=lambda item: json.dumps(item, sort_keys=True))


def _validate_refs(values: object) -> None:
    if not isinstance(values, list):
        raise TurnModelRoutingSnapshotError("input refs are invalid")
    if values != sorted(values, key=lambda item: json.dumps(item, sort_keys=True)):
        raise TurnModelRoutingSnapshotError("input refs are not canonical")
    for item in values:
        if not isinstance(item, Mapping) or set(item) not in ({"ref"}, {"kind", "object_id"}):
            raise TurnModelRoutingSnapshotError("input ref shape is invalid")
        if "ref" in item and (
            not isinstance(item["ref"], str)
            or not _OPAQUE_CRP_REF.fullmatch(item["ref"])
        ):
            raise TurnModelRoutingSnapshotError("input ref identity is invalid")
        for key in ("kind", "object_id"):
            if key in item:
                _identity(item[key], f"input ref {key}")


def _require_requirement_contract(required_capability: object, modality: object, output_contract: object, egress_purpose: object, egress_categories: object, privacy_scope: object, retention_policy: object, protocol_version: object) -> None:
    if required_capability not in _CAPABILITIES or modality != _MODALITIES[required_capability] or output_contract != _OUTPUT_CONTRACTS[required_capability]:
        raise TurnModelRoutingSnapshotError("required capability contract is invalid")
    _identity(egress_purpose, "egress purpose")
    if not isinstance(egress_categories, Sequence) or isinstance(egress_categories, (str, bytes)) or not egress_categories:
        raise TurnModelRoutingSnapshotError("egress categories are invalid")
    categories = [_identity(item, "egress category") for item in egress_categories]
    if len(categories) != len(set(categories)):
        raise TurnModelRoutingSnapshotError("egress categories must be unique")
    if privacy_scope not in _PRIVACY_SCOPES or retention_policy not in _RETENTION_POLICIES:
        raise TurnModelRoutingSnapshotError("privacy or retention policy is invalid")
    if protocol_version != SCHEMA_VERSION:
        raise TurnModelRoutingSnapshotError("routing protocol version is invalid")


def payload_requirement_identity(required_capability: str, modality: str, output_contract: str, egress_purpose: str, egress_categories: Sequence[str], privacy_scope: str, retention_policy: str, capability_ids: Sequence[str], context_policy: Mapping[str, object], input_refs: Sequence[Mapping[str, str]], *, model_call_purpose: str | None = None) -> dict[str, object]:
    payload = {"required_capability": required_capability, "modality": modality, "output_contract": output_contract, "egress_purpose": egress_purpose, "egress_categories": sorted(egress_categories), "privacy_scope": privacy_scope, "retention_policy": retention_policy, "capability_ids": sorted(capability_ids), "context_policy": context_policy, "input_refs": input_refs}
    if model_call_purpose is not None:
        payload["model_call_purpose"] = model_call_purpose
    return payload


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise TurnModelRoutingSnapshotError(f"{label} is invalid")
    return value


def _model_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not _MODEL_NAME.fullmatch(value)
        or "://" in value
        or value.startswith("/")
    ):
        raise TurnModelRoutingSnapshotError("model name is not a safe opaque identity")
    return value


def _positive(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TurnModelRoutingSnapshotError(f"{label} is invalid")
    return value


def _nonnegative(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TurnModelRoutingSnapshotError(f"{label} is invalid")
    return value


def _revision(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _SENSITIVE or normalized.endswith("_secret"):
                raise TurnModelRoutingSnapshotError("sensitive material is forbidden in turn model routing snapshot")
            _reject_sensitive(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_sensitive(nested)
    elif isinstance(value, str) and _LOCAL_ABSOLUTE_PATH.search(value):
        raise TurnModelRoutingSnapshotError(
            "local absolute paths are forbidden in turn model routing snapshot"
        )


def _shape(value: object, expected: set[str], label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise TurnModelRoutingSnapshotError(f"turn model routing {label} shape is invalid")
