from __future__ import annotations

from datetime import datetime

from backend.security.provider_egress import ProviderEgressManifest, ProviderEgressPolicyStore
from core.ai_boundary import (
    BoundaryDecision,
    BoundaryPolicyEngine,
    BoundaryRequest,
    ProjectBoundaryProfile,
)


class LegacyProviderConsentBoundaryAdapter:
    """Requires both V2 Boundary allowance and existing manifest-bound consent."""

    def __init__(
        self,
        policy: ProviderEgressPolicyStore,
        *,
        engine: BoundaryPolicyEngine | None = None,
    ) -> None:
        self._policy = policy
        self._engine = engine or BoundaryPolicyEngine()

    def evaluate(
        self,
        request: BoundaryRequest,
        profile: ProjectBoundaryProfile,
        manifest: ProviderEgressManifest,
        *,
        now: datetime | None = None,
    ) -> BoundaryDecision:
        decision = self._engine.evaluate(request, profile, now=now)
        if decision.outcome not in {"allow", "allow_redacted"}:
            return decision
        if request.destination_kind != "provider":
            return _deny(decision, "provider_destination_required")
        if request.destination_id != manifest.manifest_id:
            return _deny(decision, "provider_manifest_identity_drift")
        if manifest.external and not self._policy.is_consented(manifest):
            return _deny(decision, "legacy_provider_consent_required")
        return decision


def _deny(decision: BoundaryDecision, reason: str) -> BoundaryDecision:
    return BoundaryDecision(
        request_id=decision.request_id,
        outcome="deny",
        reason_codes=(reason,),
        matched_grant_ids=decision.matched_grant_ids,
        policy_revision=decision.policy_revision,
        requires_receipt=decision.requires_receipt,
        redaction_required=False,
    )
