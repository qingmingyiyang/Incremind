from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from backend.security.turn_boundary_adapter import ExternalExecutionAuthority, TurnBoundaryRequestFactory
from core.ai_boundary import (
    EphemeralTokenVault,
    SanitizationResult,
    ScanSummary,
    SensitiveTextScanner,
)
from core.ai_kernel import (
    CapabilityDefinition,
    ToolExecutionBoundaryDecision,
    TurnEventStorePort,
    TurnPayloadStorePort,
    context_manifest_from_payload,
    manifest_from_payload,
    validate_context_manifest_for_request,
)
from core.ai_tooling import tool_destination_identity, tool_from_capability


class AIToolExecutionBoundaryError(ValueError):
    pass


class TurnCapabilityBindingDrift(AIToolExecutionBoundaryError):
    """The persisted Turn snapshot no longer matches current local authority."""


class TurnCapabilityBindingGuard:
    """Fail closed when a project Turn's persisted binding drifts before invoke.

    The guard reads the already-persisted Context/Capability manifests.  It
    deliberately does not acquire a second Turn snapshot or rewrite the
    manifest from current policy.
    """

    def __init__(
        self,
        capability_profiles: ProjectCapabilityProfileStore,
        boundary_profiles: ProjectBoundaryProfileStore,
        *,
        events: TurnEventStorePort,
        payloads: TurnPayloadStorePort,
    ) -> None:
        self._capability_profiles = capability_profiles
        self._boundary_profiles = boundary_profiles
        self._events = events
        self._payloads = payloads

    def ensure_current(self, request: Mapping[str, object]) -> None:
        with self.dispatch_fence(request):
            return

    @contextmanager
    def dispatch_fence(self, request: Mapping[str, object]) -> Iterator[None]:
        """Linearize the final binding check and provider invocation.

        The Boundary profile lock is the write authority lock.  Holding it
        through invoke means a completed Boundary mutation cannot race a
        provider start authorized by the old revision.
        """
        scope = request.get("scope")
        if not isinstance(scope, Mapping):
            raise TurnCapabilityBindingDrift("Turn scope is unavailable")
        if scope.get("kind") == "global":
            yield
            return
        project_id = scope.get("project_id")
        turn_id = request.get("turn_id")
        if not isinstance(project_id, str) or not project_id.strip() or not isinstance(turn_id, str) or not turn_id.strip():
            raise TurnCapabilityBindingDrift("Turn project binding is unavailable")
        with self._boundary_profiles.locked_snapshot(project_id) as boundary_snapshot:
            try:
                events = tuple(self._events.events_after(turn_id))
                context_ref = next(
                    (
                        event.get("data", {}).get("payload_ref")
                        for event in events
                        if event.get("type") == "context.resolved"
                        and isinstance(event.get("data"), Mapping)
                        and isinstance(event["data"].get("payload_ref"), str)
                    ),
                    None,
                )
                if not isinstance(context_ref, str):
                    raise TurnCapabilityBindingDrift("Turn Context manifest is unavailable")
                context = context_manifest_from_payload(self._payloads.get(context_ref))
                manifest = manifest_from_payload(self._payloads.get(context.capability_manifest_ref))
                validate_context_manifest_for_request(
                    context, request, capability_manifest=manifest
                )
                capability = self._capability_profiles.get(project_id).profile
                boundary = boundary_snapshot.profile
            except TurnCapabilityBindingDrift:
                raise
            except Exception as error:
                raise TurnCapabilityBindingDrift("Turn capability binding is unreadable") from error
            if (
                context.project_id != project_id
                or manifest.profile_id != capability.profile_id
                or manifest.profile_revision != capability.revision
                or manifest.boundary_profile_id != boundary.profile_id
                or manifest.boundary_profile_revision != boundary.revision
                or context.project_profile_id != capability.profile_id
                or context.project_profile_revision != capability.revision
                or context.boundary_profile_id != boundary.profile_id
                or context.boundary_profile_revision != boundary.revision
                or capability.boundary_profile_id != boundary.profile_id
                or capability.boundary_profile_revision != boundary.revision
            ):
                raise TurnCapabilityBindingDrift("Turn capability binding drifted")
            yield


