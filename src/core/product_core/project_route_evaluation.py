from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import statistics
import time

from core.product_core.ai_project_route_reranker import (
    AIProjectRouteReranker,
    MIN_FUSED_MARGIN,
    MIN_FUSED_SCORE,
    MIN_PROVIDER_CONFIDENCE,
)
from core.product_core.global_project_series_router import (
    FreshProjectProjection,
    GlobalProjectRouteDecision,
    GlobalProjectSeriesRouter,
)
from core.product_core.memory_projection_builder import (
    build_r0_r1_memory_projection,
)
from core.product_core.memory_projection_contract import (
    serialize_memory_retrieval_projection,
)
from core.product_core.memory_projection_repository import (
    ProjectionReadResult,
)


_AUTHORITY_IDENTITY = "project-route-evaluation-v1:fictional"
_GENERATED_AT = "2026-07-29T00:00:00+00:00"
_EXPECTED_STATUSES = {"routed", "ambiguous", "fallback"}
_REASON_CODES = {
    "semantic_topic_match",
    "series_scope_match",
    "insufficient_context",
    "ambiguous",
}
_WEIGHT_GRID = (0.50, 0.60, 0.70, 0.75, 0.80, 0.90)


class ProjectRouteEvaluationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EvaluationSeries:
    series_id: str
    title: str
    description: str
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvaluationProject:
    project_id: str
    series: tuple[EvaluationSeries, ...]


@dataclass(frozen=True, slots=True)
class ReplayAIJudgment:
    selected_project_id: str | None
    confidence: float
    reason_code: str
    scores: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class ProjectRouteEvaluationCase:
    case_id: str
    query: str
    expected_status: str
    expected_project_id: str | None
    ai_replay: ReplayAIJudgment


@dataclass(frozen=True, slots=True)
class ProjectRouteEvaluationCorpus:
    version: str
    data_class: str
    projects: tuple[EvaluationProject, ...]
    cases: tuple[ProjectRouteEvaluationCase, ...]


@dataclass(frozen=True, slots=True)
class ProjectRouteCaseResult:
    case_id: str
    expected_status: str
    expected_project_id: str | None
    actual_status: str
    actual_project_id: str | None
    reason_code: str
    candidate_ids: tuple[str, ...]
    candidate_scores: tuple[tuple[str, float], ...]
    correct: bool
    unsafe_misselection: bool
    ai_assist_status: str
    latency_ms: float

    def to_payload(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "expected_status": self.expected_status,
            "expected_project_id": self.expected_project_id,
            "actual_status": self.actual_status,
            "actual_project_id": self.actual_project_id,
            "reason_code": self.reason_code,
            "candidate_ids": list(self.candidate_ids),
            "candidate_scores": [
                {"project_id": project_id, "score": score}
                for project_id, score in self.candidate_scores
            ],
            "correct": self.correct,
            "unsafe_misselection": self.unsafe_misselection,
            "ai_assist_status": self.ai_assist_status,
            "latency_ms": self.latency_ms,
            "query_recorded": False,
        }


@dataclass(frozen=True, slots=True)
class ProjectRouteEvaluationMetrics:
    mode: str
    case_count: int
    exact_outcome_accuracy: float
    routed_accuracy: float
    ambiguity_accuracy: float
    fallback_accuracy: float
    unsafe_misselection_rate: float
    provider_call_rate: float
    assist_acceptance_rate: float
    latency_p50_ms: float
    latency_p95_ms: float

    def to_payload(self) -> dict[str, object]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class WeightGridResult:
    deterministic_weight: float
    ai_weight: float
    exact_outcome_accuracy: float
    unsafe_misselection_rate: float

    def to_payload(self) -> dict[str, object]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }


class _Catalog:
    def __init__(self, items: Sequence[FreshProjectProjection]) -> None:
        self._items = tuple(items)

    def fresh_projections(self) -> tuple[FreshProjectProjection, ...]:
        return self._items


