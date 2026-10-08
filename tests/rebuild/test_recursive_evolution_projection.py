from __future__ import annotations

import pytest

from core.recursive_evolution import (
    EvaluationVerdict,
    EvolutionContractError,
    EvolutionEpisode,
    EvolutionEvaluation,
    EvolutionEvent,
    EvolutionPolicy,
    EvolutionProposal,
    EvolutionResourceEnvelope,
    EvolutionReview,
    EvolutionRolloutDecision,
    EvolutionTargetKind,
    ReviewVerdict,
    project_evolution_events,
)


TIME = "2026-09-01T00:00:00Z"
POLICY = EvolutionPolicy(3, 2, 2, 20, 2, "2026-09-05T12:00:00Z", 0.05, 2)
ENV = EvolutionResourceEnvelope(("read",), 5, 2, 2, ("local",))
EPISODE = EvolutionEpisode("episode-1", "project-alpha", EvolutionTargetKind.PROMPT_STRATEGY, POLICY)


def _proposal(*, generation: int = 1, parent: str | None = None, proposal_id: str = "proposal-1") -> EvolutionProposal:
    return EvolutionProposal(proposal_id, "episode-1", generation, EvolutionTargetKind.PROMPT_STRATEGY, "proposer-1", "executor-1", "crp://baseline/one", "base-r1", parent, f"crp://candidate/{proposal_id}", "candidate-r1", ENV, ENV)


def _evaluation(
    verdict: EvaluationVerdict = EvaluationVerdict.QUALIFIED,
    *,
    evaluation_id: str = "eval-1",
    candidate_score: float | None = None,
) -> EvolutionEvaluation:
    if candidate_score is None:
        candidate_score = (
            0.6 if verdict is EvaluationVerdict.QUALIFIED else 0.5
        )
    return EvolutionEvaluation(
        evaluation_id,
        "episode-1",
        "proposal-1",
        f"evaluator-{evaluation_id}",
        0.5,
        candidate_score,
        "metric-r1",
        "crp://inputs/eval",
        "input-r1",
        "crp://results/base",
        f"crp://results/{evaluation_id}",
        3,
        verdict,
        (f"crp://evidence/{evaluation_id}",),
    )


def test_qualified_review_canary_and_user_promotion_succeeds() -> None:
    review = EvolutionReview("review-1", "episode-1", "proposal-1", "reviewer-1", ReviewVerdict.QUALIFIED, ("eval-1",), ("crp://evidence/review",))
    decision = EvolutionRolloutDecision("decision-1", "episode-1", "proposal-1", "user-1", ("crp://evidence/user",))
    events = (
        EvolutionEvent.episode_created(EPISODE, TIME),
        EvolutionEvent.proposal_recorded(_proposal(), TIME),
        EvolutionEvent.evaluation_recorded(_evaluation(), TIME),
        EvolutionEvent.review_recorded(review, TIME),
        EvolutionEvent.canary_started(decision, TIME),
        EvolutionEvent.canary_observed("canary-1", "episode-1", "proposal-1", True, 2, ("crp://evidence/canary",), TIME),
        EvolutionEvent.promoted(decision, TIME),
    )
    result = project_evolution_events(events, project_id="project-alpha")
    assert result is not None and result.budget_used == 8


def test_parent_must_be_unqualified_or_invalidated_and_identity_drift_fails() -> None:
    events = (
        EvolutionEvent.episode_created(EPISODE, TIME),
        EvolutionEvent.proposal_recorded(_proposal(), TIME),
        EvolutionEvent.proposal_recorded(_proposal(generation=2, parent="proposal-1", proposal_id="proposal-2"), TIME),
    )
    with pytest.raises(EvolutionContractError, match="parent"):
        project_evolution_events(events, project_id="project-alpha")


@pytest.mark.parametrize(
    "verdict",
    (
        EvaluationVerdict.UNQUALIFIED,
        EvaluationVerdict.INCONCLUSIVE,
        EvaluationVerdict.INVALIDATED,
    ),
)
def test_verified_negative_result_can_trigger_an_explicit_next_generation(
    verdict: EvaluationVerdict,
) -> None:
    events = (
        EvolutionEvent.episode_created(EPISODE, TIME),
        EvolutionEvent.proposal_recorded(_proposal(), TIME),
        EvolutionEvent.evaluation_recorded(_evaluation(verdict), TIME),
        EvolutionEvent.proposal_recorded(
            _proposal(
                generation=2, parent="proposal-1", proposal_id="proposal-2",
            ),
            TIME,
        ),
    )
    projection = project_evolution_events(events, project_id="project-alpha")
    assert projection is not None
    assert projection.current_generation == 2
    assert projection.proposal_statuses["proposal-2"] == "proposed"


