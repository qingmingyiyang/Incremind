from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import json
from threading import Lock

from backend.security.project_boundary_profiles import (
    ProjectBoundaryProfileSnapshot,
    ProjectBoundaryProfileStore,
)
from backend.security.project_capability_profiles import (
    ProjectCapabilityProfileSnapshot,
    ProjectCapabilityProfileStore,
)
from core.ai_kernel import (
    CapabilityDefinition,
    CapabilityManifest,
    ContextEntry,
    ContextManifest,
    TurnPayloadStorePort,
    V1TurnContextManifestResolver,
    V1TurnPolicyCapabilityManifestResolver,
)
from core.ai_tooling import EffectiveToolPolicyResolver, tool_from_capability

from .application_skill_snapshot import TurnApplicationSkillSnapshotAuthority
from .model_routing_snapshot_authority import TurnModelRoutingSnapshotAuthority
from .personal_world_model_context import (
    PersonalWorldModelContextError,
    TurnWorldStateSnapshotAuthority,
)
from .published_project_memory_snapshot import PublishedProjectMemorySnapshotAuthority
from .context_binding_runtime import TurnContextBindingSnapshotAuthority
from .runtime_self_manifest_runtime import freeze_runtime_self_manifest_for_turn
from .context_compaction import DeterministicLocalMemoryCompactor
from core.context_graph import ContextCompiler, context_binding_to_payload


class ProjectProfileResolutionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TurnProjectProfileSnapshot:
    turn_id: str
    project_id: str
    capability: ProjectCapabilityProfileSnapshot
    boundary: ProjectBoundaryProfileSnapshot


class TurnProjectProfileSnapshotAuthority:
    """Shares one immutable profile read between both manifests for a Turn."""

    def __init__(
        self,
        capability_profiles: ProjectCapabilityProfileStore,
        boundary_profiles: ProjectBoundaryProfileStore,
    ) -> None:
        self._capability_profiles = capability_profiles
        self._boundary_profiles = boundary_profiles
        self._lock = Lock()
        self._by_turn: dict[str, TurnProjectProfileSnapshot] = {}

    def acquire(self, request: Mapping[str, object]) -> TurnProjectProfileSnapshot | None:
        scope = request.get("scope")
        if not isinstance(scope, Mapping):
            raise ProjectProfileResolutionError("turn scope is unavailable")
        if scope.get("kind") == "global":
            return None
        turn_id = _text(request.get("turn_id"), "turn id")
        project_id = _text(scope.get("project_id"), "project id")
        with self._lock:
            existing = self._by_turn.get(turn_id)
            if existing is not None:
                if existing.project_id != project_id:
                    raise ProjectProfileResolutionError("Turn project profile identity drifted")
                return existing
            capability = self._capability_profiles.get(project_id)
            boundary = self._boundary_profiles.get(project_id)
            if capability.profile.project_id != project_id or boundary.profile.project_id != project_id:
                raise ProjectProfileResolutionError("project profile identity drifted")
            if capability.profile.boundary_profile_id != boundary.profile.profile_id:
                raise ProjectProfileResolutionError("capability and Boundary profile identities do not match")
            if capability.profile.boundary_profile_revision != boundary.profile.revision:
                raise ProjectProfileResolutionError("capability and Boundary profile revisions do not match")
            snapshot = TurnProjectProfileSnapshot(turn_id, project_id, capability, boundary)
            self._by_turn[turn_id] = snapshot
            return snapshot

    def release(self, turn_id: str) -> None:
        with self._lock:
            self._by_turn.pop(turn_id, None)


