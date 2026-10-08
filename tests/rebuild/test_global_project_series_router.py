from __future__ import annotations

import json

from core.product_core.global_project_series_router import (
    CurrentGlobalProjectProjectionCatalog,
    FreshProjectProjection,
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


AUTHORITY_IDENTITY = "progressive-memory-authority-v1:test:test"
GENERATED_AT = "2026-07-29T10:00:00+08:00"


class Catalog:
    def __init__(self, *items: FreshProjectProjection) -> None:
        self.items = items

    def fresh_projections(self):
        return self.items


class GenerationAuthority:
    authority_identity = AUTHORITY_IDENTITY

    def __init__(self, tokens: dict[str, str | None]) -> None:
        self.tokens = tokens

    def generation_token(self, project_id: str) -> str | None:
        return self.tokens.get(project_id)


class ProjectionRepository:
    def __init__(
        self,
        manifests: list[dict[str, object]],
        currents: dict[str, tuple[str, ProjectionReadResult] | None],
    ) -> None:
        self._manifests = manifests
        self._currents = currents
        self.calls: list[tuple[str, str, str]] = []

    def manifests(self):
        return tuple(self._manifests)

    def load_current_for_generation(
        self,
        *,
        project_id: str,
        authority_identity: str,
        authority_generation_token: str,
    ):
        self.calls.append(
            (
                project_id,
                authority_identity,
                authority_generation_token,
            )
        )
        return self._currents.get(project_id)


def _fresh(
    *,
    project_id: str,
    series_id: str,
    title: str,
    overview: str,
    keywords: list[str],
    distractor_count: int = 0,
) -> FreshProjectProjection:
    def series_memory(
        *,
        item_id: str,
        item_series_id: str,
        item_title: str,
        item_overview: str,
        item_keywords: list[str],
    ) -> dict[str, object]:
        return {
            "id": item_id,
            "series_id": item_series_id,
            "title": item_title,
            "scope": "project",
            "overview": item_overview,
            "scenario_ids": [],
            "source_refs": [
                {
                    "source_id": f"source-{item_id}",
                    "locator": "section:overview",
                }
            ],
            "project_ids": [project_id],
            "stale": False,
            "revision": 1,
            "trust_status": "user_confirmed",
            "updated_at": GENERATED_AT,
            "tags": item_keywords,
        }

    series = series_memory(
        item_id=f"series-memory-{project_id}",
        item_series_id=series_id,
        item_title=title,
        item_overview=overview,
        item_keywords=keywords,
    )
    distractors = tuple(
        series_memory(
            item_id=f"series-memory-{project_id}-other-{index}",
            item_series_id=f"unrelated-{index}",
            item_title=f"无关资料 {index}",
            item_overview="与目标问题无关的其他系列。",
            item_keywords=["无关"],
        )
        for index in range(distractor_count)
    )
    projection = build_r0_r1_memory_projection(
        project_id=project_id,
        authority_identity=AUTHORITY_IDENTITY,
        series_memories=(series, *distractors),
        scenarios=(),
        atoms=(),
        project_skills=(),
        generated_at=GENERATED_AT,
    )
    payload = serialize_memory_retrieval_projection(projection)
    return FreshProjectProjection(
        project_id=project_id,
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=projection.authority_fingerprint,
        read_result=ProjectionReadResult(
            status="fresh",
            fallback_to_authority=False,
            reason_code="projection_fingerprint_current",
            projection=payload,
            manifest={},
        ),
    )


def test_global_router_selects_one_project_before_series_recall() -> None:
    router = GlobalProjectSeriesRouter(
        catalog=Catalog(
            _fresh(
                project_id="memory-os",
                series_id="progressive-memory",
                title="四层记忆系统",
                overview="渐进召回、系列投影和原始证据。",
                keywords=["记忆", "召回"],
            ),
            _fresh(
                project_id="brand-rice",
                series_id="rice-brand",
                title="桥米品牌",
                overview="区域公用品牌、包装与消费者价值。",
                keywords=["品牌", "桥米"],
            ),
        )
    )

    decision = router.route("桥米品牌整体方案")

    assert decision.status == "routed"
    assert decision.selected_project_id == "brand-rice"
    assert decision.candidates[0].series_candidates[0].series_id == "rice-brand"
    serialized = json.dumps(decision.to_payload(), ensure_ascii=False)
    assert "桥米品牌整体方案" not in serialized
    assert "区域公用品牌、包装与消费者价值" not in serialized


def test_global_router_requires_confirmation_for_cross_project_tie() -> None:
    router = GlobalProjectSeriesRouter(
        catalog=Catalog(
            _fresh(
                project_id="alpha",
                series_id="shared-alpha",
                title="共享研究",
                overview="共同主题的项目资料。",
                keywords=["共享", "研究"],
            ),
            _fresh(
                project_id="beta",
                series_id="shared-beta",
                title="共享研究",
                overview="共同主题的项目资料。",
                keywords=["共享", "研究"],
            ),
        )
    )

    decision = router.route("共享研究")

    assert decision.status == "ambiguous"
    assert decision.selected_project_id is None
    assert [candidate.project_id for candidate in decision.candidates[:2]] == [
        "alpha",
        "beta",
    ]


def test_global_router_score_is_not_biased_by_project_series_count() -> None:
    router = GlobalProjectSeriesRouter(
        catalog=Catalog(
            _fresh(
                project_id="compact-project",
                series_id="shared-compact",
                title="共享研究",
                overview="共同主题的项目资料。",
                keywords=["共享", "研究"],
            ),
            _fresh(
                project_id="large-project",
                series_id="shared-large",
                title="共享研究",
                overview="共同主题的项目资料。",
                keywords=["共享", "研究"],
                distractor_count=8,
            ),
        )
    )

    decision = router.route("共享研究")

    assert decision.status == "ambiguous"
    assert decision.candidates[0].score == decision.candidates[1].score


def test_global_router_falls_back_without_fresh_or_confident_match() -> None:
    empty = GlobalProjectSeriesRouter(catalog=Catalog()).route("任何问题")
    low = GlobalProjectSeriesRouter(
        catalog=Catalog(
            _fresh(
                project_id="alpha",
                series_id="memory-alpha",
                title="记忆",
                overview="分层资料。",
                keywords=["记忆"],
            )
        )
    ).route("完全无关的天气问题")

    assert empty.status == "fallback"
    assert empty.reason_code == "no_fresh_project_match"
    assert low.status == "fallback"
    assert low.reason_code == "no_fresh_project_match"


def test_global_router_only_constructs_ai_reranker_for_ambiguous_candidates() -> None:
    calls: list[str] = []

    class Reranker:
        def rerank(self, *, query, deterministic, candidates):
            calls.append(query)
            return deterministic

    router = GlobalProjectSeriesRouter(
        catalog=Catalog(
            _fresh(
                project_id="alpha",
                series_id="shared-alpha",
                title="共享研究",
                overview="共同主题的项目资料。",
                keywords=["共享", "研究"],
            ),
            _fresh(
                project_id="beta",
                series_id="shared-beta",
                title="共享研究",
                overview="共同主题的项目资料。",
                keywords=["共享", "研究"],
            ),
        ),
        reranker_factory=lambda: Reranker(),
    )

    ambiguous = router.route("共享研究")
    unique = GlobalProjectSeriesRouter(
        catalog=Catalog(
            _fresh(
                project_id="brand-rice",
                series_id="rice-brand",
                title="桥米品牌",
                overview="区域公用品牌与包装。",
                keywords=["桥米", "品牌"],
            ),
        ),
        reranker_factory=lambda: calls.append("factory") or Reranker(),
    ).route("桥米品牌")

    assert ambiguous.status == "ambiguous"
    assert calls == ["共享研究"]
    assert unique.status == "routed"
    assert calls == ["共享研究"]


def test_global_router_rejects_empty_and_oversized_query() -> None:
    router = GlobalProjectSeriesRouter(catalog=Catalog())

    for query in ("", " "):
        try:
            router.route(query)
        except ValueError as error:
            assert "query" in str(error)
        else:
            raise AssertionError("empty query must be rejected")

    try:
        router.route("问" * 4001)
    except ValueError as error:
        assert "limit" in str(error)
    else:
        raise AssertionError("oversized query must be rejected")


def test_current_catalog_only_returns_generation_bound_fresh_projections() -> None:
    fresh = _fresh(
        project_id="fresh-project",
        series_id="fresh-series",
        title="新鲜项目",
        overview="当前投影。",
        keywords=["新鲜"],
    )
    stale = _fresh(
        project_id="stale-project",
        series_id="stale-series",
        title="过期项目",
        overview="旧投影。",
        keywords=["过期"],
    )
    stale_result = ProjectionReadResult(
        status="stale",
        fallback_to_authority=True,
        reason_code="projection_fingerprint_changed",
        projection=stale.read_result.projection,
        manifest={},
    )
    repository = ProjectionRepository(
        manifests=[
            {"project_id": "fresh-project"},
            {"project_id": "stale-project"},
            {"project_id": "unbound-project"},
        ],
        currents={
            "fresh-project": (
                fresh.authority_fingerprint,
                fresh.read_result,
            ),
            "stale-project": (
                stale.authority_fingerprint,
                stale_result,
            ),
        },
    )
    catalog = CurrentGlobalProjectProjectionCatalog(
        projections=repository,
        authority=GenerationAuthority(
            {
                "fresh-project": "a" * 64,
                "stale-project": "b" * 64,
                "unbound-project": None,
            }
        ),
    )

    result = catalog.fresh_projections()

    assert [item.project_id for item in result] == ["fresh-project"]
    assert [call[0] for call in repository.calls] == [
        "fresh-project",
        "stale-project",
    ]
