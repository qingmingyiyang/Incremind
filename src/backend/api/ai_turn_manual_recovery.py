from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from backend.security.ai_recovery_boundary import AIRecoveryBoundary
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from core.ai_kernel import RecoveryReviewAuthorization, SQLiteAITurnStore


class AIRecoveryReviewError(ValueError):
    pass


class AIRecoveryReviewConflict(AIRecoveryReviewError):
    pass


class AIRecoveryReviewDenied(AIRecoveryReviewError):
    pass


class AIRecoveryReviewService:
    """Authenticated local application service for metadata-only recovery review."""

    def __init__(self, runtime_root: Path) -> None:
        root = Path(runtime_root)
        self.store = SQLiteAITurnStore(root / ".rebuild-data" / "ai-turns.sqlite3")
        self._boundary = AIRecoveryBoundary(ProjectBoundaryProfileStore(root))

    def list(self, *, project_id: str | None, limit: int) -> tuple[dict[str, object], ...]:
        return tuple(_public_review(item) for item in self.store.list_recovery_reviews(project_id=project_id, limit=limit))

    def get(self, review_id: str) -> dict[str, object] | None:
        review = self.store.get_recovery_review(review_id)
        return _public_review(review) if review is not None else None

    def decide(
        self,
        review_id: str,
        *,
        expected_revision: int,
        action: str,
    ) -> dict[str, object]:
        review = self.store.get_recovery_review(review_id)
        turn_request = self.store.get_recovery_review_request(review_id)
        if review is None or turn_request is None:
            raise AIRecoveryReviewConflict("recovery review is unavailable")
        if review.revision != expected_revision:
            raise AIRecoveryReviewConflict("recovery review revision conflict")
        with self._boundary.locked_evaluation(
            turn_request,
            review_id=review_id,
            review_revision=expected_revision,
            action=action,
        ) as decision:
            if decision.outcome == "deny":
                raise AIRecoveryReviewDenied("Boundary denied recovery review action")
            authorization = RecoveryReviewAuthorization(
                actor_id="desktop-user",
                boundary_outcome=decision.outcome,
                boundary_reason_codes=decision.reason_codes,
                policy_revision=decision.policy_revision,
                human_confirmed=True,
            )
            now = datetime.now(timezone.utc)
            if action == "confirm_no_effect_and_resume":
                resolved = self.store.confirm_no_effect_and_queue_recovery_review(
                    review_id,
                    expected_revision=expected_revision,
                    authorization=authorization,
                    observed_at=now,
                )
            elif action == "keep_quarantined":
                resolved = self.store.keep_recovery_review(
                    review_id,
                    expected_revision=expected_revision,
                    authorization=authorization,
                    observed_at=now,
                )
            else:
                raise AIRecoveryReviewError("recovery review action is unsupported")
        if resolved is None:
            raise AIRecoveryReviewConflict("recovery review changed concurrently")
        return _public_review(resolved)


def _public_review(review: object) -> dict[str, object]:
    return {
        "review_id": getattr(review, "review_id"),
        "project_id": getattr(review, "project_id"),
        "status": getattr(review, "status"),
        "revision": getattr(review, "revision"),
        "reason_code": getattr(review, "reason_code"),
        "created_at": getattr(review, "created_at").isoformat(),
        "updated_at": getattr(review, "updated_at").isoformat(),
    }
