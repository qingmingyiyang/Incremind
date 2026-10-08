from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.replay.contracts import (
    CORE_SCHEMA_MODELS,
    DateRange,
    KnowledgeItem,
    KnowledgeSource,
    Report,
    ReplayTask,
    VisionClaim,
    VisionContract,
    exportable_schema,
)


ROOT = Path(__file__).resolve().parents[4]


def test_knowledge_item_uses_stable_nested_contract() -> None:
    item = KnowledgeItem(
        type="thought",
        title="复盘桌面架构",
        content="业务保持在本地服务层。",
        source=KnowledgeSource(kind="manual"),
    )

    payload = item.model_dump(mode="json")

    assert payload["schema_version"] == "1.0"
    assert payload["id"].startswith("item_")
    assert payload["status"]["in_daily"] is False
    assert payload["evidence"]["chunk_ids"] == []
    assert payload["links"]["linked_video_ids"] == []
    assert payload["index"]["available"] is False


def test_contracts_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        KnowledgeItem(
            type="note",
            title="非法字段",
            source=KnowledgeSource(kind="manual"),
            hidden_business_state=True,
        )


def test_report_task_and_visual_contracts_preserve_traceability() -> None:
    report = Report(
        type="daily",
        title="2026-06-23 日报",
        date_range=DateRange(start="2026-06-23", end="2026-06-23"),
    )
    task = ReplayTask(type="daily_report", date_range=report.date_range)
    visual = VisionContract(
        mode="mock",
        provider="mock",
        model="deterministic-schema-v1",
        is_mock=True,
        claims=[
            VisionClaim(
                frame_id="frame-0001",
                timestamp="00:01:20",
                claim="Mock 占位内容",
                source_mode="mock",
            )
        ],
    )

    assert report.sources.previous_reports == []
    assert report.index.available is True
    assert task.status == "pending"
    assert visual.is_mock is True
    assert visual.claims[0].source_mode == "mock"


@pytest.mark.parametrize("file_name", sorted(CORE_SCHEMA_MODELS))
def test_committed_json_schema_matches_pydantic_contract(file_name: str) -> None:
    committed = json.loads((ROOT / "core-contracts" / file_name).read_text(encoding="utf-8"))

    assert committed == exportable_schema(file_name)
