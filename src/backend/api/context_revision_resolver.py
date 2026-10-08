"""Read-only LineMap compilation revision projection.

This adapter turns the existing project model-routing authority into the
complete revision tuple consumed by ContextBinding composition.  It never
opens a model gateway or a provider connection: the routing snapshot is a
pure authority projection.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from backend.model_routing_snapshot import project_turn_model_routing_snapshot
from core.context_graph import ContextCompiler, FrozenContextRevisions


class ContextRevisionResolverError(ValueError):
    """The current compilation baseline cannot be proven from core authority."""


CapabilityRevisionResolver = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class ContextRevisionResolution:
    """One authoritative Context revision projection and its route location.

    The routing snapshot is projected once.  Consumers that have a stricter
    transport requirement, such as an externally executed benchmark, can
    prove it from this immutable result instead of recomputing a route.
    """

    revisions: FrozenContextRevisions
    execution_location: str

    def require_execution_location(self, expected: str) -> "ContextRevisionResolution":
        """Fail closed unless this same routing projection used ``expected``."""

        if expected not in {"remote", "local_loopback"}:
            raise ContextRevisionResolverError("routing_execution_location_invalid")
        if self.execution_location != expected:
            raise ContextRevisionResolverError("routing_execution_location_mismatch")
        return self


class ProjectContextRevisionResolver:
    """Resolve LineMap revisions using the normal ``context.evaluate`` route.

    ``capability_revision`` is deliberately a separate injected authority.
    ContextBindingCompositionService supplies the capability id from the
    immutable graph record and independently verifies this returned revision
    against its active package loader before and after compilation.
    """

    def __init__(
        self, container: object, capability_revision: CapabilityRevisionResolver,
    ) -> None:
        if not callable(capability_revision):
            raise ContextRevisionResolverError("capability_revision_resolver_invalid")
        self._container = container
        self._capability_revision = capability_revision

    def __call__(
        self, project_id: str, capability_id: str, binding_id: str, allow_remote: bool,
    ) -> FrozenContextRevisions:
        """Preserve the original revision-only authority contract."""

        return self.resolve(
            project_id, capability_id, binding_id, allow_remote,
        ).revisions

    def resolve(
        self, project_id: str, capability_id: str, binding_id: str, allow_remote: bool,
    ) -> ContextRevisionResolution:
        """Return revisions and the execution location from one Core snapshot."""

        if not all(isinstance(value, str) and value.strip() for value in (
            project_id, capability_id, binding_id,
        )) or type(allow_remote) is not bool:
            raise ContextRevisionResolverError("context_revision_request_invalid")
        capability_revision = self._capability_revision(capability_id)
        if not isinstance(capability_revision, str) or not capability_revision.strip():
            raise ContextRevisionResolverError("capability_revision_unavailable")

        # Use a stable synthetic Turn identity only to project the same model
        # route authority that a regular context.evaluate Turn would receive.
        # The binding reference is opaque metadata, never graph text or prompt.
        snapshot = project_turn_model_routing_snapshot(
            self._container,
            turn_id=f"context-binding:{binding_id}",
            project_id=project_id,
            required_capability="structured",
            modality="text",
            output_contract="json_object",
            egress_purpose="search_answer",
            egress_categories=("instructions", "source_excerpt"),
            privacy_scope="remote_allowed" if allow_remote else "local_only",
            retention_policy="turn_only",
            capability_ids=(capability_id,),
            context_policy={
                "include_project_skill": False,
                "include_memory": False,
                "include_session_history": False,
                "max_context_bytes": 262144,
            },
            input_refs=(
                f"crp://context-bindings/{project_id}/{binding_id}",
            ),
        )
        return self._resolution(snapshot, capability_revision)

    @staticmethod
    def _resolution(
        snapshot: object, capability_revision: str,
    ) -> ContextRevisionResolution:
        if not isinstance(snapshot, Mapping):
            raise ContextRevisionResolverError("routing_snapshot_invalid")
        selected = snapshot.get("selected")
        boundary = snapshot.get("boundary")
        if not isinstance(selected, Mapping):
            raise ContextRevisionResolverError("routing_selection_unavailable")
        if not isinstance(boundary, Mapping):
            raise ContextRevisionResolverError("boundary_revision_unavailable")
        provider_revision = selected.get("provider_revision")
        if not isinstance(provider_revision, str) or not provider_revision.strip():
            raise ContextRevisionResolverError("routing_revision_incomplete")
        execution_location = selected.get("execution_location")
        if execution_location not in {"remote", "local_loopback"}:
            raise ContextRevisionResolverError("routing_execution_location_unavailable")
        model_route_revision = _revision_text(selected.get("route_revision"))
        boundary_revision = _revision_text(boundary.get("profile_revision"))
        return ContextRevisionResolution(
            revisions=FrozenContextRevisions(
                capability_revision=capability_revision,
                boundary_revision=boundary_revision,
                provider_revision=provider_revision,
                model_route_revision=model_route_revision,
                compiler_revision=ContextCompiler.compiler_revision,
            ),
            execution_location=execution_location,
        )


def _revision_text(value: object) -> str:
    """Normalize Core numeric revisions into the ContextBinding text contract."""

    if isinstance(value, str) and value.strip():
        return value.strip()
    if type(value) is int and value > 0:
        return str(value)
    raise ContextRevisionResolverError("routing_revision_incomplete")
