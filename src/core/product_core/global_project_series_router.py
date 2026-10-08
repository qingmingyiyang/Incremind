from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from core.product_core.memory_projection_repository import (
    ObjectStoreMemoryProjectionRepository,
    ProjectionReadResult,
)
from core.product_core.progressive_memory_retrieval import (
    topic_routing_query,
)
from core.product_core.progressive_recall_shadow import (
    SeriesRouteCandidate,
    route_progressive_memory_r0,
)


GLOBAL_ROUTER_VERSION = "global-project-series-router-v2"
PROJECT_HIGH_CONFIDENCE_SCORE = 0.65
PROJECT_MEDIUM_CONFIDENCE_SCORE = 0.25
PROJECT_AMBIGUOUS_SECOND_SCORE = 0.10
PROJECT_AMBIGUITY_MARGIN = 0.08
_LATIN_PATTERN = re.compile(r"[a-z0-9]+")
_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_SAFE_COMPACT_PATTERN = re.compile(
    r"[^a-z0-9\u3400-\u4dbf\u4e00-\u9fff]+"
)


@dataclass(frozen=True, slots=True)
class FreshProjectProjection:
    project_id: str
    authority_identity: str
    authority_fingerprint: str
    read_result: ProjectionReadResult


class GlobalProjectProjectionCatalogPort(Protocol):
    def fresh_projections(self) -> Sequence[FreshProjectProjection]:
        """Return only generation-bound fresh project projections."""


@dataclass(frozen=True, slots=True)
class ProjectRouteRerankInput:
    project_id: str
    deterministic_score: float
    series: tuple[Mapping[str, object], ...]

    def to_provider_payload(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "deterministic_score": self.deterministic_score,
            "series": [dict(item) for item in self.series],
        }


@dataclass(frozen=True, slots=True)
class ProjectRouteAssistTrace:
    status: str
    reason_code: str
    provider_route_fingerprint: str
    elapsed_ms: float
    deterministic_scores: tuple[tuple[str, float], ...]
    ai_scores: tuple[tuple[str, float], ...] = ()
    fused_scores: tuple[tuple[str, float], ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "provider_route_fingerprint": self.provider_route_fingerprint,
            "elapsed_ms": self.elapsed_ms,
            "deterministic_scores": [
                {"project_id": project_id, "score": score}
                for project_id, score in self.deterministic_scores
            ],
            "ai_scores": [
                {"project_id": project_id, "score": score}
                for project_id, score in self.ai_scores
            ],
            "fused_scores": [
                {"project_id": project_id, "score": score}
                for project_id, score in self.fused_scores
            ],
            "query_recorded": False,
            "projection_content_recorded": False,
        }


class ProjectRouteRerankerPort(Protocol):
    def rerank(
        self,
        *,
        query: str,
        deterministic: GlobalProjectRouteDecision,
        candidates: Sequence[ProjectRouteRerankInput],
    ) -> GlobalProjectRouteDecision:
        """Return a bounded decision or the original deterministic decision."""


@dataclass(frozen=True, slots=True)
class GlobalProjectRouteCandidate:
    project_id: str
    rank: int
    score: float
    confidence: str
    series_candidates: tuple[SeriesRouteCandidate, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "rank": self.rank,
            "score": self.score,
            "confidence": self.confidence,
            "series_ids": [
                candidate.series_id
                for candidate in self.series_candidates
            ],
        }


@dataclass(frozen=True, slots=True)
class GlobalProjectRouteDecision:
    query_fingerprint: str
    status: str
    reason_code: str
    selected_project_id: str | None
    candidates: tuple[GlobalProjectRouteCandidate, ...]
    assist: ProjectRouteAssistTrace | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "router_version": GLOBAL_ROUTER_VERSION,
            "status": self.status,
            "reason_code": self.reason_code,
            "selected_project_id": self.selected_project_id,
            "candidates": [
                candidate.to_payload()
                for candidate in self.candidates
            ],
            "ai_assist": (
                self.assist.to_payload()
                if self.assist is not None
                else None
            ),
        }