class _ReplayProvider:
    provider_name = "project-route-evaluation-replay"

    def __init__(
        self,
        cases: Mapping[str, ReplayAIJudgment],
    ) -> None:
        self._cases = dict(cases)
        self.call_count = 0

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        del system_prompt
        query = user_payload.get("query")
        candidates = user_payload.get("candidates")
        if not isinstance(query, str) or not isinstance(candidates, list):
            raise ProjectRouteEvaluationError("replay provider input is invalid")
        judgment = self._cases.get(query)
        if judgment is None:
            raise ProjectRouteEvaluationError(
                "replay provider has no matching case"
            )
        candidate_ids = tuple(
            str(candidate.get("project_id"))
            for candidate in candidates
            if isinstance(candidate, Mapping)
        )
        scores = dict(judgment.scores)
        selected = judgment.selected_project_id
        if selected not in candidate_ids:
            selected = None
        self.call_count += 1
        return {
            "schema_version": "1.0.0",
            "selected_project_id": selected,
            "confidence": judgment.confidence,
            "reason_code": (
                judgment.reason_code
                if selected is not None
                else "ambiguous"
            ),
            "scores": [
                {
                    "project_id": project_id,
                    "relevance": scores.get(project_id, 0.0),
                }
                for project_id in candidate_ids
            ],
        }


def load_project_route_evaluation_corpus(
    path: Path,
) -> ProjectRouteEvaluationCorpus:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ProjectRouteEvaluationError(
            "project route evaluation corpus is unavailable"
        ) from error
    if len(raw) > 2 * 1024 * 1024:
        raise ProjectRouteEvaluationError(
            "project route evaluation corpus is too large"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProjectRouteEvaluationError(
            "project route evaluation corpus is invalid"
        ) from error
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "data_class",
        "projects",
        "cases",
    }:
        raise ProjectRouteEvaluationError(
            "project route evaluation corpus schema is invalid"
        )
    version = _text(payload.get("version"), 40, "version")
    data_class = payload.get("data_class")
    if data_class != "fictional_desensitized":
        raise ProjectRouteEvaluationError(
            "project route evaluation corpus must be fictional_desensitized"
        )
    projects = _projects(payload.get("projects"))
    cases = _cases(payload.get("cases"), projects=projects)
    return ProjectRouteEvaluationCorpus(
        version=version,
        data_class=data_class,
        projects=projects,
        cases=cases,
    )


def evaluate_project_routes(
    corpus: ProjectRouteEvaluationCorpus,
    *,
    use_ai_replay: bool,
) -> tuple[
    ProjectRouteEvaluationMetrics,
    tuple[ProjectRouteCaseResult, ...],
]:
    projections = tuple(_projection(project) for project in corpus.projects)
    provider = _ReplayProvider(
        {case.query: case.ai_replay for case in corpus.cases}
    )
    reranker_factory = (
        lambda: AIProjectRouteReranker(
            provider=provider,
            provider_route=(
                "memory.project_routing:evaluation:"
                f"{corpus.version}"
            ),
        )
    ) if use_ai_replay else None
    router = GlobalProjectSeriesRouter(
        catalog=_Catalog(projections),
        reranker_factory=reranker_factory,
    )
    results: list[ProjectRouteCaseResult] = []
    for case in corpus.cases:
        started = time.perf_counter()
        decision = router.route(case.query)
        latency_ms = round(
            max(0.0, (time.perf_counter() - started) * 1000.0),
            3,
        )
        results.append(_case_result(case, decision, latency_ms))
    return (
        _metrics(
            mode="ai_replay" if use_ai_replay else "deterministic",
            results=results,
            provider_calls=provider.call_count,
        ),
        tuple(results),
    )


def compare_project_route_strategies(
    corpus: ProjectRouteEvaluationCorpus,
) -> dict[str, object]:
    baseline, baseline_cases = evaluate_project_routes(
        corpus,
        use_ai_replay=False,
    )
    assisted, assisted_cases = evaluate_project_routes(
        corpus,
        use_ai_replay=True,
    )
    weights = _evaluate_weight_grid(
        corpus=corpus,
        baseline_cases=baseline_cases,
        assisted_cases=assisted_cases,
    )
    recommended = min(
        weights,
        key=lambda item: (
            -item.exact_outcome_accuracy,
            item.unsafe_misselection_rate,
            -item.deterministic_weight,
        ),
    )
    reasons = ["real_provider_evidence_unavailable"]
    if assisted.unsafe_misselection_rate > 0:
        reasons.append("ai_replay_has_unsafe_misselection")
    if assisted.exact_outcome_accuracy < baseline.exact_outcome_accuracy:
        reasons.append("ai_replay_underperforms_deterministic_baseline")
    return {
        "schema_version": "1.0.0",
        "corpus_version": corpus.version,
        "evaluation_data": corpus.data_class,
        "production_weights_changed": False,
        "baseline": {
            "metrics": baseline.to_payload(),
            "cases": [item.to_payload() for item in baseline_cases],
        },
        "ai_replay": {
            "metrics": assisted.to_payload(),
            "cases": [item.to_payload() for item in assisted_cases],
            "evidence_class": "fixture_replay_not_real_provider",
        },
        "weight_grid": [item.to_payload() for item in weights],
        "recommendation": {
            "status": "lab_only",
            "deterministic_weight": recommended.deterministic_weight,
            "ai_weight": recommended.ai_weight,
            "reasons": reasons,
        },
        "activation": {
            "status": "deferred",
            "reasons": reasons,
        },
    }


