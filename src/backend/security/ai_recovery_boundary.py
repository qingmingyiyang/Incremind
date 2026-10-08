from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from core.ai_boundary import BoundaryDecision, BoundaryPolicyEngine, BoundaryRequest


class AIRecoveryBoundaryError(ValueError):
    pass


class AIRecoveryBoundary:
    """Server-owned Boundary request for an authenticated manual recovery action."""

    def __init__(
        self,
        profiles: ProjectBoundaryProfileStore,
        *,
        engine: BoundaryPolicyEngine | None = None,
    ) -> None:
        self._profiles = profiles
        self._engine = engine or BoundaryPolicyEngine()

    def evaluate(
        self,
        turn_request: Mapping[str, object],
        *,
        review_id: str,
        review_revision: int,
        action: str,
    ) -> BoundaryDecision:
        project_id, request = _boundary_request(
            turn_request,
            review_id=review_id,
            review_revision=review_revision,
            action=action,
        )
        return self._engine.evaluate(request, self._profiles.get(project_id).profile)

    @contextmanager
    def locked_evaluation(
        self,
        turn_request: Mapping[str, object],
        *,
        review_id: str,
        review_revision: int,
        action: str,
    ) -> Iterator[BoundaryDecision]:
        project_id, request = _boundary_request(
            turn_request,
            review_id=review_id,
            review_revision=review_revision,
            action=action,
        )
        with self._profiles.locked_snapshot(project_id) as snapshot:
            yield self._engine.evaluate(request, snapshot.profile)


def _boundary_request(
    turn_request: Mapping[str, object],
    *,
    review_id: str,
    review_revision: int,
    action: str,
) -> tuple[str, BoundaryRequest]:
    project_id = _project_id(turn_request)
    operation_id = turn_request.get("operation_id")
    turn_id = turn_request.get("turn_id")
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise AIRecoveryBoundaryError("recovery Turn operation identity is invalid")
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise AIRecoveryBoundaryError("recovery Turn identity is invalid")
    if action not in {"confirm_no_effect_and_resume", "keep_quarantined"}:
        raise AIRecoveryBoundaryError("recovery review action is unsupported")
    request = BoundaryRequest(
        request_id=f"boundary-recovery-{review_id}-r{review_revision}",
        turn_id=turn_id,
        project_id=project_id,
        actor_id="desktop-user",
        target_id="ai.turn.recovery-review",
        operation_id=operation_id,
        idempotency_key=f"recovery-review-{review_id}-r{review_revision}-{action}",
        effect="write",
        destination_kind="local",
        destination_id="desktop-loopback",
        data_classes=("recovery_metadata",),
        scan_state="not_required",
        reversible=action == "keep_quarantined",
        same_project=True,
        requires_receipt=True,
    )
    return project_id, request


def _project_id(turn_request: Mapping[str, object]) -> str:
    scope = turn_request.get("scope")
    if not isinstance(scope, Mapping):
        raise AIRecoveryBoundaryError("recovery Turn scope is invalid")
    project_id = scope.get("project_id")
    return project_id if isinstance(project_id, str) and project_id.strip() else "global"
