from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.recursive_evolution_runtime import (
    CanaryObservation,
    RecursiveEvolutionRuntime,
    RecursiveEvolutionRuntimeError,
)
from core.recursive_evolution import (
    EvaluationVerdict,
    EvolutionEpisode,
    EvolutionEvaluation,
    EvolutionPolicy,
    EvolutionProposal,
    EvolutionResourceEnvelope,
    EvolutionReview,
    EvolutionTargetKind,
    ReviewVerdict,
)


PROJECT = "project-alpha"
EPISODE = "episode-alpha"
PROPOSAL = "proposal-alpha"
NOW = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)


class _Evidence:
    def __init__(self, *, wrong_scope: bool = False) -> None:
        self.wrong_scope = wrong_scope

    def verify_evaluation(self, *, project_id, proposal, receipt_ref):
        assert receipt_ref == "crp://receipt/eval-alpha"
        return EvolutionEvaluation(
            evaluation_id="evaluation-alpha",
            episode_id="wrong-episode" if self.wrong_scope else proposal.episode_id,
            proposal_id=proposal.proposal_id,
            evaluator_id="evaluator-alpha",
            baseline_score=0.5,
            candidate_score=0.7,
            metric_set_revision="metric-v1",
            evaluation_input_ref="crp://inputs/eval-alpha",
            evaluation_input_revision="input-v1",
            baseline_result_ref="crp://results/baseline-alpha",
            candidate_result_ref="crp://results/candidate-alpha",
            actual_budget_units=1,
            verdict=EvaluationVerdict.QUALIFIED,
            evidence_refs=(receipt_ref,),
        )

    def verify_canary(self, *, project_id, proposal, evidence_ref):
        return CanaryObservation(
            proposal.episode_id, proposal.proposal_id, evidence_ref != "crp://canary/failure",
            1, (evidence_ref,),
        )


class _Approvals:
    def __init__(self) -> None:
        self.calls = []

    def verify_local_human(self, **kwargs) -> None:
        self.calls.append(kwargs)
        if kwargs["user_id"] == "bad-user":
            raise ValueError("not local human")


class _Targets:
    def __init__(self) -> None:
        self.done = {}
        self.calls = []
        self.reject_apply = False

    def probe(self, *, operation_id):
        return self.done.get(operation_id)

    def preflight(self, *, project_id, action, proposal):
        if project_id != PROJECT:
            raise ValueError("project scope drifted")

    def apply(self, *, operation_id, action, proposal, authorization_ref):
        if self.reject_apply:
            raise ValueError("target baseline drifted")
        self.calls.append((operation_id, action, proposal.proposal_id))
        receipt_ref = f"crp://recursive-evolution/target-operations/{operation_id}"
        self.done[operation_id] = receipt_ref
        return receipt_ref


def _episode() -> EvolutionEpisode:
    return EvolutionEpisode(
        EPISODE, PROJECT, EvolutionTargetKind.AGENT_PROFILE,
        EvolutionPolicy(2, 2, 2, 10, 2, "2026-09-05T00:00:00Z"),
    )


def _proposal() -> EvolutionProposal:
    envelope = EvolutionResourceEnvelope(("read",), 1, 0, 0, ())
    return EvolutionProposal(
        PROPOSAL, EPISODE, 1, EvolutionTargetKind.AGENT_PROFILE,
        "proposer-alpha", "executor-alpha", "crp://target/baseline", "base-v1",
        None, "crp://target/candidate", "candidate-v1", envelope, envelope,
    )


def _runtime(tmp_path: Path, *, evidence=None):
    world = PersonalWorldModelRuntime.for_root(tmp_path, now=lambda: NOW)
    targets = _Targets()
    return RecursiveEvolutionRuntime(
        world=world, evidence=evidence or _Evidence(), approvals=_Approvals(),
        targets=targets, now=lambda: NOW,
    ), targets


def _qualified_until_canary(runtime: RecursiveEvolutionRuntime) -> None:
    runtime.create_episode(episode=_episode(), command_id=EPISODE)
    runtime.record_candidate(project_id=PROJECT, proposal=_proposal(), command_id=PROPOSAL)
    runtime.record_evaluation_from_receipt(
        project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
        receipt_ref="crp://receipt/eval-alpha", command_id="evaluation-alpha",
    )
    runtime.record_review(
        project_id=PROJECT,
        review=EvolutionReview(
            "review-alpha", EPISODE, PROPOSAL, "reviewer-alpha",
            ReviewVerdict.QUALIFIED, ("evaluation-alpha",),
            ("crp://reviews/review-alpha",),
        ),
        command_id="review-alpha",
    )


