"""Provider-neutral, deterministic model tier selection contracts."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


ModelTier = Literal["fast", "standard", "deep", "vision", "image_generation"]
ModelCapability = Literal["text", "structured", "vision", "image_generation"]

_TIERS: tuple[ModelTier, ...] = (
    "fast",
    "standard",
    "deep",
    "vision",
    "image_generation",
)
_TEXT_TIERS = frozenset({"fast", "standard", "deep"})
_CAPABILITIES = frozenset({"text", "structured", "vision", "image_generation"})
_HARD_MODALITIES = frozenset({"vision", "image_generation"})
_ROUTE_KEY = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_REVISION_EVIDENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REASON = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


class ModelRoutingError(ValueError):
    """Raised when a model-routing contract is not valid."""


@dataclass(frozen=True, slots=True)
class ModelRoutingAuthorityBinding:
    registry_revision: int
    runtime_revision: int
    activation_fingerprint: str

    def __post_init__(self) -> None:
        _require_positive(self.registry_revision, "registry revision")
        _require_positive(self.runtime_revision, "runtime revision")
        if not isinstance(self.activation_fingerprint, str) or not re.fullmatch(
            r"[a-f0-9]{64}", self.activation_fingerprint,
        ):
            raise ModelRoutingError("activation fingerprint is invalid")


@dataclass(frozen=True, slots=True)
class ModelRoutingProfile:
    revision: int
    rules_version: int
    text_default_tier: ModelTier
    tier_routes: tuple[tuple[ModelTier, str | None], ...]
    authority_binding: ModelRoutingAuthorityBinding | None = None

    def __post_init__(self) -> None:
        _require_positive(self.revision, "profile revision")
        _require_positive(self.rules_version, "rules version")
        if not isinstance(self.text_default_tier, str) or self.text_default_tier not in _TEXT_TIERS:
            raise ModelRoutingError("text default tier must be a text tier")
        if not isinstance(self.tier_routes, tuple) or len(self.tier_routes) != len(_TIERS):
            raise ModelRoutingError("tier routes must contain every supported tier")
        routes: dict[str, str | None] = {}
        for item in self.tier_routes:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ModelRoutingError("tier route must be a tier and route key pair")
            tier, route_key = item
            if not isinstance(tier, str) or tier not in _TIERS or tier in routes:
                raise ModelRoutingError("tier routes must contain every supported tier exactly once")
            if route_key is not None:
                _require_match(route_key, _ROUTE_KEY, "route key")
            routes[tier] = route_key
        if tuple(routes) != _TIERS:
            raise ModelRoutingError("tier routes must use canonical tier order")
        if self.authority_binding is not None and not isinstance(
            self.authority_binding, ModelRoutingAuthorityBinding,
        ):
            raise ModelRoutingError("routing authority binding is invalid")

    def route_key_for(self, tier: ModelTier) -> str | None:
        if tier not in _TIERS:
            raise ModelRoutingError("model tier is unsupported")
        return dict(self.tier_routes)[tier]


@dataclass(frozen=True, slots=True)
class ModelRouteCandidate:
    """A route supplied by a trusted adapter or manifest, never model-name inference."""

    route_key: str
    capabilities: tuple[ModelCapability, ...]
    revision: int
    revision_evidence: str

    def __post_init__(self) -> None:
        _require_match(self.route_key, _ROUTE_KEY, "route key")
        _require_positive(self.revision, "route revision")
        _require_match(
            self.revision_evidence, _REVISION_EVIDENCE, "revision evidence",
        )
        if not isinstance(self.capabilities, tuple) or not self.capabilities:
            raise ModelRoutingError("route capabilities must be a non-empty tuple")
        if any(not isinstance(capability, str) or capability not in _CAPABILITIES for capability in self.capabilities):
            raise ModelRoutingError("route capability is unsupported")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ModelRoutingError("route capabilities must be unique")


@dataclass(frozen=True, slots=True)
class ModelSelectionRequest:
    required_capability: ModelCapability
    project_preferred_tier: ModelTier | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.required_capability, str) or self.required_capability not in _CAPABILITIES:
            raise ModelRoutingError("required capability is unsupported")
        if self.project_preferred_tier is not None and (
            not isinstance(self.project_preferred_tier, str) or self.project_preferred_tier not in _TIERS
        ):
            raise ModelRoutingError("project preferred tier is unsupported")
        if (
            self.required_capability not in _HARD_MODALITIES
            and self.project_preferred_tier is not None
            and self.project_preferred_tier not in _TEXT_TIERS
        ):
            raise ModelRoutingError("text capability requires a text project preferred tier")


@dataclass(frozen=True, slots=True)
class ModelRoutingDecision:
    available: bool
    tier: ModelTier
    route: ModelRouteCandidate | None
    reason: str
    profile_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.available, bool):
            raise ModelRoutingError("decision availability must be boolean")
        if not isinstance(self.tier, str) or self.tier not in _TIERS:
            raise ModelRoutingError("decision tier is unsupported")
        _require_match(self.reason, _REASON, "decision reason")
        _require_positive(self.profile_revision, "decision profile revision")
        if self.route is not None and not isinstance(self.route, ModelRouteCandidate):
            raise ModelRoutingError("decision route is invalid")
        if self.available != (self.route is not None):
            raise ModelRoutingError("available decision must carry exactly one route")


class ModelRoutingRouter:
    """Resolves a capability request without importing provider or backend code."""

    def resolve(
        self,
        profile: ModelRoutingProfile,
        candidates: tuple[ModelRouteCandidate, ...],
        request: ModelSelectionRequest,
    ) -> ModelRoutingDecision:
        if not isinstance(profile, ModelRoutingProfile):
            raise ModelRoutingError("routing profile is invalid")
        if not isinstance(request, ModelSelectionRequest):
            raise ModelRoutingError("selection request is invalid")
        candidate_by_key = _candidate_index(candidates)

        if request.required_capability in _HARD_MODALITIES:
            tier = request.required_capability
            route = self._route_for(profile, candidate_by_key, tier, request.required_capability)
            return self._decision(
                profile, tier, route,
                "required_modality_selected" if route is not None else "required_modality_unavailable",
            )

        preferred = request.project_preferred_tier or profile.text_default_tier
        route = self._route_for(profile, candidate_by_key, preferred, request.required_capability)
        if route is not None:
            return self._decision(
                profile, preferred, route,
                "project_preferred_tier" if request.project_preferred_tier is not None else "text_default_tier",
            )

        if preferred != profile.text_default_tier:
            fallback = self._route_for(
                profile, candidate_by_key, profile.text_default_tier, request.required_capability
            )
            if fallback is not None:
                return self._decision(profile, profile.text_default_tier, fallback, "text_default_fallback")
            return self._decision(profile, profile.text_default_tier, None, "text_default_unavailable")
        return self._decision(profile, preferred, None, "text_default_unavailable")

    @staticmethod
    def _route_for(
        profile: ModelRoutingProfile,
        candidates: dict[str, ModelRouteCandidate],
        tier: ModelTier,
        capability: ModelCapability,
    ) -> ModelRouteCandidate | None:
        route_key = profile.route_key_for(tier)
        if route_key is None:
            return None
        candidate = candidates.get(route_key)
        if candidate is None or capability not in candidate.capabilities:
            return None
        return candidate

    @staticmethod
    def _decision(
        profile: ModelRoutingProfile,
        tier: ModelTier,
        route: ModelRouteCandidate | None,
        reason: str,
    ) -> ModelRoutingDecision:
        return ModelRoutingDecision(route is not None, tier, route, reason, profile.revision)


# The concise name is retained for consumers that express routing in tier terms.
ModelTierRouter = ModelRoutingRouter


def _candidate_index(candidates: tuple[ModelRouteCandidate, ...]) -> dict[str, ModelRouteCandidate]:
    if not isinstance(candidates, tuple):
        raise ModelRoutingError("route candidates must be a tuple")
    indexed: dict[str, ModelRouteCandidate] = {}
    for candidate in candidates:
        if not isinstance(candidate, ModelRouteCandidate):
            raise ModelRoutingError("route candidate is invalid")
        if candidate.route_key in indexed:
            raise ModelRoutingError("route candidate keys must be unique")
        indexed[candidate.route_key] = candidate
    return indexed


def _require_positive(value: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ModelRoutingError(f"{label} must be positive")


def _require_match(value: str, pattern: re.Pattern[str], label: str) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ModelRoutingError(f"{label} is invalid")