class ProjectAwareCapabilityManifestResolver:
    def __init__(
        self,
        snapshots: TurnProjectProfileSnapshotAuthority,
        *,
        fallback: V1TurnPolicyCapabilityManifestResolver | None = None,
        tools: EffectiveToolPolicyResolver | None = None,
        application_skills: TurnApplicationSkillSnapshotAuthority | None = None,
        model_routing: TurnModelRoutingSnapshotAuthority | None = None,
    ) -> None:
        self._snapshots = snapshots
        self._fallback = fallback or V1TurnPolicyCapabilityManifestResolver()
        self._tools = tools or EffectiveToolPolicyResolver()
        self._application_skills = application_skills
        self._model_routing = model_routing

    def resolve(
        self,
        request: Mapping[str, object],
        capabilities: Sequence[CapabilityDefinition],
    ) -> CapabilityManifest:
        snapshot = self._snapshots.acquire(request)
        if snapshot is None:
            return self._fallback.resolve(request, capabilities)
        policy = request.get("capability_policy")
        if not isinstance(policy, Mapping):
            raise ProjectProfileResolutionError("turn capability policy is unavailable")
        resolution = self._tools.resolve(
            snapshot.capability.profile,
            tuple(tool_from_capability(item) for item in capabilities),
            turn_allowed=_string_tuple(policy.get("allowed"), "allowed capabilities"),
            turn_denied=_string_tuple(policy.get("denied"), "denied capabilities"),
            task_tool_ids=_exact_capability_ids(request),
            boundary_mode=snapshot.boundary.profile.mode,
        )
        capability_ids = tuple(tool.tool_id for tool in resolution.tools)
        skill_snapshot = (
            self._application_skills.acquire(
                request,
                project_id=snapshot.project_id,
                profile_id=snapshot.capability.profile.profile_id,
                profile_revision=snapshot.capability.profile.revision,
                enabled_skill_ids=snapshot.capability.profile.enabled_skill_ids,
                enabled_sources=snapshot.capability.profile.enabled_sources,
                enabled_plugin_ids=snapshot.capability.profile.enabled_plugin_ids,
            )
            if self._application_skills is not None
            else None
        )
        model_snapshot = (
            self._model_routing.acquire(
                request,
                project_id=snapshot.project_id,
                project_profile_id=snapshot.capability.profile.profile_id,
                project_profile_revision=snapshot.capability.profile.revision,
                boundary_profile_id=snapshot.boundary.profile.profile_id,
                boundary_profile_revision=snapshot.boundary.profile.revision,
                capability_ids=capability_ids,
                skill_snapshot_revision=(skill_snapshot.revision if skill_snapshot else None),
            )
            if self._model_routing is not None
            else None
        )
        return CapabilityManifest(
            manifest_id=f"capability-manifest-{request['turn_id']}",
            turn_id=str(request["turn_id"]),
            resolver_id="project-profile-v1",
            profile_id=snapshot.capability.profile.profile_id,
            profile_revision=snapshot.capability.profile.revision,
            capability_ids=capability_ids,
            excluded_reason_counts=resolution.excluded_reason_counts,
            descriptor_bytes=resolution.descriptor_bytes,
            boundary_profile_id=snapshot.boundary.profile.profile_id,
            boundary_profile_revision=snapshot.boundary.profile.revision,
            application_skill_snapshot_ref=(skill_snapshot.payload_ref if skill_snapshot else None),
            application_skill_snapshot_revision=(skill_snapshot.revision if skill_snapshot else None),
            model_routing_snapshot_ref=(model_snapshot.payload_ref if model_snapshot else None),
            model_routing_snapshot_revision=(model_snapshot.revision if model_snapshot else None),
        )


