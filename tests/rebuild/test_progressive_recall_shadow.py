from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

from core.product_core.memory_projection_builder import (
    build_r0_r1_memory_projection,
)
from core.product_core.memory_projection_repository import (
    ARTIFACT_COLLECTION,
    ObjectStoreMemoryProjectionRepository,
    ProjectionReadResult,
)
from core.product_core.progressive_recall_shadow import (
    ProgressiveRecallShadowError,
    route_progressive_memory_r0,
    run_progressive_recall_shadow,
)
from core.search_and_recall.ports import RecallIndexEntry
from core.search_and_recall.runtime import InMemoryRecallIndexAdapter
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = (
    ROOT
    / "core-contracts"
    / "rebuild"
    / "progressive_recall_shadow_trace.schema.json"
)
AUTHORITY_IDENTITY = "sqlite:structured-records-v1"
GENERATED_AT = "2026-07-26T16:00:00+08:00"


def _series(
    series_id: str,
    title: str,
    overview: str,
    *,
    scenario_id: str | None = None,
) -> dict[str, object]:
    return {
        "id": f"series-memory-{series_id}",
        "series_id": series_id,
        "title": title,
        "scope": "project",
        "overview": overview,
        "scenario_ids": [scenario_id] if scenario_id else [],
        "source_refs": [
            {
                "source_id": f"source-{series_id}",
                "locator": "section:overview",
                "quote": "SOURCE-BODY-MUST-NOT-ENTER-SHADOW",
            }
        ],
        "project_ids": ["project-1"],
        "stale": False,
        "revision": 1,
        "trust_status": "user_confirmed",
    }


def _scenario(
    scenario_id: str,
    series_id: str,
    *,
    tags: list[str],
) -> dict[str, object]:
    return {
        "id": scenario_id,
        "title": f"{series_id} 场景",
        "summary": f"{series_id} 的结构化工作场景。",
        "atom_ids": [],
        "source_refs": [
            {
                "source_id": f"source-{series_id}",
                "locator": "section:scenario",
            }
        ],
        "tags": tags,
        "series_id": series_id,
        "project_id": "project-1",
        "stale": False,
        "revision": 1,
        "trust_status": "trusted",
    }


def _projection(
    *,
    series: list[dict[str, object]] | None = None,
    scenarios: list[dict[str, object]] | None = None,
    authority_identity: str = AUTHORITY_IDENTITY,
):
    if series is None:
        series = [
            _series(
                "memory-system",
                "记忆系统",
                "四层记忆、渐进召回、投影失效和原始来源追溯。",
                scenario_id="scenario-memory",
            ),
            _series(
                "interview",
                "招聘面试",
                "岗位要求、面试题、候选人经历与复盘。",
                scenario_id="scenario-interview",
            ),
            _series(
                "brand-strategy",
                "品牌策略",
                "品牌定位、消费者价值和传播策略。",
                scenario_id="scenario-brand",
            ),
        ]
    if scenarios is None:
        scenarios = [
            _scenario(
                "scenario-memory",
                "memory-system",
                tags=["记忆", "召回", "memory"],
            ),
            _scenario(
                "scenario-interview",
                "interview",
                tags=["面试", "招聘", "interview"],
            ),
            _scenario(
                "scenario-brand",
                "brand-strategy",
                tags=["品牌", "策略", "brand"],
            ),
        ]
    return build_r0_r1_memory_projection(
        project_id="project-1",
        authority_identity=authority_identity,
        series_memories=series,
        scenarios=scenarios,
        atoms=[],
        project_skills=[],
        generated_at=GENERATED_AT,
    )


def _fresh_result(projection=None) -> ProjectionReadResult:
    projection = projection or _projection()
    return ProjectionReadResult(
        status="fresh",
        fallback_to_authority=False,
        reason_code="projection_fingerprint_current",
        projection=projection.to_payload(),
        manifest={"status": "ready"},
    )


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )


