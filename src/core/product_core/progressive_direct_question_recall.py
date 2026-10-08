from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from core.product_core.memory_projection_authority_contract import (
    MemoryProjectionAuthoritySnapshot,
    MemoryProjectionAuthoritySnapshotPort,
    authority_snapshot_fingerprint,
)
from core.product_core.memory_projection_repository import (
    ObjectStoreMemoryProjectionRepository,
    ProjectionReadResult,
)
from core.product_core.project_memory_recall import ProjectMemoryRecallError
from core.product_core.progressive_memory_retrieval import (
    ProgressiveRetrievalPlan,
    plan_progressive_memory_retrieval,
    topic_routing_query,
)
from core.product_core.progressive_recall_shadow import (
    SeriesRouteDecision,
    route_progressive_memory_r0,
)
from core.product_core.progressive_recall_drilldown import (
    ProgressiveRecallAuthorityReaderPort,
    ProgressiveRecallDrilldownResult,
    run_progressive_recall_drilldown,
)


ProgressiveRebuildScheduler = Callable[
    [MemoryProjectionAuthoritySnapshot, str],
    str | None,
]


@dataclass(frozen=True, slots=True)
class ProgressiveDirectQuestionRecallResult:
    project_id: str
    skill_id: str
    request_id: str
    result_id: str
    hit_count: int
    evidence_hits: tuple[Mapping[str, object], ...]
    progressive_trace: Mapping[str, object]
    ephemeral_context_bundle: Mapping[str, object] | None = None