class ProjectAwareContextManifestResolver:
    def __init__(
        self,
        snapshots: TurnProjectProfileSnapshotAuthority,
        *,
        fallback: V1TurnContextManifestResolver | None = None,
        application_skills: TurnApplicationSkillSnapshotAuthority | None = None,
        model_routing: TurnModelRoutingSnapshotAuthority | None = None,
        published_memory: PublishedProjectMemorySnapshotAuthority | None = None,
        world_state: TurnWorldStateSnapshotAuthority | None = None,
        context_bindings: TurnContextBindingSnapshotAuthority | None = None,
        compactor: DeterministicLocalMemoryCompactor | None = None,
        runtime_self_manifest: Mapping[str, object] | None = None,
        payloads: TurnPayloadStorePort | None = None,
    ) -> None:
        self._snapshots = snapshots
        self._fallback = fallback or V1TurnContextManifestResolver()
        self._application_skills = application_skills
        self._model_routing = model_routing
        self._published_memory = published_memory
        self._world_state = world_state
        self._context_bindings = context_bindings
        self._compactor = compactor or (
            DeterministicLocalMemoryCompactor(payloads) if payloads is not None else None
        )
        self._runtime_self_manifest = runtime_self_manifest
        self._payloads = payloads

    def resolve(
        self,
        request: Mapping[str, object],
        capability_manifest_ref: str,
        capability_manifest: CapabilityManifest,
    ) -> ContextManifest:
        snapshot = self._snapshots.acquire(request)
        turn_id = _text(request.get("turn_id"), "turn id")
        try:
            baseline = self._fallback.resolve(
                request, capability_manifest_ref, capability_manifest
            )
            if snapshot is None:
                return _with_runtime_self_manifest(
                    baseline,
                    manifest=self._runtime_self_manifest,
                    payloads=self._payloads,
                    turn_id=turn_id,
                    project_id=None,
                )
            _validate_capability_binding(capability_manifest, snapshot)
            skill_snapshot = (
                self._application_skills.acquire(
                    request,
                    project_id=snapshot.project_id,
                    profile_id=snapshot.capability.profile.profile_id,
                    profile_revision=snapshot.capability.profile.revision,
                    enabled_skill_ids=snapshot.capability.profile.enabled_skill_ids,
                    enabled_sources=snapshot.capability.profile.enabled_sources,
                    enabled_plugin_ids=snapshot.capability.profile.enabled_plugin_ids,
                )
                if self._application_skills is not None
                else None
            )
            model_snapshot = (
                self._model_routing.acquire(
                    request,
                    project_id=snapshot.project_id,
                    project_profile_id=snapshot.capability.profile.profile_id,
                    project_profile_revision=snapshot.capability.profile.revision,
                    boundary_profile_id=snapshot.boundary.profile.profile_id,
                    boundary_profile_revision=snapshot.boundary.profile.revision,
                    capability_ids=capability_manifest.capability_ids,
                    skill_snapshot_revision=capability_manifest.application_skill_snapshot_revision,
                )
                if self._model_routing is not None
                else None
            )
            entries = list(baseline.entries)
            selected_bytes = baseline.selected_context_bytes
            if self._world_state is not None:
                remaining_bytes = baseline.max_context_bytes - selected_bytes
                if remaining_bytes < 1:
                    raise ProjectProfileResolutionError(
                        "WorldState Context Manifest budget is unavailable"
                    )
                try:
                    world_snapshot = self._world_state.acquire(
                        request,
                        project_id=snapshot.project_id,
                        max_context_bytes=remaining_bytes,
                    )
                except PersonalWorldModelContextError as error:
                    raise ProjectProfileResolutionError(str(error)) from error
                if world_snapshot.content_bytes > remaining_bytes:
                    raise ProjectProfileResolutionError(
                        "WorldState snapshot exceeds the Turn byte budget"
                    )
                selected_bytes += world_snapshot.content_bytes
                entries.append(ContextEntry(
                    entry_id="context-entry-world-state-projection",
                    kind="world_state_projection",
                    source_ref=world_snapshot.source_ref,
                    payload_ref=world_snapshot.payload_ref,
                    source_project_id=snapshot.project_id,
                    revision_identity=world_snapshot.revision,
                    content_fingerprint=None,
                    provenance_refs=world_snapshot.provenance_refs,
                    disclosure="model",
                    selection_reason="turn_frozen_derived_project_world_state",
                    content_bytes=world_snapshot.content_bytes,
                ))
            if skill_snapshot is not None:
                if (
                    capability_manifest.application_skill_snapshot_ref != skill_snapshot.payload_ref
                    or capability_manifest.application_skill_snapshot_revision != skill_snapshot.revision
                ):
                    raise ProjectProfileResolutionError("Capability and Context SkillSnapshot bindings drifted")
                entries.append(ContextEntry(
                    entry_id="context-entry-application-skill-snapshot",
                    kind="application_skill_snapshot",
                    source_ref=None,
                    payload_ref=skill_snapshot.payload_ref,
                    source_project_id=snapshot.project_id,
                    revision_identity=skill_snapshot.revision,
                    content_fingerprint=None,
                    provenance_refs=(),
                    disclosure="audit_only",
                    selection_reason="project_skill_selection_authority",
                    content_bytes=0,
                ))
                selected = skill_snapshot.payload.get("selected")
                if not isinstance(selected, list):
                    raise ProjectProfileResolutionError("SkillSnapshot selection is unavailable")
                for index, item in enumerate(selected):
                    if not isinstance(item, Mapping):
                        raise ProjectProfileResolutionError("SkillSnapshot selection is invalid")
                    content_bytes = int(item["instruction_bytes"])
                    selected_bytes += content_bytes
                    entries.append(ContextEntry(
                        entry_id=f"context-entry-application-skill-{index + 1}",
                        kind="application_skill",
                        source_ref=None,
                        payload_ref=str(item["instruction_payload_ref"]),
                        source_project_id=snapshot.project_id,
                        revision_identity=str(item["binding_revision"]),
                        content_fingerprint=str(item["skill_fingerprint"]),
                        provenance_refs=(skill_snapshot.payload_ref,),
                        disclosure="model",
                        selection_reason="project_enabled_matched_within_budget",
                        content_bytes=content_bytes,
                    ))
            if model_snapshot is not None:
                if (
                    capability_manifest.model_routing_snapshot_ref != model_snapshot.payload_ref
                    or capability_manifest.model_routing_snapshot_revision != model_snapshot.revision
                ):
                    raise ProjectProfileResolutionError("Capability and Context model routing bindings drifted")
                entries.append(ContextEntry(
                    entry_id="context-entry-model-routing-snapshot",
                    kind="model_routing_snapshot",
                    source_ref=None,
                    payload_ref=model_snapshot.payload_ref,
                    source_project_id=snapshot.project_id,
                    revision_identity=model_snapshot.revision,
                    content_fingerprint=str(model_snapshot.payload["catalog_revision"]),
                    provenance_refs=(),
                    disclosure="audit_only",
                    selection_reason="turn_model_routing_authority",
                    content_bytes=0,
                ))
            if self._context_bindings is not None:
                binding_snapshot = self._context_bindings.acquire(
                    request, project_id=snapshot.project_id,
                )
                if binding_snapshot is not None:
                    if model_snapshot is None:
                        raise ProjectProfileResolutionError(
                            "ContextBinding requires a frozen Model Route snapshot"
                        )
                    selected_route = model_snapshot.payload.get("selected")
                    if not isinstance(selected_route, Mapping):
                        raise ProjectProfileResolutionError(
                            "ContextBinding Model Route is unavailable"
                        )
                    binding = binding_snapshot.binding
                    if (
                        binding.compiler_revision != ContextCompiler.compiler_revision
                        or binding.boundary_revision != str(snapshot.boundary.profile.revision)
                        or binding.provider_revision != str(selected_route.get("provider_revision"))
                        or binding.model_route_revision != str(selected_route.get("route_revision"))
                    ):
                        raise ProjectProfileResolutionError(
                            "ContextBinding frozen revisions drifted"
                        )
                    binding_payload = context_binding_to_payload(binding)
                    content_bytes = len(json.dumps(
                        binding_payload, ensure_ascii=False, separators=(",", ":"),
                    ).encode("utf-8"))
                    if selected_bytes + content_bytes > baseline.max_context_bytes:
                        raise ProjectProfileResolutionError(
                            "ContextBinding exceeds the Turn byte budget"
                        )
                    selected_bytes += content_bytes
                    entries.append(ContextEntry(
                        entry_id="context-entry-context-binding",
                        kind="context_binding",
                        source_ref=(
                            f"crp://context-bindings/{snapshot.project_id}/"
                            f"{binding_snapshot.binding_id}"
                        ),
                        payload_ref=binding_snapshot.payload_ref,
                        source_project_id=snapshot.project_id,
                        revision_identity=(
                            f"{binding.graph_revision}:"
                            f"{binding_snapshot.registry_revision}"
                        ),
                        content_fingerprint=None,
                        provenance_refs=(),
                        disclosure="model",
                        selection_reason="explicit_linemap_context_binding",
                        content_bytes=content_bytes,
                    ))
            if self._published_memory is not None:
                remaining_bytes = baseline.max_context_bytes - selected_bytes
                if remaining_bytes < 0:
                    raise ProjectProfileResolutionError("Context Manifest budget is unavailable")
                memory_snapshot = self._published_memory.acquire(
                    request,
                    project_id=snapshot.project_id,
                    profile_id=snapshot.capability.profile.profile_id,
                    profile_revision=snapshot.capability.profile.revision,
                    max_context_bytes=remaining_bytes,
                )
                entries.append(ContextEntry(
                    entry_id="context-entry-published-project-memory-snapshot",
                    # The current Context Manifest contract has no dedicated
                    # snapshot kind.  ``memory_r0`` is an audit-only, non-model
                    # memory layer entry; the immutable payload and revision
                    # remain the exact snapshot identity.
                    kind="memory_r0",
                    source_ref=f"crp://memory/{snapshot.project_id}/published-project-memory-snapshot",
                    payload_ref=memory_snapshot.payload_ref,
                    source_project_id=snapshot.project_id,
                    revision_identity=memory_snapshot.revision,
                    content_fingerprint=None,
                    provenance_refs=(),
                    disclosure="audit_only",
                    selection_reason="published_project_memory_snapshot_authority",
                    content_bytes=0,
                ))
                selected = memory_snapshot.payload.get("selected")
                if not isinstance(selected, list):
                    raise ProjectProfileResolutionError("Published Project Memory selection is unavailable")
                for index, item in enumerate(selected):
                    if not isinstance(item, Mapping):
                        raise ProjectProfileResolutionError("Published Project Memory selection is invalid")
                    manifest_kind = _text(item.get("manifest_kind"), "Published Project Memory manifest kind")
                    object_id = _text(item.get("object_id"), "Published Project Memory object id")
                    revision = item.get("revision")
                    content_bytes = item.get("context_bytes")
                    payload_ref = _text(item.get("payload_ref"), "Published Project Memory payload ref")
                    if (
                        not isinstance(revision, int)
                        or isinstance(revision, bool)
                        or revision < 1
                        or not isinstance(content_bytes, int)
                        or isinstance(content_bytes, bool)
                        or content_bytes < 0
                    ):
                        raise ProjectProfileResolutionError("Published Project Memory selection is invalid")
                    if manifest_kind not in PublishedProjectMemorySnapshotAuthority._manifest_kind.values():
                        raise ProjectProfileResolutionError("Published Project Memory manifest kind is invalid")
                    # L2/L3 are intentionally not prompt material.  A legacy
                    # immutable snapshot may still list them for audit, but a
                    # current Context Manifest never exposes their payload to
                    # the model.  The existing memory.recall/drilldown tool
                    # remains the only bounded access path.
                    if (
                        manifest_kind
                        not in PublishedProjectMemorySnapshotAuthority.model_injected_manifest_kinds
                    ):
                        continue
                    selected_bytes += content_bytes
                    entries.append(ContextEntry(
                        entry_id=f"context-entry-published-project-memory-{index + 1}",
                        kind=manifest_kind,
                        source_ref=_published_memory_source_ref(
                            manifest_kind, snapshot.project_id, object_id,
                        ),
                        payload_ref=payload_ref,
                        source_project_id=snapshot.project_id,
                        revision_identity=str(revision),
                        content_fingerprint=None,
                        provenance_refs=(memory_snapshot.payload_ref,),
                        disclosure="model",
                        selection_reason="published_project_memory_selected_within_remaining_budget",
                        content_bytes=content_bytes,
                    ))
            resolved = replace(
                baseline,
                resolver_id="project-profile-context-v2",
                project_profile_id=snapshot.capability.profile.profile_id,
                project_profile_revision=snapshot.capability.profile.revision,
                boundary_profile_id=snapshot.boundary.profile.profile_id,
                boundary_profile_revision=snapshot.boundary.profile.revision,
                entries=tuple(entries),
                selected_context_bytes=selected_bytes,
            )
            compacted = self._compactor.compact(resolved) if self._compactor is not None else resolved
            return _with_runtime_self_manifest(
                compacted,
                manifest=self._runtime_self_manifest,
                payloads=self._payloads,
                turn_id=turn_id,
                project_id=snapshot.project_id,
            )
        finally:
            self._snapshots.release(turn_id)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectProfileResolutionError(f"{label} must be non-empty")
    return value.strip()