def _published_repository(
    tmp_path: Path,
    projection=None,
) -> ObjectStoreMemoryProjectionRepository:
    projection = projection or _projection()
    repository = ObjectStoreMemoryProjectionRepository(_store(tmp_path))
    repository.begin_rebuild(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        job_id="job-shadow-fixture",
        updated_at=GENERATED_AT,
    )
    artifact_id = repository.stage_projection(projection)
    repository.activate_staged(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        job_id="job-shadow-fixture",
        artifact_id=artifact_id,
        updated_at=GENERATED_AT,
    )
    return repository


def _route(
    query: str,
    *,
    projection=None,
    explicit_series_id: str | None = None,
):
    projection = projection or _projection()
    return route_progressive_memory_r0(
        read_result=_fresh_result(projection),
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        query=query,
        explicit_series_id=explicit_series_id,
    )


def _recall_index() -> InMemoryRecallIndexAdapter:
    return InMemoryRecallIndexAdapter(
        (
            RecallIndexEntry(
                object_id="atom-budget-project-1",
                project_id="project-1",
                layer="l1_atom",
                content="当前预算版本是 v3。",
                source_refs=("source-budget#section:version",),
                trust_status="trusted",
                base_score=0.7,
            ),
            RecallIndexEntry(
                object_id="series-memory-memory-system",
                project_id="project-1",
                layer="l3_series_memory",
                content="记忆系统与渐进召回。",
                source_refs=("source-memory#section:overview",),
                trust_status="user_confirmed",
                base_score=0.8,
            ),
            RecallIndexEntry(
                object_id="atom-budget-project-2",
                project_id="project-2",
                layer="l1_atom",
                content="PRIVATE-PROJECT-2-BUDGET-CONTENT",
                source_refs=("source-private#section:secret",),
                trust_status="trusted",
                base_score=1.0,
            ),
        )
    )


def test_chinese_query_routes_relevant_series() -> None:
    route = _route("记忆系统如何进行渐进召回和失效恢复？")

    assert route.status == "routed"
    assert route.confidence in {"high", "medium"}
    assert route.candidates[0].series_id == "memory-system"
    assert route.candidates[0].score >= 0.5
    assert "title" in route.candidates[0].matched_fields


def test_english_unicode_long_title_routes_deterministically() -> None:
    long_title = "Tencent AI HR Interview Preparation 🚀 " + "专项" * 100
    projection = _projection(
        series=[
            _series(
                "tencent-ai-hr",
                long_title,
                "腾讯 AI HR 岗位的英文面试准备和经历复盘。",
                scenario_id="scenario-tencent",
            ),
            _series(
                "memory-system",
                "Memory System",
                "Progressive memory routing and source evidence.",
                scenario_id="scenario-memory",
            ),
        ],
        scenarios=[
            _scenario(
                "scenario-tencent",
                "tencent-ai-hr",
                tags=["interview", "preparation", "腾讯"],
            ),
            _scenario(
                "scenario-memory",
                "memory-system",
                tags=["memory", "retrieval"],
            ),
        ],
    )

    first = _route(
        "Tencent AI HR interview preparation",
        projection=projection,
    )
    payload = projection.to_payload()
    payload["r0_items"] = list(reversed(payload["r0_items"]))
    reversed_result = ProjectionReadResult(
        status="fresh",
        fallback_to_authority=False,
        reason_code="projection_fingerprint_current",
        projection=payload,
        manifest={"status": "ready"},
    )
    second = route_progressive_memory_r0(
        read_result=reversed_result,
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        query="Tencent AI HR interview preparation",
    )

    assert first.candidates == second.candidates
    assert first.candidates[0].series_id == "tencent-ai-hr"
    assert first.confidence == "high"


