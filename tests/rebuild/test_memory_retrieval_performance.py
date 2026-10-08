from __future__ import annotations

import json
import math

from core.product_core.memory_retrieval_performance import (
    aggregate_memory_retrieval_performance,
)


def _record(
    *,
    project_id: str = "default",
    total_ms: float = 12.0,
    r0_ms: float = 3.0,
    r1_ms: float = 4.0,
    fallback: bool = False,
) -> dict[str, object]:
    return {
        "id": "private-record-id",
        "question": "private question body",
        "created_at": "2026-07-26T12:00:00+00:00",
        "recall_trace": {
            "trace_version": "progressive-direct-question-v2",
            "project_id": project_id,
            "query_fingerprint": "a" * 64,
            "projection": {"reason_code": "projection_fresh"},
            "route": {"reason_code": "route_confident"},
            "fallback": {
                "used": fallback,
                "reason_code": "no_match" if fallback else None,
            },
            "performance": {
                "total_ms": total_ms,
                "legacy_recall_ms": 5.0,
                "stages": [
                    {"stage": "r0_series_router", "status": "fallback" if fallback else "used", "elapsed_ms": r0_ms, "hit_count": 2},
                    {"stage": "r1_series_digest", "status": "not_requested" if fallback else "used", "elapsed_ms": r1_ms, "hit_count": 1 if not fallback else 0},
                    {"stage": "r2_structured_content", "status": "not_requested", "elapsed_ms": 0.0, "hit_count": 0},
                    {"stage": "r3_source_evidence", "status": "not_requested", "elapsed_ms": 0.0, "hit_count": 0},
                ],
            },
        },
    }


def test_aggregate_memory_retrieval_performance_is_project_scoped_and_content_free() -> None:
    payload = aggregate_memory_retrieval_performance(
        [
            _record(total_ms=10.0, r0_ms=2.0, r1_ms=3.0),
            _record(total_ms=20.0, r0_ms=4.0, r1_ms=5.0, fallback=True),
            _record(project_id="other", total_ms=999.0),
        ],
        project_id="default",
    )

    assert payload["window"] == {
        "limit": 500,
        "matching_sample_count": 2,
        "timed_sample_count": 2,
        "ignored_trace_count": 0,
    }
    assert payload["overall"] == {
        "average_ms": 15.0,
        "p95_ms": 20.0,
        "fallback_count": 1,
        "fallback_rate": 0.5,
    }
    r0, r1, r2, r3 = payload["stages"]
    assert r0["attempt_count"] == 2
    assert r0["average_ms"] == 3.0
    assert r0["used_count"] == 1
    assert r0["fallback_count"] == 1
    assert r1["attempt_count"] == 1
    assert r1["hit_count"] == 1
    assert r2["attempt_count"] == r3["attempt_count"] == 0
    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("private question body", "private-record-id", '"query_fingerprint"'):
        assert forbidden not in serialized


def test_aggregate_ignores_legacy_corrupt_and_non_finite_timing() -> None:
    legacy = _record()
    legacy["recall_trace"]["trace_version"] = "progressive-direct-question-v1"
    nan_record = _record(total_ms=math.nan)
    infinity_record = _record(total_ms=math.inf)
    negative_record = _record(total_ms=-1)
    malformed_stage = _record()
    malformed_stage["recall_trace"]["performance"]["stages"][0]["stage"] = "private-stage"

    payload = aggregate_memory_retrieval_performance(
        [legacy, nan_record, infinity_record, negative_record, malformed_stage],
        project_id="default",
    )

    assert payload["window"]["matching_sample_count"] == 5
    assert payload["window"]["timed_sample_count"] == 0
    assert payload["window"]["ignored_trace_count"] == 5
    assert payload["overall"]["average_ms"] is None
    assert payload["overall"]["fallback_rate"] is None


def test_aggregate_bounds_window_and_reason_codes() -> None:
    safe = _record()
    safe["recall_trace"]["fallback"]["reason_code"] = "no_match"
    unsafe = _record()
    unsafe["recall_trace"]["route"]["reason_code"] = "private value with spaces"

    payload = aggregate_memory_retrieval_performance(
        [safe, unsafe],
        project_id="default",
        limit=9999,
    )

    assert payload["window"]["limit"] == 500
    assert {"code": "no_match", "count": 1} in payload["reason_counts"]
    assert all(item["code"] != "private value with spaces" for item in payload["reason_counts"])
