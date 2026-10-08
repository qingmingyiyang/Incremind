from __future__ import annotations

from datetime import datetime

from .contracts import (
    BoundaryDecision,
    BoundaryGrant,
    BoundaryRequest,
    ProjectBoundaryProfile,
    utc_now,
)


_HARD_REMOTE_DENY = frozenset({
    "credential",
    "authentication",
    "payment",
    "government_id",
    "health",
    "biometric",
})
_REMOTE_DESTINATIONS = frozenset({"provider", "mcp", "platform"})


class BoundaryPolicyEngine:
    """Deterministic policy authority; reviewers may narrow but never widen it."""

    def evaluate(
        self,
        request: BoundaryRequest,
        profile: ProjectBoundaryProfile,
        *,
        now: datetime | None = None,
    ) -> BoundaryDecision:
        current = now or utc_now()
        if request.project_id != profile.project_id:
            return self._decision(request, profile, "deny", "project_identity_drift")
        if request.effect in profile.denied_effects:
            return self._decision(request, profile, "deny", "profile_explicit_deny")
        if not request.same_project:
            outcome = "deny" if profile.mode == "sealed" else "ask"
            return self._decision(request, profile, outcome, "cross_project_requires_grant")
        if request.destination_kind in _REMOTE_DESTINATIONS:
            if set(request.data_classes) & _HARD_REMOTE_DENY:
                return self._decision(request, profile, "deny", "hard_sensitive_remote_denied")
            if request.scan_state == "sensitive":
                return self._decision(request, profile, "deny", "scanner_sensitive_denied")
        if request.effect == "delete":
            return self._decision(request, profile, "ask", "irreversible_action_requires_approval")
        if profile.mode == "sealed":
            return self._sealed(request, profile)

        grant = self._matching_grant(request, profile, current)
        if grant is not None:
            outcome = "allow_redacted" if grant.redaction_required else "allow"
            return self._decision(
                request,
                profile,
                outcome,
                "persistent_grant_matched",
                grant=grant,
                redaction_required=grant.redaction_required,
            )
        if profile.mode == "open":
            return self._open(request, profile)
        return self._guarded(request, profile)

    def _sealed(
        self,
        request: BoundaryRequest,
        profile: ProjectBoundaryProfile,
    ) -> BoundaryDecision:
        if request.destination_kind != "local" or request.effect in {"external", "platform"}:
            return self._decision(request, profile, "deny", "sealed_remote_denied")
        if request.effect == "read":
            return self._decision(request, profile, "allow", "sealed_project_read_allowed")
        if request.effect == "write" and request.reversible:
            return self._decision(request, profile, "allow", "sealed_reversible_draft_allowed")
        return self._decision(request, profile, "ask", "sealed_mutation_requires_approval")

    def _open(
        self,
        request: BoundaryRequest,
        profile: ProjectBoundaryProfile,
    ) -> BoundaryDecision:
        if request.effect == "platform" or request.destination_kind == "platform":
            return self._decision(request, profile, "ask", "platform_action_requires_approval")
        if request.destination_kind in _REMOTE_DESTINATIONS:
            if profile.remote_default == "deny":
                return self._decision(request, profile, "deny", "profile_remote_denied")
            if profile.remote_default == "review" or request.scan_state == "unknown":
                return self._decision(request, profile, "ask", "remote_review_required")
            if request.scan_state == "redacted":
                return self._decision(
                    request,
                    profile,
                    "allow_redacted",
                    "open_remote_redacted",
                    redaction_required=True,
                )
            return self._decision(request, profile, "allow", "open_profile_allowed")
        return self._decision(request, profile, "allow", "open_project_action_allowed")

    def _guarded(
        self,
        request: BoundaryRequest,
        profile: ProjectBoundaryProfile,
    ) -> BoundaryDecision:
        if request.destination_kind in _REMOTE_DESTINATIONS:
            if profile.remote_default == "deny":
                return self._decision(request, profile, "deny", "profile_remote_denied")
            if request.scan_state == "clean":
                return self._decision(request, profile, "allow", "guarded_scan_clean")
            if request.scan_state == "redacted":
                return self._decision(
                    request,
                    profile,
                    "allow_redacted",
                    "guarded_scan_redacted",
                    redaction_required=True,
                )
            return self._decision(request, profile, "ask", "guarded_remote_review_required")
        if request.effect == "read":
            return self._decision(request, profile, "allow", "guarded_project_read_allowed")
        if request.effect == "write" and request.reversible:
            return self._decision(request, profile, "allow", "guarded_reversible_draft_allowed")
        return self._decision(request, profile, "ask", "guarded_mutation_requires_approval")

    @staticmethod
    def _matching_grant(
        request: BoundaryRequest,
        profile: ProjectBoundaryProfile,
        now: datetime,
    ) -> BoundaryGrant | None:
        for grant in profile.persistent_grants:
            if not grant.is_active_at(now):
                continue
            if grant.subject_id != request.actor_id or grant.target_id != request.target_id:
                continue
            if request.effect not in grant.actions or request.destination_kind not in grant.destinations:
                continue
            if grant.data_classes and not set(request.data_classes) <= set(grant.data_classes):
                continue
            return grant
        return None

    @staticmethod
    def _decision(
        request: BoundaryRequest,
        profile: ProjectBoundaryProfile,
        outcome: str,
        reason: str,
        *,
        grant: BoundaryGrant | None = None,
        redaction_required: bool = False,
    ) -> BoundaryDecision:
        return BoundaryDecision(
            request_id=request.request_id,
            outcome=outcome,  # type: ignore[arg-type]
            reason_codes=(reason,),
            matched_grant_ids=(grant.grant_id,) if grant else (),
            policy_revision=profile.revision,
            requires_receipt=request.requires_receipt,
            redaction_required=redaction_required,
        )
