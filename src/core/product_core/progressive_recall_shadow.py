from __future__ import annotations

import hashlib
import math
import re
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.product_core.memory_projection_contract import (
    GENERATOR_POLICY_ID,
    PROJECTION_VERSION,
)
from core.product_core.memory_projection_repository import (
    ObjectStoreMemoryProjectionRepository,
    ProjectionReadResult,
)
from core.product_core.progressive_memory_retrieval import (
    plan_progressive_memory_retrieval,
)
from core.search_and_recall.ports import RecallHit, RecallPort, RecallQuery


SCHEMA_VERSION = "1.0.0"
TRACE_VERSION = "progressive-recall-shadow-v1"
ROUTER_POLICY_VERSION = "deterministic-r0-lexical-v1"
DEFAULT_TOP_K = 3
MAX_TOP_K = 5
HIGH_CONFIDENCE_SCORE = 0.72
MEDIUM_CONFIDENCE_SCORE = 0.50
AMBIGUOUS_SECOND_SCORE = 0.35
AMBIGUITY_MARGIN = 0.10
_FINGERPRINT_LENGTH = 64
_ELIGIBLE_TRUST_STATUSES = (
    "trusted",
    "user_confirmed",
    "system_generated",
)
_LATIN_PATTERN = re.compile(r"[a-z0-9]+")
_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_SAFE_COMPACT_PATTERN = re.compile(r"[^a-z0-9\u3400-\u4dbf\u4e00-\u9fff]+")
_FIELD_ORDER = (
    "explicit_series",
    "series_id",
    "title",
    "keywords",
    "description",
)


class ProgressiveRecallShadowError(ValueError):
    """Raised when a shadow routing request violates its read-only contract."""


@dataclass(frozen=True, slots=True)
class SeriesRouteCandidate:
    series_id: str
    series_memory_id: str
    projection_id: str
    rank: int
    score: float
    matched_fields: tuple[str, ...]
    explicit: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "series_id": self.series_id,
            "series_memory_id": self.series_memory_id,
            "projection_id": self.projection_id,
            "rank": self.rank,
            "score": self.score,
            "matched_fields": list(self.matched_fields),
            "explicit": self.explicit,
        }