def test_deadline_and_direct_promotion_fail_closed() -> None:
    late = "2026-09-06T00:00:00Z"
    with pytest.raises(EvolutionContractError, match="deadline"):
        project_evolution_events((EvolutionEvent.episode_created(EPISODE, TIME), EvolutionEvent.proposal_recorded(_proposal(), late)), project_id="project-alpha")
    with pytest.raises(EvolutionContractError, match="lifecycle"):
        project_evolution_events((EvolutionEvent.episode_created(EPISODE, TIME), EvolutionEvent.proposal_recorded(_proposal(), TIME), EvolutionEvent.evaluation_recorded(_evaluation(), TIME), EvolutionEvent.review_recorded(EvolutionReview("review-1", "episode-1", "proposal-1", "reviewer-1", ReviewVerdict.QUALIFIED, ("eval-1",), ("crp://evidence/review",)), TIME), EvolutionEvent.promoted(EvolutionRolloutDecision("decision-1", "episode-1", "proposal-1", "user-1", ("crp://evidence/user",)), TIME)), project_id="project-alpha")


@pytest.mark.parametrize("reason", ["deadline", "budget", "no_improvement", "safety", "user"])
def test_stop_reasons_are_persisted_and_terminal(reason: str) -> None:
    observed_at = POLICY.deadline_at if reason == "deadline" else TIME
    recorded_at = observed_at if reason == "deadline" else TIME
    stopped = EvolutionEvent.episode_stopped("episode-1", "stop-1", reason, ("crp://evidence/stop",), ("episode-1.created",), observed_at, recorded_at)
    result = project_evolution_events(
        (EvolutionEvent.episode_created(EPISODE, TIME), stopped),
        project_id="project-alpha",
    )
    assert result is not None and result.stop_reason == reason
    with pytest.raises(EvolutionContractError, match="stopped"):
        project_evolution_events(
            (EvolutionEvent.episode_created(EPISODE, TIME), stopped, EvolutionEvent.proposal_recorded(_proposal(), recorded_at)),
            project_id="project-alpha",
        )


def test_event_identity_and_payload_are_fail_closed() -> None:
    with pytest.raises(EvolutionContractError, match="identity"):
        EvolutionEvent("wrong", "episode.created", "episode-1", TIME, EPISODE.to_payload())
    with pytest.raises(EvolutionContractError, match="payload"):
        EvolutionEvent("episode-1.stop-1.stopped", "episode.stopped", "episode-1", TIME, {"stop_id": "stop-1", "reason": "user", "evidence_refs": ["crp://evidence/stop"], "trigger_event_ids": ["episode-1.created"], "observed_at": TIME, "extra": True})


def test_exact_event_replay_is_idempotent_but_lifecycle_is_closed() -> None:
    proposal = EvolutionEvent.proposal_recorded(_proposal(), TIME)
    result = project_evolution_events((EvolutionEvent.episode_created(EPISODE, TIME), proposal, proposal), project_id="project-alpha")
    assert result is not None and result.candidate_count == 1
    review = EvolutionReview("review-1", "episode-1", "proposal-1", "reviewer-1", ReviewVerdict.QUALIFIED, ("eval-1",), ("crp://evidence/review",))
    decision = EvolutionRolloutDecision("decision-1", "episode-1", "proposal-1", "user-1", ("crp://evidence/user",))
    events = (EvolutionEvent.episode_created(EPISODE, TIME), proposal, EvolutionEvent.evaluation_recorded(_evaluation(), TIME), EvolutionEvent.review_recorded(review, TIME), EvolutionEvent.canary_started(decision, TIME), EvolutionEvent.canary_observed("canary-1", "episode-1", "proposal-1", False, 2, ("crp://evidence/canary",), TIME))
    with pytest.raises(EvolutionContractError, match="lifecycle"):
        project_evolution_events(events + (EvolutionEvent.promoted(decision, TIME),), project_id="project-alpha")


