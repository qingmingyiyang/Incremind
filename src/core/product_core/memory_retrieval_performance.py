from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence


TRACE_VERSIONS = frozenset(
    {
        "progressive-direct-question-v2",
        "progressive-direct-question-v3",
        "progressive-direct-question-v4",
    }
)
STAGES = (
    "r0_series_router",
    "r1_series_digest",
    "r2_structured_content",
    "r3_source_evidence",
)
MAX_SAMPLES = 500
MAX_ELAPSED_MS = 3_600_000.0
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


def aggregate_memory_retrieval_performance(
    records: Sequence[Mapping[str, object]],
    *,
    project_id: str,
    limit: int = MAX_SAMPLES,
) -> dict[str, object]:
    safe_limit = min(MAX_SAMPLES, max(1, limit))
    candidates = sorted(
        (
            record
            for record in records
            if isinstance(record, Mapping)
            and _trace_project_id(record.get("recall_trace")) == project_id
        ),
        key=lambda record: str(record.get("created_at") or ""),
        reverse=True,
    )[:safe_limit]

    stage_values: dict[str, list[float]] = {stage: [] for stage in STAGES}
    stage_used: Counter[str] = Counter()
    stage_fallback: Counter[str] = Counter()
    stage_hits: Counter[str] = Counter()
    total_values: list[float] = []
    reason_counts: Counter[str] = Counter()
    timed_sample_count = 0
    ignored_trace_count = 0
    fallback_samples = 0

    for record in candidates:
        trace = record.get("recall_trace")
        if not isinstance(trace, Mapping) or trace.get("trace_version") not in TRACE_VERSIONS:
            ignored_trace_count += 1
            continue
        performance = trace.get("performance")
        if not isinstance(performance, Mapping):
            ignored_trace_count += 1
            continue
        total_ms = _safe_elapsed(performance.get("total_ms"))
        stages = performance.get("stages")
        if total_ms is None or not isinstance(stages, list):
            ignored_trace_count += 1
            continue
        parsed_stages = _parse_stages(stages)
        if parsed_stages is None:
            ignored_trace_count += 1
            continue
        total_values.append(total_ms)
        timed_sample_count += 1
        fallback = trace.get("fallback")
        if isinstance(fallback, Mapping) and fallback.get("used") is True:
            fallback_samples += 1
        for stage, status, elapsed_ms, hit_count in parsed_stages:
            if status == "not_requested":
                continue
            stage_values[stage].append(elapsed_ms)
            stage_hits[stage] += hit_count
            if status == "used":
                stage_used[stage] += 1
            elif status == "fallback":
                stage_fallback[stage] += 1
        for code in _reason_codes(trace):
            reason_counts[code] += 1

    return {
        "schema_version": "1.0.0",
        "project_id": project_id,
        "window": {
            "limit": safe_limit,
            "matching_sample_count": len(candidates),
            "timed_sample_count": timed_sample_count,
            "ignored_trace_count": ignored_trace_count,
        },
        "overall": {
            "average_ms": _average(total_values),
            "p95_ms": _percentile_95(total_values),
            "fallback_count": fallback_samples,
            "fallback_rate": _ratio(fallback_samples, timed_sample_count),
        },
        "stages": [
            {
                "stage": stage,
                "attempt_count": len(stage_values[stage]),
                "used_count": stage_used[stage],
                "fallback_count": stage_fallback[stage],
                "hit_count": stage_hits[stage],
                "average_ms": _average(stage_values[stage]),
                "p95_ms": _percentile_95(stage_values[stage]),
            }
            for stage in STAGES
        ],
        "reason_counts": [
            {"code": code, "count": count}
            for code, count in sorted(
                reason_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )[:8]
        ],
        "safety": {
            "read_only": True,
            "query_included": False,
            "fingerprints_included": False,
            "content_included": False,
            "object_ids_included": False,
            "source_refs_included": False,
            "paths_included": False,
            "urls_included": False,
            "provider_payload_included": False,
        },
    }


def _trace_project_id(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    project_id = value.get("project_id")
    return project_id if isinstance(project_id, str) and project_id else None


def _parse_stages(
    values: list[object],
) -> tuple[tuple[str, str, float, int], ...] | None:
    if len(values) != len(STAGES):
        return None
    parsed: list[tuple[str, str, float, int]] = []
    for expected_stage, value in zip(STAGES, values, strict=True):
        if not isinstance(value, Mapping) or value.get("stage") != expected_stage:
            return None
        status = value.get("status")
        elapsed_ms = _safe_elapsed(value.get("elapsed_ms"))
        hit_count = value.get("hit_count")
        if (
            status not in {"used", "fallback", "not_requested"}
            or elapsed_ms is None
            or isinstance(hit_count, bool)
            or not isinstance(hit_count, int)
            or not 0 <= hit_count <= 12
        ):
            return None
        parsed.append((expected_stage, status, elapsed_ms, hit_count))
    return tuple(parsed)


def _reason_codes(trace: Mapping[str, object]) -> tuple[str, ...]:
    values: list[object] = []
    for field in ("projection", "route", "fallback"):
        section = trace.get(field)
        if isinstance(section, Mapping):
            values.append(section.get("reason_code"))
    return tuple(
        value
        for value in values
        if isinstance(value, str) and _SAFE_REASON.fullmatch(value)
    )


def _safe_elapsed(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    if not math.isfinite(converted) or not 0.0 <= converted <= MAX_ELAPSED_MS:
        return None
    return round(converted, 3)


def _average(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _percentile_95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return round(ordered[index], 3)


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)
