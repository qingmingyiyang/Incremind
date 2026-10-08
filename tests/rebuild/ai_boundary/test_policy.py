from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.ai_boundary import BoundaryGrant, BoundaryPolicyEngine, BoundaryRequest, ProjectBoundaryProfile


NOW = datetime(2026, 8, 23, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("mode", "effect", "destination", "scan", "reversible", "expected"),
    [
        ("open", "read", "local", "not_required", False, "allow"),
        ("open", "write", "local", "not_required", False, "allow"),
        ("open", "external", "provider", "clean", False, "allow"),
        ("open", "platform", "platform", "clean", False, "ask"),
        ("guarded", "read", "local", "not_required", False, "allow"),
        ("guarded", "write", "local", "not_required", True, "allow"),
        ("guarded", "write", "local", "not_required", False, "ask"),
        ("guarded", "external", "provider", "clean", False, "allow"),
        ("guarded", "external", "provider", "redacted", False, "allow_redacted"),
        ("guarded", "external", "provider", "unknown", False, "ask"),
        ("sealed", "read", "local", "not_required", False, "allow"),
        ("sealed", "write", "local", "not_required", True, "allow"),
        ("sealed", "write", "local", "not_required", False, "ask"),
        ("sealed", "external", "provider", "clean", False, "deny"),
    ],
)
def test_mode_effect_destination_matrix(
    mode: str,
    effect: str,
    destination: str,
    scan: str,
    reversible: bool,
    expected: str,
) -> None:
    request = _request(
        effect=effect,
        destination_kind=destination,
        destination_id=f"{destination}-a",
        scan_state=scan,
        reversible=reversible,
        requires_receipt=effect != "read",
    )
    decision = BoundaryPolicyEngine().evaluate(request, _profile(mode=mode), now=NOW)
    assert decision.outcome == expected


@pytest.mark.parametrize("data_class", ["credential", "authentication", "payment", "government_id", "health", "biometric"])
def test_hard_sensitive_remote_deny_cannot_be_overridden_by_grant(data_class: str) -> None:
    grant = _grant(
        actions=("external",),
        destinations=("provider",),
        data_classes=(data_class,),
    )
    profile = _profile(mode="open", remote_default="allow", persistent_grants=(grant,))
    request = _request(
        effect="external",
        destination_kind="provider",
        destination_id="provider-a",
        data_classes=(data_class,),
        scan_state="clean",
        requires_receipt=True,
    )
    decision = BoundaryPolicyEngine().evaluate(request, profile, now=NOW)
    assert decision.outcome == "deny"
    assert decision.reason_codes == ("hard_sensitive_remote_denied",)
    assert decision.matched_grant_ids == ()


def test_valid_persistent_grant_allows_guarded_irreversible_write() -> None:
    grant = _grant(actions=("write",), destinations=("local",))
    request = _request(effect="write", requires_receipt=True)
    decision = BoundaryPolicyEngine().evaluate(
        request,
        _profile(persistent_grants=(grant,)),
        now=NOW,
    )
    assert decision.outcome == "allow"
    assert decision.matched_grant_ids == ("grant-1",)


@pytest.mark.parametrize(
    "grant_changes",
    [
        {"revoked": True},
        {"expires_at": NOW - timedelta(seconds=1)},
        {"subject_id": "other-agent"},
        {"target_id": "other-tool"},
        {"actions": ("read",)},
        {"destinations": ("provider",)},
        {"data_classes": ("public",)},
    ],
)
def test_invalid_grant_does_not_expand_guarded_write(grant_changes: dict[str, object]) -> None:
    grant = _grant(**grant_changes)
    request = _request(effect="write", requires_receipt=True)
    decision = BoundaryPolicyEngine().evaluate(
        request,
        _profile(persistent_grants=(grant,)),
        now=NOW,
    )
    assert decision.outcome == "ask"
    assert decision.matched_grant_ids == ()


def test_cross_project_request_is_never_implicitly_allowed() -> None:
    request = _request(same_project=False)
    assert BoundaryPolicyEngine().evaluate(request, _profile(mode="open"), now=NOW).outcome == "ask"
    assert BoundaryPolicyEngine().evaluate(request, _profile(mode="sealed"), now=NOW).outcome == "deny"


def test_delete_always_requires_exact_approval_even_with_grant() -> None:
    grant = _grant(actions=("delete",), destinations=("local",))
    request = _request(effect="delete", requires_receipt=True)
    decision = BoundaryPolicyEngine().evaluate(
        request,
        _profile(mode="open", persistent_grants=(grant,)),
        now=NOW,
    )
    assert decision.outcome == "ask"
    assert decision.reason_codes == ("irreversible_action_requires_approval",)


def _request(**changes: object) -> BoundaryRequest:
    values: dict[str, object] = {
        "request_id": "request-1",
        "turn_id": "turn-1",
        "project_id": "project-a",
        "actor_id": "agent-main",
        "target_id": "tool-a",
        "operation_id": "operation-1",
        "idempotency_key": "idem-1",
        "effect": "read",
        "destination_kind": "local",
        "destination_id": "local-runtime",
        "data_classes": ("project_content",),
        "scan_state": "not_required",
        "reversible": False,
        "same_project": True,
        "requires_receipt": False,
    }
    values.update(changes)
    return BoundaryRequest(**values)  # type: ignore[arg-type]


def _grant(**changes: object) -> BoundaryGrant:
    values: dict[str, object] = {
        "grant_id": "grant-1",
        "subject_id": "agent-main",
        "project_id": "project-a",
        "target_id": "tool-a",
        "actions": ("write",),
        "data_classes": ("project_content",),
        "destinations": ("local",),
        "expires_at": NOW + timedelta(days=1),
        "revision": 1,
    }
    values.update(changes)
    return BoundaryGrant(**values)  # type: ignore[arg-type]


def _profile(**changes: object) -> ProjectBoundaryProfile:
    values: dict[str, object] = {
        "profile_id": "profile-a",
        "project_id": "project-a",
        "mode": "guarded",
        "revision": 7,
        "remote_default": "allow",
    }
    values.update(changes)
    return ProjectBoundaryProfile(**values)  # type: ignore[arg-type]