class CurrentGlobalProjectProjectionCatalog:
    """Read current lightweight project projections without live snapshots."""

    def __init__(
        self,
        *,
        projections: ObjectStoreMemoryProjectionRepository,
        authority: object,
    ) -> None:
        self._projections = projections
        self._authority = authority

    def fresh_projections(self) -> tuple[FreshProjectProjection, ...]:
        authority_identity = getattr(self._authority, "authority_identity", None)
        generation_reader = getattr(self._authority, "generation_token", None)
        if not isinstance(authority_identity, str) or not callable(
            generation_reader
        ):
            return ()
        try:
            manifests = self._projections.manifests()
        except Exception:
            return ()
        fresh: list[FreshProjectProjection] = []
        for manifest in manifests:
            project_id = manifest.get("project_id")
            if not isinstance(project_id, str) or not project_id:
                continue
            try:
                generation_token = generation_reader(project_id)
            except Exception:
                continue
            if not _is_generation_token(generation_token):
                continue
            try:
                current = self._projections.load_current_for_generation(
                    project_id=project_id,
                    authority_identity=authority_identity,
                    authority_generation_token=generation_token,
                )
            except Exception:
                continue
            if current is None:
                continue
            fingerprint, read_result = current
            if read_result.status != "fresh" or read_result.projection is None:
                continue
            fresh.append(
                FreshProjectProjection(
                    project_id=project_id,
                    authority_identity=authority_identity,
                    authority_fingerprint=fingerprint,
                    read_result=read_result,
                )
            )
        return tuple(sorted(fresh, key=lambda item: item.project_id))


class GlobalProjectSeriesRouter:
    """Select one project using only fresh R0 project/series projections."""

    def __init__(
        self,
        *,
        catalog: GlobalProjectProjectionCatalogPort,
        max_projects: int = 3,
        reranker_factory: Callable[
            [], ProjectRouteRerankerPort | None
        ] | None = None,
    ) -> None:
        if not isinstance(max_projects, int) or isinstance(max_projects, bool):
            raise ValueError("max_projects must be an integer")
        if not 1 <= max_projects <= 5:
            raise ValueError("max_projects must be between 1 and 5")
        self._catalog = catalog
        self._max_projects = max_projects
        self._reranker_factory = reranker_factory

    def route(self, query: str) -> GlobalProjectRouteDecision:
        normalized_query = _required_query(query)
        query_fingerprint = hashlib.sha256(
            normalized_query.casefold().encode("utf-8")
        ).hexdigest()
        topic_query = topic_routing_query(normalized_query)
        project_candidates: list[
            tuple[
                float,
                str,
                tuple[SeriesRouteCandidate, ...],
                ProjectRouteRerankInput,
            ]
        ] = []
        for item in self._catalog.fresh_projections():
            decision = route_progressive_memory_r0(
                read_result=item.read_result,
                project_id=item.project_id,
                authority_identity=item.authority_identity,
                authority_fingerprint=item.authority_fingerprint,
                query=topic_query,
            )
            if not decision.candidates:
                continue
            comparable_score = _comparable_project_score(
                read_result=item.read_result,
                series_id=decision.candidates[0].series_id,
                query=topic_query,
            )
            if comparable_score <= 0:
                continue
            project_candidates.append(
                (
                    comparable_score,
                    item.project_id,
                    decision.candidates,
                    _rerank_input(
                        read_result=item.read_result,
                        project_id=item.project_id,
                        score=comparable_score,
                        series_candidates=decision.candidates,
                    ),
                )
            )
        project_candidates.sort(key=lambda value: (-value[0], value[1]))
        ranked = tuple(
            GlobalProjectRouteCandidate(
                project_id=project_id,
                rank=index + 1,
                score=score,
                confidence=_confidence(score),
                series_candidates=series_candidates,
            )
            for index, (
                score,
                project_id,
                series_candidates,
                _rerank_input_item,
            ) in enumerate(
                project_candidates[: self._max_projects]
            )
        )
        if not ranked:
            return _decision(
                query_fingerprint=query_fingerprint,
                status="fallback",
                reason_code="no_fresh_project_match",
                candidates=(),
            )
        top = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        if (
            second is not None
            and second.score >= PROJECT_AMBIGUOUS_SECOND_SCORE
            and top.score - second.score < PROJECT_AMBIGUITY_MARGIN
        ):
            deterministic = _decision(
                query_fingerprint=query_fingerprint,
                status="ambiguous",
                reason_code="ambiguous_project_scope",
                candidates=ranked,
            )
            return self._rerank_if_allowed(
                query=normalized_query,
                deterministic=deterministic,
                candidates=project_candidates,
            )
        if top.score >= PROJECT_HIGH_CONFIDENCE_SCORE:
            reason_code = "high_confidence_project_match"
        elif top.score >= PROJECT_MEDIUM_CONFIDENCE_SCORE:
            reason_code = "medium_confidence_project_match"
        else:
            deterministic = _decision(
                query_fingerprint=query_fingerprint,
                status="fallback",
                reason_code="low_confidence_project_match",
                candidates=ranked,
            )
            return self._rerank_if_allowed(
                query=normalized_query,
                deterministic=deterministic,
                candidates=project_candidates,
            )
        return _decision(
            query_fingerprint=query_fingerprint,
            status="routed",
            reason_code=reason_code,
            selected_project_id=top.project_id,
            candidates=ranked,
        )

    def _rerank_if_allowed(
        self,
        *,
        query: str,
        deterministic: GlobalProjectRouteDecision,
        candidates: Sequence[
            tuple[
                float,
                str,
                tuple[SeriesRouteCandidate, ...],
                ProjectRouteRerankInput,
            ]
        ],
    ) -> GlobalProjectRouteDecision:
        inputs = tuple(
            item[3]
            for item in candidates[: self._max_projects]
        )
        deterministic_scores = tuple(
            (item.project_id, item.deterministic_score)
            for item in inputs
        )
        if len(inputs) < 2 or self._reranker_factory is None:
            return _with_assist(
                deterministic,
                status="disabled",
                reason_code=(
                    "insufficient_candidates"
                    if len(inputs) < 2
                    else "ai_assist_disabled"
                ),
                deterministic_scores=deterministic_scores,
            )
        try:
            reranker = self._reranker_factory()
        except Exception:
            reranker = None
        if reranker is None:
            return _with_assist(
                deterministic,
                status="disabled",
                reason_code="ai_assist_unavailable",
                deterministic_scores=deterministic_scores,
            )
        try:
            return reranker.rerank(
                query=query,
                deterministic=deterministic,
                candidates=inputs,
            )
        except Exception:
            return _with_assist(
                deterministic,
                status="failed",
                reason_code="ai_assist_runtime_failed",
                deterministic_scores=deterministic_scores,
            )