def test_multi_topic_query_is_ambiguous_and_requires_fallback() -> None:
    route = _route("请整合记忆系统和招聘面试两个主题")

    assert route.status == "fallback"
    assert route.reason_code == "ambiguous_series"
    assert [candidate.series_id for candidate in route.candidates[:2]] == [
        "interview",
        "memory-system",
    ]


def test_explicit_series_id_routes_exactly_or_falls_back() -> None:
    selected = _route(
        "帮我处理这个系列",
        explicit_series_id="brand-strategy",
    )
    missing = _route(
        "帮我处理这个系列",
        explicit_series_id="missing-series",
    )

    assert selected.status == "routed"
    assert selected.reason_code == "explicit_series_match"
    assert selected.candidates[0].series_id == "brand-strategy"
    assert selected.candidates[0].explicit is True
    assert missing.status == "fallback"
    assert missing.reason_code == "explicit_series_not_found"


@pytest.mark.parametrize(
    ("status", "reason_code", "expected_reason"),
    (
        ("missing", "projection_manifest_missing", "projection_missing"),
        ("stale", "projection_rebuilding", "projection_stale"),
        ("stale", "projection_failed", "projection_stale"),
        ("stale", "projection_policy_changed", "projection_stale"),
        ("corrupt", "projection_artifact_corrupt", "projection_corrupt"),
    ),
)
def test_unavailable_projection_never_routes_body(
    status: str,
    reason_code: str,
    expected_reason: str,
) -> None:
    projection = _projection()
    route = route_progressive_memory_r0(
        read_result=ProjectionReadResult(
            status=status,
            fallback_to_authority=True,
            reason_code=reason_code,
            projection=None,
            manifest=None,
        ),
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        query="记忆系统",
    )

    assert route.status == "fallback"
    assert route.reason_code == expected_reason
    assert route.candidates == ()


def test_fresh_status_with_fallback_flag_is_not_trusted() -> None:
    projection = _projection()
    route = route_progressive_memory_r0(
        read_result=ProjectionReadResult(
            status="fresh",
            fallback_to_authority=True,
            reason_code="inconsistent_fixture",
            projection=projection.to_payload(),
            manifest={"status": "ready"},
        ),
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        query="记忆系统",
    )

    assert route.status == "fallback"
    assert route.reason_code == "projection_invalid"
    assert route.candidates == ()


def test_empty_or_malformed_projection_falls_back() -> None:
    empty = _projection(series=[], scenarios=[])
    empty_route = _route("任意问题", projection=empty)
    malformed_payload = _projection().to_payload()
    malformed_payload["r0_items"][0]["project_id"] = "project-2"
    malformed = ProjectionReadResult(
        status="fresh",
        fallback_to_authority=False,
        reason_code="projection_fingerprint_current",
        projection=malformed_payload,
        manifest={"status": "ready"},
    )
    malformed_route = route_progressive_memory_r0(
        read_result=malformed,
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=_projection().authority_fingerprint,
        query="记忆系统",
    )

    assert empty_route.reason_code == "projection_empty"
    assert empty_route.candidates == ()
    assert malformed_route.reason_code == "projection_invalid"
    assert malformed_route.candidates == ()


def test_old_projection_policy_is_rejected_by_direct_router() -> None:
    projection = _projection()
    payload = projection.to_payload()
    payload["projection_version"] = "progressive-memory-r0-r1-v0"
    read_result = ProjectionReadResult(
        status="fresh",
        fallback_to_authority=False,
        reason_code="projection_fingerprint_current",
        projection=payload,
        manifest={"status": "ready"},
    )

    route = route_progressive_memory_r0(
        read_result=read_result,
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        query="记忆系统",
    )

    assert route.status == "fallback"
    assert route.reason_code == "projection_invalid"


