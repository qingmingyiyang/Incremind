"""Strict routing-snapshot support shared by legacy AI Turn fixtures.

The production capabilities intentionally fail closed if a remote model call
does not have the immutable routing authority frozen for that Turn.  These
fixtures use the same projection builder as production instead of hand-made
snapshot payloads.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from backend.api.model_routing_snapshot_authority import TurnModelRoutingSnapshotAuthority
from backend.model_route_context import model_route_provider_context_from_record
from backend.model_routing_profile import ModelRoutingProfileStore
from backend.model_routing_snapshot import (
    project_turn_model_routing_snapshot,
    turn_model_routing_snapshot_revision,
)
from backend.providers import ProviderRegistry
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.ai_kernel import CapabilityManifest, ContextEntry, ContextManifest
from core.product_core.model_route_registry import ModelRouteRegistry
from core.product_core.model_route_runtime import ModelRouteRuntimeService


class RoutingSnapshotFixture:
    """Strict fixture resolvers that expose one official snapshot to a Turn."""

    def __init__(
        self,
        payloads: object,
        *,
        required_capability: str,
        egress_purpose: str,
    ) -> None:
        self._payloads = payloads
        self._required_capability = required_capability
        self._egress_purpose = egress_purpose
        self.context_resolver = _RoutingSnapshotContextResolver(self)

    def resolve(self, request, capabilities):
        seed_turn_model_routing_snapshot(
            self._payloads,
            request,
            required_capability=self._required_capability,
            egress_purpose=self._egress_purpose,
        )
        turn_id = str(request["turn_id"])
        snapshot_ref, snapshot = self._payloads.get_immutable_payload(
            turn_id, TurnModelRoutingSnapshotAuthority.snapshot_kind,
        )
        profile = snapshot["profile"]
        boundary = snapshot["boundary"]
        return CapabilityManifest(
            manifest_id=f"routing-fixture-manifest-{turn_id}",
            turn_id=turn_id,
            resolver_id="integration-routing-fixture",
            profile_id=str(profile["profile_id"]),
            profile_revision=int(profile["profile_revision"]),
            capability_ids=tuple(item.capability_id for item in capabilities),
            excluded_reason_counts=(),
            descriptor_bytes=0,
            boundary_profile_id=str(boundary["profile_id"]),
            boundary_profile_revision=int(boundary["profile_revision"]),
            model_routing_snapshot_ref=snapshot_ref,
            model_routing_snapshot_revision=turn_model_routing_snapshot_revision(snapshot),
        )

    def resolve_context(self, request, capability_manifest_ref, capability_manifest):
        turn_id = str(request["turn_id"])
        snapshot_ref, snapshot = self._payloads.get_immutable_payload(
            turn_id, TurnModelRoutingSnapshotAuthority.snapshot_kind,
        )
        scope = request["scope"]
        policy = request["context_policy"]
        assert isinstance(scope, Mapping)
        assert isinstance(policy, Mapping)
        profile = snapshot["profile"]
        boundary = snapshot["boundary"]
        entries = (
            ContextEntry(
                entry_id="routing-fixture-capability-manifest",
                kind="capability_manifest", source_ref=None,
                payload_ref=capability_manifest_ref,
                source_project_id=str(scope["project_id"]),
                revision_identity=str(capability_manifest.profile_revision),
                content_fingerprint=None, provenance_refs=(), disclosure="tool_only",
                selection_reason="kernel_execution_boundary", content_bytes=0,
            ),
            ContextEntry(
                entry_id="routing-fixture-model-snapshot",
                kind="model_routing_snapshot", source_ref=None,
                payload_ref=snapshot_ref, source_project_id=str(scope["project_id"]),
                revision_identity=str(capability_manifest.model_routing_snapshot_revision),
                content_fingerprint=str(snapshot["catalog_revision"]),
                provenance_refs=(), disclosure="audit_only",
                selection_reason="turn_model_routing_authority", content_bytes=0,
            ),
        )
        return ContextManifest(
            manifest_id=f"routing-fixture-context-{turn_id}", turn_id=turn_id,
            resolver_id="integration-routing-fixture", project_id=str(scope["project_id"]),
            series_id=None, project_profile_id=str(profile["profile_id"]),
            project_profile_revision=int(profile["profile_revision"]),
            boundary_profile_id=str(boundary["profile_id"]),
            boundary_profile_revision=int(boundary["profile_revision"]),
            capability_manifest_ref=capability_manifest_ref, entries=entries,
            compactions=(), excluded_reason_counts=(),
            max_context_bytes=int(policy["max_context_bytes"]), selected_context_bytes=0,
        )


class _RoutingSnapshotContextResolver:
    def __init__(self, fixture: RoutingSnapshotFixture) -> None:
        self._fixture = fixture

    def resolve(self, request, capability_manifest_ref, capability_manifest):
        return self._fixture.resolve_context(
            request, capability_manifest_ref, capability_manifest,
        )


def seed_turn_model_routing_snapshot(
    payloads: object,
    request: Mapping[str, object],
    *,
    required_capability: str,
    egress_purpose: str,
) -> None:
    """Freeze the official projection in the same immutable payload store."""

    turn_id = str(request["turn_id"])
    kind = TurnModelRoutingSnapshotAuthority.snapshot_kind
    if payloads.get_immutable_payload(turn_id, kind) is not None:
        return
    scope = request["scope"]
    privacy = request["privacy"]
    policy = request["capability_policy"]
    input_value = request["input"]
    context_policy = request["context_policy"]
    assert isinstance(scope, Mapping)
    assert isinstance(privacy, Mapping)
    assert isinstance(policy, Mapping)
    assert isinstance(input_value, Mapping)
    assert isinstance(context_policy, Mapping)
    allowed = policy.get("allowed")
    refs = input_value.get("refs")
    assert isinstance(allowed, list)
    assert isinstance(refs, list)
    with TemporaryDirectory(prefix="chriptmas-routing-fixture-") as root:
        _activate_fixture_route(Path(root), project_id=str(scope["project_id"]))
        snapshot = project_turn_model_routing_snapshot(
            SimpleNamespace(root_dir=Path(root)),
            turn_id=turn_id,
            project_id=str(scope["project_id"]),
            required_capability=required_capability,
            modality="text",
            output_contract=("text" if required_capability == "text" else "json_object"),
            egress_purpose=egress_purpose,
            egress_categories=("instructions", "source_excerpt"),
            privacy_scope=(
                "remote_allowed"
                if privacy.get("mode") == "remote_allowed" and privacy.get("allow_remote") is True
                else "local_only"
            ),
            capability_ids=tuple(str(item) for item in allowed),
            context_policy=dict(context_policy),
            input_refs=refs,
        )
    payloads.get_or_create_immutable_payload(turn_id, kind, snapshot)


def _activate_fixture_route(root: Path, *, project_id: str) -> None:
    """Build a real, consented active route so remote fixture calls stay remote."""
    provider = ProviderRegistry(root).create(
        {
            "provider_id": "fixture-provider", "name": "fixture-provider",
            "llm_provider": "openai", "base_url": "http://127.0.0.1:8317",
            "api_path": "/chat/completions", "model": "fixture-model",
            "models": ["fixture-model"], "enabled": True,
        },
        fallback={},
    )
    context = model_route_provider_context_from_record(root, provider)
    registry = ModelRouteRegistry(root)
    registry.update(
        "tier.standard",
        {"provider_id": "fixture-provider", "model_name": "fixture-model", "adapter_kind": "openai-compatible", "enabled": True, "reason": "integration fixture"},
        expected_registry_revision=0, provider=provider, egress_consented=True,
    )
    runtime = ModelRouteRuntimeService(root)
    preview = runtime.preview(
        route_keys=["tier.standard"], compatibility={"tier.standard": context},
        providers=[context],
    )
    runtime.activate(
        shadow_token=preview["shadow_token"], route_keys=preview["route_keys"],
        expected_runtime_revision=0, confirm=True,
        compatibility={"tier.standard": context}, providers=[context],
    )
    ModelRoutingProfileStore(root).update(
        expected_revision=1, rules_version=1, text_default_tier="standard",
        tier_routes={"fast": None, "standard": "tier.standard", "deep": None, "vision": None, "image_generation": None},
    )
    ProjectCapabilityProfileStore(root).update(
        project_id, expected_revision=0,
        boundary_profile_id=f"project-boundary-{project_id}",
        boundary_profile_revision=1, preferred_model_tier="standard",
    )