def test_unqualified_evaluation_cannot_be_hidden_from_review() -> None:
    qualified = EvolutionEvent.evaluation_recorded(_evaluation(), TIME)
    unqualified = EvolutionEvent.evaluation_recorded(
        _evaluation(EvaluationVerdict.UNQUALIFIED, evaluation_id="eval-2"), TIME
    )
    review = EvolutionReview(
        "review-1",
        "episode-1",
        "proposal-1",
        "reviewer-1",
        ReviewVerdict.QUALIFIED,
        ("eval-1",),
        ("crp://evidence/review",),
    )
    events = (
        EvolutionEvent.episode_created(EPISODE, TIME),
        EvolutionEvent.proposal_recorded(_proposal(), TIME),
        qualified,
        unqualified,
        EvolutionEvent.review_recorded(review, TIME),
    )
    with pytest.raises(EvolutionContractError, match="lifecycle"):
        project_evolution_events(events, project_id="project-alpha")


def test_deadline_is_derived_without_a_new_event() -> None:
    projection = project_evolution_events(
        (EvolutionEvent.episode_created(EPISODE, TIME),),
        project_id="project-alpha",
        observed_at="2026-09-06T00:00:00Z",
    )
    assert projection is not None
    assert projection.stop_required_reason == "deadline"
    assert projection.persisted_stop_reason is None
    assert projection.is_terminal is False


def test_required_stop_blocks_further_work_until_persisted() -> None:
    budget_policy = EvolutionPolicy(3, 2, 2, 5, 2, POLICY.deadline_at, 0.05, 2)
    episode = EvolutionEpisode(
        "episode-1",
        "project-alpha",
        EvolutionTargetKind.PROMPT_STRATEGY,
        budget_policy,
    )
    events = (
        EvolutionEvent.episode_created(episode, TIME),
        EvolutionEvent.proposal_recorded(_proposal(), TIME),
    )
    projection = project_evolution_events(events, project_id="project-alpha")
    assert projection is not None
    assert projection.stop_required_reason == "budget"
    with pytest.raises(EvolutionContractError, match="requires stop"):
        project_evolution_events(
            events + (EvolutionEvent.evaluation_recorded(_evaluation(), TIME),),
            project_id="project-alpha",
        )


def test_canary_observations_accumulate_and_terminal_states_are_mutually_exclusive() -> None:
    review = EvolutionReview(
        "review-1",
        "episode-1",
        "proposal-1",
        "reviewer-1",
        ReviewVerdict.QUALIFIED,
        ("eval-1",),
        ("crp://evidence/review",),
    )
    decision = EvolutionRolloutDecision(
        "decision-1",
        "episode-1",
        "proposal-1",
        "user-1",
        ("crp://evidence/user",),
    )
    prefix = (
        EvolutionEvent.episode_created(EPISODE, TIME),
        EvolutionEvent.proposal_recorded(_proposal(), TIME),
        EvolutionEvent.evaluation_recorded(_evaluation(), TIME),
        EvolutionEvent.review_recorded(review, TIME),
        EvolutionEvent.canary_started(decision, TIME),
        EvolutionEvent.canary_observed(
            "canary-1",
            "episode-1",
            "proposal-1",
            True,
            1,
            ("crp://evidence/canary-1",),
            TIME,
        ),
        EvolutionEvent.canary_observed(
            "canary-2",
            "episode-1",
            "proposal-1",
            True,
            1,
            ("crp://evidence/canary-2",),
            TIME,
        ),
    )
    projection = project_evolution_events(prefix, project_id="project-alpha")
    assert projection is not None
    assert projection.canary_passed_ids == ("proposal-1",)
    promoted = EvolutionEvent.promoted(decision, TIME)
    rolled_back = project_evolution_events(
        prefix
        + (
            promoted,
            EvolutionEvent.rolled_back(decision, TIME),
        ),
        project_id="project-alpha",
    )
    assert rolled_back is not None
    assert rolled_back.rolled_back_proposal_ids == ("proposal-1",)
    with pytest.raises(EvolutionContractError, match="lifecycle"):
        project_evolution_events(
            prefix
            + (
                promoted,
                EvolutionEvent.rolled_back(decision, TIME),
                EvolutionEvent.promoted(
                    EvolutionRolloutDecision(
                        "decision-2", "episode-1", "proposal-1", "user-1",
                        ("crp://evidence/user-2",),
                    ),
                    TIME,
                ),
            ),
            project_id="project-alpha",
        )