def test_fact_lookup_keeps_exact_atom_and_same_project_fallback(
    tmp_path: Path,
) -> None:
    projection = _projection()
    result = run_progressive_recall_shadow(
        projections=_published_repository(tmp_path, projection),
        recall=_recall_index(),
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        query="具体预算版本是多少？",
        legacy_hits=(),
    )
    payload = result.to_payload()

    assert result.route.fallback_required is True
    assert payload["fallback"]["exact_atom_requested"] is True
    assert payload["fallback"]["cross_series_requested"] is True
    assert payload["fallback"]["exact_atom_hit_ids"] == [
        "atom-budget-project-1",
    ]
    assert "atom-budget-project-2" not in json.dumps(payload)


def test_shadow_trace_is_private_contract_valid_and_prompt_neutral(
    tmp_path: Path,
) -> None:
    query_canary = "CANARY-RAW-QUERY-9631 记忆系统"
    hit_body_canary = "CANARY-LEGACY-HIT-BODY-8520"
    authority_canary = "sqlite:CANARY-PRIVATE-VAULT-PATH-4711"
    projection = _projection(authority_identity=authority_canary)
    result = run_progressive_recall_shadow(
        projections=_published_repository(tmp_path, projection),
        recall=_recall_index(),
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        query=query_canary,
        legacy_hits=(
            {
                "layer": "l3_series_memory",
                "object_id": "series-memory-memory-system",
                "project_id": "project-1",
                "snippet": hit_body_canary,
                "source_refs": [
                    {
                        "source_id": "source-private",
                        "locator": "section:private",
                    }
                ],
            },
        ),
    )
    payload = result.to_payload()
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert validate_contract_instance(
        SCHEMA_PATH.name,
        schema,
        payload,
    ) == []
    assert query_canary not in serialized
    assert hit_body_canary not in serialized
    assert authority_canary not in serialized
    assert "SOURCE-BODY-MUST-NOT-ENTER-SHADOW" not in serialized
    assert '"source_refs"' not in serialized
    assert payload["safety"] == {
        "read_only": True,
        "prompt_unchanged": True,
        "raw_query_recorded": False,
        "hit_content_recorded": False,
        "source_refs_recorded": False,
        "business_writes_allowed": False,
        "team_memory_body_allowed": False,
    }
    assert payload["comparison"]["production_cutover_allowed"] is False


def test_shadow_comparison_uses_only_same_project_series_ids(
    tmp_path: Path,
) -> None:
    projection = _projection()
    result = run_progressive_recall_shadow(
        projections=_published_repository(tmp_path, projection),
        recall=_recall_index(),
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        query="记忆系统",
        legacy_hits=(
            {
                "layer": "l3_series_memory",
                "object_id": "series-memory-memory-system",
                "project_id": "project-1",
            },
            {
                "layer": "l3_series_memory",
                "object_id": "series-memory-secret",
                "project_id": "project-2",
            },
        ),
    )
    payload = result.to_payload()

    assert payload["legacy"] == {
        "hit_count": 1,
        "series_memory_ids": ["series-memory-memory-system"],
    }
    assert payload["comparison"]["top1_agreement"] is True
    assert payload["comparison"]["legacy_coverage_ratio"] == 1.0
    assert "series-memory-secret" not in json.dumps(payload)


class _FailingRecall:
    def recall(self, query):
        raise RuntimeError("injected read-only index failure")


class _RecallSpy:
    def __init__(self) -> None:
        self.queries = []

    def recall(self, query):
        self.queries.append(query)
        return ()


def test_confident_standard_route_does_not_run_fallback_search(
    tmp_path: Path,
) -> None:
    projection = _projection()
    recall = _RecallSpy()

    result = run_progressive_recall_shadow(
        projections=_published_repository(tmp_path, projection),
        recall=recall,
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        query="记忆系统",
    )
    payload = result.to_payload()

    assert result.route.status == "routed"
    assert recall.queries == []
    assert payload["fallback"]["required"] is False
    assert payload["fallback"]["exact_atom_requested"] is False
    assert payload["fallback"]["cross_series_requested"] is False


class _FailingProjectionRepository:
    def load_current(self, **kwargs):
        raise OSError("injected projection storage failure with private path")


