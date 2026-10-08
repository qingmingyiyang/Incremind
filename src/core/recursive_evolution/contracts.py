"""Immutable contracts for governed recursive evolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class EvolutionContractError(ValueError):
    """Raised when an evolution fact weakens a frozen policy boundary."""


class EvolutionTargetKind(StrEnum):
    PROMPT_STRATEGY = "prompt_strategy"
    PROJECT_SKILL = "project_skill"
    AGENT_PROFILE = "agent_profile"
    SCHEDULER_POLICY = "scheduler_policy"


class EvaluationVerdict(StrEnum):
    QUALIFIED = "qualified"
    UNQUALIFIED = "unqualified"
    INCONCLUSIVE = "inconclusive"
    INVALIDATED = "invalidated"


class ReviewVerdict(StrEnum):
    QUALIFIED = "qualified"
    REJECTED = "rejected"


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise EvolutionContractError(f"{label} is invalid")
    if any(char.isspace() for char in value):
        raise EvolutionContractError(f"{label} is invalid")
    return value


def _ref(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("crp://"):
        raise EvolutionContractError(f"{label} is invalid")
    return value


def _positive(value: object, label: str, *, allow_zero: bool = False) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < (0 if allow_zero else 1)
    ):
        raise EvolutionContractError(f"{label} is invalid")
    return value


def _score(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0 <= value <= 1
    ):
        raise EvolutionContractError(f"{label} is invalid")
    return float(value)


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvolutionContractError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvolutionContractError(f"{label} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise EvolutionContractError(f"{label} is invalid")
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _refs(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise EvolutionContractError("evidence refs are invalid")
    try:
        refs = tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise EvolutionContractError("evidence refs are invalid") from error
    if not refs:
        raise EvolutionContractError("evidence refs are invalid")
    normalized = tuple(_ref(item, "evidence ref") for item in refs)
    if len(normalized) != len(set(normalized)):
        raise EvolutionContractError("evidence refs are invalid")
    return normalized


def _payload(cls: type, value: object, label: str) -> dict[str, object]:
    fields = set(cls.__dataclass_fields__)
    if not isinstance(value, Mapping) or set(value) != fields:
        raise EvolutionContractError(f"{label} payload is invalid")
    return dict(value)


@dataclass(frozen=True, slots=True)
class EvolutionPolicy:
    max_generations: int
    max_candidates_per_generation: int
    max_evaluations_per_candidate: int
    total_budget_units: int
    max_no_improvement_cycles: int
    deadline_at: str
    minimum_improvement: float = 0.01
    minimum_canary_samples: int = 1
    requires_user_confirmation: bool = True
    auto_promote_allowed: bool = False
    canary_required: bool = True

    def __post_init__(self) -> None:
        for field in (
            "max_generations",
            "max_candidates_per_generation",
            "max_evaluations_per_candidate",
            "total_budget_units",
            "max_no_improvement_cycles",
            "minimum_canary_samples",
        ):
            object.__setattr__(self, field, _positive(getattr(self, field), field))
        object.__setattr__(self, "deadline_at", _timestamp(self.deadline_at, "policy deadline"))
        object.__setattr__(
            self,
            "minimum_improvement",
            _score(self.minimum_improvement, "minimum improvement"),
        )
        if self.requires_user_confirmation is not True:
            raise EvolutionContractError("policy requires user confirmation")
        if self.auto_promote_allowed is not False:
            raise EvolutionContractError("policy forbids auto promotion")
        if self.canary_required is not True:
            raise EvolutionContractError("policy requires canary")

    def to_payload(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}

    @classmethod
    def from_payload(cls, value: object) -> "EvolutionPolicy":
        return cls(**_payload(cls, value, "policy"))


@dataclass(frozen=True, slots=True)
class EvolutionEpisode:
    episode_id: str
    project_id: str
    target_kind: EvolutionTargetKind
    policy: EvolutionPolicy

    def __post_init__(self) -> None:
        object.__setattr__(self, "episode_id", _id(self.episode_id, "episode id"))
        object.__setattr__(self, "project_id", _id(self.project_id, "project id"))
        try:
            target_kind = EvolutionTargetKind(self.target_kind)
        except (TypeError, ValueError) as error:
            raise EvolutionContractError("target kind is invalid") from error
        object.__setattr__(self, "target_kind", target_kind)
        if not isinstance(self.policy, EvolutionPolicy):
            raise EvolutionContractError("episode policy is invalid")

    def to_payload(self) -> dict[str, object]:
        return {"project_id": self.project_id, "target_kind": self.target_kind.value, "policy": self.policy.to_payload()}

    @classmethod
    def from_payload(cls, episode_id: object, value: object) -> "EvolutionEpisode":
        if not isinstance(value, Mapping) or set(value) != {"project_id", "target_kind", "policy"}:
            raise EvolutionContractError("episode payload is invalid")
        return cls(
            _id(episode_id, "episode id"),
            value["project_id"],
            value["target_kind"],
            EvolutionPolicy.from_payload(value["policy"]),
        )


@dataclass(frozen=True, slots=True)
class EvolutionResourceEnvelope:
    capabilities: tuple[str, ...]
    budget_units: int
    max_depth: int
    max_concurrency: int
    egress: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.capabilities, (str, bytes)) or isinstance(self.egress, (str, bytes)):
            raise EvolutionContractError("resource envelope is invalid")
        try:
            capabilities, egress = tuple(self.capabilities), tuple(self.egress)
        except TypeError as error:
            raise EvolutionContractError("resource envelope is invalid") from error
        if any(not isinstance(item, str) or not item for item in capabilities + egress):
            raise EvolutionContractError("resource envelope is invalid")
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "egress", egress)
        for field in ("budget_units", "max_depth", "max_concurrency"):
            object.__setattr__(self, field, _positive(getattr(self, field), field, allow_zero=True))

    def does_not_expand(self, baseline: "EvolutionResourceEnvelope") -> bool:
        return (
            set(self.capabilities).issubset(baseline.capabilities)
            and self.budget_units <= baseline.budget_units
            and self.max_depth <= baseline.max_depth
            and self.max_concurrency <= baseline.max_concurrency
            and set(self.egress).issubset(baseline.egress)
        )

    def to_payload(self) -> dict[str, object]:
        return {"capabilities": list(self.capabilities), "budget_units": self.budget_units, "max_depth": self.max_depth, "max_concurrency": self.max_concurrency, "egress": list(self.egress)}

    @classmethod
    def from_payload(cls, value: object) -> "EvolutionResourceEnvelope":
        fields = _payload(cls, value, "resource envelope")
        return cls(
            fields["capabilities"],  # type: ignore[arg-type]
            fields["budget_units"],
            fields["max_depth"],
            fields["max_concurrency"],
            fields["egress"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class EvolutionProposal:
    proposal_id: str
    episode_id: str
    generation: int
    target_kind: EvolutionTargetKind
    proposer_id: str
    executor_id: str
    baseline_ref: str
    baseline_revision: str
    parent_proposal_id: str | None
    candidate_ref: str
    candidate_revision: str
    baseline_envelope: EvolutionResourceEnvelope
    candidate_envelope: EvolutionResourceEnvelope

    def __post_init__(self) -> None:
        for field in ("proposal_id", "episode_id", "proposer_id", "executor_id", "baseline_revision", "candidate_revision"):
            object.__setattr__(self, field, _id(getattr(self, field), field))
        object.__setattr__(self, "generation", _positive(self.generation, "generation"))
        try:
            target_kind = EvolutionTargetKind(self.target_kind)
        except (TypeError, ValueError) as error:
            raise EvolutionContractError("target kind is invalid") from error
        object.__setattr__(self, "target_kind", target_kind)
        object.__setattr__(self, "baseline_ref", _ref(self.baseline_ref, "baseline ref"))
        object.__setattr__(self, "candidate_ref", _ref(self.candidate_ref, "candidate ref"))
        if self.parent_proposal_id is not None:
            object.__setattr__(self, "parent_proposal_id", _id(self.parent_proposal_id, "parent proposal id"))
        if self.proposer_id == self.executor_id:
            raise EvolutionContractError("proposer and executor identities must differ")
        if not isinstance(
            self.baseline_envelope, EvolutionResourceEnvelope
        ) or not isinstance(self.candidate_envelope, EvolutionResourceEnvelope):
            raise EvolutionContractError("proposal resource envelope is invalid")
        if not self.candidate_envelope.does_not_expand(self.baseline_envelope):
            raise EvolutionContractError("proposal cannot expand capability, budget, depth, concurrency, or egress")

    def to_payload(self) -> dict[str, object]:
        return {"proposal_id": self.proposal_id, "episode_id": self.episode_id, "generation": self.generation, "target_kind": self.target_kind.value, "proposer_id": self.proposer_id, "executor_id": self.executor_id, "baseline_ref": self.baseline_ref, "baseline_revision": self.baseline_revision, "parent_proposal_id": self.parent_proposal_id, "candidate_ref": self.candidate_ref, "candidate_revision": self.candidate_revision, "baseline_envelope": self.baseline_envelope.to_payload(), "candidate_envelope": self.candidate_envelope.to_payload()}

    @classmethod
    def from_payload(cls, value: object) -> "EvolutionProposal":
        fields = _payload(cls, value, "proposal")
        fields["baseline_envelope"] = EvolutionResourceEnvelope.from_payload(fields["baseline_envelope"])
        fields["candidate_envelope"] = EvolutionResourceEnvelope.from_payload(fields["candidate_envelope"])
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class EvolutionEvaluation:
    evaluation_id: str
    episode_id: str
    proposal_id: str
    evaluator_id: str
    baseline_score: float
    candidate_score: float
    metric_set_revision: str
    evaluation_input_ref: str
    evaluation_input_revision: str
    baseline_result_ref: str
    candidate_result_ref: str
    actual_budget_units: int
    verdict: EvaluationVerdict
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("evaluation_id", "episode_id", "proposal_id", "evaluator_id", "metric_set_revision", "evaluation_input_revision"):
            object.__setattr__(self, field, _id(getattr(self, field), field))
        object.__setattr__(self, "baseline_score", _score(self.baseline_score, "baseline score"))
        object.__setattr__(self, "candidate_score", _score(self.candidate_score, "candidate score"))
        object.__setattr__(self, "actual_budget_units", _positive(self.actual_budget_units, "actual budget", allow_zero=True))
        for field in ("evaluation_input_ref", "baseline_result_ref", "candidate_result_ref"):
            object.__setattr__(self, field, _ref(getattr(self, field), field.replace("_", " ")))
        try:
            verdict = EvaluationVerdict(self.verdict)
        except (TypeError, ValueError) as error:
            raise EvolutionContractError("evaluation verdict is invalid") from error
        object.__setattr__(self, "verdict", verdict)
        object.__setattr__(self, "evidence_refs", _refs(self.evidence_refs))

    def to_payload(self) -> dict[str, object]:
        return {"evaluation_id": self.evaluation_id, "episode_id": self.episode_id, "proposal_id": self.proposal_id, "evaluator_id": self.evaluator_id, "baseline_score": self.baseline_score, "candidate_score": self.candidate_score, "metric_set_revision": self.metric_set_revision, "evaluation_input_ref": self.evaluation_input_ref, "evaluation_input_revision": self.evaluation_input_revision, "baseline_result_ref": self.baseline_result_ref, "candidate_result_ref": self.candidate_result_ref, "actual_budget_units": self.actual_budget_units, "verdict": self.verdict.value, "evidence_refs": list(self.evidence_refs)}

    @classmethod
    def from_payload(cls, value: object) -> "EvolutionEvaluation":
        return cls(**_payload(cls, value, "evaluation"))


@dataclass(frozen=True, slots=True)
class EvolutionReview:
    review_id: str
    episode_id: str
    proposal_id: str
    reviewer_id: str
    verdict: ReviewVerdict
    evaluation_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("review_id", "episode_id", "proposal_id", "reviewer_id"):
            object.__setattr__(self, field, _id(getattr(self, field), field))
        try:
            verdict = ReviewVerdict(self.verdict)
        except (TypeError, ValueError) as error:
            raise EvolutionContractError("review verdict is invalid") from error
        object.__setattr__(self, "verdict", verdict)
        try:
            evaluation_ids = tuple(self.evaluation_ids)
        except TypeError as error:
            raise EvolutionContractError("review evaluation ids are invalid") from error
        if not evaluation_ids:
            raise EvolutionContractError("review requires evaluation ids")
        normalized_ids = tuple(_id(item, "evaluation id") for item in evaluation_ids)
        if len(normalized_ids) != len(set(normalized_ids)):
            raise EvolutionContractError("review evaluation ids are invalid")
        object.__setattr__(self, "evaluation_ids", normalized_ids)
        object.__setattr__(self, "evidence_refs", _refs(self.evidence_refs))

    def to_payload(self) -> dict[str, object]:
        return {"review_id": self.review_id, "episode_id": self.episode_id, "proposal_id": self.proposal_id, "reviewer_id": self.reviewer_id, "verdict": self.verdict.value, "evaluation_ids": list(self.evaluation_ids), "evidence_refs": list(self.evidence_refs)}

    @classmethod
    def from_payload(cls, value: object) -> "EvolutionReview":
        return cls(**_payload(cls, value, "review"))


@dataclass(frozen=True, slots=True)
class EvolutionRolloutDecision:
    decision_id: str
    episode_id: str
    proposal_id: str
    user_id: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("decision_id", "episode_id", "proposal_id", "user_id"):
            object.__setattr__(self, field, _id(getattr(self, field), field))
        object.__setattr__(self, "evidence_refs", _refs(self.evidence_refs))

    def to_payload(self) -> dict[str, object]:
        return {"decision_id": self.decision_id, "episode_id": self.episode_id, "proposal_id": self.proposal_id, "user_id": self.user_id, "evidence_refs": list(self.evidence_refs)}

    @classmethod
    def from_payload(cls, value: object) -> "EvolutionRolloutDecision":
        return cls(**_payload(cls, value, "rollout decision"))
