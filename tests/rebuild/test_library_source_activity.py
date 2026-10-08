from __future__ import annotations

from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.product_core import (
    BulkLibraryItemAction,
    QuerySourceActivity,
    UpdateLibrarySourceMetadata,
    serialize_source_activity_result,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _source(store: JsonObjectStore, title: str = "活动来源") -> str:
    item = ObjectStoreSourceRegistrar(store).register(
        SourceSubmission(kind="text", title=title, content=f"{title} 的正文内容")
    )
    return str(item["id"])


def test_source_activity_empty_for_source_without_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source_id = _source(store)

    result = QuerySourceActivity(store).execute(source_id=source_id)

    assert result.status == "completed"
    assert result.events == ()


def test_source_activity_not_found_and_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)

    missing = QuerySourceActivity(store).execute(source_id="missing-source")
    assert missing.status == "not_found"

    rejected = QuerySourceActivity(store).execute(source_id="  ")
    assert rejected.status == "rejected"


def test_source_activity_lists_latest_operations_with_labels(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source_id = _source(store)

    BulkLibraryItemAction(store).execute(
        action="move_series", item_ids=[source_id], series_name="活动系列",
    )
    BulkLibraryItemAction(store).execute(
        action="move_project", item_ids=[source_id], project_id="project-activity",
    )
    BulkLibraryItemAction(store).execute(
        action="attach_tags", item_ids=[source_id], tags=("活动标签",),
    )
    UpdateLibrarySourceMetadata(store).execute(
        source_id=source_id,
        expected_revision=store.revision("sources", source_id),
        title="活动标题",
        tags=("活动标签",),
    )

    result = QuerySourceActivity(store).execute(source_id=source_id)
    assert result.status == "completed"

    by_type = {event.type: event for event in result.events}
    assert set(by_type) == {
        "library_series_moved", "library_project_moved", "library_tags_attached", "library_source_edited",
    }
    assert by_type["library_series_moved"].label == "移动系列"
    assert by_type["library_series_moved"].summary == "系列调整为「活动系列」"
    assert by_type["library_project_moved"].summary == "分组调整为「project-activity」"
    assert by_type["library_tags_attached"].summary == "添加标签：活动标签"
    assert by_type["library_source_edited"].summary == "更新资料信息（活动标题）"
    for event in result.events:
        assert isinstance(event.revision, int)

    payload = serialize_source_activity_result(result)
    assert payload["status"] == "completed"
    assert payload["source_id"] == source_id
    assert {event["type"] for event in payload["events"]} == set(by_type)


def test_source_activity_excludes_other_sources_and_unknown_types(tmp_path: Path) -> None:
    store = _store(tmp_path)
    alpha = _source(store, title="Alpha")
    beta = _source(store, title="Beta")

    BulkLibraryItemAction(store).execute(
        action="attach_tags", item_ids=[alpha], tags=("甲",),
    )
    # beta 上写入一个非白名单 event type，读取端应过滤
    store.write("activity_events", "event-unknown-beta", {
        "schema_version": "1.0.0",
        "id": "event-unknown-beta",
        "type": "library_something_new",
        "source_id": beta,
        "status": "completed",
        "details": {},
        "created_at": "2026-07-03T21:30:00+08:00",
        "ref": "crp://default/activity/event-unknown-beta.json",
    }, expected_revision=None)

    alpha_result = QuerySourceActivity(store).execute(source_id=alpha)
    beta_result = QuerySourceActivity(store).execute(source_id=beta)

    assert [event.type for event in alpha_result.events] == ["library_tags_attached"]
    assert beta_result.events == ()


def test_source_activity_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source_id = _source(store)
    BulkLibraryItemAction(store).execute(
        action="attach_tags", item_ids=[source_id], tags=("甲",),
    )
    BulkLibraryItemAction(store).execute(
        action="move_project", item_ids=[source_id], project_id="p1",
    )

    limited = QuerySourceActivity(store).execute(source_id=source_id, limit=1)
    assert len(limited.events) == 1