def test_shadow_projection_store_failure_preserves_legacy_recall() -> None:
    projection = _projection()
    result = run_progressive_recall_shadow(
        projections=_FailingProjectionRepository(),  # type: ignore[arg-type]
        recall=_recall_index(),
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        query="记忆系统",
        legacy_hits=(
            {
                "layer": "l3_series_memory",
                "object_id": "series-memory-memory-system",
                "project_id": "project-1",
            },
        ),
    )
    payload = result.to_payload()

    assert result.route.reason_code == "projection_corrupt"
    assert payload["projection"]["reason_code"] == (
        "projection_repository_unavailable"
    )
    assert payload["fallback"]["legacy_recall_preserved"] is True
    assert "private path" not in json.dumps(payload)


def test_shadow_index_failure_is_anonymous_and_does_not_fail_request(
    tmp_path: Path,
) -> None:
    projection = _projection()
    result = run_progressive_recall_shadow(
        projections=_published_repository(tmp_path, projection),
        recall=_FailingRecall(),
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        query="具体不存在的版本是多少？",
    )
    payload = result.to_payload()

    assert payload["fallback"]["error_codes"] == [
        "exact_atom_unavailable",
        "cross_series_unavailable",
    ]
    assert payload["safety"]["prompt_unchanged"] is True


@pytest.mark.parametrize("top_k", [0, 6, True])
def test_invalid_top_k_fails_closed(top_k: object) -> None:
    projection = _projection()

    with pytest.raises(ProgressiveRecallShadowError):
        route_progressive_memory_r0(
            read_result=_fresh_result(projection),
            project_id=projection.project_id,
            authority_identity=projection.authority_identity,
            authority_fingerprint=projection.authority_fingerprint,
            query="记忆系统",
            top_k=top_k,  # type: ignore[arg-type]
        )


def test_r0_router_meets_1000_series_cold_budget() -> None:
    series = [
        _series(
            f"topic-{index:04d}",
            f"主题{index:04d}",
            f"主题{index:04d} 的专项资料、长期摘要和工作记录。",
        )
        for index in range(1000)
    ]
    projection = _projection(series=series, scenarios=[])
    read_result = _fresh_result(projection)
    durations: list[float] = []

    for _ in range(20):
        started = time.perf_counter()
        route = route_progressive_memory_r0(
            read_result=read_result,
            project_id=projection.project_id,
            authority_identity=projection.authority_identity,
            authority_fingerprint=projection.authority_fingerprint,
            query="请查看主题0731的专项资料",
        )
        durations.append((time.perf_counter() - started) * 1000)

    ordered = sorted(durations)
    p50 = ordered[len(ordered) // 2]
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    assert route.candidates[0].series_id == "topic-0731"
    assert p50 < 100, f"R0 p50 exceeded target: {p50:.3f} ms"
    assert p95 < 100, f"R0 p95 exceeded target: {p95:.3f} ms"


def test_corrupt_artifact_shadow_uses_fallback_without_body(
    tmp_path: Path,
) -> None:
    projection = _projection()
    repository = _published_repository(tmp_path, projection)
    store = repository.object_store
    artifact_id = next(
        str(item["artifact_id"])
        for item in store.list(ARTIFACT_COLLECTION)
    )
    artifact = copy.deepcopy(
        store.read(ARTIFACT_COLLECTION, artifact_id)
    )
    assert artifact is not None
    artifact["projection_digest"] = "0" * 64
    store.write(
        ARTIFACT_COLLECTION,
        artifact_id,
        artifact,
        expected_revision=1,
    )

    result = run_progressive_recall_shadow(
        projections=repository,
        recall=_recall_index(),
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        query="记忆系统",
    )

    assert result.route.reason_code == "projection_corrupt"
    assert result.route.candidates == ()
    assert result.to_payload()["projection"]["projection_available"] is False
