from __future__ import annotations

from backend.api.routes.product.memory_hierarchy import (
    _local_series_refresh_overview,
    _memory_classification_tokens,
    _rank_memory_series_suggestions,
)


def test_local_series_suggestions_are_explainable_ranked_and_exclude_current() -> None:
    scenario = {
        "object_id": "scenario-startup",
        "title": "桌面冷启动优化",
        "preview": "后端并行预热，窗口优先出现，缩短冷启动等待。",
        "tags": ["启动性能"],
        "series_id": "series-current",
    }
    series = [
        {
            "id": "series-current",
            "object_id": "series-current-object",
            "title": "桌面冷启动",
            "preview": "当前归类不能形成无变化建议。",
            "revision": 1,
        },
        {
            "id": "series-performance",
            "object_id": "series-performance-object",
            "title": "桌面性能优化",
            "preview": "覆盖冷启动、后端预热和窗口展示速度。",
            "tags": ["启动性能"],
            "revision": 3,
        },
        {
            "id": "series-unrelated",
            "object_id": "series-unrelated-object",
            "title": "品牌设计",
            "preview": "颜色、字体和视觉规范。",
            "revision": 2,
        },
    ]

    first = _rank_memory_series_suggestions(scenario=scenario, series=series)
    second = _rank_memory_series_suggestions(scenario=scenario, series=series)

    assert first == second
    assert [value["series_id"] for value in first] == ["series-performance"]
    assert 0.12 <= first[0]["score"] <= 1.0
    assert first[0]["matched_features"]
    assert "本地结构化特征" in first[0]["explanation"]
    assert all(value["series_id"] != "series-current" for value in first)


def test_local_series_suggestions_return_empty_without_sufficient_overlap() -> None:
    assert _rank_memory_series_suggestions(
        scenario={
            "object_id": "scenario-1",
            "title": "季度预算",
            "preview": "现金流和采购审批。",
            "series_id": "series-current",
        },
        series=[
            {
                "id": "series-design",
                "object_id": "series-design-object",
                "title": "视觉设计",
                "preview": "图标和排版规范。",
                "revision": 1,
            }
        ],
    ) == []


def test_classification_tokens_are_bounded_normalized_and_drop_common_words() -> None:
    tokens = _memory_classification_tokens(
        " 当前项目需要进行桌面冷启动优化  BACKEND-warmup backend-warmup "
    )

    assert "当前" not in tokens
    assert "项目" not in tokens
    assert "需要" not in tokens
    assert "backend-warmup" in tokens
    assert "冷启" in tokens
    assert all(2 <= len(value) <= 32 for value in tokens)


def test_series_freshness_helper_builds_bounded_local_overview() -> None:
    overview = _local_series_refresh_overview(
        series_title="启动性能",
        scenarios=[
            {"title": "冷启动", "preview": "窗口先出现，后端并行预热。"},
            {"title": "恢复", "preview": "失败后保持来源证据。"},
        ],
        fallback="旧总览",
    )
    assert overview.startswith("启动性能：冷启动")
    assert "恢复 — 失败后保持来源证据" in overview
    assert len(overview) <= 1200
