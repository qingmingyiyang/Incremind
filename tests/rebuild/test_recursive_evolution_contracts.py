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
)


TIME = "2026-09-01T00:00:00Z"


def _policy() -> EvolutionPolicy:
    return EvolutionPolicy(3, 2, 2, 20, 2, "2026-09-05T12:00:00Z", 0.05, 2)


def _envelope() -> EvolutionResourceEnvelope:
    return EvolutionResourceEnvelope(("read",), 5, 2, 2, ("local",))


def _proposal() -> EvolutionProposal:
    return EvolutionProposal(
        "proposal-1", "episode-1", 1, EvolutionTargetKind.PROMPT_STRATEGY,
        "proposer-1", "executor-1", "crp://baseline/one", "base-r1", None,
        "crp://candidate/one", "candidate-r1", _envelope(), _envelope(),
    )


def test_policy_is_frozen_and_contracts_round_trip() -> None:
    policy = _policy()
    assert policy.requires_user_confirmation is True
    assert policy.auto_promote_allowed is False
    assert policy.canary_required is True
    assert EvolutionPolicy.from_payload(policy.to_payload()) == policy
    assert EvolutionProposal.from_payload(_proposal().to_payload()) == _proposal()
    evaluation = EvolutionEvaluation(
        "eval-1", "episode-1", "proposal-1", "evaluator-1", 0.5, 0.6,
        "metric-r1", "crp://inputs/eval", "input-r1", "crp://results/base", "crp://results/candidate", 3, EvaluationVerdict.QUALIFIED, ("crp://evidence/eval",),
    )
    assert EvolutionEvaluation.from_payload(evaluation.to_payload()) == evaluation

    review = EvolutionReview(
        "review-1",
        "episode-1",
        "proposal-1",
        "reviewer-1",
        ReviewVerdict.QUALIFIED,
        ("eval-1",),
        ("crp://evidence/review",),
    )
    assert EvolutionReview.from_payload(review.to_payload()) == review
    rollout = EvolutionRolloutDecision(
        "decision-1",
        "episode-1",
        "proposal-1",
        "user-1",
        ("crp://evidence/user",),
    )
    assert EvolutionRolloutDecision.from_payload(rollout.to_payload()) == rollout


def test_every_event_kind_round_trips_strictly() -> None:
    episode = EvolutionEpisode(
        "episode-1", "project-alpha", EvolutionTargetKind.PROMPT_STRATEGY, _policy()
    )
    evaluation = EvolutionEvaluation(
        "eval-1",
        "episode-1",
        "proposal-1",
        "evaluator-1",
        0.5,
        0.6,
        "metric-r1",
        "crp://inputs/eval",
        "input-r1",
        "crp://results/base",
        "crp://results/candidate",
        3,
        EvaluationVerdict.QUALIFIED,
        ("crp://evidence/eval",),
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
    rollout = EvolutionRolloutDecision(
        "decision-1",
        "episode-1",
        "proposal-1",
        "user-1",
        ("crp://evidence/user",),
    )
    events = (
        EvolutionEvent.episode_created(episode, TIME),
        EvolutionEvent.proposal_recorded(_proposal(), TIME),
        EvolutionEvent.evaluation_recorded(evaluation, TIME),
        EvolutionEvent.review_recorded(review, TIME),
        EvolutionEvent.canary_started(rollout, TIME),
        EvolutionEvent.canary_observed(
            "observation-1",
            "episode-1",
            "proposal-1",
            True,
            2,
            ("crp://evidence/canary",),
            TIME,
        ),
        EvolutionEvent.promoted(rollout, TIME),
        EvolutionEvent.rolled_back(rollout, TIME),
        EvolutionEvent.rejected(rollout, TIME),
        EvolutionEvent.episode_stopped(
            "episode-1",
            "stop-1",
            "user",
            ("crp://evidence/stop",),
            ("episode-1.created",),
            TIME,
            TIME,
        ),
    )
    for event in events:
        assert EvolutionEvent.from_payload(event.to_payload()) == event
        malformed = event.to_payload()
        malformed["extra"] = True
        with pytest.raises(EvolutionContractError, match="envelope"):
            EvolutionEvent.from_payload(malformed)


def test_proposal_cannot_expand_and_evaluation_rejects_bool_or_free_form_verdict() -> None:
    with pytest.raises(EvolutionContractError, match="cannot expand"):
        EvolutionProposal(
            "proposal-1", "episode-1", 1, EvolutionTargetKind.AGENT_PROFILE,
            "proposer-1", "executor-1", "crp://baseline/one", "base-r1", None,
            "crp://candidate/one", "candidate-r1", _envelope(),
            EvolutionResourceEnvelope(("read", "write"), 5, 2, 2, ("local",)),
        )


def test_zero_resource_envelope_is_valid_and_round_trips() -> None:
    envelope = EvolutionResourceEnvelope((), 0, 0, 0, ())
    assert EvolutionResourceEnvelope.from_payload(envelope.to_payload()) == envelope
    with pytest.raises((EvolutionContractError, ValueError)):
        EvolutionEvaluation(
            "eval-1", "episode-1", "proposal-1", "evaluator-1", 0.5, 0.6,
            "metric-r1", "crp://inputs/eval", "input-r1", "crp://results/base", "crp://results/candidate", 3, True, ("crp://evidence/eval",),  # type: ignore[arg-type]
        )


def test_review_references_are_unique_and_collections_fail_closed() -> None:
    with pytest.raises(EvolutionContractError, match="evaluation ids"):
        EvolutionReview("review-1", "episode-1", "proposal-1", "reviewer-1", ReviewVerdict.QUALIFIED, ("eval-1", "eval-1"), ("crp://evidence/review",))
    with pytest.raises(EvolutionContractError, match="resource envelope"):
        EvolutionResourceEnvelope(None, 0, 0, 0, ())  # type: ignore[arg-type]
    with pytest.raises(EvolutionContractError, match="resource envelope"):
        EvolutionResourceEnvelope.from_payload(
            {
                "capabilities": None,
                "budget_units": 0,
                "max_depth": 0,
                "max_concurrency": 0,
                "egress": [],
            }
        )
