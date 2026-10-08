from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar
from core.job_runner import ObjectStoreJobRepository
from core.product_core import (
    OrchestrateWorkbenchAutoIntake,
    ServeWorkbenchAutoIntakeEndpoint,
    StoreWorkbenchOriginalAssetBatch,
    serialize_workbench_auto_intake_result,
    serialize_workbench_original_asset_batch,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _orchestrator(store: JsonObjectStore, *, namespace_id: str = "default") -> OrchestrateWorkbenchAutoIntake:
    return OrchestrateWorkbenchAutoIntake(
        object_store=store,
        source_registrar=ObjectStoreSourceRegistrar(store, namespace_id=namespace_id),
        job_repository=ObjectStoreJobRepository(store),
        fetch_url=lambda url: "",
        namespace_id=namespace_id,
    )


def _child_inputs(count: int = 2) -> list[dict[str, object]]:
    children: list[dict[str, object]] = []
    for index in range(count):
        children.append(
            {
                "file_name": f"note-{index}.md",
                "media_type": "text/markdown",
                "size_bytes": 0,
                "file_reference": f"crp-ref-default-assets-originals-{index}",
                "title": f"批量笔记 {index + 1}",
            }
        )
    return children


def test_file_collection_registers_parent_source_and_child_sources(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        child_inputs=_child_inputs(2),
        add_to_knowledge_base=True,
        title="批量笔记集合",
    )

    assert result.status == "accepted"
    assert result.job_id.startswith("job-intake-source-collection-")
    assert len(result.items) == 2
    child_source_ids = {item.source_id for item in result.items}
    assert len(child_source_ids) == 2
    for item in result.items:
        assert item.input_type == "file"
        assert item.workflow == "document_text_extraction"
        assert item.status == "needs_extractor"
        assert item.next_step == "await_document_extractor"

    all_sources = store.list("sources")
    file_sources = [s for s in all_sources if s.get("type") == "file"]
    assert len(file_sources) == 2
    collection_sources = [s for s in all_sources if s.get("type") == "collection"]
    assert len(collection_sources) == 1
    parent = collection_sources[0]
    assert parent["metadata"]["collection_type"] == "file_collection"
    assert parent["metadata"]["item_count"] == 2
    assert set(parent["metadata"]["child_source_ids"]) == child_source_ids
    assert parent["title"] == "批量笔记集合"


def test_file_collection_writes_parent_job_with_child_steps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(child_inputs=_child_inputs(3))

    assert result.status == "accepted"
    job = store.read("jobs", result.job_id)
    assert job is not None
    assert job["job_type"] == "workbench_auto_intake"
    assert job["status"] == "pending"
    assert job["progress"] == {
        "current": 0,
        "total": 3,
        "percent": 0,
        "message": "awaiting required workflow for 3 workbench intake item(s)",
    }
    assert job["max_attempts"] == 3
    assert len(job["steps"]) == 3
    step_source_ids = {step["source_id"] for step in job["steps"]}
    assert step_source_ids == {item.source_id for item in result.items}
    for step in job["steps"]:
        assert step["name"].startswith("orchestrate_file_collection_")
        assert step["input_type"] == "file"


def test_file_collection_rejects_empty_child_inputs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(child_inputs=[{"file_name": "", "file_reference": ""}])

    assert result.status == "failed"
    assert "no usable child files" in (result.error or "")
    assert result.items == ()


def test_file_collection_rejects_child_without_file_reference(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        child_inputs=[{"file_name": "note.md", "file_reference": ""}],
    )

    assert result.status == "failed"
    assert "no usable child files" in (result.error or "")


def test_file_collection_skips_non_mapping_children(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        child_inputs=[
            "not-a-dict",
            {"file_name": "valid.md", "file_reference": "crp-ref-1"},
        ],
    )

    assert result.status == "accepted"
    assert len(result.items) == 1


def test_file_collection_uses_custom_title(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        child_inputs=_child_inputs(2),
        title="自定义批量标题",
    )

    assert result.status == "accepted"
    parent_sources = [s for s in store.list("sources") if s.get("type") == "collection"]
    assert parent_sources[0]["title"] == "自定义批量标题"


def test_file_collection_default_title_when_single_child(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(child_inputs=_child_inputs(1))

    assert result.status == "accepted"
    parent_sources = [s for s in store.list("sources") if s.get("type") == "collection"]
    assert parent_sources[0]["title"] == "批量笔记 1"


def test_file_collection_default_title_when_multiple_children(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(child_inputs=_child_inputs(3))

    assert result.status == "accepted"
    parent_sources = [s for s in store.list("sources") if s.get("type") == "collection"]
    assert parent_sources[0]["title"] == "文件批量"


def test_file_collection_classification_payload_marks_file_collection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(child_inputs=_child_inputs(2))

    assert result.classification["input_type"] == "file_collection"
    assert result.classification["child_count"] == 2
    assert result.classification["collection_type"] == "file_collection"


def test_file_collection_with_size_bytes_and_asset_ref(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        child_inputs=[
            {
                "file_name": "report.pdf",
                "media_type": "application/pdf",
                "size_bytes": 4096,
                "file_reference": "crp-ref-default-assets-originals-original-file-abc123",
                "asset_ref": "crp-ref-default-assets-originals-original-file-abc123",
            }
        ],
    )

    assert result.status == "accepted"
    assert len(result.items) == 1
    child_source_id = result.items[0].source_id
    source = store.read("sources", child_source_id)
    assert source is not None
    assert source["metadata"]["file_reference"] == "crp-ref-default-assets-originals-original-file-abc123"
    assert source["size_bytes"] == 4096


def test_serve_auto_intake_endpoint_passes_child_inputs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    def orchestrate(**kwargs):
        return orchestrator.execute(**kwargs)

    endpoint = ServeWorkbenchAutoIntakeEndpoint()
    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/auto-intake",
        body={
            "content": "",
            "media_type": "",
            "file_name": "",
            "urls": [],
            "add_to_knowledge_base": True,
            "title": "Endpoint 批量",
            "child_inputs": _child_inputs(2),
        },
        orchestrate=orchestrate,
    )

    assert response.status_code == 202
    body = response.body
    assert body["status"] == "accepted"
    assert body["classification"]["input_type"] == "file_collection"
    assert len(body["items"]) == 2


def test_serve_auto_intake_endpoint_rejects_empty_child_inputs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    def orchestrate(**kwargs):
        return orchestrator.execute(**kwargs)

    endpoint = ServeWorkbenchAutoIntakeEndpoint()
    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/auto-intake",
        body={
            "content": "",
            "media_type": "",
            "file_name": "",
            "urls": [],
            "add_to_knowledge_base": True,
            "title": "",
            "child_inputs": [],
        },
        orchestrate=orchestrate,
    )

    assert response.status_code == 400
    assert "child_inputs must be a non-empty array" in response.body["detail"]


def test_serve_auto_intake_endpoint_rejects_child_missing_file_reference(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    def orchestrate(**kwargs):
        return orchestrator.execute(**kwargs)

    endpoint = ServeWorkbenchAutoIntakeEndpoint()
    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/auto-intake",
        body={
            "content": "",
            "media_type": "",
            "file_name": "",
            "urls": [],
            "add_to_knowledge_base": True,
            "title": "",
            "child_inputs": [{"file_name": "note.md", "file_reference": ""}],
        },
        orchestrate=orchestrate,
    )

    assert response.status_code == 400
    assert "requires file_name and file_reference" in response.body["detail"]


def test_serialize_workbench_auto_intake_result_file_collection_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(child_inputs=_child_inputs(2))

    payload = serialize_workbench_auto_intake_result(result)
    assert payload["status"] == "accepted"
    assert payload["classification"]["input_type"] == "file_collection"
    assert len(payload["items"]) == 2
    for item_payload in payload["items"]:
        assert item_payload["input_type"] == "file"
        assert item_payload["workflow"] == "document_text_extraction"


def test_batch_original_assets_uploads_multiple_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    use_case = StoreWorkbenchOriginalAssetBatch(
        object_store=store,
        assets_root=tmp_path / "library" / "assets" / "originals",
    )

    content_a = b"asset A content"
    content_b = b"asset B content"
    result = use_case.execute(
        assets=[
            {
                "display_name": "a.pdf",
                "media_type": "application/pdf",
                "size_bytes": len(content_a),
                "content_base64": base64.b64encode(content_a).decode("ascii"),
                "source_kind": "file",
            },
            {
                "display_name": "b.pdf",
                "media_type": "application/pdf",
                "size_bytes": len(content_b),
                "content_base64": base64.b64encode(content_b).decode("ascii"),
                "source_kind": "file",
            },
        ],
    )

    assert result.status == "completed"
    assert result.total == 2
    assert result.succeeded == 2
    assert result.failed == 0
    assert len(result.uploaded) == 2
    assert len(result.failed_items) == 0
    expected_hash_a = hashlib.sha256(content_a).hexdigest()
    assert result.uploaded[0].asset.asset_id == f"original-file-{expected_hash_a[:16]}"
    expected_hash_b = hashlib.sha256(content_b).hexdigest()
    assert result.uploaded[1].asset.asset_id == f"original-file-{expected_hash_b[:16]}"


def test_batch_original_assets_partial_failure_when_one_invalid(tmp_path: Path) -> None:
    store = _store(tmp_path)
    use_case = StoreWorkbenchOriginalAssetBatch(
        object_store=store,
        assets_root=tmp_path / "library" / "assets" / "originals",
    )

    content_a = b"good content"
    result = use_case.execute(
        assets=[
            {
                "display_name": "good.pdf",
                "media_type": "application/pdf",
                "size_bytes": len(content_a),
                "content_base64": base64.b64encode(content_a).decode("ascii"),
            },
            {
                "display_name": "bad.pdf",
                "media_type": "application/pdf",
                "size_bytes": 10,
                "content_base64": "not-valid-base64!!",
            },
        ],
    )

    assert result.status == "partial"
    assert result.total == 2
    assert result.succeeded == 1
    assert result.failed == 1
    assert len(result.failed_items) == 1
    assert result.failed_items[0].error is not None


def test_batch_original_assets_rejects_non_list(tmp_path: Path) -> None:
    from core.product_core import WorkbenchOriginalAssetError

    store = _store(tmp_path)
    use_case = StoreWorkbenchOriginalAssetBatch(
        object_store=store,
        assets_root=tmp_path / "library" / "assets" / "originals",
    )

    import pytest

    with pytest.raises(WorkbenchOriginalAssetError, match="assets must be a list"):
        use_case.execute(assets="not-a-list")  # type: ignore[arg-type]


def test_serialize_workbench_original_asset_batch_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    use_case = StoreWorkbenchOriginalAssetBatch(
        object_store=store,
        assets_root=tmp_path / "library" / "assets" / "originals",
    )

    content = b"round trip"
    result = use_case.execute(
        assets=[
            {
                "display_name": "trip.md",
                "media_type": "text/markdown",
                "size_bytes": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        ],
    )

    payload = serialize_workbench_original_asset_batch(result)
    assert payload["status"] == "completed"
    assert payload["total"] == 1
    assert payload["succeeded"] == 1
    assert payload["failed"] == 0
    assert len(payload["uploaded"]) == 1
    assert payload["uploaded"][0]["asset"]["display_name"] == "trip.md"