@dataclass(frozen=True, slots=True)
class SeriesRouteDecision:
    project_id: str
    query_fingerprint: str
    status: str
    confidence: str
    reason_code: str
    top_k: int
    candidates: tuple[SeriesRouteCandidate, ...]

    @property
    def fallback_required(self) -> bool:
        return self.status == "fallback"

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "confidence": self.confidence,
            "reason_code": self.reason_code,
            "top_k": self.top_k,
            "candidates": [candidate.to_payload() for candidate in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class ProgressiveRecallShadowResult:
    route: SeriesRouteDecision
    trace: Mapping[str, object]

    def to_payload(self) -> dict[str, object]:
        return dict(self.trace)


@dataclass(frozen=True, slots=True)
class _QueryTerm:
    value: str
    kind: str


@dataclass(frozen=True, slots=True)
class _IndexedSeries:
    series_id: str
    series_memory_id: str
    projection_id: str
    project_id: str
    authority_identity: str
    authority_fingerprint: str
    series_compact: str
    title_compact: str
    description_compact: str
    keyword_compacts: tuple[str, ...]
    series_latin: frozenset[str]
    title_latin: frozenset[str]
    description_latin: frozenset[str]
    keyword_latin: frozenset[str]

    def matching_fields(self, term: _QueryTerm) -> tuple[str, ...]:
        fields: list[str] = []
        if _term_matches(
            term,
            compact=self.series_compact,
            latin=self.series_latin,
        ):
            fields.append("series_id")
        if _term_matches(
            term,
            compact=self.title_compact,
            latin=self.title_latin,
        ):
            fields.append("title")
        if any(
            _term_matches(
                term,
                compact=keyword,
                latin=self.keyword_latin,
            )
            for keyword in self.keyword_compacts
        ):
            fields.append("keywords")
        if _term_matches(
            term,
            compact=self.description_compact,
            latin=self.description_latin,
        ):
            fields.append("description")
        return tuple(fields)


def route_progressive_memory_r0(
    *,
    read_result: ProjectionReadResult,
    project_id: str,
    authority_identity: str,
    authority_fingerprint: str,
    query: str,
    explicit_series_id: str | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> SeriesRouteDecision:
    """Route a query over a fresh R0 projection without changing recall."""

    project_id = _required_text(project_id, "project_id")
    authority_identity = _required_text(
        authority_identity,
        "authority_identity",
    )
    _require_fingerprint(authority_fingerprint)
    top_k = _validated_top_k(top_k)
    plan = plan_progressive_memory_retrieval(query)
    unavailable_reason = _projection_unavailable_reason(read_result)
    if unavailable_reason is not None:
        return _fallback_decision(
            project_id=project_id,
            query_fingerprint=plan.query_fingerprint,
            reason_code=unavailable_reason,
            top_k=top_k,
        )
    projection = read_result.projection
    if not isinstance(projection, Mapping):
        return _fallback_decision(
            project_id=project_id,
            query_fingerprint=plan.query_fingerprint,
            reason_code="projection_invalid",
            top_k=top_k,
        )
    indexed, invalid = _projection_index(
        projection,
        project_id=project_id,
        authority_identity=authority_identity,
        authority_fingerprint=authority_fingerprint,
    )
    if invalid:
        return _fallback_decision(
            project_id=project_id,
            query_fingerprint=plan.query_fingerprint,
            reason_code="projection_invalid",
            top_k=top_k,
        )
    if not indexed:
        return _fallback_decision(
            project_id=project_id,
            query_fingerprint=plan.query_fingerprint,
            reason_code="projection_empty",
            top_k=top_k,
        )
    if explicit_series_id is not None:
        explicit_series_id = _required_text(
            explicit_series_id,
            "explicit_series_id",
        )
        match = next(
            (
                item
                for item in indexed
                if item.series_id == explicit_series_id
            ),
            None,
        )
        if match is None:
            return _fallback_decision(
                project_id=project_id,
                query_fingerprint=plan.query_fingerprint,
                reason_code="explicit_series_not_found",
                top_k=top_k,
            )
        return SeriesRouteDecision(
            project_id=project_id,
            query_fingerprint=plan.query_fingerprint,
            status="routed",
            confidence="high",
            reason_code="explicit_series_match",
            top_k=top_k,
            candidates=(
                SeriesRouteCandidate(
                    series_id=match.series_id,
                    series_memory_id=match.series_memory_id,
                    projection_id=match.projection_id,
                    rank=1,
                    score=1.0,
                    matched_fields=("explicit_series",),
                    explicit=True,
                ),
            ),
        )

    terms = _query_terms(query)
    if not terms:
        return _fallback_decision(
            project_id=project_id,
            query_fingerprint=plan.query_fingerprint,
            reason_code="no_match",
            top_k=top_k,
        )
    query_compact = _compact_text(query)
    scored = _score_series(
        indexed,
        terms=terms,
        query_compact=query_compact,
    )
    positive = [item for item in scored if item[0] > 0]
    if not positive:
        return _fallback_decision(
            project_id=project_id,
            query_fingerprint=plan.query_fingerprint,
            reason_code="no_match",
            top_k=top_k,
        )
    candidates = tuple(
        SeriesRouteCandidate(
            series_id=item.series_id,
            series_memory_id=item.series_memory_id,
            projection_id=item.projection_id,
            rank=index + 1,
            score=score,
            matched_fields=fields,
        )
        for index, (score, fields, item) in enumerate(positive[:top_k])
    )
    top_score = candidates[0].score
    second_score = candidates[1].score if len(candidates) > 1 else 0.0
    if (
        second_score >= AMBIGUOUS_SECOND_SCORE
        and top_score - second_score < AMBIGUITY_MARGIN
    ):
        return SeriesRouteDecision(
            project_id=project_id,
            query_fingerprint=plan.query_fingerprint,
            status="fallback",
            confidence="low",
            reason_code="ambiguous_series",
            top_k=top_k,
            candidates=candidates,
        )
    if top_score >= HIGH_CONFIDENCE_SCORE:
        status, confidence, reason = "routed", "high", "high_confidence"
    elif top_score >= MEDIUM_CONFIDENCE_SCORE:
        status, confidence, reason = "routed", "medium", "medium_confidence"
    else:
        status, confidence, reason = "fallback", "low", "low_confidence"
    return SeriesRouteDecision(
        project_id=project_id,
        query_fingerprint=plan.query_fingerprint,
        status=status,
        confidence=confidence,
        reason_code=reason,
        top_k=top_k,
        candidates=candidates,
    )


def run_progressive_recall_shadow(
    *,
    projections: ObjectStoreMemoryProjectionRepository,
    recall: RecallPort,
    project_id: str,
    authority_identity: str,
    authority_fingerprint: str,
    query: str,
    legacy_hits: Sequence[Mapping[str, object]] = (),
    explicit_series_id: str | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> ProgressiveRecallShadowResult:
    """Compare R0 routing with existing recall while leaving Prompt unchanged."""

    project_id = _required_text(project_id, "project_id")
    authority_identity = _required_text(
        authority_identity,
        "authority_identity",
    )
    _require_fingerprint(authority_fingerprint)
    plan = plan_progressive_memory_retrieval(query)
    total_started = time.perf_counter()
    route_started = time.perf_counter()
    try:
        read_result = projections.load_current(
            project_id=project_id,
            authority_identity=authority_identity,
            authority_fingerprint=authority_fingerprint,
        )
    except Exception:
        read_result = ProjectionReadResult(
            status="corrupt",
            fallback_to_authority=True,
            reason_code="projection_repository_unavailable",
            projection=None,
            manifest=None,
        )
    route = route_progressive_memory_r0(
        read_result=read_result,
        project_id=project_id,
        authority_identity=authority_identity,
        authority_fingerprint=authority_fingerprint,
        query=query,
        explicit_series_id=explicit_series_id,
        top_k=top_k,
    )
    route_ms = _elapsed_ms(route_started)

    fallback_started = time.perf_counter()
    exact_atom_requested = "k_atom_exact" in plan.context_lanes
    exact_atom_hits: tuple[RecallHit, ...] = ()
    cross_series_hits: tuple[RecallHit, ...] = ()
    error_codes: list[str] = []
    if exact_atom_requested:
        exact_atom_hits, error = _safe_recall(
            recall,
            RecallQuery(
                text=query,
                project_id=project_id,
                layers=("l1_atom",),
                allowed_trust_statuses=_ELIGIBLE_TRUST_STATUSES,
                limit=6,
            ),
            error_code="exact_atom_unavailable",
        )
        if error is not None:
            error_codes.append(error)
    cross_series_requested = (
        route.fallback_required and plan.allow_cross_series_fallback
    )
    if cross_series_requested:
        cross_series_hits, error = _safe_recall(
            recall,
            RecallQuery(
                text=query,
                project_id=project_id,
                layers=(
                    "l3_series_memory",
                    "l2_scenario",
                    "l1_atom",
                ),
                allowed_trust_statuses=_ELIGIBLE_TRUST_STATUSES,
                limit=12,
            ),
            error_code="cross_series_unavailable",
        )
        if error is not None:
            error_codes.append(error)
    fallback_ms = _elapsed_ms(fallback_started)

    legacy_hit_count, legacy_series_ids = _legacy_summary(
        legacy_hits,
        project_id=project_id,
    )
    candidate_series_ids = tuple(
        candidate.series_memory_id for candidate in route.candidates
    )
    comparison = _comparison(
        candidate_series_ids,
        legacy_series_ids,
    )
    exact_atom_hit_ids = _recall_hit_ids(exact_atom_hits)
    cross_series_hit_ids = _recall_hit_ids(cross_series_hits)
    total_ms = _elapsed_ms(total_started)
    trace_core = {
        "project_id": project_id,
        "query_fingerprint": plan.query_fingerprint,
        "authority_identity_fingerprint": _sha256_text(
            authority_identity
        ),
        "authority_fingerprint": authority_fingerprint,
        "projection": {
            "read_status": read_result.status,
            "reason_code": read_result.reason_code,
            "projection_available": (
                read_result.status == "fresh"
                and read_result.projection is not None
            ),
        },
        "route": route.to_payload(),
        "fallback": {
            "required": route.fallback_required,
            "exact_atom_requested": exact_atom_requested,
            "exact_atom_hit_ids": list(exact_atom_hit_ids),
            "cross_series_requested": cross_series_requested,
            "cross_series_hit_ids": list(cross_series_hit_ids),
            "error_codes": error_codes,
            "legacy_recall_preserved": True,
        },
        "legacy": {
            "hit_count": legacy_hit_count,
            "series_memory_ids": list(legacy_series_ids),
        },
        "comparison": comparison,
    }
    trace = {
        "schema_version": SCHEMA_VERSION,
        "trace_version": TRACE_VERSION,
        "trace_id": _trace_id(trace_core),
        "mode": "shadow",
        "router_policy_version": ROUTER_POLICY_VERSION,
        **trace_core,
        "performance": {
            "route_ms": route_ms,
            "fallback_ms": fallback_ms,
            "total_ms": total_ms,
        },
        "safety": {
            "read_only": True,
            "prompt_unchanged": True,
            "raw_query_recorded": False,
            "hit_content_recorded": False,
            "source_refs_recorded": False,
            "business_writes_allowed": False,
            "team_memory_body_allowed": False,
        },
    }
    return ProgressiveRecallShadowResult(route=route, trace=trace)


def _projection_unavailable_reason(
    read_result: ProjectionReadResult,
) -> str | None:
    if (
        read_result.status == "fresh"
        and read_result.projection is not None
        and read_result.fallback_to_authority is False
    ):
        return None
    return {
        "missing": "projection_missing",
        "stale": "projection_stale",
        "corrupt": "projection_corrupt",
    }.get(read_result.status, "projection_invalid")


def _projection_index(
    projection: Mapping[str, object],
    *,
    project_id: str,
    authority_identity: str,
    authority_fingerprint: str,
) -> tuple[tuple[_IndexedSeries, ...], bool]:
    if (
        projection.get("project_id") != project_id
        or projection.get("authority_identity") != authority_identity
        or projection.get("authority_fingerprint") != authority_fingerprint
        or projection.get("projection_version") != PROJECTION_VERSION
        or projection.get("status") not in {"ready", "empty"}
    ):
        return (), True
    raw_items = projection.get("r0_items")
    if not isinstance(raw_items, list):
        return (), True
    if projection.get("status") == "empty":
        return ((), False) if not raw_items else ((), True)
    indexed: list[_IndexedSeries] = []
    seen_series: set[str] = set()
    seen_projections: set[str] = set()
    try:
        for raw in raw_items:
            item = _index_series(
                raw,
                project_id=project_id,
                authority_identity=authority_identity,
                authority_fingerprint=authority_fingerprint,
            )
            if (
                item.series_id in seen_series
                or item.projection_id in seen_projections
            ):
                return (), True
            seen_series.add(item.series_id)
            seen_projections.add(item.projection_id)
            indexed.append(item)
    except ProgressiveRecallShadowError:
        return (), True
    return tuple(indexed), False


def _index_series(
    raw: object,
    *,
    project_id: str,
    authority_identity: str,
    authority_fingerprint: str,
) -> _IndexedSeries:
    if not isinstance(raw, Mapping):
        raise ProgressiveRecallShadowError("R0 item must be an object")
    if (
        raw.get("projection_type") != "r0_series_router"
        or raw.get("project_id") != project_id
        or raw.get("authority_identity") != authority_identity
        or raw.get("authority_fingerprint") != authority_fingerprint
        or raw.get("projection_version") != PROJECTION_VERSION
        or raw.get("generator_policy_id") != GENERATOR_POLICY_ID
        or raw.get("status") != "ready"
    ):
        raise ProgressiveRecallShadowError("R0 item identity drifted")
    series_id = _required_mapping_text(raw, "series_id")
    series_memory_id = _required_mapping_text(raw, "series_memory_id")
    projection_id = _required_mapping_text(raw, "projection_id")
    title = _required_mapping_text(raw, "title")
    description = _required_mapping_text(raw, "description")
    keywords = raw.get("keywords")
    if not isinstance(keywords, list) or not all(
        isinstance(value, str) and value.strip()
        for value in keywords
    ):
        raise ProgressiveRecallShadowError("R0 keywords are invalid")
    keyword_values = tuple(str(value) for value in keywords)
    return _IndexedSeries(
        series_id=series_id,
        series_memory_id=series_memory_id,
        projection_id=projection_id,
        project_id=project_id,
        authority_identity=authority_identity,
        authority_fingerprint=authority_fingerprint,
        series_compact=_compact_text(series_id),
        title_compact=_compact_text(title),
        description_compact=_compact_text(description),
        keyword_compacts=tuple(_compact_text(value) for value in keyword_values),
        series_latin=frozenset(_latin_tokens(series_id)),
        title_latin=frozenset(_latin_tokens(title)),
        description_latin=frozenset(_latin_tokens(description)),
        keyword_latin=frozenset(
            token
            for value in keyword_values
            for token in _latin_tokens(value)
        ),
    )


def _score_series(
    items: Sequence[_IndexedSeries],
    *,
    terms: tuple[_QueryTerm, ...],
    query_compact: str,
) -> list[tuple[float, tuple[str, ...], _IndexedSeries]]:
    frequencies = {
        term: sum(bool(item.matching_fields(term)) for item in items)
        for term in terms
    }
    denominator = sum(
        _inverse_document_frequency(len(items), frequencies[term])
        for term in terms
    )
    scored: list[tuple[float, tuple[str, ...], _IndexedSeries]] = []
    field_weights = {
        "series_id": 0.65,
        "title": 1.0,
        "keywords": 0.90,
        "description": 0.45,
    }
    for item in items:
        weighted_match = 0.0
        matched_fields: set[str] = set()
        for term in terms:
            fields = item.matching_fields(term)
            if not fields:
                continue
            matched_fields.update(fields)
            weighted_match += (
                _inverse_document_frequency(len(items), frequencies[term])
                * max(field_weights[field] for field in fields)
            )
        coverage = weighted_match / denominator if denominator else 0.0
        boost = 0.0
        if _meaningful_phrase(item.series_compact, query_compact):
            boost += 0.25
            matched_fields.add("series_id")
        if _meaningful_phrase(item.title_compact, query_compact):
            boost += 0.28
            matched_fields.add("title")
        if any(
            _meaningful_phrase(keyword, query_compact)
            for keyword in item.keyword_compacts
        ):
            boost += 0.14
            matched_fields.add("keywords")
        score = round(min(1.0, coverage * 0.75 + boost), 6)
        ordered_fields = tuple(
            field for field in _FIELD_ORDER if field in matched_fields
        )
        scored.append((score, ordered_fields, item))
    return sorted(
        scored,
        key=lambda value: (
            -value[0],
            value[2].series_id,
            value[2].projection_id,
        ),
    )


def _query_terms(query: str) -> tuple[_QueryTerm, ...]:
    normalized = unicodedata.normalize("NFKC", query).casefold()
    terms = {
        _QueryTerm(value=token, kind="latin")
        for token in _LATIN_PATTERN.findall(normalized)
        if token
    }
    for span in _CJK_PATTERN.findall(normalized):
        if len(span) == 1:
            terms.add(_QueryTerm(value=span, kind="cjk"))
            continue
        for index in range(len(span) - 1):
            terms.add(
                _QueryTerm(
                    value=span[index : index + 2],
                    kind="cjk",
                )
            )
    return tuple(sorted(terms, key=lambda term: (term.kind, term.value)))


def _term_matches(
    term: _QueryTerm,
    *,
    compact: str,
    latin: frozenset[str],
) -> bool:
    if term.kind == "latin":
        return term.value in latin
    return term.value in compact


def _inverse_document_frequency(total: int, frequency: int) -> float:
    return math.log((total + 1) / (frequency + 1)) + 1.0


def _meaningful_phrase(candidate: str, query: str) -> bool:
    return len(candidate) >= 2 and candidate in query


def _compact_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _SAFE_COMPACT_PATTERN.sub("", normalized)


def _latin_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return tuple(_LATIN_PATTERN.findall(normalized))


def _fallback_decision(
    *,
    project_id: str,
    query_fingerprint: str,
    reason_code: str,
    top_k: int,
) -> SeriesRouteDecision:
    return SeriesRouteDecision(
        project_id=project_id,
        query_fingerprint=query_fingerprint,
        status="fallback",
        confidence="none",
        reason_code=reason_code,
        top_k=top_k,
        candidates=(),
    )


def _safe_recall(
    recall: RecallPort,
    query: RecallQuery,
    *,
    error_code: str,
) -> tuple[tuple[RecallHit, ...], str | None]:
    try:
        hits = recall.recall(query)
    except Exception:
        return (), error_code
    if not isinstance(hits, tuple):
        return (), error_code
    valid = tuple(hit for hit in hits if isinstance(hit, RecallHit))
    if len(valid) != len(hits):
        return (), error_code
    return valid, None


def _recall_hit_ids(hits: Sequence[RecallHit]) -> tuple[str, ...]:
    values: list[str] = []
    for hit in hits:
        if hit.object_id and hit.object_id not in values:
            values.append(hit.object_id)
    return tuple(values)


def _legacy_summary(
    hits: Sequence[Mapping[str, object]],
    *,
    project_id: str,
) -> tuple[int, tuple[str, ...]]:
    count = 0
    series_ids: list[str] = []
    for hit in hits:
        if not isinstance(hit, Mapping) or hit.get("project_id") != project_id:
            continue
        object_id = hit.get("object_id")
        if not isinstance(object_id, str) or not object_id:
            continue
        count += 1
        if (
            hit.get("layer") == "l3_series_memory"
            and object_id not in series_ids
        ):
            series_ids.append(object_id)
    return count, tuple(series_ids)


def _comparison(
    candidate_ids: tuple[str, ...],
    legacy_ids: tuple[str, ...],
) -> dict[str, object]:
    candidate_set = set(candidate_ids)
    legacy_set = set(legacy_ids)
    intersection = tuple(
        value for value in candidate_ids if value in legacy_set
    )
    union = candidate_set | legacy_set
    top1_agreement: bool | None = None
    if candidate_ids and legacy_ids:
        top1_agreement = candidate_ids[0] == legacy_ids[0]
    coverage = (
        len(intersection) / len(legacy_set)
        if legacy_set
        else 1.0
    )
    jaccard = (
        len(intersection) / len(union)
        if union
        else 1.0
    )
    return {
        "candidate_series_memory_ids": list(candidate_ids),
        "intersection_series_memory_ids": list(intersection),
        "top1_agreement": top1_agreement,
        "legacy_coverage_ratio": round(coverage, 6),
        "jaccard_similarity": round(jaccard, 6),
        "production_cutover_allowed": False,
    }


def _trace_id(trace_core: Mapping[str, object]) -> str:
    safe_identity = (
        str(trace_core["project_id"]),
        str(trace_core["query_fingerprint"]),
        str(trace_core["authority_identity_fingerprint"]),
        str(trace_core["authority_fingerprint"]),
        ROUTER_POLICY_VERSION,
        _safe_trace_sequence(trace_core),
    )
    digest = hashlib.sha256(
        "\n".join(safe_identity).encode("utf-8")
    ).hexdigest()
    return f"progressive-shadow-{digest[:40]}"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_trace_sequence(trace_core: Mapping[str, object]) -> str:
    route = trace_core.get("route")
    fallback = trace_core.get("fallback")
    legacy = trace_core.get("legacy")
    values: list[str] = []
    if isinstance(route, Mapping):
        values.extend(
            (
                str(route.get("status")),
                str(route.get("confidence")),
                str(route.get("reason_code")),
            )
        )
        candidates = route.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, Mapping):
                    values.extend(
                        (
                            str(candidate.get("series_memory_id")),
                            str(candidate.get("score")),
                        )
                    )
    if isinstance(fallback, Mapping):
        for key in ("exact_atom_hit_ids", "cross_series_hit_ids", "error_codes"):
            raw = fallback.get(key)
            if isinstance(raw, list):
                values.extend(str(value) for value in raw)
    if isinstance(legacy, Mapping):
        values.append(str(legacy.get("hit_count")))
        raw = legacy.get("series_memory_ids")
        if isinstance(raw, list):
            values.extend(str(value) for value in raw)
    return "\n".join(values)


def _elapsed_ms(started: float) -> float:
    return round(max(0.0, (time.perf_counter() - started) * 1000), 3)


def _validated_top_k(value: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_TOP_K
    ):
        raise ProgressiveRecallShadowError(
            f"top_k must be between 1 and {MAX_TOP_K}"
        )
    return value


def _require_fingerprint(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != _FINGERPRINT_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProgressiveRecallShadowError(
            "authority_fingerprint must be lowercase SHA-256"
        )


def _required_mapping_text(
    value: Mapping[str, object],
    field: str,
) -> str:
    return _required_text(value.get(field), field)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProgressiveRecallShadowError(f"{field} is required")
    return value.strip()