def test_complete_success_path_replays_and_applies_target_once(tmp_path: Path) -> None:
    runtime, targets = _runtime(tmp_path)
    _qualified_until_canary(runtime)
    runtime.approve_canary(
        project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
        user_id="human-alpha", confirmation_ref="crp://approval/canary-alpha",
        evidence_refs=("crp://reviews/review-alpha",), command_id="canary-alpha",
    )
    runtime.observe_canary(
        project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
        evidence_ref="crp://canary/alpha",
        command_id="observation-alpha",
    )
    first = runtime.promote(
        project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
        user_id="human-alpha", confirmation_ref="crp://approval/promote-alpha",
        evidence_refs=("crp://canary/alpha",), command_id="promote-alpha",
    )
    second = runtime.promote(
        project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
        user_id="human-alpha", confirmation_ref="crp://approval/promote-alpha",
        evidence_refs=("crp://canary/alpha",), command_id="promote-alpha",
    )
    assert first.projection.proposal_statuses[PROPOSAL] == "promoted"
    assert second.replayed is True
    assert [call[1] for call in targets.calls] == [
        "record_candidate", "start_canary", "promote",
    ]
    assert any(
        ref.startswith("crp://recursive-evolution/target-operations/")
        for ref in first.event.payload["evidence_refs"]
    )


def test_receipt_scope_drift_and_agent_approval_are_rejected(tmp_path: Path) -> None:
    runtime, _targets = _runtime(tmp_path, evidence=_Evidence(wrong_scope=True))
    runtime.create_episode(episode=_episode(), command_id=EPISODE)
    runtime.record_candidate(project_id=PROJECT, proposal=_proposal(), command_id=PROPOSAL)
    with pytest.raises(RecursiveEvolutionRuntimeError, match="Receipt evaluation scope"):
        runtime.record_evaluation_from_receipt(
            project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
            receipt_ref="crp://receipt/eval-alpha", command_id="evaluation-alpha",
        )
    with pytest.raises(RecursiveEvolutionRuntimeError, match="local human"):
        runtime.approve_canary(
            project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
            user_id="system", confirmation_ref="crp://approval/nope",
            evidence_refs=("crp://reviews/nope",), command_id="canary-nope",
        )


def test_failed_canary_automatically_rolls_back(tmp_path: Path) -> None:
    runtime, targets = _runtime(tmp_path)
    _qualified_until_canary(runtime)
    runtime.approve_canary(
        project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
        user_id="human-alpha", confirmation_ref="crp://approval/canary-alpha",
        evidence_refs=("crp://reviews/review-alpha",), command_id="canary-alpha",
    )
    runtime.observe_canary(
        project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
        evidence_ref="crp://canary/failure",
        command_id="observation-failed",
    )
    projection = runtime.get_projection(project_id=PROJECT, episode_id=EPISODE)
    assert projection is not None
    assert projection.proposal_statuses[PROPOSAL] == "rolled_back"
    assert [call[1] for call in targets.calls] == [
        "record_candidate", "start_canary", "rollback_canary",
    ]


def test_recovery_reconciles_failed_canary_after_apply_failure(tmp_path: Path) -> None:
    runtime, targets = _runtime(tmp_path)
    _qualified_until_canary(runtime)
    runtime.approve_canary(
        project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
        user_id="human-alpha", confirmation_ref="crp://approval/canary-alpha",
        evidence_refs=("crp://reviews/review-alpha",), command_id="canary-alpha",
    )
    targets.reject_apply = True
    with pytest.raises(RecursiveEvolutionRuntimeError, match="target authority"):
        runtime.observe_canary(
            project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
            evidence_ref="crp://canary/failure", command_id="observation-failed",
        )
    targets.reject_apply = False
    runtime.recover_pending_rollbacks(project_id=PROJECT)
    projection = runtime.get_projection(project_id=PROJECT, episode_id=EPISODE)
    assert projection is not None and projection.proposal_statuses[PROPOSAL] == "rolled_back"