def _projects(value: object) -> tuple[EvaluationProject, ...]:
    if not isinstance(value, list) or not 2 <= len(value) <= 64:
        raise ProjectRouteEvaluationError(
            "evaluation projects must contain 2-64 entries"
        )
    result: list[EvaluationProject] = []
    identities: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "project_id",
            "series",
        }:
            raise ProjectRouteEvaluationError(
                "evaluation project schema is invalid"
            )
        project_id = _text(item.get("project_id"), 80, "project_id")
        if project_id in identities:
            raise ProjectRouteEvaluationError(
                "evaluation project IDs must be unique"
            )
        raw_series = item.get("series")
        if not isinstance(raw_series, list) or not 1 <= len(raw_series) <= 32:
            raise ProjectRouteEvaluationError(
                "evaluation project series are invalid"
            )
        series: list[EvaluationSeries] = []
        series_ids: set[str] = set()
        for raw_item in raw_series:
            if not isinstance(raw_item, dict) or set(raw_item) != {
                "series_id",
                "title",
                "description",
                "keywords",
            }:
                raise ProjectRouteEvaluationError(
                    "evaluation series schema is invalid"
                )
            series_id = _text(
                raw_item.get("series_id"),
                120,
                "series_id",
            )
            if series_id in series_ids:
                raise ProjectRouteEvaluationError(
                    "evaluation series IDs must be unique per project"
                )
            keywords = _text_list(
                raw_item.get("keywords"),
                item_limit=80,
                count_limit=16,
                field="keywords",
            )
            series.append(
                EvaluationSeries(
                    series_id=series_id,
                    title=_text(raw_item.get("title"), 160, "title"),
                    description=_text(
                        raw_item.get("description"),
                        480,
                        "description",
                    ),
                    keywords=keywords,
                )
            )
            series_ids.add(series_id)
        result.append(
            EvaluationProject(project_id=project_id, series=tuple(series))
        )
        identities.add(project_id)
    return tuple(result)


def _cases(
    value: object,
    *,
    projects: Sequence[EvaluationProject],
) -> tuple[ProjectRouteEvaluationCase, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 1000:
        raise ProjectRouteEvaluationError(
            "evaluation cases must contain 1-1000 entries"
        )
    project_ids = {project.project_id for project in projects}
    result: list[ProjectRouteEvaluationCase] = []
    identities: set[str] = set()
    queries: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "query",
            "expected_status",
            "expected_project_id",
            "ai_replay",
        }:
            raise ProjectRouteEvaluationError(
                "evaluation case schema is invalid"
            )
        case_id = _text(item.get("id"), 80, "case id")
        query = _text(item.get("query"), 500, "query")
        expected_status = item.get("expected_status")
        expected_project_id = item.get("expected_project_id")
        if expected_status not in _EXPECTED_STATUSES:
            raise ProjectRouteEvaluationError(
                "evaluation expected status is invalid"
            )
        if expected_status == "routed":
            if expected_project_id not in project_ids:
                raise ProjectRouteEvaluationError(
                    "routed case requires a known expected project"
                )
        elif expected_project_id is not None:
            raise ProjectRouteEvaluationError(
                "non-routed case cannot declare an expected project"
            )
        if case_id in identities or query in queries:
            raise ProjectRouteEvaluationError(
                "evaluation case IDs and queries must be unique"
            )
        replay = _ai_replay(
            item.get("ai_replay"),
            project_ids=project_ids,
        )
        result.append(
            ProjectRouteEvaluationCase(
                case_id=case_id,
                query=query,
                expected_status=str(expected_status),
                expected_project_id=(
                    str(expected_project_id)
                    if expected_project_id is not None
                    else None
                ),
                ai_replay=replay,
            )
        )
        identities.add(case_id)
        queries.add(query)
    return tuple(result)


