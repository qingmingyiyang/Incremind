from __future__ import annotations

import json

import pytest

from core.product_core.memory_maintenance_plan import (
    MemoryMaintenancePlanError,
    build_memory_maintenance_plan,
)


def _item(
    object_id: str,
    *,
    title: str,
    preview: str,
    tags: list[str],
    revision: int = 1,
) -> dict[str, object]:
    return {
        "id": object_id,
        "object_id": object_id,
        "title": title,
        "preview": preview,
        "tags": tags,
        "revision": revision,
        "object_revision": revision,
        "series_id": None,
        "atom_ids": [],
        "scenario_ids": [],
    }


def _plan(project_skill=None):
    atoms = [
        _item(
            "atom-duplicate-a",
            title="atom-duplicate-a",
            preview="用户喜欢热豆浆",
            tags=["饮食", "偏好"],
        ),
        _item(
            "atom-duplicate-b",
            title="atom-duplicate-b",
            preview=" 用户喜欢热豆浆 ",
            tags=["饮食", "偏好"],
        ),
        _item(
            "atom-conflict",
            title="atom-conflict",
            preview="用户不喜欢热豆浆",
            tags=["饮食", "偏好"],
        ),
    ]
    scenarios = [
        {
            **_item(
                "scenario-a",
                title="桥米包装场景",
                preview="桥米品牌包装与消费者价值",
                tags=["桥米", "包装"],
            ),
            "series_id": "series-a",
            "atom_ids": ["atom-duplicate-a"],
        }
    ]
    series = [
        {
            **_item(
                "series-object-a",
                title="桥米品牌包装策略",
                preview="桥米包装与消费者价值",
                tags=["桥米", "品牌", "包装", "消费者"],
            ),
            "id": "series-a",
            "scenario_ids": ["scenario-a"],
        },
        {
            **_item(
                "series-object-b",
                title="桥米品牌包装方案",
                preview="桥米包装与消费者价值",
                tags=["桥米", "品牌", "包装", "消费者"],
            ),
            "id": "series-b",
            "scenario_ids": [],
        },
    ]
    classification = [
        {
            "scenario_id": "scenario-a",
            "candidates": [
                {
                    "series_id": "series-b",
                    "score": 0.88,
                }
            ],
        }
    ]
    freshness = [
        {
            "series_object_id": "series-object-a",
            "needs_refresh": True,
            "reasons": [
                {"code": "scenario_revision_newer"}
            ],
        }
    ]
    return build_memory_maintenance_plan(
        project_id="brand-project",
        atoms=atoms,
        scenarios=scenarios,
        series=series,
        classification_suggestions=classification,
        freshness_items=freshness,
        project_skill=project_skill,
    )


def test_plan_unifies_safe_candidates_and_manual_only_high_risk_reviews() -> None:
    plan = _plan()
    payload = plan.to_payload()
    by_type = {
        item["suggestion_type"]: item
        for item in payload["suggestions"]
    }

    assert set(by_type) == {
        "scenario_reclassification",
        "series_refresh",
        "duplicate_atom_merge_review",
        "fact_replacement_review",
        "series_merge_review",
        "project_skill_refresh_review",
    }
    assert by_type["scenario_reclassification"][
        "batch_candidate_supported"
    ] is True
    assert by_type["series_refresh"]["candidate_supported"] is True
    for suggestion_type in (
        "duplicate_atom_merge_review",
        "fact_replacement_review",
        "series_merge_review",
        "project_skill_refresh_review",
    ):
        assert by_type[suggestion_type]["candidate_supported"] is False
        assert by_type[suggestion_type]["action_endpoint"] is None
    assert all(
        item["requires_user_confirmation"] is True
        and item["auto_apply_allowed"] is False
        and item["content_included"] is False
        for item in payload["suggestions"]
    )
    assert payload["writes_performed"] is False
    assert payload["network_called"] is False


def test_plan_is_stable_and_omits_atom_and_series_content() -> None:
    first = _plan().to_payload()
    second = _plan().to_payload()

    assert first == second
    serialized = json.dumps(first, ensure_ascii=False)
    assert "用户喜欢热豆浆" not in serialized
    assert "桥米包装与消费者价值" not in serialized
    assert "preview" not in serialized
    assert len(first["plan_fingerprint"]) == 64


def test_current_project_skill_suppresses_unneeded_refresh() -> None:
    plan = _plan(
        {
            "id": "skill-brand",
            "project_id": "brand-project",
            "revision": 2,
            "purpose": (
                "桥米品牌包装策略 桥米品牌包装方案 "
                "桥米包装与消费者价值 桥米 品牌 包装 消费者"
            ),
        }
    )

    assert "project_skill_refresh_review" not in {
        item.suggestion_type for item in plan.suggestions
    }


def test_plan_rejects_stale_or_duplicate_authority_objects() -> None:
    atom = _item(
        "atom-a",
        title="atom-a",
        preview="有效事实",
        tags=["事实"],
    )
    with pytest.raises(
        MemoryMaintenancePlanError,
        match="object IDs must be unique",
    ):
        build_memory_maintenance_plan(
            project_id="project-a",
            atoms=[atom, atom],
            scenarios=[],
            series=[],
            classification_suggestions=[],
            freshness_items=[],
            project_skill=None,
        )

    with pytest.raises(
        MemoryMaintenancePlanError,
        match="revisions are invalid",
    ):
        build_memory_maintenance_plan(
            project_id="project-a",
            atoms=[{**atom, "object_revision": 0}],
            scenarios=[],
            series=[],
            classification_suggestions=[],
            freshness_items=[],
            project_skill=None,
        )