def _decision(
    *,
    query_fingerprint: str,
    status: str,
    reason_code: str,
    candidates: tuple[GlobalProjectRouteCandidate, ...],
    selected_project_id: str | None = None,
) -> GlobalProjectRouteDecision:
    return GlobalProjectRouteDecision(
        query_fingerprint=query_fingerprint,
        status=status,
        reason_code=reason_code,
        selected_project_id=selected_project_id,
        candidates=candidates,
    )


def _with_assist(
    decision: GlobalProjectRouteDecision,
    *,
    status: str,
    reason_code: str,
    deterministic_scores: tuple[tuple[str, float], ...],
) -> GlobalProjectRouteDecision:
    return GlobalProjectRouteDecision(
        query_fingerprint=decision.query_fingerprint,
        status=decision.status,
        reason_code=decision.reason_code,
        selected_project_id=decision.selected_project_id,
        candidates=decision.candidates,
        assist=ProjectRouteAssistTrace(
            status=status,
            reason_code=reason_code,
            provider_route_fingerprint="",
            elapsed_ms=0.0,
            deterministic_scores=deterministic_scores,
        ),
    )


def _confidence(score: float) -> str:
    if score >= PROJECT_HIGH_CONFIDENCE_SCORE:
        return "high"
    if score >= PROJECT_MEDIUM_CONFIDENCE_SCORE:
        return "medium"
    return "low"


