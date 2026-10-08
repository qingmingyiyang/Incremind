from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.routes.product import (
    memory_hierarchy as product_memory_hierarchy,
    repositories as product_repositories,
)


def _item(
    object_id: str,
    *,
    title: str,
    preview: str,
    tags: list[str],
) -> dict[str, object]:
    return {
        "id": object_id,
        "object_id": object_id,
        "title": title,
        "preview": preview,
        "tags": tags,
        "revision": 1,
        "object_revision": 1,
        "series_id": None,
        "atom_ids": [],
        "scenario_ids": [],
    }


def test_maintenance_plan_composes_existing_authorities_without_writes(
    tmp_path,
    monkeypatch,
) -> None:
    atoms = [
        _item(
            "atom-a",
            title="atom-a",
            preview="PRIVATE-MAINTENANCE-CANARY",
            tags=["偏好", "饮食"],
        ),
        _item(
            "atom-b",
            title="atom-b",
            preview="PRIVATE-MAINTENANCE-CANARY",
            tags=["偏好", "饮食"],
        ),
    ]
    scenario = {
        **_item(
            "scenario-a",
            title="桥米包装",
            preview="桥米品牌包装",
            tags=["桥米", "包装"],
        ),
        "series_id": "series-a",
        "atom_ids": ["atom-a"],
    }
    series_a = {
        **_item(
            "series-object-a",
            title="桥米品牌包装策略",
            preview="桥米包装消费者价值",
            tags=["桥米", "品牌", "包装", "消费者"],
        ),
        "id": "series-a",
        "scenario_ids": ["scenario-a"],
    }
    series_b = {
        **_item(
            "series-object-b",
            title="桥米品牌包装方案",
            preview="桥米包装消费者价值",
            tags=["桥米", "品牌", "包装", "消费者"],
        ),
        "id": "series-b",
        "scenario_ids": [],
    }
    calls: list[tuple[str, str]] = []

    class Records:
        def read(self, collection, object_id):
            calls.append((f"read:{collection}", object_id))
            return None

    def hierarchy(*, project_id, container, unavailable_detail):
        del container
        assert unavailable_detail == (
            "Memory maintenance plan requires active SQLite authority"
        )
        calls.append(("snapshot", project_id))
        return None, SimpleNamespace(namespace_id="default"), Records(), {
            "project_id": project_id,
            "atoms": atoms,
            "scenarios": [scenario],
            "series": [series_a, series_b],
        }

    class Skills:
        def load(self, project_id):
            calls.append(("skill", project_id))
            return None

    monkeypatch.setattr(
        product_memory_hierarchy,
        "_memory_hierarchy_snapshot",
        hierarchy,
    )
    monkeypatch.setattr(
        product_repositories,
        "_project_skill_repository",
        lambda *_args, **_kwargs: Skills(),
    )
    client = TestClient(
        create_app(SimpleNamespace(root_dir=tmp_path))
    )

    response = client.get(
        "/api/rebuild/projects/brand%20project/"
        "memory-maintenance-plan"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project_id"] == "brand project"
    assert body["writes_performed"] is False
    assert body["network_called"] is False
    assert body["content_included"] is False
    assert body["candidate_contracts"][
        "scenario_reclassification"
    ]["candidate_endpoint"].endswith("update-candidates/batch")
    assert set(body["unsupported_automatic_actions"]) == {
        "duplicate_atom_merge_review",
        "fact_replacement_review",
        "series_merge_review",
        "project_skill_refresh_review",
    }
    assert calls == [
        ("snapshot", "brand project"),
        ("read:memory_series_memory", "series-object-a"),
        (
            "read:memory_series_freshness_receipts",
            "series-object-a~r1",
        ),
        ("read:memory_series_memory", "series-object-b"),
        (
            "read:memory_series_freshness_receipts",
            "series-object-b~r1",
        ),
        ("skill", "brand project"),
    ]
    serialized = json.dumps(body, ensure_ascii=False)
    assert "PRIVATE-MAINTENANCE-CANARY" not in serialized
    assert all(
        suggestion["requires_user_confirmation"] is True
        and suggestion["auto_apply_allowed"] is False
        for suggestion in body["suggestions"]
    )


def test_maintenance_plan_rejects_empty_project_scope(
    tmp_path,
) -> None:
    client = TestClient(
        create_app(SimpleNamespace(root_dir=tmp_path))
    )

    response = client.get(
        "/api/rebuild/projects/%20/memory-maintenance-plan"
    )

    assert response.status_code == 400
