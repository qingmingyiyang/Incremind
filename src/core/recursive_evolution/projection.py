"""Pure, fail-closed projection of one governed evolution episode."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from .contracts import (
    EvaluationVerdict,
    EvolutionContractError,
    EvolutionEpisode,
    EvolutionEvaluation,
    EvolutionPolicy,
    EvolutionProposal,
    EvolutionReview,
    EvolutionRolloutDecision,
    EvolutionTargetKind,
    ReviewVerdict,
    _id,
    _refs,
    _timestamp,
)


class EvolutionEventKind(StrEnum):
    EPISODE_CREATED = "episode.created"
    PROPOSAL_RECORDED = "proposal.recorded"
    EVALUATION_RECORDED = "evaluation.recorded"
    REVIEW_RECORDED = "review.recorded"
    CANARY_STARTED = "canary.started"
    CANARY_OBSERVED = "canary.observed"
    PROMOTED = "rollout.promoted"
    ROLLED_BACK = "rollout.rolled_back"
    REJECTED = "rollout.rejected"
    EPISODE_STOPPED = "episode.stopped"


_STOP_REASONS = frozenset(
    {
        "deadline",
        "budget",
        "no_improvement",
        "max_generations",
        "max_candidates_per_generation",
        "max_evaluations_per_candidate",
        "safety",
        "user",
    }
)
_PROPOSAL_TERMINAL = frozenset(
    {"invalidated", "rejected", "promoted", "rolled_back"}
)
_EVALUATION_OPEN = frozenset(
    {"proposed", "evaluated_qualified", "unqualified", "inconclusive"}
)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _time(value: str) -> datetime:
    canonical = _timestamp(value, "timestamp")
    return datetime.fromisoformat(canonical.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class EvolutionEvent:
    event_id: str
    kind: EvolutionEventKind
    episode_id: str
    recorded_at: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "event_id", _id(self.event_id, "evolution event id")
        )
        object.__setattr__(self, "episode_id", _id(self.episode_id, "episode id"))
        try:
            kind = EvolutionEventKind(self.kind)
        except (TypeError, ValueError) as error:
            raise EvolutionContractError("evolution event kind is invalid") from error
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self, "recorded_at", _timestamp(self.recorded_at, "recorded_at")
        )
        if not isinstance(self.payload, Mapping):
            raise EvolutionContractError("evolution event payload is invalid")
        _validate_event_payload(self.event_id, kind, self.episode_id, self.payload)
        object.__setattr__(self, "payload", _freeze(self.payload))

    @classmethod
    def episode_created(
        cls, episode: EvolutionEpisode, recorded_at: str
    ) -> "EvolutionEvent":
        return cls(
            f"{episode.episode_id}.created",
            EvolutionEventKind.EPISODE_CREATED,
            episode.episode_id,
            recorded_at,
            episode.to_payload(),
        )

    @classmethod
    def proposal_recorded(
        cls, proposal: EvolutionProposal, recorded_at: str
    ) -> "EvolutionEvent":
        return cls(
            f"{proposal.proposal_id}.proposed",
            EvolutionEventKind.PROPOSAL_RECORDED,
            proposal.episode_id,
            recorded_at,
            proposal.to_payload(),
        )

    @classmethod
    def evaluation_recorded(
        cls, evaluation: EvolutionEvaluation, recorded_at: str
    ) -> "EvolutionEvent":
        return cls(
            evaluation.evaluation_id,
            EvolutionEventKind.EVALUATION_RECORDED,
            evaluation.episode_id,
            recorded_at,
            evaluation.to_payload(),
        )

    @classmethod
    def review_recorded(
        cls, review: EvolutionReview, recorded_at: str
    ) -> "EvolutionEvent":
        return cls(
            review.review_id,
            EvolutionEventKind.REVIEW_RECORDED,
            review.episode_id,
            recorded_at,
            review.to_payload(),
        )

    @classmethod
    def canary_started(
        cls, rollout: EvolutionRolloutDecision, recorded_at: str
    ) -> "EvolutionEvent":
        return cls(
            f"{rollout.decision_id}.canary",
            EvolutionEventKind.CANARY_STARTED,
            rollout.episode_id,
            recorded_at,
            rollout.to_payload(),
        )

    @classmethod
    def canary_observed(
        cls,
        observation_id: str,
        episode_id: str,
        proposal_id: str,
        passed: bool,
        samples: int,
        evidence_refs: tuple[str, ...],
        recorded_at: str,
    ) -> "EvolutionEvent":
        return cls(
            _id(observation_id, "canary observation id"),
            EvolutionEventKind.CANARY_OBSERVED,
            episode_id,
            recorded_at,
            {
                "proposal_id": _id(proposal_id, "proposal id"),
                "passed": passed,
                "samples": samples,
                "evidence_refs": list(evidence_refs),
            },
        )

    @classmethod
    def promoted(
        cls, rollout: EvolutionRolloutDecision, recorded_at: str
    ) -> "EvolutionEvent":
        return cls(
            f"{rollout.decision_id}.promoted",
            EvolutionEventKind.PROMOTED,
            rollout.episode_id,
            recorded_at,
            rollout.to_payload(),
        )

    @classmethod
    def rolled_back(
        cls, rollout: EvolutionRolloutDecision, recorded_at: str
    ) -> "EvolutionEvent":
        return cls(
            f"{rollout.decision_id}.rolled-back",
            EvolutionEventKind.ROLLED_BACK,
            rollout.episode_id,
            recorded_at,
            rollout.to_payload(),
        )

    @classmethod
    def rejected(
        cls, rollout: EvolutionRolloutDecision, recorded_at: str
    ) -> "EvolutionEvent":
        return cls(
            f"{rollout.decision_id}.rejected",
            EvolutionEventKind.REJECTED,
            rollout.episode_id,
            recorded_at,
            rollout.to_payload(),
        )

    @classmethod
    def episode_stopped(
        cls,
        episode_id: str,
        stop_id: str,
        reason: str,
        evidence_refs: tuple[str, ...],
        trigger_event_ids: tuple[str, ...],
        observed_at: str,
        recorded_at: str,
    ) -> "EvolutionEvent":
        stop = _id(stop_id, "stop id")
        return cls(
            f"{episode_id}.{stop}.stopped",
            EvolutionEventKind.EPISODE_STOPPED,
            episode_id,
            recorded_at,
            {
                "stop_id": stop,
                "reason": reason,
                "evidence_refs": list(evidence_refs),
                "trigger_event_ids": list(trigger_event_ids),
                "observed_at": observed_at,
            },
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "episode_id": self.episode_id,
            "recorded_at": self.recorded_at,
            "payload": _plain(self.payload),
        }

    @classmethod
    def from_payload(cls, value: object) -> "EvolutionEvent":
        expected = {"event_id", "kind", "episode_id", "recorded_at", "payload"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise EvolutionContractError("evolution event envelope is invalid")
        return cls(
            value["event_id"],
            value["kind"],
            value["episode_id"],
            value["recorded_at"],
            value["payload"],
        )


@dataclass(frozen=True, slots=True)
class EvolutionEpisodeProjection:
    episode_id: str
    project_id: str
    target_kind: EvolutionTargetKind
    policy: EvolutionPolicy
    current_generation: int
    candidate_count: int
    evaluation_count: int
    budget_used: int
    stop_required_reason: str | None
    persisted_stop_reason: str | None
    is_terminal: bool
    proposal_statuses: Mapping[str, str]
    canary_active_ids: tuple[str, ...]
    canary_passed_ids: tuple[str, ...]
    invalidated_proposal_ids: tuple[str, ...]
    rolled_back_proposal_ids: tuple[str, ...]
    rejected_proposal_ids: tuple[str, ...]
    promoted_proposal_ids: tuple[str, ...]
    source_event_ids: tuple[str, ...]

    @property
    def stop_reason(self) -> str | None:
        return self.persisted_stop_reason or self.stop_required_reason


def project_evolution_events(
    events: Sequence[EvolutionEvent],
    *,
    project_id: str,
    observed_at: str | None = None,
) -> EvolutionEpisodeProjection | None:
    stream = tuple(events)
    if not stream:
        return None
    if stream[0].kind is not EvolutionEventKind.EPISODE_CREATED:
        raise EvolutionContractError("evolution stream must start with episode")

    episode = EvolutionEpisode.from_payload(stream[0].episode_id, stream[0].payload)
    if episode.project_id != project_id:
        raise EvolutionContractError("evolution episode project scope is invalid")

    proposals: dict[str, EvolutionProposal] = {}
    evaluations: dict[str, EvolutionEvaluation] = {}
    proposal_evaluation_ids: dict[str, list[str]] = {}
    generations: dict[int, int] = {}
    reviews: dict[str, EvolutionReview] = {}
    statuses: dict[str, str] = {}
    canary_samples: dict[str, int] = {}
    seen: dict[str, EvolutionEvent] = {}
    source_ids: list[str] = []
    budget = 0
    no_improvement = 0
    current_generation = 0
    required: str | None = None
    persisted: str | None = None
    prior_time: datetime | None = None

    for event in stream:
        known = seen.get(event.event_id)
        if known is not None:
            if known != event:
                raise EvolutionContractError("evolution event identity drifted")
            continue
        if event.episode_id != episode.episode_id:
            raise EvolutionContractError("evolution event episode scope drifted")
        event_time = _time(event.recorded_at)
        if prior_time is not None and event_time < prior_time:
            raise EvolutionContractError("evolution event time moved backwards")
        prior_time = event_time
        seen[event.event_id] = event
        source_ids.append(event.event_id)

        if len(source_ids) == 1:
            continue
        if event.kind is EvolutionEventKind.EPISODE_CREATED:
            raise EvolutionContractError("evolution episode creation is duplicated")
        if persisted is not None:
            raise EvolutionContractError("evolution episode is stopped")
        if required is not None and event.kind is not EvolutionEventKind.EPISODE_STOPPED:
            raise EvolutionContractError("evolution episode requires stop")
        if event_time > _time(episode.policy.deadline_at):
            if (
                event.kind is not EvolutionEventKind.EPISODE_STOPPED
                or event.payload.get("reason") != "deadline"
            ):
                raise EvolutionContractError("evolution deadline requires stop")

        if event.kind is EvolutionEventKind.PROPOSAL_RECORDED:
            proposal = EvolutionProposal.from_payload(event.payload)
            _validate_proposal(
                proposal,
                episode,
                proposals,
                generations,
                statuses,
                current_generation,
            )
            _validate_budget(budget, proposal.candidate_envelope.budget_units, episode)
            proposals[proposal.proposal_id] = proposal
            statuses[proposal.proposal_id] = "proposed"
            generations[proposal.generation] = generations.get(proposal.generation, 0) + 1
            budget += proposal.candidate_envelope.budget_units
            current_generation = max(current_generation, proposal.generation)

        elif event.kind is EvolutionEventKind.EVALUATION_RECORDED:
            evaluation = EvolutionEvaluation.from_payload(event.payload)
            proposal = _proposal(evaluation.proposal_id, proposals)
            _require_status(statuses, proposal.proposal_id, _EVALUATION_OPEN)
            ids = proposal_evaluation_ids.setdefault(proposal.proposal_id, [])
            _validate_evaluation(evaluation, proposal, episode, len(ids))
            _validate_budget(budget, evaluation.actual_budget_units, episode)
            evaluations[evaluation.evaluation_id] = evaluation
            ids.append(evaluation.evaluation_id)
            budget += evaluation.actual_budget_units
            statuses[proposal.proposal_id] = _evaluation_status(
                tuple(evaluations[item] for item in ids)
            )
            if evaluation.verdict is EvaluationVerdict.QUALIFIED:
                no_improvement = 0
            else:
                no_improvement += 1

        elif event.kind is EvolutionEventKind.REVIEW_RECORDED:
            review = EvolutionReview.from_payload(event.payload)
            proposal = _proposal(review.proposal_id, proposals)
            _require_status(statuses, proposal.proposal_id, {"evaluated_qualified"})
            if proposal.proposal_id in reviews:
                raise EvolutionContractError("proposal review is already recorded")
            _validate_review(
                review,
                proposal,
                episode,
                evaluations,
                tuple(proposal_evaluation_ids.get(proposal.proposal_id, ())),
            )
            reviews[proposal.proposal_id] = review
            statuses[proposal.proposal_id] = (
                "reviewed_qualified"
                if review.verdict is ReviewVerdict.QUALIFIED
                else "rejected"
            )

        elif event.kind is EvolutionEventKind.CANARY_STARTED:
            rollout = EvolutionRolloutDecision.from_payload(event.payload)
            _require_qualified_review(rollout, episode, reviews)
            _proposal(rollout.proposal_id, proposals)
            _require_status(statuses, rollout.proposal_id, {"reviewed_qualified"})
            statuses[rollout.proposal_id] = "canary_active"
            canary_samples[rollout.proposal_id] = 0

        elif event.kind is EvolutionEventKind.CANARY_OBSERVED:
            proposal_id, passed, samples = _validate_canary_observation(
                event.payload, episode, reviews, proposals, statuses
            )
            canary_samples[proposal_id] += samples
            if not passed:
                statuses[proposal_id] = "canary_failed"
            elif canary_samples[proposal_id] >= episode.policy.minimum_canary_samples:
                statuses[proposal_id] = "canary_passed"

        elif event.kind in {
            EvolutionEventKind.PROMOTED,
            EvolutionEventKind.ROLLED_BACK,
            EvolutionEventKind.REJECTED,
        }:
            rollout = EvolutionRolloutDecision.from_payload(event.payload)
            _require_qualified_review(rollout, episode, reviews)
            _proposal(rollout.proposal_id, proposals)
            expected_status = {
                EvolutionEventKind.PROMOTED: "canary_passed",
                EvolutionEventKind.ROLLED_BACK: {"canary_failed", "promoted"},
                EvolutionEventKind.REJECTED: "reviewed_qualified",
            }[event.kind]
            result_status = {
                EvolutionEventKind.PROMOTED: "promoted",
                EvolutionEventKind.ROLLED_BACK: "rolled_back",
                EvolutionEventKind.REJECTED: "rejected",
            }[event.kind]
            allowed_statuses = (
                expected_status if isinstance(expected_status, set) else {expected_status}
            )
            _require_status(statuses, rollout.proposal_id, allowed_statuses)
            statuses[rollout.proposal_id] = result_status

        elif event.kind is EvolutionEventKind.EPISODE_STOPPED:
            reason = _validate_stop_event(
                event.payload, episode, event_time, set(source_ids[:-1])
            )
            if (
                required is not None
                and reason != required
                and reason not in {"safety", "user"}
            ):
                raise EvolutionContractError("stop reason drifted")
            persisted = reason

        else:
            raise EvolutionContractError("evolution event is unsupported")

        if persisted is None:
            if budget >= episode.policy.total_budget_units:
                required = "budget"
            elif no_improvement >= episode.policy.max_no_improvement_cycles:
                required = "no_improvement"

    if persisted is None and observed_at is not None:
        observed_time = _time(_timestamp(observed_at, "observed_at"))
        if observed_time > _time(episode.policy.deadline_at) and required is None:
            required = "deadline"

    return EvolutionEpisodeProjection(
        episode_id=episode.episode_id,
        project_id=project_id,
        target_kind=episode.target_kind,
        policy=episode.policy,
        current_generation=current_generation,
        candidate_count=len(proposals),
        evaluation_count=len(evaluations),
        budget_used=budget,
        stop_required_reason=None if persisted is not None else required,
        persisted_stop_reason=persisted,
        is_terminal=persisted is not None,
        proposal_statuses=MappingProxyType(dict(statuses)),
        canary_active_ids=_ids_with_status(statuses, "canary_active"),
        canary_passed_ids=_ids_with_status(statuses, "canary_passed"),
        invalidated_proposal_ids=_ids_with_status(statuses, "invalidated"),
        rolled_back_proposal_ids=_ids_with_status(statuses, "rolled_back"),
        rejected_proposal_ids=_ids_with_status(statuses, "rejected"),
        promoted_proposal_ids=_ids_with_status(statuses, "promoted"),
        source_event_ids=tuple(source_ids),
    )


def _validate_proposal(
    proposal: EvolutionProposal,
    episode: EvolutionEpisode,
    proposals: Mapping[str, EvolutionProposal],
    generations: Mapping[int, int],
    statuses: Mapping[str, str],
    current_generation: int,
) -> None:
    if (
        proposal.episode_id != episode.episode_id
        or proposal.target_kind is not episode.target_kind
        or proposal.proposal_id in proposals
    ):
        raise EvolutionContractError("proposal identity or scope drifted")
    if proposal.generation > episode.policy.max_generations:
        raise EvolutionContractError("max_generations requires stop")
    if (
        generations.get(proposal.generation, 0)
        >= episode.policy.max_candidates_per_generation
    ):
        raise EvolutionContractError("max_candidates_per_generation requires stop")
    if proposal.generation > current_generation + 1:
        raise EvolutionContractError("proposal generation skipped")
    if proposal.generation == 1:
        if proposal.parent_proposal_id is not None:
            raise EvolutionContractError("generation one cannot have parent")
        return
    parent = _proposal(proposal.parent_proposal_id, proposals)
    if proposal.generation != parent.generation + 1:
        raise EvolutionContractError("proposal generation parent drifted")
    if statuses.get(parent.proposal_id) not in {
        "unqualified",
        "inconclusive",
        "invalidated",
        "rejected",
        "rolled_back",
    }:
        raise EvolutionContractError("proposal parent is not eligible")


def _validate_evaluation(
    evaluation: EvolutionEvaluation,
    proposal: EvolutionProposal,
    episode: EvolutionEpisode,
    prior_count: int,
) -> None:
    if (
        evaluation.episode_id != episode.episode_id
        or evaluation.proposal_id != proposal.proposal_id
        or evaluation.evaluator_id in {proposal.proposer_id, proposal.executor_id}
    ):
        raise EvolutionContractError("evaluation scope or identity is invalid")
    if prior_count >= episode.policy.max_evaluations_per_candidate:
        raise EvolutionContractError("max_evaluations_per_candidate requires stop")
    gain = evaluation.candidate_score - evaluation.baseline_score
    if (
        evaluation.verdict is EvaluationVerdict.QUALIFIED
        and gain < episode.policy.minimum_improvement
    ):
        raise EvolutionContractError("qualified evaluation misses minimum improvement")
    if (
        evaluation.verdict is EvaluationVerdict.UNQUALIFIED
        and gain >= episode.policy.minimum_improvement
    ):
        raise EvolutionContractError("unqualified evaluation contradicts improvement")


def _evaluation_status(evaluations: tuple[EvolutionEvaluation, ...]) -> str:
    verdicts = {item.verdict for item in evaluations}
    if EvaluationVerdict.INVALIDATED in verdicts:
        return "invalidated"
    if EvaluationVerdict.UNQUALIFIED in verdicts:
        return "unqualified"
    if EvaluationVerdict.INCONCLUSIVE in verdicts:
        return "inconclusive"
    return "evaluated_qualified"


def _validate_review(
    review: EvolutionReview,
    proposal: EvolutionProposal,
    episode: EvolutionEpisode,
    evaluations: Mapping[str, EvolutionEvaluation],
    expected_evaluation_ids: tuple[str, ...],
) -> None:
    if (
        review.episode_id != episode.episode_id
        or review.proposal_id != proposal.proposal_id
    ):
        raise EvolutionContractError("review scope drifted")
    if set(review.evaluation_ids) != set(expected_evaluation_ids):
        raise EvolutionContractError("review must bind every proposal evaluation")
    selected = tuple(evaluations[item] for item in review.evaluation_ids)
    identities = {proposal.proposer_id, proposal.executor_id}
    identities.update(item.evaluator_id for item in selected)
    if review.reviewer_id in identities:
        raise EvolutionContractError("reviewer identity must be independent")
    if review.verdict is ReviewVerdict.QUALIFIED:
        if any(item.verdict is not EvaluationVerdict.QUALIFIED for item in selected):
            raise EvolutionContractError("review requires qualified evaluations")
        bindings = {
            (
                item.metric_set_revision,
                item.evaluation_input_ref,
                item.evaluation_input_revision,
                item.baseline_result_ref,
            )
            for item in selected
        }
        if len(bindings) != 1:
            raise EvolutionContractError(
                "review evaluations require the same metric, input, and baseline"
            )


def _require_qualified_review(
    rollout: EvolutionRolloutDecision,
    episode: EvolutionEpisode,
    reviews: Mapping[str, EvolutionReview],
) -> None:
    review = reviews.get(rollout.proposal_id)
    if (
        rollout.episode_id != episode.episode_id
        or review is None
        or review.verdict is not ReviewVerdict.QUALIFIED
    ):
        raise EvolutionContractError("rollout requires qualified review")


def _validate_canary_observation(
    payload: Mapping[str, object],
    episode: EvolutionEpisode,
    reviews: Mapping[str, EvolutionReview],
    proposals: Mapping[str, EvolutionProposal],
    statuses: Mapping[str, str],
) -> tuple[str, bool, int]:
    expected = {"proposal_id", "passed", "samples", "evidence_refs"}
    if set(payload) != expected:
        raise EvolutionContractError("canary observation payload is invalid")
    proposal_id = _id(payload.get("proposal_id"), "proposal id")
    _proposal(proposal_id, proposals)
    _require_status(statuses, proposal_id, {"canary_active"})
    review = reviews.get(proposal_id)
    if review is None or review.verdict is not ReviewVerdict.QUALIFIED:
        raise EvolutionContractError("canary requires qualified review")
    passed = payload.get("passed")
    samples = payload.get("samples")
    if (
        not isinstance(passed, bool)
        or not isinstance(samples, int)
        or isinstance(samples, bool)
        or samples < 1
    ):
        raise EvolutionContractError("canary observation is invalid")
    _refs(payload.get("evidence_refs"))
    return proposal_id, passed, samples


def _validate_stop_event(
    payload: Mapping[str, object],
    episode: EvolutionEpisode,
    recorded_at: datetime,
    known_event_ids: set[str],
) -> str:
    expected = {
        "stop_id",
        "reason",
        "evidence_refs",
        "trigger_event_ids",
        "observed_at",
    }
    if set(payload) != expected:
        raise EvolutionContractError("stop payload is invalid")
    _id(payload.get("stop_id"), "stop id")
    reason = payload.get("reason")
    if reason not in _STOP_REASONS:
        raise EvolutionContractError("stop reason is invalid")
    _refs(payload.get("evidence_refs"))
    triggers = _identifier_tuple(payload.get("trigger_event_ids"), "trigger event id")
    if not triggers or not set(triggers).issubset(known_event_ids):
        raise EvolutionContractError("stop trigger event ids are invalid")
    observed_at = _time(_timestamp(payload.get("observed_at"), "stop observed_at"))
    if observed_at > recorded_at:
        raise EvolutionContractError("stop observed_at follows recorded_at")
    if reason == "deadline" and observed_at < _time(episode.policy.deadline_at):
        raise EvolutionContractError("deadline stop observed before deadline")
    return str(reason)


def _validate_event_payload(
    event_id: str,
    kind: EvolutionEventKind,
    episode_id: str,
    payload: Mapping[str, object],
) -> None:
    if kind is EvolutionEventKind.EPISODE_CREATED:
        EvolutionEpisode.from_payload(episode_id, payload)
        expected_id = f"{episode_id}.created"
    elif kind is EvolutionEventKind.PROPOSAL_RECORDED:
        proposal = EvolutionProposal.from_payload(payload)
        if proposal.episode_id != episode_id:
            raise EvolutionContractError("proposal event scope drifted")
        expected_id = f"{proposal.proposal_id}.proposed"
    elif kind is EvolutionEventKind.EVALUATION_RECORDED:
        evaluation = EvolutionEvaluation.from_payload(payload)
        if evaluation.episode_id != episode_id:
            raise EvolutionContractError("evaluation event scope drifted")
        expected_id = evaluation.evaluation_id
    elif kind is EvolutionEventKind.REVIEW_RECORDED:
        review = EvolutionReview.from_payload(payload)
        if review.episode_id != episode_id:
            raise EvolutionContractError("review event scope drifted")
        expected_id = review.review_id
    elif kind in {
        EvolutionEventKind.CANARY_STARTED,
        EvolutionEventKind.PROMOTED,
        EvolutionEventKind.ROLLED_BACK,
        EvolutionEventKind.REJECTED,
    }:
        rollout = EvolutionRolloutDecision.from_payload(payload)
        if rollout.episode_id != episode_id:
            raise EvolutionContractError("rollout event scope drifted")
        suffix = {
            EvolutionEventKind.CANARY_STARTED: ".canary",
            EvolutionEventKind.PROMOTED: ".promoted",
            EvolutionEventKind.ROLLED_BACK: ".rolled-back",
            EvolutionEventKind.REJECTED: ".rejected",
        }[kind]
        expected_id = f"{rollout.decision_id}{suffix}"
    elif kind is EvolutionEventKind.CANARY_OBSERVED:
        expected = {"proposal_id", "passed", "samples", "evidence_refs"}
        if set(payload) != expected:
            raise EvolutionContractError("canary observation payload is invalid")
        _id(payload.get("proposal_id"), "proposal id")
        passed = payload.get("passed")
        samples = payload.get("samples")
        if (
            not isinstance(passed, bool)
            or not isinstance(samples, int)
            or isinstance(samples, bool)
            or samples < 1
        ):
            raise EvolutionContractError("canary observation is invalid")
        _refs(payload.get("evidence_refs"))
        expected_id = event_id
    elif kind is EvolutionEventKind.EPISODE_STOPPED:
        expected = {
            "stop_id",
            "reason",
            "evidence_refs",
            "trigger_event_ids",
            "observed_at",
        }
        if set(payload) != expected:
            raise EvolutionContractError("stop payload is invalid")
        stop_id = _id(payload.get("stop_id"), "stop id")
        if payload.get("reason") not in _STOP_REASONS:
            raise EvolutionContractError("stop payload is invalid")
        _refs(payload.get("evidence_refs"))
        _identifier_tuple(payload.get("trigger_event_ids"), "trigger event id")
        _timestamp(payload.get("observed_at"), "stop observed_at")
        expected_id = f"{episode_id}.{stop_id}.stopped"
    else:
        raise EvolutionContractError("evolution event is unsupported")
    if event_id != expected_id:
        raise EvolutionContractError("evolution event identity drifted")


def _identifier_tuple(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise EvolutionContractError(f"{label}s are invalid")
    try:
        values = tuple(_id(item, label) for item in value)  # type: ignore[union-attr]
    except TypeError as error:
        raise EvolutionContractError(f"{label}s are invalid") from error
    if len(values) != len(set(values)):
        raise EvolutionContractError(f"{label}s are invalid")
    return values


def _proposal(
    proposal_id: object, proposals: Mapping[str, EvolutionProposal]
) -> EvolutionProposal:
    value = proposals.get(_id(proposal_id, "proposal id"))
    if value is None:
        raise EvolutionContractError("evolution proposal is unknown")
    return value


def _require_status(
    statuses: Mapping[str, str],
    proposal_id: str,
    allowed: set[str] | frozenset[str],
) -> None:
    status = statuses.get(proposal_id)
    if status not in allowed or (
        status in _PROPOSAL_TERMINAL and not (status == "promoted" and status in allowed)
    ):
        raise EvolutionContractError("proposal lifecycle transition is invalid")


def _validate_budget(used: int, addition: int, episode: EvolutionEpisode) -> None:
    if used + addition > episode.policy.total_budget_units:
        raise EvolutionContractError("evolution budget requires stop")


def _ids_with_status(statuses: Mapping[str, str], expected: str) -> tuple[str, ...]:
    return tuple(sorted(key for key, value in statuses.items() if value == expected))


def evolution_event_world_identity(event: EvolutionEvent) -> tuple[str, str, str]:
    if not isinstance(event, EvolutionEvent):
        raise EvolutionContractError("evolution event is invalid")
    return (
        f"evolution.{event.episode_id}.{event.event_id}",
        f"crp://recursive-evolution/{event.episode_id}/{event.event_id}",
        "1",
    )