def _required_query(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("query must be a string")
    normalized = " ".join(value.split()).strip()
    if not normalized:
        raise ValueError("query is required")
    if len(normalized) > 4000:
        raise ValueError("query exceeds the global router limit")
    return normalized


def _is_generation_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _comparable_project_score(
    *,
    read_result: ProjectionReadResult,
    series_id: str,
    query: str,
) -> float:
    """Score the best validated R0 item on one cross-project scale."""

    projection = read_result.projection
    if not isinstance(projection, Mapping):
        return 0.0
    raw_items = projection.get("r0_items")
    if not isinstance(raw_items, list):
        return 0.0
    raw = next(
        (
            item
            for item in raw_items
            if isinstance(item, Mapping)
            and item.get("series_id") == series_id
        ),
        None,
    )
    if raw is None:
        return 0.0
    title = raw.get("title")
    description = raw.get("description")
    keywords = raw.get("keywords")
    if (
        not isinstance(title, str)
        or not isinstance(description, str)
        or not isinstance(keywords, list)
        or not all(isinstance(value, str) for value in keywords)
    ):
        return 0.0
    fields = {
        "series_id": str(series_id),
        "title": title,
        "keywords": " ".join(keywords),
        "description": description,
    }
    terms = _query_terms(query)
    if not terms:
        return 0.0
    weights = {
        "series_id": 0.65,
        "title": 1.0,
        "keywords": 0.90,
        "description": 0.45,
    }
    weighted_match = 0.0
    for kind, term in terms:
        matched = [
            weights[field]
            for field, value in fields.items()
            if _term_matches(kind, term, value)
        ]
        if matched:
            weighted_match += max(matched)
    # Long natural-language questions contain many connective CJK bigrams.
    # Capping the denominator keeps several exact domain terms from being
    # diluted by sentence length while a lone generic match remains weak.
    coverage = weighted_match / min(len(terms), 10)
    query_compact = _compact_text(query)
    boost = 0.0
    if _meaningful_phrase(_compact_text(series_id), query_compact):
        boost += 0.25
    if _meaningful_phrase(_compact_text(title), query_compact):
        boost += 0.28
    keyword_phrase_hits = sum(
        _meaningful_phrase(_compact_text(value), query_compact)
        for value in keywords
    )
    boost += min(0.42, keyword_phrase_hits * 0.14)
    return round(min(1.0, coverage * 0.75 + boost), 6)


def _rerank_input(
    *,
    read_result: ProjectionReadResult,
    project_id: str,
    score: float,
    series_candidates: Sequence[SeriesRouteCandidate],
) -> ProjectRouteRerankInput:
    projection = read_result.projection
    raw_items = (
        projection.get("r0_items")
        if isinstance(projection, Mapping)
        else None
    )
    by_series = {
        str(item.get("series_id")): item
        for item in raw_items
        if isinstance(item, Mapping)
        and isinstance(item.get("series_id"), str)
    } if isinstance(raw_items, list) else {}
    series: list[Mapping[str, object]] = []
    for candidate in series_candidates[:2]:
        raw = by_series.get(candidate.series_id)
        if raw is None:
            continue
        keywords = raw.get("keywords")
        series.append(
            {
                "series_id": candidate.series_id[:160],
                "title": _bounded_text(raw.get("title"), 160),
                "description": _bounded_text(
                    raw.get("description"),
                    480,
                ),
                "keywords": [
                    value[:80]
                    for value in keywords[:12]
                    if isinstance(value, str)
                ] if isinstance(keywords, list) else [],
            }
        )
    return ProjectRouteRerankInput(
        project_id=project_id,
        deterministic_score=score,
        series=tuple(series),
    )


def _query_terms(query: str) -> tuple[tuple[str, str], ...]:
    normalized = unicodedata.normalize("NFKC", query).casefold()
    terms = {
        ("latin", token)
        for token in _LATIN_PATTERN.findall(normalized)
        if token
    }
    for span in _CJK_PATTERN.findall(normalized):
        if len(span) == 1:
            terms.add(("cjk", span))
            continue
        for index in range(len(span) - 1):
            terms.add(("cjk", span[index : index + 2]))
    return tuple(sorted(terms))


def _term_matches(kind: str, term: str, value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    if kind == "latin":
        return term in _LATIN_PATTERN.findall(normalized)
    return term in _compact_text(normalized)


def _meaningful_phrase(candidate: str, query: str) -> bool:
    return len(candidate) >= 2 and candidate in query


def _compact_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _SAFE_COMPACT_PATTERN.sub("", normalized)


def _bounded_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()[:limit]
