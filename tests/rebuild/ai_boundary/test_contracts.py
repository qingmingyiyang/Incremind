from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.ai_boundary import (
    BoundaryContractError,
    BoundaryDecision,
    BoundaryGrant,
    BoundaryRequest,
    ProjectBoundaryProfile,
)


def test_side_effecting_request_requires_receipt() -> None:
    with pytest.raises(BoundaryContractError, match="requires a receipt"):
        _request(effect="write", requires_receipt=False)


def test_remote_request_requires_scan_result() -> None:
    with pytest.raises(BoundaryContractError, match="requires a scan result"):
        _request(destination_kind="provider", scan_state="not_required", requires_receipt=True)


def test_profile_rejects_grant_from_another_project() -> None:
    with pytest.raises(BoundaryContractError, match="project identity drifted"):
        _profile(persistent_grants=(_grant(project_id="project-b"),))


def test_grant_expiry_must_be_timezone_aware() -> None:
    with pytest.raises(BoundaryContractError, match="timezone-aware"):
        _grant(expires_at=datetime(2026, 8, 23))


def test_redacted_decision_requires_transform_flag() -> None:
    with pytest.raises(BoundaryContractError, match="requires a transform"):
        BoundaryDecision(
            request_id="request-1",
            outcome="allow_redacted",
            reason_codes=("scanner_redacted",),
            matched_grant_ids=(),
            policy_revision=1,
            requires_receipt=True,
        )


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
        "expires_at": datetime(2027, 1, 1, tzinfo=timezone.utc),
        "revision": 1,
    }
    values.update(changes)
    return BoundaryGrant(**values)  # type: ignore[arg-type]


def _profile(**changes: object) -> ProjectBoundaryProfile:
    values: dict[str, object] = {
        "profile_id": "profile-a",
        "project_id": "project-a",
        "mode": "guarded",
        "revision": 1,
        "remote_default": "review",
    }
    values.update(changes)
    return ProjectBoundaryProfile(**values)  # type: ignore[arg-type]
