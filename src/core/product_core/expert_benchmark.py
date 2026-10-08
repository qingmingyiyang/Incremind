"""Offline, frozen Expert A/B benchmark contract.

This is deliberately a scorer, not an execution path.  It accepts only
non-content metadata and already-observed numeric measurements.  In
particular it cannot call a provider, load a user Session, retain a prompt or
output, or change an Expert binding.  Fixture evidence is therefore useful
only for checking experimental discipline; it never authorizes an Expert.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re


_EXPERT_ID = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_IDENTITY = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}$")
_DECODER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:/=-]{0,127}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SCHEMA_VERSION = "1.0.0"
_FIXTURE_DATA_CLASS = "synthetic_non_personal"


class ExpertBenchmarkError(ValueError):
    """Raised when a benchmark can no longer support a fair comparison."""


@dataclass(frozen=True, slots=True)
class FrozenModelPrice:
    """A published, frozen per-token price for one benchmark model identity."""

    input_per_token: float
    output_per_token: float
    currency: str
    price_revision: str
    source_revision: str

    def __post_init__(self) -> None:
        _positive_number(self.input_per_token, "input_per_token")
        _positive_number(self.output_per_token, "output_per_token")
        if not isinstance(self.currency, str) or not _CURRENCY.fullmatch(self.currency):
            raise ExpertBenchmarkError("expert benchmark price currency is invalid")
        _identity(self.price_revision, "price_revision")
        _identity(self.source_revision, "source_revision")


@dataclass(frozen=True, slots=True)
class FrozenExpertBenchmarkCase:
    """The invariant experimental envelope for exactly one A/B comparison."""

    case_id: str
    route_id: str
    boundary_revision: str
    model_id: str
    decoder: str
    tool_budget: int
    rubric_revision: str
    treatment_expert_id: str
    price: FrozenModelPrice | None = None

    def __post_init__(self) -> None:
        _identity(self.case_id, "case_id")
        _identity(self.route_id, "route_id")
        _identity(self.boundary_revision, "boundary_revision")
        _identity(self.model_id, "model_id")
        _decoder(self.decoder)
        _tool_budget(self.tool_budget)
        _identity(self.rubric_revision, "rubric_revision")
        _expert_id(self.treatment_expert_id)
        if self.price is not None and not isinstance(self.price, FrozenModelPrice):
            raise ExpertBenchmarkError("expert benchmark price is invalid")


@dataclass(frozen=True, slots=True)
class ExpertArmObservation:
    """One numeric observation.  Prompt, output and user/session IDs are absent."""

    case_id: str
    arm: str
    expert_id: str | None
    route_id: str
    boundary_revision: str
    model_id: str
    decoder: str
    tool_budget: int
    rubric_revision: str
    quality_score: float
    input_tokens: int
    output_tokens: int
    latency_ms: float

    def __post_init__(self) -> None:
        _identity(self.case_id, "case_id")
        if self.arm not in {"baseline", "treatment"}:
            raise ExpertBenchmarkError("expert benchmark arm is invalid")
        if self.expert_id is not None:
            _expert_id(self.expert_id)
        _identity(self.route_id, "route_id")
        _identity(self.boundary_revision, "boundary_revision")
        _identity(self.model_id, "model_id")
        _decoder(self.decoder)
        _tool_budget(self.tool_budget)
        _identity(self.rubric_revision, "rubric_revision")
        _quality_score(self.quality_score)
        _tokens_value(self.input_tokens, "input_tokens")
        _tokens_value(self.output_tokens, "output_tokens")
        _number(self.latency_ms, "latency_ms")


@dataclass(frozen=True, slots=True)
class ExpertBenchmarkCorpus:
    version: str
    data_class: str
    cases: tuple[FrozenExpertBenchmarkCase, ...]
    observations: tuple[ExpertArmObservation, ...]

    def __post_init__(self) -> None:
        _identity(self.version, "version")
        if self.data_class != _FIXTURE_DATA_CLASS:
            raise ExpertBenchmarkError("expert benchmark corpus must be synthetic_non_personal")
        if not 1 <= len(self.cases) <= 1000 or not 2 <= len(self.observations) <= 2000:
            raise ExpertBenchmarkError("expert benchmark corpus size is invalid")


def load_expert_benchmark_corpus(path: Path) -> ExpertBenchmarkCorpus:
    """Load a bounded synthetic fixture; production/user data is rejected."""
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ExpertBenchmarkError("expert benchmark corpus is unavailable") from error
    if len(raw) > 512 * 1024:
        raise ExpertBenchmarkError("expert benchmark corpus is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExpertBenchmarkError("expert benchmark corpus is invalid") from error
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version", "version", "data_class", "cases", "observations"
    }:
        raise ExpertBenchmarkError("expert benchmark corpus schema is invalid")
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ExpertBenchmarkError("expert benchmark schema version is unsupported")
    if payload.get("data_class") != _FIXTURE_DATA_CLASS:
        raise ExpertBenchmarkError("expert benchmark corpus must be synthetic_non_personal")
    version = _identity(payload.get("version"), "version")
    cases = _cases(payload.get("cases"))
    observations = _observations(payload.get("observations"), cases=cases)
    return ExpertBenchmarkCorpus(
        version=version,
        data_class=_FIXTURE_DATA_CLASS,
        cases=cases,
        observations=observations,
    )


def evaluate_expert_ab_case(
    case: FrozenExpertBenchmarkCase,
    *,
    baseline: ExpertArmObservation,
    treatment: ExpertArmObservation,
) -> dict[str, object]:
    """Score a fair, frozen comparison without making an activation claim."""
    _validate_observation(case, baseline, expected_arm="baseline", expected_expert=None)
    _validate_observation(
        case,
        treatment,
        expected_arm="treatment",
        expected_expert=case.treatment_expert_id,
    )
    baseline_cost = _cost(case.price, baseline)
    treatment_cost = _cost(case.price, treatment)
    cost_available = baseline_cost is not None and treatment_cost is not None
    return {
        "schema_version": _SCHEMA_VERSION,
        "case_id": case.case_id,
        "comparison": {
            "baseline_expert_id": None,
            "treatment_expert_id": case.treatment_expert_id,
            "route_id": case.route_id,
            "boundary_revision": case.boundary_revision,
            "model_id": case.model_id,
            "decoder": case.decoder,
            "tool_budget": case.tool_budget,
            "rubric_revision": case.rubric_revision,
        },
        "deltas": {
            "quality_score": _round(treatment.quality_score - baseline.quality_score),
            "input_tokens": treatment.input_tokens - baseline.input_tokens,
            "output_tokens": treatment.output_tokens - baseline.output_tokens,
            "total_tokens": _tokens(treatment) - _tokens(baseline),
            "latency_ms": _round(treatment.latency_ms - baseline.latency_ms),
            "cost": (
                _round(treatment_cost - baseline_cost)
                if cost_available else None
            ),
        },
        "cost": {
            "available": cost_available,
            "currency": case.price.currency if case.price else None,
            "price_revision": case.price.price_revision if case.price else None,
            "source_revision": case.price.source_revision if case.price else None,
            "baseline": _round(baseline_cost) if baseline_cost is not None else None,
            "treatment": _round(treatment_cost) if treatment_cost is not None else None,
        },
        "governance": {
            "provider_called": False,
            "network_used": False,
            "user_session_read": False,
            "prompt_or_output_recorded": False,
            "expert_binding_changed": False,
            "benefit_claimable": False,
            "status": "inconclusive",
            "activation": "disabled",
            "reason": "offline_observations_cannot_authorize_expert_activation",
        },
    }


def evaluate_expert_benchmark_corpus(corpus: ExpertBenchmarkCorpus) -> dict[str, object]:
    """Aggregate a fixture corpus.  Results remain lab-only and inconclusive."""
    if not isinstance(corpus, ExpertBenchmarkCorpus):
        raise ExpertBenchmarkError("expert benchmark corpus is invalid")
    if corpus.data_class != _FIXTURE_DATA_CLASS:
        raise ExpertBenchmarkError("expert benchmark corpus must be synthetic_non_personal")
    case_ids = [case.case_id for case in corpus.cases]
    if len(case_ids) != len(set(case_ids)):
        raise ExpertBenchmarkError("expert benchmark case IDs must be unique")
    by_case: dict[str, dict[str, ExpertArmObservation]] = {}
    for observation in corpus.observations:
        arms = by_case.setdefault(observation.case_id, {})
        if observation.arm in arms:
            raise ExpertBenchmarkError("expert benchmark arm observation is duplicated")
        arms[observation.arm] = observation
    if set(by_case) != set(case_ids):
        raise ExpertBenchmarkError("expert benchmark observations do not match cases")
    results: list[dict[str, object]] = []
    for case in corpus.cases:
        arms = by_case.get(case.case_id, {})
        if set(arms) != {"baseline", "treatment"}:
            raise ExpertBenchmarkError("expert benchmark case requires both arms")
        results.append(evaluate_expert_ab_case(
            case, baseline=arms["baseline"], treatment=arms["treatment"]
        ))
    return {
        "schema_version": _SCHEMA_VERSION,
        "corpus_version": corpus.version,
        "evaluation_data": corpus.data_class,
        "case_count": len(results),
        "aggregate": {
            "quality_score_delta": _mean(result["deltas"]["quality_score"] for result in results),
            "total_token_delta": _mean(result["deltas"]["total_tokens"] for result in results),
            "latency_ms_delta": _mean(result["deltas"]["latency_ms"] for result in results),
            "cost_available": all(result["cost"]["available"] is True for result in results),
        },
        "results": results,
        "recommendation": {
            "status": "inconclusive",
            "benefit_claimable": False,
            "activation": "disabled",
            "reason": "synthetic_offline_fixture_is_not_production_evidence",
        },
    }


def _cases(value: object) -> tuple[FrozenExpertBenchmarkCase, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 1000:
        raise ExpertBenchmarkError("expert benchmark cases must contain 1-1000 entries")
    result: list[FrozenExpertBenchmarkCase] = []
    identifiers: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "case_id", "route_id", "boundary_revision", "model_id", "decoder",
            "tool_budget", "rubric_revision", "treatment_expert_id", "price"
        }:
            raise ExpertBenchmarkError("expert benchmark case schema is invalid")
        case = FrozenExpertBenchmarkCase(
            case_id=_identity(item.get("case_id"), "case_id"),
            route_id=_identity(item.get("route_id"), "route_id"),
            boundary_revision=_identity(item.get("boundary_revision"), "boundary_revision"),
            model_id=_identity(item.get("model_id"), "model_id"),
            decoder=_decoder(item.get("decoder")),
            tool_budget=_tool_budget(item.get("tool_budget")),
            rubric_revision=_identity(item.get("rubric_revision"), "rubric_revision"),
            treatment_expert_id=_expert_id(item.get("treatment_expert_id")),
            price=_price(item.get("price")),
        )
        if case.case_id in identifiers:
            raise ExpertBenchmarkError("expert benchmark case IDs must be unique")
        identifiers.add(case.case_id)
        result.append(case)
    return tuple(result)


def _observations(
    value: object, *, cases: Sequence[FrozenExpertBenchmarkCase]
) -> tuple[ExpertArmObservation, ...]:
    if not isinstance(value, list) or not 2 <= len(value) <= 2000:
        raise ExpertBenchmarkError("expert benchmark observations are invalid")
    known = {case.case_id for case in cases}
    result: list[ExpertArmObservation] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "case_id", "arm", "expert_id", "route_id", "boundary_revision", "model_id",
            "decoder", "tool_budget", "rubric_revision", "quality_score", "input_tokens", "output_tokens", "latency_ms"
        }:
            raise ExpertBenchmarkError("expert benchmark observation schema is invalid")
        case_id = _identity(item.get("case_id"), "case_id")
        if case_id not in known:
            raise ExpertBenchmarkError("expert benchmark observation references an unknown case")
        arm = item.get("arm")
        if arm not in {"baseline", "treatment"}:
            raise ExpertBenchmarkError("expert benchmark arm is invalid")
        expert = item.get("expert_id")
        if expert is not None:
            expert = _expert_id(expert)
        result.append(ExpertArmObservation(
            case_id=case_id, arm=str(arm), expert_id=expert,
            route_id=_identity(item.get("route_id"), "route_id"),
            boundary_revision=_identity(item.get("boundary_revision"), "boundary_revision"),
            model_id=_identity(item.get("model_id"), "model_id"),
            decoder=_decoder(item.get("decoder")), tool_budget=_tool_budget(item.get("tool_budget")),
            rubric_revision=_identity(item.get("rubric_revision"), "rubric_revision"),
            quality_score=_quality_score(item.get("quality_score")),
            input_tokens=_tokens_value(item.get("input_tokens"), "input_tokens"),
            output_tokens=_tokens_value(item.get("output_tokens"), "output_tokens"),
            latency_ms=_number(item.get("latency_ms"), "latency_ms"),
        ))
    return tuple(result)


def _validate_observation(
    case: FrozenExpertBenchmarkCase, observation: ExpertArmObservation, *,
    expected_arm: str, expected_expert: str | None,
) -> None:
    if observation.case_id != case.case_id or observation.arm != expected_arm:
        raise ExpertBenchmarkError("expert benchmark observation arm identity drifted")
    if observation.expert_id != expected_expert:
        raise ExpertBenchmarkError("expert benchmark differs by more than explicit Expert")
    if (
        observation.route_id != case.route_id
        or observation.boundary_revision != case.boundary_revision
        or observation.model_id != case.model_id
        or observation.decoder != case.decoder
        or observation.tool_budget != case.tool_budget
        or observation.rubric_revision != case.rubric_revision
    ):
        raise ExpertBenchmarkError("expert benchmark frozen envelope drifted")


def _price(value: object) -> FrozenModelPrice | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "input_per_token", "output_per_token", "currency", "price_revision",
        "source_revision",
    }:
        raise ExpertBenchmarkError("expert benchmark price schema is invalid")
    return FrozenModelPrice(
        input_per_token=_positive_number(value.get("input_per_token"), "input_per_token"),
        output_per_token=_positive_number(value.get("output_per_token"), "output_per_token"),
        currency=str(value.get("currency")),
        price_revision=_identity(value.get("price_revision"), "price_revision"),
        source_revision=_identity(value.get("source_revision"), "source_revision"),
    )


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ExpertBenchmarkError(f"expert benchmark {field} is invalid")
    return value


def _decoder(value: object) -> str:
    if not isinstance(value, str) or not _DECODER.fullmatch(value):
        raise ExpertBenchmarkError("expert benchmark decoder is invalid")
    return value


def _expert_id(value: object) -> str:
    if not isinstance(value, str) or not _EXPERT_ID.fullmatch(value):
        raise ExpertBenchmarkError("expert benchmark treatment_expert_id is invalid")
    return value


def _tool_budget(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 12:
        raise ExpertBenchmarkError("expert benchmark tool_budget must be between 0 and 12")
    return value


def _tokens_value(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ExpertBenchmarkError(f"expert benchmark {field} is invalid")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExpertBenchmarkError(f"expert benchmark {field} is invalid")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ExpertBenchmarkError(f"expert benchmark {field} is invalid")
    return number


def _positive_number(value: object, field: str) -> float:
    number = _number(value, field)
    if number <= 0:
        raise ExpertBenchmarkError(f"expert benchmark {field} must be positive")
    return number


def _quality_score(value: object) -> float:
    score = _number(value, "quality_score")
    if score > 1:
        raise ExpertBenchmarkError("expert benchmark quality_score must be between 0 and 1")
    return score


def _tokens(observation: ExpertArmObservation) -> int:
    return observation.input_tokens + observation.output_tokens


def _cost(price: FrozenModelPrice | None, observation: ExpertArmObservation) -> float | None:
    if price is None:
        return None
    return (observation.input_tokens * price.input_per_token) + (
        observation.output_tokens * price.output_per_token
    )


def _mean(values: Sequence[object]) -> float:
    numbers = [float(item) for item in values]
    return _round(sum(numbers) / len(numbers)) if numbers else 0.0


def _round(value: float) -> float:
    return round(value, 6)