class ProgressiveDirectQuestionRecall:
    """Prefer fresh R0/R1 evidence while preserving the current recall fallback."""

    def __init__(
        self,
        *,
        legacy_recall: object,
        projections: ObjectStoreMemoryProjectionRepository,
        authority: MemoryProjectionAuthoritySnapshotPort,
        schedule_rebuild: ProgressiveRebuildScheduler | None = None,
        authority_reader: ProgressiveRecallAuthorityReaderPort | None = None,
    ) -> None:
        self._legacy_recall = legacy_recall
        self._projections = projections
        self._authority = authority
        self._schedule_rebuild = schedule_rebuild
        self._authority_reader = authority_reader

    def execute(
        self,
        project_id: str,
        *,
        query: str,
        created_at: str | None = None,
    ) -> ProgressiveDirectQuestionRecallResult:
        total_started = time.perf_counter()
        plan = plan_progressive_memory_retrieval(query)
        legacy = _EmptyLegacyResult(project_id=project_id)
        legacy_error: ProjectMemoryRecallError | None = None
        legacy_ms = 0.0
        r0_started = time.perf_counter()
        snapshot: MemoryProjectionAuthoritySnapshot | None = None
        try:
            authority_identity = getattr(self._authority, "authority_identity", None)
            generation_reader = getattr(self._authority, "generation_token", None)
            generation_token = (
                generation_reader(project_id)
                if isinstance(authority_identity, str)
                and authority_identity
                and callable(generation_reader)
                else None
            )
            current = (
                self._projections.load_current_for_generation(
                    project_id=project_id,
                    authority_identity=authority_identity,
                    authority_generation_token=generation_token,
                )
                if _is_generation_token(generation_token)
                else None
            )
            if current is not None:
                authority_fingerprint, read_result = current
            else:
                snapshot = self._authority.load(project_id)
                authority_identity = snapshot.authority_identity
                authority_fingerprint = authority_snapshot_fingerprint(snapshot)
                read_result = self._projections.load_current(
                    project_id=project_id,
                    authority_identity=authority_identity,
                    authority_fingerprint=authority_fingerprint,
                )
            route = route_progressive_memory_r0(
                read_result=read_result,
                project_id=project_id,
                authority_identity=authority_identity,
                authority_fingerprint=authority_fingerprint,
                query=topic_routing_query(query),
            )
        except Exception:
            r0_ms = _elapsed_ms(r0_started)
            legacy_started = time.perf_counter()
            legacy, legacy_error = self._execute_legacy(
                project_id=project_id,
                query=query,
                created_at=created_at,
                layers=None,
            )
            legacy_ms = _elapsed_ms(legacy_started)
            if legacy_error is not None:
                raise legacy_error
            return _fallback_result(
                legacy,
                project_id=project_id,
                reason_code="progressive_runtime_unavailable",
                query=query,
                plan=plan,
                performance=_performance(
                    total_started=total_started,
                    legacy_ms=legacy_ms,
                    r0_ms=r0_ms,
                    r0_status="fallback",
                ),
            )
        r0_ms = _elapsed_ms(r0_started)

        rebuild_job_id: str | None = None
        if (
            read_result.status in {"missing", "stale", "rebuilding", "failed"}
            and self._schedule_rebuild is not None
            and snapshot is not None
        ):
            try:
                rebuild_job_id = self._schedule_rebuild(
                    snapshot,
                    authority_fingerprint,
                )
            except Exception:
                rebuild_job_id = None

        if route.status != "routed" or read_result.projection is None:
            legacy_started = time.perf_counter()
            legacy, legacy_error = self._execute_legacy(
                project_id=project_id,
                query=query,
                created_at=created_at,
                layers=None,
            )
            legacy_ms = _elapsed_ms(legacy_started)
            if legacy_error is not None:
                raise legacy_error
            return _fallback_result(
                legacy,
                project_id=project_id,
                reason_code=route.reason_code,
                query=query,
                plan=plan,
                read_result=read_result,
                route=route,
                authority_fingerprint=authority_fingerprint,
                rebuild_job_id=rebuild_job_id,
                performance=_performance(
                    total_started=total_started,
                    legacy_ms=legacy_ms,
                    r0_ms=r0_ms,
                    r0_status="fallback",
                    r0_hit_count=len(route.candidates),
                ),
            )

        r1_started = time.perf_counter()
        progressive_hits = _r1_hits(
            read_result.projection,
            route=route,
        )
        drilldown = _run_drilldown_safely(
            project_id=project_id,
            query=query,
            read_result=read_result,
            route=route,
            authority_reader=self._authority_reader,
            insufficient_r1=not progressive_hits,
            consumed_items=len(progressive_hits),
            consumed_chars=sum(
                len(str(hit.get("snippet") or ""))
                for hit in progressive_hits
            ),
        )
        deep_items = _bundle_items(drilldown)
        if not progressive_hits and not deep_items:
            r1_ms = _elapsed_ms(r1_started)
            legacy_started = time.perf_counter()
            legacy, legacy_error = self._execute_legacy(
                project_id=project_id,
                query=query,
                created_at=created_at,
                layers=None,
            )
            legacy_ms = _elapsed_ms(legacy_started)
            if legacy_error is not None:
                raise legacy_error
            return _fallback_result(
                legacy,
                project_id=project_id,
                reason_code="r1_evidence_missing",
                query=query,
                plan=plan,
                read_result=read_result,
                route=route,
                authority_fingerprint=authority_fingerprint,
                performance=_performance(
                    total_started=total_started,
                    legacy_ms=legacy_ms,
                    r0_ms=r0_ms,
                    r0_status="used",
                    r0_hit_count=len(route.candidates),
                    r1_ms=r1_ms,
                    r1_status="fallback",
                    drilldown=drilldown,
                ),
                drilldown=drilldown,
            )
        legacy_started = time.perf_counter()
        legacy, legacy_error = self._execute_legacy(
            project_id=project_id,
            query=query,
            created_at=created_at,
            layers=_authority_layers(plan),
        )
        legacy_ms = _elapsed_ms(legacy_started)
        combined = _merge_hits(
            progressive_hits,
            _fresh_legacy_hits(
                getattr(legacy, "evidence_hits", ()),
                plan=plan,
                query=query,
            ),
        )
        r1_ms = _elapsed_ms(r1_started)
        trace = _trace(
            project_id=project_id,
            query=query,
            read_result=read_result,
            route=route,
            authority_fingerprint=authority_fingerprint,
            fallback_used=False,
            fallback_reason=None,
            rebuild_job_id=None,
            evidence_hits=combined,
            performance=_performance(
                total_started=total_started,
                legacy_ms=legacy_ms,
                r0_ms=r0_ms,
                r0_status="used",
                r0_hit_count=len(route.candidates),
                r1_ms=r1_ms,
                r1_status="used" if progressive_hits else "fallback",
                r1_hit_count=len(progressive_hits),
                drilldown=drilldown,
            ),
            drilldown=drilldown,
            r1_used=bool(progressive_hits),
            plan=plan,
        )
        return ProgressiveDirectQuestionRecallResult(
            project_id=project_id,
            skill_id=str(getattr(legacy, "skill_id", "")),
            request_id=(
                str(getattr(legacy, "request_id", ""))
                or _stable_id(
                    "progressive-recall-request",
                    project_id,
                    route.query_fingerprint,
                    authority_fingerprint,
                )
            ),
            result_id=_stable_id(
                "progressive-recall-result",
                project_id,
                authority_fingerprint,
                route.query_fingerprint,
                *(
                    str(hit.get("object_id"))
                    for hit in combined
                ),
            ),
            hit_count=len(combined),
            evidence_hits=combined,
            progressive_trace=trace,
            ephemeral_context_bundle=(
                dict(drilldown.bundle)
                if drilldown is not None and deep_items
                else None
            ),
        )

    def _execute_legacy(
        self,
        *,
        project_id: str,
        query: str,
        created_at: str | None,
        layers: tuple[str, ...] | None,
    ) -> tuple[object, ProjectMemoryRecallError | None]:
        execute = self._legacy_recall.execute
        kwargs: dict[str, object] = {
            "query": query,
            "created_at": created_at,
        }
        try:
            parameters = inspect.signature(execute).parameters
        except (TypeError, ValueError):
            parameters = {}
        if layers is not None and "layers" in parameters:
            kwargs["layers"] = layers
        try:
            return execute(project_id, **kwargs), None
        except ProjectMemoryRecallError as error:
            return _EmptyLegacyResult(project_id=project_id), error