def _ai_replay(
    value: object,
    *,
    project_ids: set[str],
) -> ReplayAIJudgment:
    if not isinstance(value, dict) or set(value) != {
        "selected_project_id",
        "confidence",
        "reason_code",
        "scores",
    }:
        raise ProjectRouteEvaluationError(
            "evaluation AI replay schema is invalid"
        )
    selected = value.get("selected_project_id")
    if selected is not None and selected not in project_ids:
        raise ProjectRouteEvaluationError(
            "evaluation AI replay selected an unknown project"
        )
    confidence = _score(value.get("confidence"), "confidence")
    reason_code = value.get("reason_code")
    if reason_code not in _REASON_CODES:
        raise ProjectRouteEvaluationError(
            "evaluation AI replay reason is invalid"
        )
    raw_scores = value.get("scores")
    if not isinstance(raw_scores, dict) or set(raw_scores) != project_ids:
        raise ProjectRouteEvaluationError(
            "evaluation AI replay scores must cover all projects"
        )
    scores = tuple(
        sorted(
            (
                str(project_id),
                _score(score, "AI replay score"),
            )
            for project_id, score in raw_scores.items()
        )
    )
    return ReplayAIJudgment(
        selected_project_id=(
            str(selected) if selected is not None else None
        ),
        confidence=confidence,
        reason_code=str(reason_code),
        scores=scores,
    )


def _projection(project: EvaluationProject) -> FreshProjectProjection:
    memories = tuple(
        {
            "id": f"series-memory-{project.project_id}-{index}",
            "series_id": item.series_id,
            "title": item.title,
            "scope": "project",
            "overview": item.description,
            "scenario_ids": [],
            "source_refs": [
                {
                    "source_id": (
                        f"fictional:{project.project_id}:{index}"
                    ),
                    "locator": "evaluation:r0",
                }
            ],
            "project_ids": [project.project_id],
            "stale": False,
            "revision": 1,
            "trust_status": "user_confirmed",
            "updated_at": _GENERATED_AT,
            "tags": list(item.keywords),
        }
        for index, item in enumerate(project.series, 1)
    )
    projection = build_r0_r1_memory_projection(
        project_id=project.project_id,
        authority_identity=_AUTHORITY_IDENTITY,
        series_memories=memories,
        scenarios=(),
        atoms=(),
        project_skills=(),
        generated_at=_GENERATED_AT,
    )
    return FreshProjectProjection(
        project_id=project.project_id,
        authority_identity=_AUTHORITY_IDENTITY,
        authority_fingerprint=projection.authority_fingerprint,
        read_result=ProjectionReadResult(
            status="fresh",
            fallback_to_authority=False,
            reason_code="projection_fingerprint_current",
            projection=serialize_memory_retrieval_projection(projection),
            manifest={},
        ),
    )


def _case_result(
    case: ProjectRouteEvaluationCase,
    decision: GlobalProjectRouteDecision,
    latency_ms: float,
) -> ProjectRouteCaseResult:
    correct = (
        decision.status == case.expected_status
        and decision.selected_project_id == case.expected_project_id
    )
    unsafe = (
        decision.status == "routed"
        and (
            case.expected_status != "routed"
            or decision.selected_project_id != case.expected_project_id
        )
    )
    return ProjectRouteCaseResult(
        case_id=case.case_id,
        expected_status=case.expected_status,
        expected_project_id=case.expected_project_id,
        actual_status=decision.status,
        actual_project_id=decision.selected_project_id,
        reason_code=decision.reason_code,
        candidate_ids=tuple(
            candidate.project_id for candidate in decision.candidates
        ),
        candidate_scores=tuple(
            (candidate.project_id, candidate.score)
            for candidate in decision.candidates
        ),
        correct=correct,
        unsafe_misselection=unsafe,
        ai_assist_status=(
            decision.assist.status
            if decision.assist is not None
            else "not_invoked"
        ),
        latency_ms=latency_ms,
    )


def _metrics(
    *,
    mode: str,
    results: Sequence[ProjectRouteCaseResult],
    provider_calls: int,
) -> ProjectRouteEvaluationMetrics:
    latencies = [result.latency_ms for result in results]
    routed = [
        result for result in results
        if result.expected_status == "routed"
    ]
    ambiguous = [
        result for result in results
        if result.expected_status == "ambiguous"
    ]
    fallback = [
        result for result in results
        if result.expected_status == "fallback"
    ]
    accepted = sum(
        result.ai_assist_status == "succeeded"
        for result in results
    )
    count = len(results)
    return ProjectRouteEvaluationMetrics(
        mode=mode,
        case_count=count,
        exact_outcome_accuracy=_rate(results),
        routed_accuracy=_rate(routed),
        ambiguity_accuracy=_rate(ambiguous),
        fallback_accuracy=_rate(fallback),
        unsafe_misselection_rate=round(
            sum(result.unsafe_misselection for result in results) / count,
            6,
        ),
        provider_call_rate=round(provider_calls / count, 6),
        assist_acceptance_rate=round(accepted / count, 6),
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
    )


