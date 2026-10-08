"""Frozen-Manifest platform routing for durable Media Hands providers.

The router is deliberately an in-process Hands component.  It does not create
or update Source, Job, Document, or other durable authority; it selects an
already-admitted provider using the platform recorded in the frozen manifest.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
import re

from .handler import (
    MediaOperationProviderPort,
    MediaOperationReceipt,
    MediaOperationRequest,
)


_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class MediaOperationProviderRouterError(ValueError):
    """A platform/provider contract is unavailable or has drifted."""


ManifestPlatformResolver = Callable[[MediaOperationRequest], str]


@dataclass(frozen=True, slots=True)
class _DelegateBinding:
    platform: str
    provider: MediaOperationProviderPort
    provider_id: str
    provider_revision: str
    supported_platforms: frozenset[str]


class MediaOperationProviderRouter:
    """Route an admitted operation by its frozen SourceManifest platform.

    ``manifest_platform_resolver`` is intentionally injected rather than
    reading a field from ``MediaOperationRequest``.  Request fields are job
    execution metadata and must never become a second source of truth for the
    platform selected during SourceManifest admission.
    """

    provider_id = "media-operation-provider-router"
    supports_durable_recipe_resume = True

    def __init__(
        self,
        providers: Mapping[str, MediaOperationProviderPort],
        manifest_platform_resolver: ManifestPlatformResolver,
    ) -> None:
        if not isinstance(providers, Mapping) or not providers:
            raise MediaOperationProviderRouterError("media provider routing requires providers")
        if not callable(manifest_platform_resolver):
            raise TypeError("manifest platform resolver is required")

        bindings: dict[str, _DelegateBinding] = {}
        for configured_platform, provider in providers.items():
            platform = _platform(configured_platform, "configured platform")
            if platform in bindings:
                raise MediaOperationProviderRouterError("duplicate media provider platform")
            bindings[platform] = _bind(platform, provider)

        self._bindings = bindings
        self._manifest_platform_resolver = manifest_platform_resolver
        self.supported_platforms = frozenset(bindings)
        self.provider_revision = _router_revision(bindings.values())

    def execute(self, request: MediaOperationRequest) -> MediaOperationReceipt:
        """Dispatch only after resolving platform from the frozen manifest."""

        platform = _platform(
            self._manifest_platform_resolver(request), "frozen manifest platform"
        )
        binding = self._bindings.get(platform)
        if binding is None:
            raise MediaOperationProviderRouterError(
                "frozen manifest platform has no admitted media provider"
            )
        _assert_binding_current(binding)
        return binding.provider.execute(request)


def _bind(platform: str, provider: MediaOperationProviderPort) -> _DelegateBinding:
    provider_id = _identity(getattr(provider, "provider_id", None), "provider_id")
    provider_revision = _identity(
        getattr(provider, "provider_revision", None), "provider_revision"
    )
    supported = getattr(provider, "supported_platforms", None)
    if not isinstance(supported, frozenset) or supported != frozenset({platform}):
        raise MediaOperationProviderRouterError(
            "each media provider must declare exactly its configured platform"
        )
    if not all(isinstance(value, str) for value in supported):
        raise MediaOperationProviderRouterError("media provider platforms are invalid")
    if getattr(provider, "supports_durable_recipe_resume", False) is not True:
        raise MediaOperationProviderRouterError(
            "media provider must support durable recipe resume"
        )
    if not callable(getattr(provider, "execute", None)):
        raise MediaOperationProviderRouterError("media provider execute is required")
    return _DelegateBinding(
        platform=platform,
        provider=provider,
        provider_id=provider_id,
        provider_revision=provider_revision,
        supported_platforms=supported,
    )


def _assert_binding_current(binding: _DelegateBinding) -> None:
    """Reject mutable delegate identity/capability changes before effects."""

    provider = binding.provider
    if (
        getattr(provider, "provider_id", None) != binding.provider_id
        or getattr(provider, "provider_revision", None) != binding.provider_revision
        or getattr(provider, "supported_platforms", None) != binding.supported_platforms
        or getattr(provider, "supports_durable_recipe_resume", False) is not True
        or not callable(getattr(provider, "execute", None))
    ):
        raise MediaOperationProviderRouterError("media provider routing contract drifted")


def _router_revision(bindings: Iterable[_DelegateBinding]) -> str:
    canonical = [
        {"platform": item.platform, "provider_id": item.provider_id, "provider_revision": item.provider_revision}
        for item in sorted(bindings, key=lambda value: value.platform)
    ]
    encoded = json.dumps(canonical, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return "router-" + hashlib.sha256(encoded).hexdigest()


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise MediaOperationProviderRouterError(f"media provider {label} is invalid")
    return value


def _platform(value: object, label: str) -> str:
    if not isinstance(value, str) or value != value.strip().lower() or _IDENTITY.fullmatch(value) is None:
        raise MediaOperationProviderRouterError(f"{label} is invalid")
    return value