@dataclass(frozen=True, slots=True)
class _EmptyLegacyResult:
    project_id: str
    skill_id: str = ""
    request_id: str = ""
    result_id: str = ""
    hit_count: int = 0
    evidence_hits: tuple[Mapping[str, object], ...] = ()


def _r1_hits(
    projection: Mapping[str, object],
    *,
    route: SeriesRouteDecision,
) -> tuple[Mapping[str, object], ...]:
    candidates = {
        candidate.series_memory_id: candidate
        for candidate in route.candidates
    }
    raw_items = projection.get("r1_items")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        return ()
    hits: list[Mapping[str, object]] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        series_memory_id = item.get("series_memory_id")
        candidate = candidates.get(series_memory_id)
        summary = item.get("summary")
        if candidate is None or not isinstance(summary, str) or not summary.strip():
            continue
        source_refs = item.get("source_refs")
        safe_refs = (
            [
                dict(ref)
                for ref in source_refs
                if isinstance(ref, Mapping)
            ]
            if isinstance(source_refs, list)
            else []
        )
        hits.append(
            {
                "hit_id": _stable_id(
                    "progressive-hit",
                    str(item.get("projection_id")),
                ),
                "layer": "l3_series_memory",
                "object_id": str(series_memory_id),
                "source_refs": safe_refs,
                "snippet": " ".join(summary.split())[:480],
                "explanation": "R0 系列路由命中的当前 R1 系列概况。",
                "score": max(0.0, min(float(candidate.score), 1.0)),
                "token_estimate": max(1, len(summary) // 4),
                "trust_status": "user_confirmed",
                "projection_id": item.get("projection_id"),
                "authority_fingerprint": item.get("authority_fingerprint"),
            }
        )
    return tuple(hits)


def _merge_hits(
    preferred: Sequence[Mapping[str, object]],
    legacy: object,
    *,
    limit: int = 6,
) -> tuple[Mapping[str, object], ...]:
    values = legacy if isinstance(legacy, Sequence) and not isinstance(legacy, (str, bytes)) else ()
    merged: list[Mapping[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for hit in (*preferred, *values):
        if not isinstance(hit, Mapping):
            continue
        key = (str(hit.get("layer")), str(hit.get("object_id")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(hit))
    merged.sort(
        key=lambda hit: (
            _CANONICAL_LAYER_ORDER.get(str(hit.get("layer")), 99),
            -float(hit.get("score") or 0.0),
            str(hit.get("object_id") or ""),
        )
    )
    return tuple(merged[:limit])


_CANONICAL_LAYER_ORDER = {
    "l4_persona": 0,
    "l3_project_skill": 1,
    "l3_series_memory": 2,
    "l2_scenario": 3,
    "l1_atom": 4,
    "l0_source": 5,
}


def _authority_layers(plan: ProgressiveRetrievalPlan) -> tuple[str, ...]:
    lane_layers = {
        "k_global_profile": "l4_persona",
        "k_atom_exact": "l1_atom",
        "k_project_skill": "l3_project_skill",
    }
    return tuple(
        lane_layers[lane]
        for lane in plan.context_lanes
        if lane in lane_layers
    )


def _fresh_legacy_hits(
    value: object,
    *,
    plan: ProgressiveRetrievalPlan,
    query: str,
) -> tuple[Mapping[str, object], ...]:
    hits = _mapping_hits(value)
    allowed_context = set(_authority_layers(plan))
    high_context = tuple(
        hit
        for hit in hits
        if hit.get("layer") in allowed_context
    )
    if "l1_atom" not in allowed_context:
        return high_context
    details = [
        hit
        for hit in hits
        if hit.get("layer") == "l1_atom"
    ]
    details.sort(
        key=lambda hit: (
            _CANONICAL_LAYER_ORDER.get(str(hit.get("layer")), 99),
            -_query_relevance(query, str(hit.get("snippet") or "")),
            str(hit.get("object_id") or ""),
        )
    )
    selected_details: list[Mapping[str, object]] = []
    counts: dict[str, int] = {}
    for hit in details:
        layer = str(hit.get("layer"))
        if counts.get(layer, 0) >= 2:
            continue
        selected_details.append(hit)
        counts[layer] = counts.get(layer, 0) + 1
    return (*high_context, *selected_details)


def _fallback_legacy_hits(
    value: object,
    *,
    plan: ProgressiveRetrievalPlan,
    query: str,
) -> tuple[Mapping[str, object], ...]:
    hits = _mapping_hits(value)
    allowed = {
        "l4_persona",
        "l3_project_skill",
        "l3_series_memory",
    }
    if plan.intent == "fact_lookup":
        allowed.update({"l2_scenario", "l1_atom"})
    elif plan.intent in {"deep_synthesis", "source_verification"}:
        allowed.update({"l2_scenario", "l1_atom"})
    selected = [hit for hit in hits if hit.get("layer") in allowed]
    selected.sort(
        key=lambda hit: (
            _CANONICAL_LAYER_ORDER.get(str(hit.get("layer")), 99),
            -_query_relevance(query, str(hit.get("snippet") or "")),
            str(hit.get("object_id") or ""),
        )
    )
    return tuple(selected[:6])


def _mapping_hits(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(hit for hit in value if isinstance(hit, Mapping))


def _query_relevance(query: str, content: str) -> float:
    query_terms = _terms(query)
    if not query_terms:
        return 0.0
    content_terms = _terms(content)
    return len(query_terms & content_terms) / len(query_terms)


def _terms(value: str) -> frozenset[str]:
    normalized = value.casefold()
    latin = {
        token
        for token in re.findall(r"[a-z0-9_]+", normalized)
        if len(token) > 1
    }
    han = {
        char
        for char in normalized
        if "\u3400" <= char <= "\u9fff"
    }
    return frozenset((*latin, *han))


def _fallback_result(
    legacy: object,
    *,
    project_id: str,
    reason_code: str,
    query: str,
    plan: ProgressiveRetrievalPlan,
    read_result: ProjectionReadResult | None = None,
    route: SeriesRouteDecision | None = None,
    authority_fingerprint: str = "",
    rebuild_job_id: str | None = None,
    performance: Mapping[str, object],
    drilldown: ProgressiveRecallDrilldownResult | None = None,
) -> ProgressiveDirectQuestionRecallResult:
    evidence_hits = tuple(
        dict(hit)
        for hit in _fallback_legacy_hits(
            getattr(legacy, "evidence_hits", ()),
            plan=plan,
            query=query,
        )
    )
    trace = _trace(
        project_id=project_id,
        query=query,
        read_result=read_result,
        route=route,
        authority_fingerprint=authority_fingerprint,
        fallback_used=True,
        fallback_reason=reason_code,
        rebuild_job_id=rebuild_job_id,
        evidence_hits=evidence_hits,
        performance=performance,
        drilldown=drilldown,
        r1_used=False,
        plan=plan,
    )
    return ProgressiveDirectQuestionRecallResult(
        project_id=str(getattr(legacy, "project_id", project_id)),
        skill_id=str(getattr(legacy, "skill_id", "")),
        request_id=str(getattr(legacy, "request_id", "")),
        result_id=str(getattr(legacy, "result_id", "")),
        hit_count=len(evidence_hits),
        evidence_hits=evidence_hits,
        progressive_trace=trace,
        ephemeral_context_bundle=None,
    )


def _trace(
    *,
    project_id: str,
    query: str,
    read_result: ProjectionReadResult | None,
    route: SeriesRouteDecision | None,
    authority_fingerprint: str,
    fallback_used: bool,
    fallback_reason: str | None,
    rebuild_job_id: str | None,
    evidence_hits: Sequence[Mapping[str, object]],
    performance: Mapping[str, object],
    drilldown: ProgressiveRecallDrilldownResult | None = None,
    r1_used: bool,
    plan: ProgressiveRetrievalPlan,
) -> Mapping[str, object]:
    query_fingerprint = hashlib.sha256(query.encode("utf-8")).hexdigest()
    trace_core = {
        "schema_version": "1.0.0",
        "trace_version": "progressive-direct-question-v4",
        "mode": "production",
        "project_id": project_id,
        "query_fingerprint": query_fingerprint,
        "authority_fingerprint": authority_fingerprint or None,
        "projection": {
            "read_status": read_result.status if read_result is not None else "unavailable",
            "reason_code": (
                read_result.reason_code
                if read_result is not None
                else "progressive_runtime_unavailable"
            ),
        },
        "route": (
            route.to_payload()
            if route is not None
            else {
                "status": "fallback",
                "confidence": "low",
                "reason_code": fallback_reason,
                "top_k": 0,
                "candidates": [],
            }
        ),
        "layers_read": _layers_read(
            fallback_used=fallback_used,
            drilldown=drilldown,
            r1_used=r1_used,
        ),
        "memory_layers": _memory_layer_trace(
            plan=plan,
            evidence_hits=evidence_hits,
            drilldown=drilldown,
        ),
        "fallback": {
            "used": fallback_used,
            "reason_code": fallback_reason,
            "legacy_recall_preserved": True,
        },
        "rebuild": {
            "scheduled": rebuild_job_id is not None,
            "job_id": rebuild_job_id,
        },
        "evidence": {
            "count": len(evidence_hits),
            "object_ids": sorted(
                {
                    str(hit.get("object_id"))
                    for hit in evidence_hits
                    if hit.get("object_id")
                }
            ),
        },
        "safety": {
            "query_recorded": False,
            "content_recorded": False,
            "source_refs_recorded": False,
            "path_recorded": False,
            "url_recorded": False,
            "r2_r3_provider_egress_allowed": False,
            "business_writes_allowed": False,
        },
        "drilldown": _drilldown_summary(drilldown),
    }
    trace_id = _stable_id(
        "progressive-direct-question-trace",
        json.dumps(trace_core, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    return {"trace_id": trace_id, **trace_core, "performance": dict(performance)}


def _performance(
    *,
    total_started: float,
    legacy_ms: float,
    r0_ms: float,
    r0_status: str,
    r0_hit_count: int = 0,
    r1_ms: float = 0.0,
    r1_status: str = "not_requested",
    r1_hit_count: int = 0,
    drilldown: ProgressiveRecallDrilldownResult | None = None,
) -> dict[str, object]:
    drilldown_trace = drilldown.trace if drilldown is not None else {}
    drilldown_performance = (
        drilldown_trace.get("performance")
        if isinstance(drilldown_trace, Mapping)
        else None
    )
    r2_ms = _safe_trace_ms(drilldown_performance, "r2_ms")
    r3_ms = _safe_trace_ms(drilldown_performance, "r3_ms")
    drilldown_stages = (
        drilldown_trace.get("stages")
        if isinstance(drilldown_trace, Mapping)
        else None
    )
    r2_status, r2_hits = _deep_stage_performance(
        drilldown_stages,
        "r2_structured_content",
    )
    r3_status, r3_hits = _deep_stage_performance(
        drilldown_stages,
        "r3_source_evidence",
    )
    return {
        "total_ms": _elapsed_ms(total_started),
        "legacy_recall_ms": legacy_ms,
        "stages": [
            {
                "stage": "r0_series_router",
                "status": r0_status,
                "elapsed_ms": r0_ms,
                "hit_count": max(0, r0_hit_count),
            },
            {
                "stage": "r1_series_digest",
                "status": r1_status,
                "elapsed_ms": r1_ms,
                "hit_count": max(0, r1_hit_count),
            },
            {
                "stage": "r2_structured_content",
                "status": r2_status,
                "elapsed_ms": r2_ms,
                "hit_count": r2_hits,
            },
            {
                "stage": "r3_source_evidence",
                "status": r3_status,
                "elapsed_ms": r3_ms,
                "hit_count": r3_hits,
            },
        ],
    }


def _elapsed_ms(started: float) -> float:
    return round(max(0.0, (time.perf_counter() - started) * 1000.0), 3)


def _run_drilldown_safely(
    *,
    project_id: str,
    query: str,
    read_result: ProjectionReadResult,
    route: SeriesRouteDecision,
    authority_reader: ProgressiveRecallAuthorityReaderPort | None,
    insufficient_r1: bool,
    consumed_items: int,
    consumed_chars: int,
) -> ProgressiveRecallDrilldownResult | None:
    if authority_reader is None:
        return None
    try:
        return run_progressive_recall_drilldown(
            project_id=project_id,
            query=query,
            projection_result=read_result,
            route=route,
            authority_reader=authority_reader,
            escalation_signals=(
                ("insufficient_evidence",)
                if insufficient_r1
                else ()
            ),
            consumed_items=consumed_items,
            consumed_chars=consumed_chars,
        )
    except Exception:
        return None


def _bundle_items(
    drilldown: ProgressiveRecallDrilldownResult | None,
) -> tuple[Mapping[str, object], ...]:
    if drilldown is None:
        return ()
    items = drilldown.bundle.get("items")
    if not isinstance(items, list):
        return ()
    return tuple(item for item in items if isinstance(item, Mapping))


def _layers_read(
    *,
    fallback_used: bool,
    drilldown: ProgressiveRecallDrilldownResult | None,
    r1_used: bool,
) -> list[str]:
    layers = [] if fallback_used else ["r0_series_router"]
    if not fallback_used and r1_used:
        layers.append("r1_series_digest")
    for item in _bundle_items(drilldown):
        layer = item.get("layer")
        if layer in {"r2_structured_content", "r3_source_evidence"} and layer not in layers:
            layers.append(str(layer))
    return layers


def _memory_layer_trace(
    *,
    plan: ProgressiveRetrievalPlan,
    evidence_hits: Sequence[Mapping[str, object]],
    drilldown: ProgressiveRecallDrilldownResult | None,
) -> list[dict[str, object]]:
    requested = {"L4", "L3"}
    if plan.intent == "fact_lookup":
        requested.update({"L2", "L1"})
    elif plan.intent == "deep_synthesis":
        requested.add("L2")
    elif plan.intent == "source_verification":
        requested.update({"L2", "L0"})

    stats = {
        layer: {"hit_count": 0, "token_estimate": 0}
        for layer in ("L4", "L3", "L2", "L1", "L0")
    }
    canonical = {
        "l4_persona": "L4",
        "l3_project_skill": "L3",
        "l3_series_memory": "L3",
        "l2_scenario": "L2",
        "l1_atom": "L1",
        "l0_source": "L0",
    }
    for hit in evidence_hits:
        layer = canonical.get(str(hit.get("layer")))
        if layer is None:
            continue
        stats[layer]["hit_count"] += 1
        token_estimate = hit.get("token_estimate")
        if isinstance(token_estimate, int) and not isinstance(token_estimate, bool):
            stats[layer]["token_estimate"] += max(0, token_estimate)

    drilldown_items = _bundle_items(drilldown)
    for item in drilldown_items:
        layer = {
            "r2_structured_content": "L2",
            "r3_source_evidence": "L0",
        }.get(str(item.get("layer")))
        if layer is None:
            continue
        stats[layer]["hit_count"] += 1
        char_count = item.get("char_count")
        if isinstance(char_count, int) and not isinstance(char_count, bool):
            stats[layer]["token_estimate"] += max(1, char_count // 4)

    drilldown_trace = drilldown.trace if drilldown is not None else {}
    drop_codes = (
        drilldown_trace.get("drop_codes")
        if isinstance(drilldown_trace, Mapping)
        else ()
    )
    budget_dropped = bool(drop_codes)
    result: list[dict[str, object]] = []
    for layer in ("L4", "L3", "L2", "L1", "L0"):
        hit_count = int(stats[layer]["hit_count"])
        if hit_count:
            status = "selected"
            reason = {
                "L4": "confirmed_persona_current",
                "L3": "project_scope_context",
                "L2": "structured_detail_requested",
                "L1": "exact_fact_requested",
                "L0": "source_verification_requested",
            }[layer]
        elif layer not in requested:
            status = "skipped"
            reason = "query_depth_not_requested"
        elif budget_dropped and layer in {"L2", "L0"}:
            status = "budget_dropped"
            reason = "context_budget_or_authority_filter"
        else:
            status = "unavailable"
            reason = "eligible_evidence_not_found"
        result.append(
            {
                "layer": layer,
                "status": status,
                "reason": reason,
                "hit_count": hit_count,
                "token_estimate": min(
                    12000,
                    int(stats[layer]["token_estimate"]),
                ),
            }
        )
    return result


def _drilldown_summary(
    drilldown: ProgressiveRecallDrilldownResult | None,
) -> dict[str, object]:
    if drilldown is None:
        return {
            "status": "not_available",
            "stages": [],
            "drop_codes": [],
            "error_codes": [],
            "budget": {
                "used_items": 0,
                "used_chars": 0,
                "remaining_items": 0,
                "remaining_chars": 0,
            },
        }
    trace = drilldown.trace
    stages = trace.get("stages")
    safe_stages = [
        {
            "stage": item.get("stage"),
            "attempted": item.get("attempted") is True,
            "item_count": int(item.get("item_count") or 0),
            "char_count": int(item.get("char_count") or 0),
            "reason_codes": [
                str(code)
                for code in item.get("reason_codes", [])
                if isinstance(code, str)
            ],
        }
        for item in stages
        if isinstance(item, Mapping)
    ] if isinstance(stages, list) else []
    return {
        "status": (
            "completed"
            if any(item["attempted"] for item in safe_stages)
            else "not_requested"
        ),
        "stages": safe_stages,
        "drop_codes": [
            str(code)
            for code in trace.get("drop_codes", [])
            if isinstance(code, str)
        ],
        "error_codes": [
            str(code)
            for code in trace.get("error_codes", [])
            if isinstance(code, str)
        ],
        "budget": dict(trace.get("budget")) if isinstance(trace.get("budget"), Mapping) else {},
    }


def _safe_trace_ms(value: object, key: str) -> float:
    if not isinstance(value, Mapping):
        return 0.0
    raw = value.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    return round(max(0.0, float(raw)), 3)


def _deep_stage_performance(
    values: object,
    stage: str,
) -> tuple[str, int]:
    if not isinstance(values, list):
        return "not_requested", 0
    item = next(
        (
            value
            for value in values
            if isinstance(value, Mapping) and value.get("stage") == stage
        ),
        None,
    )
    if not isinstance(item, Mapping) or item.get("attempted") is not True:
        return "not_requested", 0
    hit_count = item.get("item_count")
    count = hit_count if isinstance(hit_count, int) and not isinstance(hit_count, bool) else 0
    return ("used" if count > 0 else "fallback"), max(0, count)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:40]
    return f"{prefix}-{digest}"


def _is_generation_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