def _evaluate_weight_grid(
    *,
    corpus: ProjectRouteEvaluationCorpus,
    baseline_cases: Sequence[ProjectRouteCaseResult],
    assisted_cases: Sequence[ProjectRouteCaseResult],
) -> tuple[WeightGridResult, ...]:
    baseline_by_id = {item.case_id: item for item in baseline_cases}
    assisted_by_id = {item.case_id: item for item in assisted_cases}
    result: list[WeightGridResult] = []
    for deterministic_weight in _WEIGHT_GRID:
        outcomes: list[tuple[bool, bool]] = []
        for case in corpus.cases:
            baseline = baseline_by_id[case.case_id]
            assisted = assisted_by_id[case.case_id]
            actual_status = baseline.actual_status
            actual_project_id = baseline.actual_project_id
            if assisted.ai_assist_status in {"succeeded", "rejected"}:
                candidate_ids = baseline.candidate_ids
                replay_scores = dict(case.ai_replay.scores)
                deterministic_scores = dict(
                    baseline.candidate_scores
                )
                fused = sorted(
                    (
                        (
                            project_id,
                            deterministic_weight
                            * deterministic_scores[project_id]
                            + (1.0 - deterministic_weight)
                            * replay_scores[project_id],
                        )
                        for project_id in candidate_ids
                    ),
                    key=lambda item: (-item[1], item[0]),
                )
                if fused:
                    top = fused[0]
                    second = fused[1][1] if len(fused) > 1 else 0.0
                    if (
                        case.ai_replay.selected_project_id == top[0]
                        and case.ai_replay.confidence
                        >= MIN_PROVIDER_CONFIDENCE
                        and top[1] >= MIN_FUSED_SCORE
                        and top[1] - second >= MIN_FUSED_MARGIN
                    ):
                        actual_status = "routed"
                        actual_project_id = top[0]
            correct = (
                actual_status == case.expected_status
                and actual_project_id == case.expected_project_id
            )
            unsafe = (
                actual_status == "routed"
                and (
                    case.expected_status != "routed"
                    or actual_project_id != case.expected_project_id
                )
            )
            outcomes.append((correct, unsafe))
        result.append(
            WeightGridResult(
                deterministic_weight=deterministic_weight,
                ai_weight=round(1.0 - deterministic_weight, 2),
                exact_outcome_accuracy=round(
                    sum(correct for correct, _ in outcomes)
                    / len(outcomes),
                    6,
                ),
                unsafe_misselection_rate=round(
                    sum(unsafe for _, unsafe in outcomes)
                    / len(outcomes),
                    6,
                ),
            )
        )
    return tuple(result)
def _rate(results: Sequence[ProjectRouteCaseResult]) -> float:
    if not results:
        return 1.0
    return round(sum(result.correct for result in results) / len(results), 6)


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    index = (len(ordered) - 1) * quantile
    lower = int(index)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = index - lower
    return round(
        ordered[lower] * (1.0 - fraction)
        + ordered[upper] * fraction,
        3,
    )


def _text(value: object, limit: int, field: str) -> str:
    if not isinstance(value, str):
        raise ProjectRouteEvaluationError(f"{field} must be a string")
    normalized = " ".join(value.split()).strip()
    if not normalized or len(normalized) > limit:
        raise ProjectRouteEvaluationError(f"{field} is invalid")
    return normalized


def _text_list(
    value: object,
    *,
    item_limit: int,
    count_limit: int,
    field: str,
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= count_limit
    ):
        raise ProjectRouteEvaluationError(f"{field} is invalid")
    result = tuple(
        _text(item, item_limit, field)
        for item in value
    )
    if len(result) != len(set(result)):
        raise ProjectRouteEvaluationError(f"{field} contains duplicates")
    return result


def _score(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectRouteEvaluationError(f"{field} must be numeric")
    score = float(value)
    if not 0.0 <= score <= 1.0:
        raise ProjectRouteEvaluationError(
            f"{field} must be between 0 and 1"
        )
    return score