def _with_runtime_self_manifest(
    baseline: ContextManifest,
    *,
    manifest: Mapping[str, object] | None,
    payloads: TurnPayloadStorePort | None,
    turn_id: str,
    project_id: str | None,
) -> ContextManifest:
    """Append a diagnostic-only runtime snapshot without widening Turn authority.

    The model receives the manifest only when it fits the remaining Context
    budget.  Otherwise the same immutable payload is retained as tool-only
    diagnostic context.  If no payload authority is composed, omission keeps
    legacy hosts running safely.
    """
    if manifest is None or payloads is None:
        return baseline
    model_context = freeze_runtime_self_manifest_for_turn(
        manifest,
        turn_id=turn_id,
        payloads=payloads,
        disclosure="model",
        source_project_id=project_id,
    )
    if model_context is None:
        return baseline
    if baseline.selected_context_bytes + model_context.entry.content_bytes <= baseline.max_context_bytes:
        return replace(
            baseline,
            entries=(*baseline.entries, model_context.entry),
            selected_context_bytes=(baseline.selected_context_bytes + model_context.entry.content_bytes),
        )
    diagnostic_context = freeze_runtime_self_manifest_for_turn(
        manifest,
        turn_id=turn_id,
        payloads=payloads,
        disclosure="tool_only",
        source_project_id=project_id,
    )
    if diagnostic_context is None:
        return baseline
    return replace(baseline, entries=(*baseline.entries, diagnostic_context.entry))


