from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from core.ai_boundary import (
    BoundaryDecision,
    BoundaryPolicyEngine,
    BoundaryRequest,
    EphemeralTokenVault,
    SanitizationResult,
    ScanSummary,
    SensitiveTextScanner,
)


class AIModelEgressBoundaryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AuthorizedModelPayload:
    input_text: str
    parameters: Mapping[str, object]
    decision: BoundaryDecision


class AIModelEgressBoundary:
    """Project Boundary gate for one exact model Provider destination.

    Routing preflight evaluates static policy only.  The dispatch fence scans
    and, where policy permits it, redacts the actual textual payload while the
    Boundary profile revision remains locked.  Binary image bytes are never
    copied into a policy record and remain governed by their declared egress
    category plus the upstream capture approval.
    """

    def __init__(
        self,
        root_dir: Path,
        *,
        scanner: SensitiveTextScanner | None = None,
        vault: EphemeralTokenVault | None = None,
        engine: BoundaryPolicyEngine | None = None,
    ) -> None:
        self._profiles = ProjectBoundaryProfileStore(root_dir)
        self._scanner = scanner or SensitiveTextScanner()
        self._vault = vault or EphemeralTokenVault()
        self._engine = engine or BoundaryPolicyEngine()

    def preflight(
        self,
        *,
        turn_id: str,
        project_id: str,
        route_key: str,
        provider_id: str,
        egress_categories: Sequence[str],
        privacy_scope: str,
        execution_location: str = "remote",
    ) -> BoundaryDecision:
        profile = self._profiles.get(project_id).profile
        request = _boundary_request(
            turn_id=turn_id,
            project_id=project_id,
            route_key=route_key,
            provider_id=provider_id,
            egress_categories=egress_categories,
            privacy_scope=privacy_scope,
            execution_location=execution_location,
            # This is a static eligibility check. Dispatch performs the real
            # local scan before any Provider call.
            sanitization=_clean_sanitization(),
        )
        return self._engine.evaluate(request, profile)

    @contextmanager
    def dispatch_fence(
        self,
        *,
        turn_id: str,
        project_id: str,
        route_key: str,
        provider_id: str,
        egress_categories: Sequence[str],
        privacy_scope: str,
        execution_location: str = "remote",
        input_text: str,
        parameters: Mapping[str, object],
        expected_profile_id: str,
        expected_profile_revision: int,
    ) -> Iterator[AuthorizedModelPayload]:
        destination_id = _destination_id(provider_id, route_key)
        if execution_location == "local_loopback":
            # Local loopback is a read inside the project Boundary, not
            # remote egress.  Do not route its payload through the remote
            # scanner/token vault or redact it into an unrecoverable proxy.
            # The profile revision is still locked below and the request
            # remains receipt-bearing through the caller's Turn lifecycle.
            transformed_input = input_text
            transformed_parameters = dict(parameters)
            sanitization = _local_sanitization()
        else:
            transformed_input, transformed_parameters, sanitization = self._sanitize(
                input_text,
                parameters,
                turn_id=turn_id,
                destination_id=destination_id,
            )
        with self._profiles.locked_snapshot(project_id) as snapshot:
            profile = snapshot.profile
            if (
                profile.profile_id != expected_profile_id
                or profile.revision != expected_profile_revision
            ):
                raise AIModelEgressBoundaryError("model Boundary profile drifted")
            request = _boundary_request(
                turn_id=turn_id,
                project_id=project_id,
                route_key=route_key,
                provider_id=provider_id,
                egress_categories=egress_categories,
                privacy_scope=privacy_scope,
                execution_location=execution_location,
                sanitization=sanitization,
            )
            decision = self._engine.evaluate(request, profile)
            if decision.outcome not in {"allow", "allow_redacted"}:
                raise AIModelEgressBoundaryError(
                    "model Provider egress requires approval"
                    if decision.outcome == "ask"
                    else "model Provider egress is denied"
                )
            if decision.redaction_required and sanitization.outcome != "redacted":
                raise AIModelEgressBoundaryError("model Provider egress redaction is unavailable")
            if transformed_input is None or transformed_parameters is None:
                raise AIModelEgressBoundaryError("model Provider egress contains blocked data")
            yield AuthorizedModelPayload(
                input_text=transformed_input,
                parameters=transformed_parameters,
                decision=decision,
            )

    def _sanitize(
        self,
        input_text: str,
        parameters: Mapping[str, object],
        *,
        turn_id: str,
        destination_id: str,
    ) -> tuple[str | None, dict[str, object] | None, SanitizationResult]:
        counts: dict[str, int] = {}
        hard_classes: set[str] = set()
        token_count = 0
        blocked = False

        def visit(value: object, *, binary_context: bool = False) -> object:
            nonlocal blocked, token_count
            if isinstance(value, str) and binary_context:
                return value
            if isinstance(value, str) and not binary_context:
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
            if isinstance(value, Mapping):
                result: dict[str, object] = {}
                for key, item in value.items():
                    name = str(key)
                    result[name] = visit(
                        item,
                        binary_context=binary_context or name in {"pixels", "data", "image_payload"},
                    )
                return result
            if isinstance(value, (list, tuple)):
                return [visit(item, binary_context=binary_context) for item in value]
            if value is None or isinstance(value, (bool, int, float, bytes, bytearray)):
                return value
            raise AIModelEgressBoundaryError("model payload contains an unsupported value")

        safe_input = visit(input_text)
        safe_parameters = visit(parameters)
        summary = ScanSummary(
            counts=tuple(sorted(counts.items())),
            hard_blocked_classes=tuple(sorted(hard_classes)),
        )
        if blocked:
            return None, None, SanitizationResult("blocked", None, summary, 0)
        outcome = "redacted" if token_count else "clean"
        return (
            str(safe_input),
            dict(safe_parameters),
            SanitizationResult(outcome, "[model-payload]", summary, token_count),
        )


