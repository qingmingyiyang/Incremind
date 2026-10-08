from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from backend.api.app import create_app

from core.composition import ObjectStoreLibraryOverviewReader
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.product_core import GetLibraryOverview, UpdateLibrarySourceMetadata, serialize_library_overview
from core.storage_provider import JsonObjectStore


def _store(root: Path) -> JsonObjectStore:
    return JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library")


def _source(store: JsonObjectStore) -> str:
    item = ObjectStoreSourceRegistrar(store).register(
        SourceSubmission(kind="text", title="原始标题", content="可编辑资料正文")
    )
    return str(item["id"])


def test_source_edit_updates_title_series_tags_and_index(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source_id = _source(store)

    result = UpdateLibrarySourceMetadata(store).execute(
        source_id=source_id, expected_revision=1, title="用户标题", series_name="产品系列", tags=("产品", "验收"),
    )

    assert result.status == "updated"
    assert result.revision == 2
    source = store.read("sources", source_id)
    assert source["title"] == "用户标题"
    assert source["metadata"]["series_assignment"]["series_name"] == "产品系列"
    assert source["metadata"]["series_assignment"]["memory_publication_state"] == "not_published"
    assert source["metadata"]["manual_tags"] == ["产品", "验收"]
    for item in store.list("tag_index"):
        assert item["schema_version"] == "1.0.0"
        manual_refs = [ref for ref in item["refs"] if ref["origin"] == "manual"]
        assert any(ref["source_id"] == source_id for ref in manual_refs)
        assert item["source_count"] == 1


def test_source_edit_writes_activity_receipt_on_success_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source_id = _source(store)
    service = UpdateLibrarySourceMetadata(store)

    rejected = service.execute(
        source_id=source_id, expected_revision=1, title="", tags=("X",),
    )
    assert rejected.status == "rejected"
    conflict = service.execute(
        source_id=source_id, expected_revision=99, title="旧稿", tags=("X",),
    )
    assert conflict.status == "conflict"
    assert store.list("activity_events") == ()

    result = service.execute(
        source_id=source_id, expected_revision=1, title="审计标题", series_name="审计系列", tags=("审计",),
    )
    assert result.status == "updated"
    events = list(store.list("activity_events"))
    assert len(events) == 1
    event = events[0]
    assert event["id"] == f"event-library-source-edited-{source_id}"
    assert event["type"] == "library_source_edited"
    assert event["source_id"] == source_id
    assert event["status"] == "completed"
    assert event["details"]["title"] == "审计标题"
    assert event["details"]["series_name"] == "审计系列"
    assert event["details"]["manual_tags"] == ["审计"]
    assert event["details"]["source_revision"] == result.revision
    assert event["schema_version"] == "1.0.0"

    second = service.execute(
        source_id=source_id, expected_revision=result.revision, title="再次编辑", tags=("审计",),
    )
    assert second.status == "updated"
    events = list(store.list("activity_events"))
    assert len(events) == 1, "deterministic event id must overwrite, not append"
    assert events[0]["details"]["title"] == "再次编辑"
    assert events[0]["details"]["source_revision"] == second.revision


def test_source_edit_stale_revision_preserves_winning_user_update(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source_id = _source(store)
    service = UpdateLibrarySourceMetadata(store)
    winner = service.execute(source_id=source_id, expected_revision=1, title="先保存", tags=("A",))
    stale = service.execute(source_id=source_id, expected_revision=1, title="后到旧稿", tags=("B",))

    assert winner.status == "updated"
    assert stale.status == "conflict"
    assert stale.revision == 2
    assert store.read("sources", source_id)["title"] == "先保存"
    assert store.read("sources", source_id)["metadata"]["manual_tags"] == ["A"]


def test_source_edit_removes_old_tag_index_reference_and_survives_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source_id = _source(store)
    service = UpdateLibrarySourceMetadata(store)
    service.execute(source_id=source_id, expected_revision=1, title="第一次", tags=("旧标签",))
    result = service.execute(source_id=source_id, expected_revision=2, title="重启标题", tags=("新标签",))

    restarted = _store(tmp_path)
    assert restarted.read("sources", source_id)["title"] == "重启标题"
    assert restarted.revision("sources", source_id) == result.revision == 3
    records = {item["tag"]: item for item in restarted.list("tag_index")}
    assert all(ref.get("source_id") != source_id for ref in records["旧标签"]["refs"])
    assert any(
        ref.get("source_id") == source_id and ref.get("origin") == "manual"
        for ref in records["新标签"]["refs"]
    )
    assert records["新标签"]["source_count"] == 1


def test_library_overview_projects_current_source_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source_id = _source(store)
    UpdateLibrarySourceMetadata(store).execute(source_id=source_id, expected_revision=1, title="版本二")

    payload = serialize_library_overview(GetLibraryOverview(ObjectStoreLibraryOverviewReader(store)).execute())
    item = next(item for item in payload["items"] if item["item_id"] == source_id)
    assert item["source_revision"] == 2
    assert item["title"] == "版本二"


def test_source_edit_http_route_conflict_and_restart(tmp_path: Path) -> None:
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        intake = client.post("/api/rebuild/workbench/auto-intake", json={"content": "真实中文资料编辑案例"})
        assert intake.status_code == 201
        source_id = intake.json()["items"][0]["source_id"]
        before = next(item for item in client.get("/api/rebuild/library/overview").json()["items"] if item["item_id"] == source_id)
        revision = before["source_revision"]
        winner = client.put(f"/api/rebuild/library/sources/{source_id}/metadata", json={
            "expected_revision": revision, "title": "获胜的用户标题", "series_name": "真实案例", "tags": ["中文", "验收"],
        })
        stale = client.put(f"/api/rebuild/library/sources/{source_id}/metadata", json={
            "expected_revision": revision, "title": "过期草稿", "series_name": "", "tags": [],
        })
        assert winner.status_code == 200
        assert stale.status_code == 409
        assert stale.json()["title"] == "获胜的用户标题"

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as restarted:
        item = next(item for item in restarted.get("/api/rebuild/library/overview").json()["items"] if item["item_id"] == source_id)
        assert item["title"] == "获胜的用户标题"
        assert item["series_name"] == "真实案例"
        assert {"中文", "验收"}.issubset(set(item["content_tags"]))
        assert item["source_revision"] == winner.json()["revision"]