def _published_memory_source_ref(manifest_kind: str, project_id: str, object_id: str) -> str:
    if manifest_kind == "project_skill":
        return f"crp://skills/{project_id}/{object_id}"
    if manifest_kind in {"memory_r1", "memory_r2", "memory_r3"}:
        return f"crp://memory/{project_id}/{object_id}"
    raise ProjectProfileResolutionError("Published Project Memory manifest kind is invalid")


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ProjectProfileResolutionError(f"{label} must be a string array")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise ProjectProfileResolutionError(f"{label} must be unique")
    return result


def _exact_capability_ids(request: Mapping[str, object]) -> tuple[str, ...]:
    """Turn-owned explicit execution narrows, never expands, the manifest."""

    capability_request = request.get("capability_request")
    if capability_request is None:
        return ()
    if not isinstance(capability_request, Mapping):
        raise ProjectProfileResolutionError("exact capability request is invalid")
    if capability_request.get("mode") != "execute_exact_v1":
        raise ProjectProfileResolutionError("exact capability request mode is invalid")
    return (_text(capability_request.get("capability_id"), "exact capability id"),)


def _validate_capability_binding(
    manifest: CapabilityManifest,
    snapshot: TurnProjectProfileSnapshot,
) -> None:
    capability = snapshot.capability.profile
    boundary = snapshot.boundary.profile
    if (
        manifest.turn_id != snapshot.turn_id
        or manifest.profile_id != capability.profile_id
        or manifest.profile_revision != capability.revision
        or manifest.boundary_profile_id != boundary.profile_id
        or manifest.boundary_profile_revision != boundary.revision
    ):
        raise ProjectProfileResolutionError("capability manifest Boundary binding drifted")
