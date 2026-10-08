from __future__ import annotations

from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.product_core import (
    BulkLibraryItemAction,
    serialize_bulk_library_item_action_result,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _register_source(store: JsonObjectStore, *, title: str = "Bulk fixture") -> str:
    source = ObjectStoreSourceRegistrar(store).register(
        SourceSubmission(kind="text", title=title, content=f"Content for {title}.")
    )
    return str(source.get("id"))


def test_bulk_delete_removes_multiple_sources() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid_a = _register_source(store, title="Bulk delete A")
        sid_b = _register_source(store, title="Bulk delete B")
        # 第三条不参与删除
        _register_source(store, title="Keep me")

        result = BulkLibraryItemAction(store).execute(
            action="delete", item_ids=[sid_a, sid_b]
        )

        assert result.status == "completed"
        assert result.total == 2
        assert result.succeeded == 2
        assert result.failed == 0
        remaining = [
            str(s.get("id"))
            for s in store.list("sources")
            if (s.get("library_lifecycle") or {}).get("status") != "deleted"
        ]
        assert sid_a not in remaining
        assert sid_b not in remaining
        assert len(remaining) == 1
        assert all(item.operation_id and item.revision == 2 for item in result.results)


def test_bulk_delete_partial_when_one_id_missing() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid_a = _register_source(store, title="Exists")

        result = BulkLibraryItemAction(store).execute(
            action="delete", item_ids=[sid_a, "source-does-not-exist"]
        )

        assert result.status == "partial"
        assert result.total == 2
        assert result.succeeded == 1
        assert result.failed == 1
        statuses = {r.item_id: r.status for r in result.results}
        assert statuses[sid_a] == "ok"
        assert statuses["source-does-not-exist"] == "not_found"
        successful = next(item for item in result.results if item.item_id == sid_a)
        assert successful.operation_id is not None
        assert successful.revision == 2


def test_bulk_delete_does_not_guess_non_source_collection_on_id_collision(tmp_path) -> None:
    store = _store(tmp_path)
    shared_id = "shared-library-id"
    store.write("documents", shared_id, {"id": shared_id, "title": "Keep canonical document"}, expected_revision=None)
    store.write("memory_candidates", shared_id, {"id": shared_id, "status": "pending_review"}, expected_revision=None)

    result = BulkLibraryItemAction(store).execute(action="delete", item_ids=[shared_id])

    assert result.status == "failed"
    assert result.results[0].status == "not_found"
    assert store.read("documents", shared_id) is not None
    assert store.read("memory_candidates", shared_id) is not None


def test_bulk_move_series_updates_metadata() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid_a = _register_source(store, title="Series A")
        sid_b = _register_source(store, title="Series B")

        result = BulkLibraryItemAction(store).execute(
            action="move_series",
            item_ids=[sid_a, sid_b],
            series_name="灵感系列",
        )

        assert result.status == "completed"
        assert result.succeeded == 2
        for sid in (sid_a, sid_b):
            source = store.read("sources", sid)
            assert source["metadata"]["series_assignment"]["series_name"] == "灵感系列"
            assert source["metadata"]["series_assignment"]["series_id"].startswith("series-")
            assert source["metadata"]["series_assignment"]["status"] == "assigned"


def test_bulk_move_project_updates_top_level_field() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid_a = _register_source(store, title="Project A")
        sid_b = _register_source(store, title="Project B")

        result = BulkLibraryItemAction(store).execute(
            action="move_project",
            item_ids=[sid_a, sid_b],
            project_id="project-bulk-test",
        )

        assert result.status == "completed"
        for sid in (sid_a, sid_b):
            source = store.read("sources", sid)
            assert source["project_id"] == "project-bulk-test"


def test_bulk_attach_tags_appends_to_manual_tags_and_syncs_tag_index() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid_a = _register_source(store, title="Tag A")
        sid_b = _register_source(store, title="Tag B")

        result = BulkLibraryItemAction(store).execute(
            action="attach_tags",
            item_ids=[sid_a, sid_b],
            tags=["灵感", "产品"],
        )

        assert result.status == "completed"
        for sid in (sid_a, sid_b):
            source = store.read("sources", sid)
            manual_tags = source["metadata"]["manual_tags"]
            assert "灵感" in manual_tags
            assert "产品" in manual_tags

        # tag_index 应该有 2 条记录（每个 tag 一条），每条 refs 包含两个 source 的 manual ref
        tag_records = store.list("tag_index")
        tags_in_index = {r.get("tag") for r in tag_records}
        assert tags_in_index == {"灵感", "产品"}
        for record in tag_records:
            ref_source_ids = {ref.get("source_id") for ref in record["refs"]}
            assert sid_a in ref_source_ids
            assert sid_b in ref_source_ids
            assert all(ref.get("origin") == "manual" for ref in record["refs"])
            assert record["source_count"] == 2


def test_bulk_attach_tags_does_not_duplicate_existing_tag() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid_a = _register_source(store, title="Tag dup")
        # 第一次追加
        BulkLibraryItemAction(store).execute(
            action="attach_tags", item_ids=[sid_a], tags=["灵感"]
        )
        # 第二次追加相同 tag
        result = BulkLibraryItemAction(store).execute(
            action="attach_tags", item_ids=[sid_a], tags=["灵感", "新标签"]
        )

        assert result.status == "completed"
        source = store.read("sources", sid_a)
        assert source["metadata"]["manual_tags"].count("灵感") == 1
        assert "新标签" in source["metadata"]["manual_tags"]


def test_bulk_action_rejects_empty_action() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        result = BulkLibraryItemAction(store).execute(action="", item_ids=["x"])
        assert result.status == "rejected"
        assert "action is required" in (result.error or "")


def test_bulk_action_rejects_unsupported_action() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        result = BulkLibraryItemAction(store).execute(action="publish", item_ids=["x"])
        assert result.status == "unsupported"
        assert "publish" in (result.error or "")


def test_bulk_action_rejects_empty_item_ids() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        result = BulkLibraryItemAction(store).execute(action="delete", item_ids=[])
        assert result.status == "rejected"
        assert "item_ids" in (result.error or "")


def test_bulk_move_series_requires_series_name() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid = _register_source(store, title="No series name")
        result = BulkLibraryItemAction(store).execute(
            action="move_series", item_ids=[sid], series_name=""
        )
        assert result.status == "rejected"
        assert "series_name" in (result.error or "")


def test_bulk_move_project_requires_project_id() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid = _register_source(store, title="No project id")
        result = BulkLibraryItemAction(store).execute(
            action="move_project", item_ids=[sid], project_id=""
        )
        assert result.status == "rejected"
        assert "project_id" in (result.error or "")


def test_bulk_attach_tags_requires_non_empty_tags() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid = _register_source(store, title="No tags")
        result = BulkLibraryItemAction(store).execute(
            action="attach_tags", item_ids=[sid], tags=[]
        )
        assert result.status == "rejected"
        assert "tags" in (result.error or "")


class _CompetingSourceWriter:
    """在首次 sources 写入前注入一次并发修改,模拟读取-写入窗口内的他人更新。"""

    def __init__(self, inner: JsonObjectStore) -> None:
        self._inner = inner
        self.armed = True

    def read(self, collection: str, object_id: str):
        return self._inner.read(collection, object_id)

    def read_including_deleted(self, collection: str, object_id: str):
        return self._inner.read_including_deleted(collection, object_id)

    def list(self, collection: str):
        return self._inner.list(collection)

    def delete(self, collection: str, object_id: str) -> bool:
        return self._inner.delete(collection, object_id)

    def revision(self, collection: str, object_id: str) -> int:
        return self._inner.revision(collection, object_id)

    def write(self, collection: str, object_id: str, payload, expected_revision):
        if collection == "sources" and self.armed:
            self.armed = False
            competing = dict(self._inner.read(collection, object_id) or {})
            competing["title"] = "Competing concurrent edit"
            self._inner.write(
                collection,
                object_id,
                competing,
                expected_revision=self._inner.revision(collection, object_id),
            )
        return self._inner.write(collection, object_id, payload, expected_revision=expected_revision)


class _FailingTagIndexStore:
    """首次 tag_index 写入失败,模拟派生索引写入的瞬时故障。"""

    def __init__(self, inner: JsonObjectStore) -> None:
        self._inner = inner
        self.armed = True

    def read(self, collection: str, object_id: str):
        return self._inner.read(collection, object_id)

    def read_including_deleted(self, collection: str, object_id: str):
        return self._inner.read_including_deleted(collection, object_id)

    def list(self, collection: str):
        return self._inner.list(collection)

    def delete(self, collection: str, object_id: str) -> bool:
        return self._inner.delete(collection, object_id)

    def revision(self, collection: str, object_id: str) -> int:
        return self._inner.revision(collection, object_id)

    def write(self, collection: str, object_id: str, payload, expected_revision):
        if collection == "tag_index" and self.armed:
            self.armed = False
            raise RuntimeError("tag index temporarily unavailable")
        return self._inner.write(collection, object_id, payload, expected_revision=expected_revision)


def test_bulk_action_deduplicates_repeated_ids_and_keeps_first_order() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid_a = _register_source(store, title="Dedup A")
        sid_b = _register_source(store, title="Dedup B")

        result = BulkLibraryItemAction(store).execute(
            action="move_project",
            item_ids=[sid_b, sid_a, sid_b, "", sid_a],
            project_id="project-dedup",
        )

        assert result.status == "completed"
        assert result.total == 2
        assert result.succeeded == 2
        assert [item.item_id for item in result.results] == [sid_b, sid_a]
        for sid in (sid_a, sid_b):
            assert store.read("sources", sid)["project_id"] == "project-dedup"


def test_bulk_move_series_reports_conflict_without_overwriting() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid = _register_source(store, title="Race series")
        proxy = _CompetingSourceWriter(store)

        result = BulkLibraryItemAction(proxy).execute(
            action="move_series", item_ids=[sid], series_name="并发系列"
        )

        assert result.status == "failed"
        assert result.results[0].status == "conflict"
        assert "revision" in (result.results[0].error or "")
        source = store.read("sources", sid)
        assert source["title"] == "Competing concurrent edit"
        assert not ((source.get("metadata") or {}).get("series_assignment"))


def test_bulk_conflict_is_per_item_and_partial() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid_a = _register_source(store, title="Race A")
        sid_b = _register_source(store, title="Race B")
        proxy = _CompetingSourceWriter(store)

        result = BulkLibraryItemAction(proxy).execute(
            action="move_project", item_ids=[sid_a, sid_b], project_id="project-race"
        )

        assert result.status == "partial"
        statuses = {item.item_id: item.status for item in result.results}
        assert statuses[sid_a] == "conflict"
        assert statuses[sid_b] == "ok"
        assert store.read("sources", sid_a)["title"] == "Competing concurrent edit"
        assert "project_id" not in store.read("sources", sid_a)
        assert store.read("sources", sid_b)["project_id"] == "project-race"


def test_bulk_attach_tags_retry_converges_after_index_failure() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid = _register_source(store, title="Index retry")
        proxy = _FailingTagIndexStore(store)

        first = BulkLibraryItemAction(proxy).execute(
            action="attach_tags", item_ids=[sid], tags=["灵感", "产品"]
        )

        assert first.status == "failed"
        assert first.results[0].status == "error"
        source = store.read("sources", sid)
        assert source["metadata"]["manual_tags"] == ["灵感", "产品"]
        assert store.list("tag_index") == ()

        second = BulkLibraryItemAction(store).execute(
            action="attach_tags", item_ids=[sid], tags=["灵感", "产品"]
        )

        assert second.status == "completed"
        source = store.read("sources", sid)
        assert source["metadata"]["manual_tags"].count("灵感") == 1
        assert source["metadata"]["manual_tags"].count("产品") == 1
        records = {str(record.get("tag")): record for record in store.list("tag_index")}
        assert set(records) == {"灵感", "产品"}
        for record in records.values():
            refs = [ref for ref in record["refs"] if ref.get("source_id") == sid]
            assert refs == [{
                "source_id": sid,
                "origin": "manual",
                "content_read_id": None,
                "paragraph_id": None,
                "text_preview": None,
            }]
            assert record["source_count"] == 1
            assert record["ref_count"] == 1


def test_serialize_bulk_result_round_trip() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid = _register_source(store, title="Serialize")
        result = BulkLibraryItemAction(store).execute(
            action="move_project", item_ids=[sid], project_id="p1"
        )
        payload = serialize_bulk_library_item_action_result(result)
        assert payload["action"] == "move_project"
        assert payload["status"] == "completed"
        assert payload["total"] == 1
        assert payload["succeeded"] == 1
        assert payload["failed"] == 0
        assert payload["results"][0]["item_id"] == sid
        assert payload["results"][0]["status"] == "ok"
        assert payload["error"] is None


def test_bulk_move_and_attach_write_activity_receipts() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid = _register_source(store, title="Receipt fixture")
        BulkLibraryItemAction(store).execute(
            action="move_series", item_ids=[sid], series_name="审计系列"
        )
        revision_after_series = store.revision("sources", sid)
        BulkLibraryItemAction(store).execute(
            action="move_project", item_ids=[sid], project_id="project-audit"
        )
        revision_after_project = store.revision("sources", sid)
        BulkLibraryItemAction(store).execute(
            action="attach_tags", item_ids=[sid], tags=["审计"]
        )

        events = {event["type"]: event for event in store.list("activity_events")}
        series_event = events["library_series_moved"]
        assert series_event["id"] == f"event-library-series-moved-{sid}"
        assert series_event["source_id"] == sid
        assert series_event["status"] == "completed"
        assert series_event["details"]["series_name"] == "审计系列"
        assert series_event["details"]["source_revision"] == revision_after_series

        project_event = events["library_project_moved"]
        assert project_event["id"] == f"event-library-project-moved-{sid}"
        assert project_event["details"]["project_id"] == "project-audit"
        assert project_event["details"]["source_revision"] == revision_after_project

        tags_event = events["library_tags_attached"]
        assert tags_event["id"] == f"event-library-tags-attached-{sid}"
        assert tags_event["details"]["attached_tags"] == ["审计"]
        assert tags_event["details"]["manual_tags"] == ["审计"]
        assert tags_event["details"]["source_revision"] == store.revision("sources", sid)

        for event in events.values():
            assert event["schema_version"] == "1.0.0"
            assert event["ref"].startswith("crp://default/activity/event-library-")


def test_bulk_delete_does_not_write_activity_receipt() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid = _register_source(store, title="Delete receiptless")

        BulkLibraryItemAction(store).execute(action="delete", item_ids=[sid])

        assert store.list("activity_events") == ()
        lifecycle = store.read_including_deleted("sources", sid)["library_lifecycle"]
        assert lifecycle["operation_id"].startswith("library-delete-")


def test_bulk_move_series_skips_cas_when_already_converged() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid = _register_source(store, title="Converged series")
        service = BulkLibraryItemAction(store)

        first = service.execute(action="move_series", item_ids=[sid], series_name="同系列")
        assert first.status == "completed"
        revision_after_first = store.revision("sources", sid)
        assert first.results[0].revision == revision_after_first

        second = service.execute(action="move_series", item_ids=[sid], series_name="同系列")
        assert second.status == "completed"
        assert second.results[0].revision is None
        assert store.revision("sources", sid) == revision_after_first

        receipts = [
            event for event in store.list("activity_events")
            if event["type"] == "library_series_moved"
        ]
        assert len(receipts) == 1


def test_bulk_move_project_skips_cas_when_already_converged() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid = _register_source(store, title="Converged project")
        service = BulkLibraryItemAction(store)

        first = service.execute(action="move_project", item_ids=[sid], project_id="same-project")
        revision_after_first = store.revision("sources", sid)

        second = service.execute(action="move_project", item_ids=[sid], project_id="same-project")

        assert second.status == "completed"
        assert second.results[0].revision is None
        assert store.revision("sources", sid) == revision_after_first
        receipts = [
            event for event in store.list("activity_events")
            if event["type"] == "library_project_moved"
        ]
        assert len(receipts) == 1


class _FailingActivityStore:
    """首次 activity_events 写入失败,模拟审计投影写入的瞬时故障。"""

    def __init__(self, inner: JsonObjectStore) -> None:
        self._inner = inner
        self.armed = True

    def read(self, collection: str, object_id: str):
        return self._inner.read(collection, object_id)

    def read_including_deleted(self, collection: str, object_id: str):
        return self._inner.read_including_deleted(collection, object_id)

    def list(self, collection: str):
        return self._inner.list(collection)

    def delete(self, collection: str, object_id: str) -> bool:
        return self._inner.delete(collection, object_id)

    def revision(self, collection: str, object_id: str) -> int:
        return self._inner.revision(collection, object_id)

    def write(self, collection: str, object_id: str, payload, expected_revision):
        if collection == "activity_events" and self.armed:
            self.armed = False
            raise RuntimeError("activity store temporarily unavailable")
        return self._inner.write(collection, object_id, payload, expected_revision=expected_revision)


def test_bulk_receipt_failure_reports_error_and_retry_converges() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        sid = _register_source(store, title="Receipt retry")
        proxy = _FailingActivityStore(store)

        first = BulkLibraryItemAction(proxy).execute(
            action="move_series", item_ids=[sid], series_name="重试系列"
        )

        assert first.status == "failed"
        assert first.results[0].status == "error"
        source = store.read("sources", sid)
        assert source["metadata"]["series_assignment"]["series_name"] == "重试系列"
        assert store.list("activity_events") == ()
        revision_after_first = store.revision("sources", sid)

        second = BulkLibraryItemAction(store).execute(
            action="move_series", item_ids=[sid], series_name="重试系列"
        )

        assert second.status == "completed"
        assert second.results[0].revision is None
        # source 不重复写，receipt 补齐且仅一条
        assert store.revision("sources", sid) == revision_after_first
        receipts = list(store.list("activity_events"))
        assert len(receipts) == 1
        assert receipts[0]["type"] == "library_series_moved"
        assert receipts[0]["details"]["source_revision"] == revision_after_first
