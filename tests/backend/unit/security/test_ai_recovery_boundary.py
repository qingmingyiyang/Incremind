from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from time import sleep

from backend.security.ai_recovery_boundary import AIRecoveryBoundary
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore


def _request() -> dict[str, object]:
    return {
        "turn_id": "turn-0123456789abcdef0123456789abcdef",
        "operation_id": "operation-recovery-1",
        "scope": {"kind": "project", "project_id": "project-alpha"},
    }


def test_guarded_manual_resume_is_an_explicit_boundary_ask(tmp_path) -> None:
    decision = AIRecoveryBoundary(ProjectBoundaryProfileStore(tmp_path)).evaluate(
        _request(),
        review_id="review-opaque",
        review_revision=1,
        action="confirm_no_effect_and_resume",
    )

    assert decision.outcome == "ask"
    assert decision.reason_codes == ("guarded_mutation_requires_approval",)
    assert decision.requires_receipt is True


def test_profile_deny_cannot_be_widened_by_manual_reviewer(tmp_path) -> None:
    profiles = ProjectBoundaryProfileStore(tmp_path)
    profiles.update(
        "project-alpha",
        mode="open",
        remote_default="allow",
        denied_effects=("write",),
        expected_revision=0,
    )

    decision = AIRecoveryBoundary(profiles).evaluate(
        _request(),
        review_id="review-opaque",
        review_revision=1,
        action="confirm_no_effect_and_resume",
    )

    assert decision.outcome == "deny"
    assert decision.reason_codes == ("profile_explicit_deny",)


def test_locked_recovery_evaluation_blocks_profile_revision_drift_until_action_finishes(tmp_path) -> None:
    profiles = ProjectBoundaryProfileStore(tmp_path)
    boundary = AIRecoveryBoundary(profiles)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with boundary.locked_evaluation(
            _request(),
            review_id="review-opaque",
            review_revision=1,
            action="confirm_no_effect_and_resume",
        ) as decision:
            assert decision.policy_revision == 1
            update = pool.submit(
                profiles.update,
                "project-alpha",
                mode="sealed",
                remote_default="deny",
                denied_effects=("write",),
                expected_revision=0,
            )
            sleep(0.02)
            assert not update.done()
        assert update.result(timeout=1).profile.mode == "sealed"