def test_target_drift_does_not_write_promotion_event(tmp_path: Path) -> None:
    runtime, targets = _runtime(tmp_path)
    _qualified_until_canary(runtime)
    runtime.approve_canary(
        project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
        user_id="human-alpha", confirmation_ref="crp://approval/canary-alpha",
        evidence_refs=("crp://reviews/review-alpha",), command_id="canary-alpha",
    )
    runtime.observe_canary(
        project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
        evidence_ref="crp://canary/alpha",
        command_id="observation-alpha",
    )
    targets.reject_apply = True
    with pytest.raises(RecursiveEvolutionRuntimeError, match="target authority"):
        runtime.promote(
            project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
            user_id="human-alpha", confirmation_ref="crp://approval/promote-alpha",
            evidence_refs=("crp://canary/alpha",), command_id="promote-alpha",
        )
    projection = runtime.get_projection(project_id=PROJECT, episode_id=EPISODE)
    assert projection is not None
    assert projection.proposal_statuses[PROPOSAL] == "canary_passed"


def test_illegal_direct_promotion_never_calls_target(tmp_path: Path) -> None:
    runtime, targets = _runtime(tmp_path)
    _qualified_until_canary(runtime)
    with pytest.raises(RecursiveEvolutionRuntimeError, match="lifecycle-valid"):
        runtime.promote(
            project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
            user_id="human-alpha", confirmation_ref="crp://approval/promote-alpha",
            evidence_refs=("crp://reviews/review-alpha",), command_id="promote-alpha",
        )
    assert [call[1] for call in targets.calls] == ["record_candidate"]


def test_user_stop_requires_human_confirmation_and_auto_stop_is_persisted(tmp_path: Path) -> None:
    runtime, _targets = _runtime(tmp_path)
    constrained = EvolutionEpisode(
        EPISODE, PROJECT, EvolutionTargetKind.AGENT_PROFILE,
        EvolutionPolicy(2, 2, 2, 1, 2, "2026-09-05T00:00:00Z"),
    )
    runtime.create_episode(episode=constrained, command_id=EPISODE)
    runtime.record_candidate(project_id=PROJECT, proposal=_proposal(), command_id=PROPOSAL)
    with pytest.raises(RecursiveEvolutionRuntimeError, match="stop policy"):
        runtime.record_evaluation_from_receipt(
            project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
            receipt_ref="crp://receipt/eval-alpha", command_id="evaluation-alpha",
        )
    projection = runtime.get_projection(project_id=PROJECT, episode_id=EPISODE)
    assert projection is not None and projection.persisted_stop_reason == "budget"

    other, _targets = _runtime(tmp_path / "other")
    other.create_episode(episode=_episode(), command_id=EPISODE)
    with pytest.raises(RecursiveEvolutionRuntimeError, match="local human"):
        other.stop(
            project_id=PROJECT, episode_id=EPISODE, reason="user", user_id="system",
            confirmation_ref="crp://approval/stop", evidence_refs=("crp://stop/user",),
            command_id="stop-user",
        )


@pytest.mark.parametrize(
    ("policy", "proposal", "reason"),
    (
        (
            EvolutionPolicy(1, 2, 2, 10, 2, "2026-09-05T00:00:00Z"),
            replace(
                _proposal(), proposal_id="proposal-beta", generation=2,
                parent_proposal_id=PROPOSAL,
                candidate_ref="crp://target/candidate-beta",
                candidate_revision="candidate-v2",
            ),
            "max_generations",
        ),
        (
            EvolutionPolicy(2, 1, 2, 10, 2, "2026-09-05T00:00:00Z"),
            replace(
                _proposal(), proposal_id="proposal-beta",
                candidate_ref="crp://target/candidate-beta",
                candidate_revision="candidate-v2",
            ),
            "max_candidates_per_generation",
        ),
    ),
)
def test_candidate_hard_limit_persists_stop_before_target_effect(
    tmp_path: Path,
    policy: EvolutionPolicy,
    proposal: EvolutionProposal,
    reason: str,
) -> None:
    runtime, targets = _runtime(tmp_path)
    runtime.create_episode(
        episode=EvolutionEpisode(
            EPISODE, PROJECT, EvolutionTargetKind.AGENT_PROFILE, policy,
        ),
        command_id=EPISODE,
    )
    runtime.record_candidate(
        project_id=PROJECT, proposal=_proposal(), command_id=PROPOSAL,
    )
    with pytest.raises(RecursiveEvolutionRuntimeError, match="stop policy"):
        runtime.record_candidate(
            project_id=PROJECT,
            proposal=proposal,
            command_id=proposal.proposal_id,
        )
    projection = runtime.get_projection(project_id=PROJECT, episode_id=EPISODE)
    assert projection is not None and projection.persisted_stop_reason == reason
    assert [call[2] for call in targets.calls] == [PROPOSAL]