class AIToolExecutionBoundary:
    """Production pre-invoke gate with local deterministic argument scanning."""

    def __init__(
        self,
        profiles: ProjectBoundaryProfileStore,
        *,
        scanner: SensitiveTextScanner | None = None,
        vault: EphemeralTokenVault | None = None,
        binding_guard: TurnCapabilityBindingGuard | None = None,
        division_capabilities=None,
        external_execution_authority: ExternalExecutionAuthority | None = None,
    ) -> None:
        self._factory = TurnBoundaryRequestFactory(
            profiles, division_capabilities=division_capabilities,
            external_execution_authority=external_execution_authority,
        )
        self._scanner = scanner or SensitiveTextScanner()
        self._vault = vault or EphemeralTokenVault()
        self._binding_guard = binding_guard

    def evaluate(
        self,
        request: Mapping[str, object],
        capability: CapabilityDefinition,
        decision: Mapping[str, object],
    ) -> ToolExecutionBoundaryDecision:
        arguments = decision.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise AIToolExecutionBoundaryError("tool arguments must be an object")
        if self._binding_guard is not None:
            try:
                self._binding_guard.ensure_current(request)
            except TurnCapabilityBindingDrift:
                return ToolExecutionBoundaryDecision(
                    outcome="deny",
                    reason_codes=("ai.boundary_binding_drift",),
                    matched_grant_ids=(),
                    policy_revision=1,
                    requires_receipt=False,
                    redaction_required=False,
                    arguments={},
                )
        tool = tool_from_capability(capability)
        destination_id = tool_destination_identity(tool, capability.capability_id)
        transformed, sanitization = self._sanitize(
            dict(arguments),
            turn_id=str(request.get("turn_id") or ""),
            destination_id=destination_id,
            remote=tool.destination != "local",
        )
        evaluation = self._factory.evaluate(
            request,
            capability,
            destination_id=destination_id,
            sanitization=sanitization,
        )
        boundary = evaluation.decision
        safe_arguments = transformed if transformed is not None else {}
        return ToolExecutionBoundaryDecision(
            outcome=boundary.outcome,
            reason_codes=boundary.reason_codes,
            matched_grant_ids=boundary.matched_grant_ids,
            policy_revision=boundary.policy_revision,
            requires_receipt=boundary.requires_receipt,
            redaction_required=boundary.redaction_required,
            arguments=safe_arguments,
        )

    def sanitize_candidate_arguments(
        self,
        capability: CapabilityDefinition,
        arguments: Mapping[str, object],
        *,
        turn_id: str,
    ) -> Mapping[str, object] | None:
        """Apply only the deterministic local argument hard guard.

        Frozen-authority dispatch uses this after the control plane has
        already issued a Turn-scoped grant.  It intentionally performs no
        profile lookup, binding guard, Boundary evaluation, network access,
        or model call.
        """

        if not isinstance(arguments, Mapping):
            raise AIToolExecutionBoundaryError("tool arguments must be an object")
        tool = tool_from_capability(capability)
        destination_id = tool_destination_identity(tool, capability.capability_id)
        transformed, _sanitization = self._sanitize(
            dict(arguments),
            turn_id=turn_id,
            destination_id=destination_id,
            remote=tool.destination != "local",
        )
        return transformed

    def sanitize_host_candidate_arguments(
        self,
        capability: CapabilityDefinition,
        arguments: Mapping[str, object],
        *,
        turn_id: str,
    ) -> Mapping[str, object] | None:
        """Apply the same hard scanner to a proven in-process host operation."""

        if not isinstance(arguments, Mapping):
            raise AIToolExecutionBoundaryError("tool arguments must be an object")
        tool = tool_from_capability(capability)
        if (
            tool.owner_id != "agent-coordinator"
            or tool.destination != "platform"
            or tool.egress_class != "none"
            or tool.network_scope
            or tool.data_egress_scope
        ):
            raise AIToolExecutionBoundaryError(
                "host Agent tool contract is not local-only"
            )
        transformed, _sanitization = self._sanitize(
            dict(arguments),
            turn_id=turn_id,
            destination_id="agent-coordinator-runtime",
            remote=False,
        )
        return transformed

    @contextmanager
    def dispatch_fence(self, request: Mapping[str, object]) -> Iterator[None]:
        if self._binding_guard is None:
            yield
            return
        with self._binding_guard.dispatch_fence(request):
            yield

    def _sanitize(
        self,
        arguments: dict[str, object],
        *,
        turn_id: str,
        destination_id: str,
        remote: bool,
    ) -> tuple[dict[str, object] | None, SanitizationResult]:
        counts: dict[str, int] = {}
        hard_classes: set[str] = set()
        token_count = 0
        blocked = False

        def visit(value: object) -> object:
            nonlocal token_count, blocked
            if isinstance(value, str):
                if remote:
                    result = self._scanner.sanitize_for_remote(
                        value,
                        vault=self._vault,
                        turn_id=turn_id,
                        destination_id=destination_id,
                    )
                    token_count += result.token_count
                    for data_class, count in result.summary.counts:
                        counts[data_class] = counts.get(data_class, 0) + count
                    hard_classes.update(result.summary.hard_blocked_classes)
                    if result.outcome == "blocked" or result.transformed_text is None:
                        blocked = True
                        return "[blocked-sensitive-value]"
                    return result.transformed_text
                findings = self._scanner.scan(value)
                for finding in findings:
                    counts[finding.data_class] = counts.get(finding.data_class, 0) + 1
                    if finding.hard_block:
                        hard_classes.add(finding.data_class)
                        blocked = True
                return value if not blocked else "[blocked-sensitive-value]"
            if isinstance(value, Mapping):
                return {str(key): visit(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [visit(item) for item in value]
            if value is None or isinstance(value, (bool, int, float)):
                return value
            raise AIToolExecutionBoundaryError("tool arguments contain an unsupported value")

        transformed = visit(arguments)
        summary = ScanSummary(
            counts=tuple(sorted(counts.items())),
            hard_blocked_classes=tuple(sorted(hard_classes)),
        )
        if blocked:
            return None, SanitizationResult("blocked", None, summary, 0)
        outcome = "redacted" if token_count else "clean"
        return dict(transformed), SanitizationResult(
            outcome,
            "[structured-arguments]",
            summary,
            token_count,
        )
