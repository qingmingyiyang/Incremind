from __future__ import annotations

import pytest

from backend.security.ai_model_egress_boundary import (
    AIModelEgressBoundary,
    AIModelEgressBoundaryError,
)
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore


def _kwargs() -> dict[str, object]:
    return {
        "turn_id": "turn-model-boundary",
        "project_id": "project-a",
        "route_key": "tier.deep",
        "provider_id": "deep-provider",
        "egress_categories": ("instructions", "source_excerpt"),
        "privacy_scope": "remote_allowed",
    }


def test_guarded_profile_preflight_and_clean_dispatch_are_allowed(tmp_path) -> None:
    boundary = AIModelEgressBoundary(tmp_path)
    decision = boundary.preflight(**_kwargs())
    assert decision.outcome == "allow"
    profile = ProjectBoundaryProfileStore(tmp_path).get("project-a").profile

    with boundary.dispatch_fence(
        **_kwargs(),
        input_text="ordinary question",
        parameters={"messages": [{"role": "user", "content": "ordinary question"}]},
        expected_profile_id=profile.profile_id,
        expected_profile_revision=profile.revision,
    ) as authorized:
        assert authorized.decision.outcome == "allow"
        assert authorized.input_text == "ordinary question"


def test_sealed_profile_denies_model_provider_before_dispatch(tmp_path) -> None:
    profiles = ProjectBoundaryProfileStore(tmp_path)
    profiles.update(
        "project-a", mode="sealed", remote_default="deny", expected_revision=0,
    )
    boundary = AIModelEgressBoundary(tmp_path)
    assert boundary.preflight(**_kwargs()).outcome == "deny"
    profile = profiles.get("project-a").profile

    with pytest.raises(AIModelEgressBoundaryError, match="denied"):
        with boundary.dispatch_fence(
            **_kwargs(), input_text="question", parameters={},
            expected_profile_id=profile.profile_id,
            expected_profile_revision=profile.revision,
        ):
            raise AssertionError("denied dispatch must not yield")


def test_soft_personal_data_is_redacted_before_provider_dispatch(tmp_path) -> None:
    boundary = AIModelEgressBoundary(tmp_path)
    profile = ProjectBoundaryProfileStore(tmp_path).get("project-a").profile
    with boundary.dispatch_fence(
        **_kwargs(), input_text="contact me at user@example.com",
        parameters={"messages": [{"role": "user", "content": "call 13800138000"}]},
        expected_profile_id=profile.profile_id,
        expected_profile_revision=profile.revision,
    ) as authorized:
        assert authorized.decision.outcome == "allow_redacted"
        assert "user@example.com" not in authorized.input_text
        assert "13800138000" not in str(authorized.parameters)


def test_hard_sensitive_data_is_denied_and_never_yielded(tmp_path) -> None:
    boundary = AIModelEgressBoundary(tmp_path)
    profile = ProjectBoundaryProfileStore(tmp_path).get("project-a").profile
    with pytest.raises(AIModelEgressBoundaryError, match="denied"):
        with boundary.dispatch_fence(
            **_kwargs(), input_text="secret sk-1234567890abcdef",
            parameters={}, expected_profile_id=profile.profile_id,
            expected_profile_revision=profile.revision,
        ):
            raise AssertionError("blocked dispatch must not yield")


def test_boundary_revision_drift_is_rejected(tmp_path) -> None:
    profiles = ProjectBoundaryProfileStore(tmp_path)
    baseline = profiles.get("project-a").profile
    profiles.update(
        "project-a", mode="open", remote_default="allow", expected_revision=0,
    )
    profiles.set_mode(
        "project-a", mode="guarded", remote_default="review", expected_revision=1,
    )
    boundary = AIModelEgressBoundary(tmp_path)
    with pytest.raises(AIModelEgressBoundaryError, match="drifted"):
        with boundary.dispatch_fence(
            **_kwargs(), input_text="question", parameters={},
            expected_profile_id=baseline.profile_id,
            expected_profile_revision=baseline.revision,
        ):
            raise AssertionError("drifted dispatch must not yield")


def test_binary_image_payload_is_not_scanned_as_text(tmp_path) -> None:
    boundary = AIModelEgressBoundary(tmp_path)
    profile = ProjectBoundaryProfileStore(tmp_path).get("project-a").profile
    with boundary.dispatch_fence(
        **_kwargs(), input_text="describe image",
        parameters={
            "image_payload": {
                "media_type": "image/png",
                "pixels": b"sk-1234567890abcdef",
            },
        },
        expected_profile_id=profile.profile_id,
        expected_profile_revision=profile.revision,
    ) as authorized:
        assert authorized.parameters["image_payload"]["pixels"] == b"sk-1234567890abcdef"


def test_loopback_local_only_keeps_payload_local_and_locks_profile_revision(tmp_path) -> None:
    profiles = ProjectBoundaryProfileStore(tmp_path)
    profiles.update(
        "project-a", mode="sealed", remote_default="deny", expected_revision=0,
    )
    boundary = AIModelEgressBoundary(tmp_path)
    profile = profiles.get("project-a").profile
    kwargs = {
        **_kwargs(), "privacy_scope": "local_only", "execution_location": "local_loopback",
    }
    assert boundary.preflight(**kwargs).outcome == "allow"
    with boundary.dispatch_fence(
        **kwargs, input_text="secret sk-1234567890abcdef",
        parameters={"messages": [{"role": "user", "content": "private@example.com"}]},
        expected_profile_id=profile.profile_id,
        expected_profile_revision=profile.revision,
    ) as authorized:
        assert authorized.decision.outcome == "allow"
        assert authorized.input_text == "secret sk-1234567890abcdef"
        assert authorized.parameters["messages"][0]["content"] == "private@example.com"