def _boundary_request(
    *,
    turn_id: str,
    project_id: str,
    route_key: str,
    provider_id: str,
    egress_categories: Sequence[str],
    privacy_scope: str,
    execution_location: str,
    sanitization: SanitizationResult,
) -> BoundaryRequest:
    if execution_location not in {"local_loopback", "remote"}:
        raise AIModelEgressBoundaryError("model execution location is invalid")
    local = execution_location == "local_loopback"
    if local:
        scan_state = "local"
    elif privacy_scope != "remote_allowed":
        scan_state = "unknown"
    else:
        scan_state = {
            "blocked": "sensitive",
            "redacted": "redacted",
            "clean": "clean",
        }.get(sanitization.outcome, "unknown")
    data_classes = set(str(item) for item in egress_categories)
    data_classes.update(data_class for data_class, _count in sanitization.summary.counts)
    return BoundaryRequest(
        request_id=f"boundary-{turn_id}-model-{route_key}",
        turn_id=turn_id,
        project_id=project_id,
        actor_id="ai-kernel",
        target_id=_destination_id(provider_id, route_key),
        operation_id=f"model-route:{route_key}",
        idempotency_key=f"{turn_id}:{route_key}",
        effect="read" if local else "external",
        destination_kind="local" if local else "provider",
        destination_id=provider_id,
        data_classes=tuple(sorted(data_classes)),
        scan_state=scan_state,  # type: ignore[arg-type]
        reversible=False,
        same_project=True,
        requires_receipt=True,
    )


def _destination_id(provider_id: str, route_key: str) -> str:
    return f"model-provider:{provider_id}:route:{route_key}"


def _clean_sanitization() -> SanitizationResult:
    return SanitizationResult("clean", "[preflight]", ScanSummary((), ()), 0)


def _local_sanitization() -> SanitizationResult:
    return SanitizationResult("clean", "[local-model-payload]", ScanSummary((), ()), 0)