def test_evaluation_hard_limit_persists_stop(tmp_path: Path) -> None:
    class Evidence(_Evidence):
        def verify_evaluation(self, *, project_id, proposal, receipt_ref):
            evaluation_id = receipt_ref.rsplit("/", 1)[-1]
            return EvolutionEvaluation(
                evaluation_id, proposal.episode_id, proposal.proposal_id,
                f"evaluator-{evaluation_id}", 0.5, 0.7, "metric-v1",
                f"crp://inputs/{evaluation_id}", "input-v1",
                f"crp://results/{evaluation_id}-baseline",
                f"crp://results/{evaluation_id}-candidate",
                1, EvaluationVerdict.QUALIFIED, (receipt_ref,),
            )

    runtime, _targets = _runtime(tmp_path, evidence=Evidence())
    runtime.create_episode(
        episode=EvolutionEpisode(
            EPISODE, PROJECT, EvolutionTargetKind.AGENT_PROFILE,
            EvolutionPolicy(2, 2, 1, 10, 2, "2026-09-05T00:00:00Z"),
        ),
        command_id=EPISODE,
    )
    runtime.record_candidate(
        project_id=PROJECT, proposal=_proposal(), command_id=PROPOSAL,
    )
    for evaluation_id in ("evaluation-alpha", "evaluation-beta"):
        if evaluation_id == "evaluation-beta":
            with pytest.raises(RecursiveEvolutionRuntimeError, match="stop policy"):
                runtime.record_evaluation_from_receipt(
                    project_id=PROJECT, episode_id=EPISODE,
                    proposal_id=PROPOSAL,
                    receipt_ref=f"crp://receipt/{evaluation_id}",
                    command_id=evaluation_id,
                )
            break
        runtime.record_evaluation_from_receipt(
            project_id=PROJECT, episode_id=EPISODE,
            proposal_id=PROPOSAL,
            receipt_ref=f"crp://receipt/{evaluation_id}",
            command_id=evaluation_id,
        )
    projection = runtime.get_projection(project_id=PROJECT, episode_id=EPISODE)
    assert projection is not None
    assert projection.persisted_stop_reason == "max_evaluations_per_candidate"


def test_cross_time_replay_and_target_probe_recovery(tmp_path: Path) -> None:
    runtime, targets = _runtime(tmp_path)
    runtime.create_episode(episode=_episode(), command_id=EPISODE)
    later_world = PersonalWorldModelRuntime.for_root(
        tmp_path, now=lambda: datetime(2026, 9, 4, 11, 0, tzinfo=UTC),
    )
    later = RecursiveEvolutionRuntime(
        world=later_world, evidence=_Evidence(), approvals=_Approvals(), targets=targets,
        now=lambda: datetime(2026, 9, 4, 11, 0, tzinfo=UTC),
    )
    assert later.create_episode(episode=_episode(), command_id=EPISODE).replayed

    _qualified_until_canary(later)
    later.approve_canary(
        project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
        user_id="human-alpha", confirmation_ref="crp://approval/canary-alpha",
        evidence_refs=("crp://reviews/review-alpha",), command_id="canary-alpha",
    )
    later.observe_canary(
        project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
        evidence_ref="crp://canary/alpha",
        command_id="observation-alpha",
    )
    later.create_episode(
        episode=EvolutionEpisode(
            "episode-beta", PROJECT, EvolutionTargetKind.PROJECT_SKILL,
            EvolutionPolicy(1, 1, 1, 1, 1, "2026-09-05T00:00:00Z"),
        ),
        command_id="episode-beta",
    )
    operation_id = f"evolution.{PROJECT}.{EPISODE}.promote-alpha.promote"
    targets.done[operation_id] = (
        f"crp://recursive-evolution/target-operations/{operation_id}"
    )
    later.promote(
        project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL,
        user_id="human-alpha", confirmation_ref="crp://approval/promote-alpha",
        evidence_refs=("crp://canary/alpha",), command_id="promote-alpha",
    )
    assert [call[1] for call in targets.calls] == [
        "record_candidate", "start_canary",
    ]
