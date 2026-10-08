from __future__ import annotations

from datetime import datetime, timezone

from backend.security.provider_egress import ProviderEgressPolicyStore
from backend.security.provider_egress_boundary_adapter import LegacyProviderConsentBoundaryAdapter
from core.ai_boundary import BoundaryRequest, ProjectBoundaryProfile


NOW = datetime(2026, 8, 23, tzinfo=timezone.utc)


def test_external_provider_requires_boundary_allow_and_legacy_consent(tmp_path) -> None:
    policy = ProviderEgressPolicyStore(tmp_path)
    manifest = _manifest(policy)
    adapter = LegacyProviderConsentBoundaryAdapter(policy)

    denied = adapter.evaluate(_request(manifest.manifest_id), _profile("guarded"), manifest, now=NOW)
    assert denied.outcome == "deny"
    assert denied.reason_codes == ("legacy_provider_consent_required",)

    policy.grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    allowed = adapter.evaluate(_request(manifest.manifest_id), _profile("guarded"), manifest, now=NOW)
    assert allowed.outcome == "allow"
    assert allowed.reason_codes == ("guarded_scan_clean",)


def test_manifest_identity_drift_fails_closed_even_with_consent(tmp_path) -> None:
    policy = ProviderEgressPolicyStore(tmp_path)
    manifest = _manifest(policy)
    policy.grant(manifest, manifest_id=manifest.manifest_id, confirm=True)

    decision = LegacyProviderConsentBoundaryAdapter(policy).evaluate(
        _request("egress-stale"),
        _profile("open"),
        manifest,
        now=NOW,
    )
    assert decision.outcome == "deny"
    assert decision.reason_codes == ("provider_manifest_identity_drift",)


def test_sealed_profile_denial_cannot_be_overridden_by_consent(tmp_path) -> None:
    policy = ProviderEgressPolicyStore(tmp_path)
    manifest = _manifest(policy)
    policy.grant(manifest, manifest_id=manifest.manifest_id, confirm=True)

    decision = LegacyProviderConsentBoundaryAdapter(policy).evaluate(
        _request(manifest.manifest_id),
        _profile("sealed"),
        manifest,
        now=NOW,
    )
    assert decision.outcome == "deny"
    assert decision.reason_codes == ("sealed_remote_denied",)


def test_loopback_provider_does_not_require_external_consent(tmp_path) -> None:
    policy = ProviderEgressPolicyStore(tmp_path)
    manifest = _manifest(policy, endpoint="http://127.0.0.1:11434/v1")
    decision = LegacyProviderConsentBoundaryAdapter(policy).evaluate(
        _request(manifest.manifest_id),
        _profile("guarded"),
        manifest,
        now=NOW,
    )
    assert decision.outcome == "allow"


def _manifest(policy: ProviderEgressPolicyStore, *, endpoint: str = "https://api.example.com/v1"):
    return policy.manifest(
        provider_id="example",
        endpoint=endpoint,
        purposes=("search_answer",),
        payload_categories=("instructions", "source_excerpt"),
        max_payload_bytes=1024,
    )


def _request(manifest_id: str) -> BoundaryRequest:
    return BoundaryRequest(
        request_id="request-1",
        turn_id="turn-1",
        project_id="project-a",
        actor_id="agent-main",
        target_id="search.answer",
        operation_id="operation-1",
        idempotency_key="idem-1",
        effect="external",
        destination_kind="provider",
        destination_id=manifest_id,
        data_classes=("project_content",),
        scan_state="clean",
        reversible=False,
        same_project=True,
        requires_receipt=True,
    )


def _profile(mode: str) -> ProjectBoundaryProfile:
    return ProjectBoundaryProfile(
        profile_id=f"profile-{mode}",
        project_id="project-a",
        mode=mode,
        revision=1,
        remote_default="allow",
    )
