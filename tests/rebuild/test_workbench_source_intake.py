from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.ingestion_core import DeterministicSourceRegistrar
from core.job_runner import InMemoryJobRepository
from core.product_core import (
    CaptureWorkbenchAudioSource,
    CaptureWorkbenchBookmarkCollection,
    CaptureWorkbenchFileSource,
    CaptureWorkbenchImageSource,
    CaptureWorkbenchLinkSource,
    CaptureWorkbenchTextSource,
    CaptureWorkbenchVideoSource,
    ServeWorkbenchAudioSourceIntakeEndpoint,
    ServeWorkbenchBookmarkCollectionIntakeEndpoint,
    ServeWorkbenchFileSourceIntakeEndpoint,
    ServeWorkbenchImageSourceIntakeEndpoint,
    ServeWorkbenchLinkSourceIntakeEndpoint,
    ServeWorkbenchTextSourceIntakeEndpoint,
    ServeWorkbenchVideoSourceIntakeEndpoint,
    serialize_workbench_audio_source_intake,
    serialize_workbench_bookmark_collection_intake,
    serialize_workbench_file_source_intake,
    serialize_workbench_image_source_intake,
    serialize_workbench_link_source_intake,
    serialize_workbench_text_source_intake,
    serialize_workbench_video_source_intake,
)
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _use_case() -> tuple[CaptureWorkbenchTextSource, DeterministicSourceRegistrar, InMemoryJobRepository]:
    source_registrar = DeterministicSourceRegistrar()
    job_repository = InMemoryJobRepository()
    return (
        CaptureWorkbenchTextSource(
            source_registrar=source_registrar,
            job_repository=job_repository,
        ),
        source_registrar,
        job_repository,
    )


def _link_use_case() -> tuple[CaptureWorkbenchLinkSource, DeterministicSourceRegistrar, InMemoryJobRepository]:
    source_registrar = DeterministicSourceRegistrar()
    job_repository = InMemoryJobRepository()
    return (
        CaptureWorkbenchLinkSource(
            source_registrar=source_registrar,
            job_repository=job_repository,
        ),
        source_registrar,
        job_repository,
    )


def _bookmark_use_case() -> tuple[CaptureWorkbenchBookmarkCollection, DeterministicSourceRegistrar, InMemoryJobRepository]:
    source_registrar = DeterministicSourceRegistrar()
    job_repository = InMemoryJobRepository()
    return (
        CaptureWorkbenchBookmarkCollection(
            source_registrar=source_registrar,
            job_repository=job_repository,
        ),
        source_registrar,
        job_repository,
    )


def _file_use_case() -> tuple[CaptureWorkbenchFileSource, DeterministicSourceRegistrar, InMemoryJobRepository]:
    source_registrar = DeterministicSourceRegistrar()
    job_repository = InMemoryJobRepository()
    return (
        CaptureWorkbenchFileSource(
            source_registrar=source_registrar,
            job_repository=job_repository,
        ),
        source_registrar,
        job_repository,
    )


def _image_use_case() -> tuple[CaptureWorkbenchImageSource, DeterministicSourceRegistrar, InMemoryJobRepository]:
    source_registrar = DeterministicSourceRegistrar()
    job_repository = InMemoryJobRepository()
    return (
        CaptureWorkbenchImageSource(
            source_registrar=source_registrar,
            job_repository=job_repository,
        ),
        source_registrar,
        job_repository,
    )


def _audio_use_case() -> tuple[CaptureWorkbenchAudioSource, DeterministicSourceRegistrar, InMemoryJobRepository]:
    source_registrar = DeterministicSourceRegistrar()
    job_repository = InMemoryJobRepository()
    return (
        CaptureWorkbenchAudioSource(
            source_registrar=source_registrar,
            job_repository=job_repository,
        ),
        source_registrar,
        job_repository,
    )


def _video_use_case() -> tuple[CaptureWorkbenchVideoSource, DeterministicSourceRegistrar, InMemoryJobRepository]:
    source_registrar = DeterministicSourceRegistrar()
    job_repository = InMemoryJobRepository()
    return (
        CaptureWorkbenchVideoSource(
            source_registrar=source_registrar,
            job_repository=job_repository,
        ),
        source_registrar,
        job_repository,
    )


def test_workbench_text_source_intake_captures_source_and_job_trace() -> None:
    use_case, sources, jobs = _use_case()

    result = use_case.execute(
        title="Workbench note",
        content="Workbench input should become a traceable Source before broader UI work.",
    )

    source = sources.get(result.source_id)
    job = jobs.get(result.job_id)

    assert result.status == "captured"
    assert result.source_id.startswith("source-text-")
    assert result.source_title == "Workbench note"
    assert result.source_type == "text"
    assert result.capture_mode == "inline"
    assert result.media_type == "text/plain"
    assert result.size_bytes == len("Workbench input should become a traceable Source before broader UI work.".encode("utf-8"))
    assert result.processing_state == "captured"
    assert result.job_id == f"job-capture-{result.source_id}"
    assert result.job_type == "capture"
    assert result.job_progress_percent == 100
    assert result.job_progress_message == "captured workbench text source"
    assert result.source_uri == f"crp://default/sources/{result.source_id}"
    assert result.step_names == ("persist_source",)
    assert result.published_output_kinds == ("source",)
    assert result.next_step == "minimal_library_view_bridge_ready"
    assert result.trace_refs == (
        result.source_uri,
        f"crp://default/logs/jobs/{result.job_id}/persist_source.jsonl",
    )
    assert result.library_bridge_item.item_id == f"library-source-{result.source_id}"
    assert result.library_bridge_item.item_kind == "source_preview"
    assert result.library_bridge_item.title == "Workbench note"
    assert result.library_bridge_item.source_id == result.source_id
    assert result.library_bridge_item.capture_job_id == result.job_id
    assert result.library_bridge_item.evidence_refs == result.trace_refs
    assert result.library_bridge_item.selectable_evidence_refs == result.trace_refs
    assert result.library_bridge_item.selection_id == f"library-selection-{result.source_id}"
    assert result.library_bridge_item.selection_state == "available"
    assert result.library_bridge_item.selection_persistence_ref == (
        f"crp://default/library/selections/{result.source_id}.json"
    )
    assert result.library_bridge_item.memory_publication_state == "not_started"
    assert result.library_bridge_item.boundary == "source_job_selection_only"
    assert source is not None
    assert job is not None
    assert source["metadata"]["content"] == "Workbench input should become a traceable Source before broader UI work."
    assert job["job_type"] == "capture"
    assert job["status"] == "completed"
    assert job["published_outputs"] == [
        {
            "kind": "source",
            "uri": result.source_uri,
            "object_id": result.source_id,
            "published": True,
        }
    ]
    assert validate_contract_instance("source.schema.json", _schema("source.schema.json"), source) == []
    assert validate_contract_instance("job.schema.json", _schema("job.schema.json"), job) == []


def test_workbench_text_source_intake_rejects_empty_content_before_job_creation() -> None:
    use_case, _sources, jobs = _use_case()

    with pytest.raises(ValueError, match="requires content"):
        use_case.execute(title="Empty", content="  ")

    assert jobs.all() == ()


def test_workbench_text_source_intake_serializer_returns_json_ready_payload() -> None:
    use_case, _sources, _jobs = _use_case()
    result = use_case.execute(title="Serializer", content="Serialize this intake result.")

    payload = serialize_workbench_text_source_intake(result)

    assert payload == {
        "status": "captured",
        "source_id": result.source_id,
        "source_uri": result.source_uri,
        "source_title": "Serializer",
        "source_type": "text",
        "capture_mode": "inline",
        "media_type": "text/plain",
        "size_bytes": len("Serialize this intake result.".encode("utf-8")),
        "processing_state": "captured",
        "content_hash": result.content_hash,
        "job_id": result.job_id,
        "job_type": "capture",
        "job_status": "completed",
        "job_progress_percent": 100,
        "job_progress_message": "captured workbench text source",
        "step_names": ["persist_source"],
        "published_output_kinds": ["source"],
        "trace_refs": list(result.trace_refs),
        "next_step": "minimal_library_view_bridge_ready",
        "intake_intent": "inspiration",
        "intake_intent_label": "灵感",
        "intake_route": "inspiration_material",
        "intake_feedback": "已识别为灵感或想法，后续适合保留原文、提炼可行动假设和可能关联的系列。",
        "structured_output_plan": ["原始想法", "可行动假设", "关联标签", "待确认问题"],
        "memory_layer_update_plan": ["atom", "scenario"],
        "suggested_next_actions": ["生成灵感卡片", "创建待审原子记忆", "等待用户确认系列"],
        "library_bridge_item": {
            "item_id": f"library-source-{result.source_id}",
            "item_kind": "source_preview",
            "title": "Serializer",
            "source_id": result.source_id,
            "source_uri": result.source_uri,
            "media_type": "text/plain",
            "processing_state": "captured",
            "capture_job_id": result.job_id,
            "capture_job_status": "completed",
            "evidence_refs": list(result.trace_refs),
            "selectable_evidence_refs": list(result.trace_refs),
            "selection_id": f"library-selection-{result.source_id}",
            "selection_state": "available",
            "selection_persistence_ref": f"crp://default/library/selections/{result.source_id}.json",
            "memory_publication_state": "not_started",
            "boundary": "source_job_selection_only",
        },
    }


def test_workbench_text_source_intake_endpoint_serves_narrow_post() -> None:
    use_case, _sources, _jobs = _use_case()
    endpoint = ServeWorkbenchTextSourceIntakeEndpoint()

    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/text-source-intake",
        body={"title": "Endpoint", "content": "Endpoint should return trace evidence."},
        intake=use_case.execute,
    )

    assert response.status_code == 201
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.body["status"] == "captured"
    assert response.body["source_id"].startswith("source-text-")
    assert response.body["source_title"] == "Endpoint"
    assert response.body["job_type"] == "capture"
    assert response.body["job_progress_percent"] == 100
    assert response.body["published_output_kinds"] == ["source"]
    assert response.body["next_step"] == "minimal_library_view_bridge_ready"
    assert response.body["intake_intent"] == "inspiration"
    assert response.body["intake_route"] == "inspiration_material"
    assert response.body["memory_layer_update_plan"] == ["atom", "scenario"]
    assert response.body["library_bridge_item"]["item_kind"] == "source_preview"
    assert response.body["library_bridge_item"]["capture_job_id"] == response.body["job_id"]
    assert response.body["library_bridge_item"]["evidence_refs"] == response.body["trace_refs"]
    assert response.body["library_bridge_item"]["selectable_evidence_refs"] == response.body["trace_refs"]
    assert response.body["library_bridge_item"]["selection_state"] == "available"
    assert response.body["library_bridge_item"]["selection_persistence_ref"] == (
        f"crp://default/library/selections/{response.body['source_id']}.json"
    )
    assert response.body["library_bridge_item"]["memory_publication_state"] == "not_started"
    assert response.body["job_id"] == f"job-capture-{response.body['source_id']}"
    assert response.body["trace_refs"][0] == response.body["source_uri"]


def test_workbench_text_source_intake_endpoint_rejects_wrong_method_path_and_body() -> None:
    use_case, _sources, _jobs = _use_case()
    endpoint = ServeWorkbenchTextSourceIntakeEndpoint()

    wrong_method = endpoint.execute(
        method="GET",
        path="/api/rebuild/workbench/text-source-intake",
        body={"title": "Wrong", "content": "Wrong method"},
        intake=use_case.execute,
    )
    wrong_path = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/other",
        body={"title": "Wrong", "content": "Wrong path"},
        intake=use_case.execute,
    )
    wrong_body = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/text-source-intake",
        body={"title": "Wrong", "content": ""},
        intake=use_case.execute,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "POST"
    assert wrong_path.status_code == 404
    assert wrong_body.status_code == 400
    assert wrong_body.body == {
        "detail": "workbench text Source intake rejected",
        "reason": "workbench text intake requires content",
        "actionable": True,
    }


def test_workbench_link_source_intake_captures_url_metadata_without_remote_fetch() -> None:
    use_case, sources, jobs = _link_use_case()

    result = use_case.execute(
        title="参考链接",
        url="https://example.com/rebuild-note",
    )

    source = sources.get(result.source_id)
    job = jobs.get(result.job_id)

    assert result.status == "captured"
    assert result.source_id.startswith("source-link-")
    assert result.source_title == "参考链接"
    assert result.source_type == "link"
    assert result.capture_mode == "reference"
    assert result.media_type == "text/uri-list"
    assert result.original_url == "https://example.com/rebuild-note"
    assert result.remote_fetch_state == "not_performed"
    assert result.source_display_kind == "url_reference"
    assert result.capture_boundary == "url_metadata_only"
    assert result.remote_fetch_boundary == "remote_fetch_not_performed"
    assert result.library_selection_scope == "source_job_url_reference_only"
    assert result.size_bytes == len("https://example.com/rebuild-note".encode("utf-8"))
    assert result.processing_state == "captured"
    assert result.job_id == f"job-capture-{result.source_id}"
    assert result.job_progress_message == "captured workbench link source without remote fetch"
    assert result.step_names == ("persist_source",)
    assert result.published_output_kinds == ("source",)
    assert result.next_step == "minimal_library_view_bridge_ready"
    assert result.library_bridge_item.item_id == f"library-source-{result.source_id}"
    assert result.library_bridge_item.item_kind == "source_preview"
    assert result.library_bridge_item.media_type == "text/uri-list"
    assert result.library_bridge_item.evidence_refs == result.trace_refs
    assert result.library_bridge_item.selectable_evidence_refs == result.trace_refs
    assert result.library_bridge_item.selection_persistence_ref == (
        f"crp://default/library/selections/{result.source_id}.json"
    )
    assert result.library_bridge_item.memory_publication_state == "not_started"
    assert result.library_bridge_item.boundary == "source_job_selection_only"
    assert source is not None
    assert job is not None
    assert source["original_url"] == "https://example.com/rebuild-note"
    assert source["metadata"]["remote_fetch"] == "not_performed"
    assert source["metadata"]["content_snapshot"] is None
    assert job["checkpoint"] is None
    assert validate_contract_instance("source.schema.json", _schema("source.schema.json"), source) == []
    assert validate_contract_instance("job.schema.json", _schema("job.schema.json"), job) == []


def test_workbench_link_source_intake_rejects_empty_or_non_http_url_before_job_creation() -> None:
    use_case, _sources, jobs = _link_use_case()

    with pytest.raises(ValueError, match="requires url"):
        use_case.execute(title="Empty", url="  ")
    with pytest.raises(ValueError, match="http or https url"):
        use_case.execute(title="Local", url="file:///tmp/note.md")

    assert jobs.all() == ()


def test_workbench_link_source_intake_serializer_returns_json_ready_payload() -> None:
    use_case, _sources, _jobs = _link_use_case()
    result = use_case.execute(title="Serializer Link", url="https://example.com/source")

    payload = serialize_workbench_link_source_intake(result)

    assert payload["status"] == "captured"
    assert payload["source_id"] == result.source_id
    assert payload["source_type"] == "link"
    assert payload["capture_mode"] == "reference"
    assert payload["media_type"] == "text/uri-list"
    assert payload["original_url"] == "https://example.com/source"
    assert payload["remote_fetch_state"] == "not_performed"
    assert payload["source_display_kind"] == "url_reference"
    assert payload["capture_boundary"] == "url_metadata_only"
    assert payload["remote_fetch_boundary"] == "remote_fetch_not_performed"
    assert payload["library_selection_scope"] == "source_job_url_reference_only"
    assert payload["job_progress_message"] == "captured workbench link source without remote fetch"
    assert payload["library_bridge_item"]["selectable_evidence_refs"] == list(result.trace_refs)
    assert payload["library_bridge_item"]["memory_publication_state"] == "not_started"


def test_workbench_link_source_intake_endpoint_serves_narrow_post() -> None:
    use_case, _sources, _jobs = _link_use_case()
    endpoint = ServeWorkbenchLinkSourceIntakeEndpoint()

    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/link-source-intake",
        body={"title": "Endpoint Link", "url": "https://example.com/endpoint"},
        intake=use_case.execute,
    )

    assert response.status_code == 201
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.body["status"] == "captured"
    assert response.body["source_id"].startswith("source-link-")
    assert response.body["source_type"] == "link"
    assert response.body["original_url"] == "https://example.com/endpoint"
    assert response.body["remote_fetch_state"] == "not_performed"
    assert response.body["source_display_kind"] == "url_reference"
    assert response.body["capture_boundary"] == "url_metadata_only"
    assert response.body["remote_fetch_boundary"] == "remote_fetch_not_performed"
    assert response.body["library_selection_scope"] == "source_job_url_reference_only"
    assert response.body["library_bridge_item"]["selection_state"] == "available"
    assert response.body["library_bridge_item"]["memory_publication_state"] == "not_started"


def test_workbench_link_source_intake_endpoint_rejects_wrong_method_path_and_body() -> None:
    use_case, _sources, _jobs = _link_use_case()
    endpoint = ServeWorkbenchLinkSourceIntakeEndpoint()

    wrong_method = endpoint.execute(
        method="GET",
        path="/api/rebuild/workbench/link-source-intake",
        body={"title": "Wrong", "url": "https://example.com"},
        intake=use_case.execute,
    )
    wrong_path = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/other",
        body={"title": "Wrong", "url": "https://example.com"},
        intake=use_case.execute,
    )
    wrong_body = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/link-source-intake",
        body={"title": "Wrong", "url": "ftp://example.com/file"},
        intake=use_case.execute,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "POST"
    assert wrong_path.status_code == 404
    assert wrong_body.status_code == 400
    assert wrong_body.body == {
        "detail": "workbench link Source intake rejected",
        "reason": "link source requires http or https url",
        "actionable": True,
    }


def test_workbench_bookmark_collection_intake_captures_collection_and_child_links() -> None:
    use_case, sources, jobs = _bookmark_use_case()

    result = use_case.execute(
        title="产品资料收藏夹",
        urls=(
            "https://example.com/product-a",
            "https://example.com/product-b",
            "https://example.com/product-a",
        ),
    )

    collection_source = sources.get(result.source_id)
    assert result.status == "captured"
    assert result.source_type == "collection"
    assert result.media_type == "application/vnd.chriptmas.bookmark-collection+json"
    assert result.collection_item_count == 2
    assert len(result.child_source_ids) == 2
    assert result.original_urls == (
        "https://example.com/product-a",
        "https://example.com/product-b",
    )
    assert result.remote_fetch_state == "not_performed"
    assert result.capture_boundary == "collection_metadata_and_child_links_only"
    assert result.remote_fetch_boundary == "remote_fetch_not_performed_for_collection_items"
    assert collection_source is not None
    assert collection_source["metadata"]["collection_type"] == "bookmark_collection"
    assert collection_source["metadata"]["item_count"] == 2
    for child_source_id in result.child_source_ids:
        child_source = sources.get(child_source_id)
        assert child_source is not None
        assert child_source["type"] == "link"
        assert child_source["media_type"] == "text/uri-list"
    job = jobs.get(result.job_id)
    assert job is not None
    assert job["status"] == "completed"
    assert job["outputs"][1]["kind"] == "link_sources"
    payload = serialize_workbench_bookmark_collection_intake(result)
    assert payload["collection_item_count"] == 2
    assert payload["child_source_ids"] == list(result.child_source_ids)
    assert payload["library_bridge_item"]["boundary"] == "collection_source_and_child_link_sources_only"
    assert "sk-" not in str(payload).lower()


def test_workbench_bookmark_collection_intake_endpoint_serves_narrow_post() -> None:
    use_case, _sources, _jobs = _bookmark_use_case()
    endpoint = ServeWorkbenchBookmarkCollectionIntakeEndpoint()

    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/bookmark-collection-intake",
        body={
            "title": "Endpoint Collection",
            "urls": ["https://example.com/a", "https://example.com/b"],
        },
        intake=use_case.execute,
    )

    assert response.status_code == 201
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.body["status"] == "captured"
    assert response.body["source_id"].startswith("source-collection-")
    assert response.body["source_type"] == "collection"
    assert response.body["collection_item_count"] == 2
    assert len(response.body["child_source_ids"]) == 2
    assert response.body["remote_fetch_state"] == "not_performed"
    assert response.body["library_bridge_item"]["selection_state"] == "available"


def test_workbench_bookmark_collection_intake_endpoint_rejects_wrong_method_path_and_body() -> None:
    use_case, _sources, _jobs = _bookmark_use_case()
    endpoint = ServeWorkbenchBookmarkCollectionIntakeEndpoint()

    wrong_method = endpoint.execute(
        method="GET",
        path="/api/rebuild/workbench/bookmark-collection-intake",
        body={"title": "Wrong", "urls": ["https://example.com"]},
        intake=use_case.execute,
    )
    wrong_path = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/other",
        body={"title": "Wrong", "urls": ["https://example.com"]},
        intake=use_case.execute,
    )
    wrong_body = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/bookmark-collection-intake",
        body={"title": "Wrong", "urls": []},
        intake=use_case.execute,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "POST"
    assert wrong_path.status_code == 404
    assert wrong_body.status_code == 400
    assert wrong_body.body == {
        "detail": "workbench bookmark collection intake rejected",
        "reason": "bookmark collection intake requires at least one url",
        "actionable": True,
    }


def test_workbench_file_source_intake_captures_metadata_source_asset_and_job_trace() -> None:
    use_case, sources, jobs = _file_use_case()

    result = use_case.execute(
        title="本地报告",
        display_name="report.pdf",
        media_type="application/pdf",
        size_bytes=4096,
        file_reference="platform-ref-report-001",
    )

    source = sources.get(result.source_id)
    job = jobs.get(result.job_id)

    assert result.status == "captured"
    assert result.source_id.startswith("source-file-")
    assert result.source_title == "本地报告"
    assert result.source_type == "file"
    assert result.capture_mode == "reference"
    assert result.media_type == "application/pdf"
    assert result.size_bytes == 4096
    assert result.file_display_name == "report.pdf"
    assert result.file_reference == "platform-ref-report-001"
    assert result.file_content_policy == "metadata_only_no_content_read"
    assert result.path_policy == "no_os_absolute_path_in_product_core"
    assert result.source_display_kind == "file_reference"
    assert result.capture_boundary == "file_metadata_only"
    assert result.parser_state == "not_started"
    assert result.asset_id == result.source_id.replace("source-", "asset-", 1)
    assert result.asset_uri == f"crp-ref://default/assets/{result.asset_id}"
    assert result.asset_record_ref == f"crp://default/assets/{result.asset_id}"
    assert result.asset_uri_role == "original_file_reference_uri"
    assert result.asset_record_ref_role == "published_asset_record_ref"
    assert result.job_asset_output_ref == result.asset_record_ref
    assert result.asset_storage_mode == "reference"
    assert result.asset_availability == "unknown"
    assert result.asset_availability_reason == "metadata_only_reference_not_verified"
    assert result.asset_handoff_state == "reference_record_created"
    assert result.library_selection_scope == "source_job_asset_file_reference_only"
    assert result.library_selection_copy == (
        "select Source, Asset reference and capture Job evidence; no file content or parser output"
    )
    assert result.no_content_read_boundary == "file_bytes_not_read_or_copied"
    assert result.parser_boundary == "parser_not_started_until_asset_verification"
    assert result.job_id == f"job-capture-{result.source_id}"
    assert result.job_progress_message == (
        "captured workbench file metadata and asset reference without content read"
    )
    assert result.step_names == ("persist_source", "persist_asset_reference")
    assert result.published_output_kinds == ("source", "asset")
    assert result.next_step == "file_trace_display_hardening_ready"
    assert result.trace_refs == (
        result.source_uri,
        result.asset_uri,
        f"crp://default/logs/jobs/{result.job_id}/persist_source.jsonl",
        f"crp://default/logs/jobs/{result.job_id}/persist_asset_reference.jsonl",
    )
    assert result.library_bridge_item.item_kind == "source_preview"
    assert result.library_bridge_item.media_type == "application/pdf"
    assert result.library_bridge_item.boundary == "source_job_asset_reference_selection_only"
    assert result.library_bridge_item.evidence_refs == result.trace_refs
    assert result.library_bridge_item.memory_publication_state == "not_started"
    assert source is not None
    assert job is not None
    assert source["storage_uri"] == result.source_uri
    assert source["original_url"] is None
    assert source["parser_version"] is None
    assert source["metadata"]["display_name"] == "report.pdf"
    assert source["metadata"]["file_reference"] == "platform-ref-report-001"
    assert source["metadata"]["file_content_read"] is False
    assert source["metadata"]["content_snapshot"] is None
    assert result.asset_record["source_id"] == result.source_id
    assert result.asset_record["uri"] == result.asset_uri
    assert result.asset_record["metadata"]["file_content_read"] is False
    assert result.asset_record["metadata"]["parser"] == "not_started"
    assert job["published_outputs"] == [
        {
            "kind": "source",
            "uri": result.source_uri,
            "object_id": result.source_id,
            "published": True,
        },
        {
            "kind": "asset",
            "uri": result.asset_record_ref,
            "object_id": result.asset_id,
            "published": True,
        },
    ]
    assert validate_contract_instance("source.schema.json", _schema("source.schema.json"), source) == []
    assert validate_contract_instance("asset.schema.json", _schema("asset.schema.json"), result.asset_record) == []
    assert validate_contract_instance("job.schema.json", _schema("job.schema.json"), job) == []


def test_workbench_file_source_intake_rejects_paths_and_invalid_metadata_before_job_creation() -> None:
    use_case, _sources, jobs = _file_use_case()

    with pytest.raises(ValueError, match="display name"):
        use_case.execute(
            title="Bad",
            display_name="C:\\Users\\example\\report.pdf",
            media_type="application/pdf",
            size_bytes=10,
            file_reference="platform-ref-report-001",
        )
    with pytest.raises(ValueError, match="platform-neutral"):
        use_case.execute(
            title="Bad",
            display_name="report.pdf",
            media_type="application/pdf",
            size_bytes=10,
            file_reference="C:\\Users\\example\\report.pdf",
        )
    with pytest.raises(ValueError, match="non-negative size bytes"):
        use_case.execute(
            title="Bad",
            display_name="report.pdf",
            media_type="application/pdf",
            size_bytes=-1,
            file_reference="platform-ref-report-001",
        )

    assert jobs.all() == ()


def test_workbench_file_source_intake_serializer_returns_json_ready_payload() -> None:
    use_case, _sources, _jobs = _file_use_case()
    result = use_case.execute(
        title="Serializer File",
        display_name="notes.md",
        media_type="text/markdown",
        size_bytes=128,
        file_reference="platform-ref-notes-001",
    )

    payload = serialize_workbench_file_source_intake(result)

    assert payload["status"] == "captured"
    assert payload["source_id"] == result.source_id
    assert payload["source_type"] == "file"
    assert payload["capture_mode"] == "reference"
    assert payload["media_type"] == "text/markdown"
    assert payload["file_display_name"] == "notes.md"
    assert payload["file_reference"] == "platform-ref-notes-001"
    assert payload["file_content_policy"] == "metadata_only_no_content_read"
    assert payload["path_policy"] == "no_os_absolute_path_in_product_core"
    assert payload["asset_handoff_state"] == "reference_record_created"
    assert payload["asset_record_ref"] == result.asset_record_ref
    assert payload["asset_uri_role"] == "original_file_reference_uri"
    assert payload["asset_record_ref_role"] == "published_asset_record_ref"
    assert payload["job_asset_output_ref"] == result.asset_record_ref
    assert payload["library_selection_copy"] == (
        "select Source, Asset reference and capture Job evidence; no file content or parser output"
    )
    assert payload["no_content_read_boundary"] == "file_bytes_not_read_or_copied"
    assert payload["parser_boundary"] == "parser_not_started_until_asset_verification"
    assert payload["parser_state"] == "not_started"
    assert payload["published_output_kinds"] == ["source", "asset"]
    assert payload["trace_refs"] == list(result.trace_refs)
    assert payload["library_bridge_item"]["selectable_evidence_refs"] == list(result.trace_refs)
    assert payload["asset_record"]["uri"] == result.asset_uri


def test_workbench_file_source_intake_endpoint_serves_narrow_post() -> None:
    use_case, _sources, _jobs = _file_use_case()
    endpoint = ServeWorkbenchFileSourceIntakeEndpoint()

    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/file-source-intake",
        body={
            "title": "Endpoint File",
            "display_name": "brief.docx",
            "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "size_bytes": 2048,
            "file_reference": "platform-ref-brief-001",
        },
        intake=use_case.execute,
    )

    assert response.status_code == 201
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.body["status"] == "captured"
    assert response.body["source_type"] == "file"
    assert response.body["asset_handoff_state"] == "reference_record_created"
    assert response.body["asset_uri_role"] == "original_file_reference_uri"
    assert response.body["asset_record_ref_role"] == "published_asset_record_ref"
    assert response.body["no_content_read_boundary"] == "file_bytes_not_read_or_copied"
    assert response.body["file_content_policy"] == "metadata_only_no_content_read"
    assert response.body["path_policy"] == "no_os_absolute_path_in_product_core"
    assert response.body["parser_state"] == "not_started"
    assert response.body["published_output_kinds"] == ["source", "asset"]
    assert response.body["library_bridge_item"]["memory_publication_state"] == "not_started"


def test_workbench_file_source_intake_endpoint_rejects_wrong_method_path_and_body() -> None:
    use_case, _sources, _jobs = _file_use_case()
    endpoint = ServeWorkbenchFileSourceIntakeEndpoint()

    wrong_method = endpoint.execute(
        method="GET",
        path="/api/rebuild/workbench/file-source-intake",
        body={
            "title": "Wrong",
            "display_name": "report.pdf",
            "media_type": "application/pdf",
            "size_bytes": 1,
            "file_reference": "platform-ref-report-001",
        },
        intake=use_case.execute,
    )
    wrong_path = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/other",
        body={
            "title": "Wrong",
            "display_name": "report.pdf",
            "media_type": "application/pdf",
            "size_bytes": 1,
            "file_reference": "platform-ref-report-001",
        },
        intake=use_case.execute,
    )
    wrong_body = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/file-source-intake",
        body={
            "title": "Wrong",
            "display_name": "report.pdf",
            "media_type": "application/pdf",
            "size_bytes": 1,
            "file_reference": "C:\\Users\\example\\report.pdf",
        },
        intake=use_case.execute,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "POST"
    assert wrong_path.status_code == 404
    assert wrong_body.status_code == 400
    assert wrong_body.body == {
        "detail": "workbench file Source intake rejected",
        "reason": "file source reference must be platform-neutral",
        "actionable": True,
    }


def test_workbench_image_source_intake_captures_metadata_source_asset_and_job_trace() -> None:
    use_case, sources, jobs = _image_use_case()

    result = use_case.execute(
        title="截图素材",
        display_name="screen.png",
        media_type="image/png",
        size_bytes=8192,
        image_reference="platform-image-ref-001",
        width_px=1280,
        height_px=720,
    )

    source = sources.get(result.source_id)
    job = jobs.get(result.job_id)

    assert result.status == "captured"
    assert result.source_id.startswith("source-image-")
    assert result.source_title == "截图素材"
    assert result.source_type == "image"
    assert result.capture_mode == "reference"
    assert result.media_type == "image/png"
    assert result.size_bytes == 8192
    assert result.image_display_name == "screen.png"
    assert result.image_reference == "platform-image-ref-001"
    assert result.image_reference_role == "platform_neutral_original_image_reference"
    assert result.width_px == 1280
    assert result.height_px == 720
    assert result.binary_content_policy == "metadata_only_no_binary_read"
    assert result.path_policy == "no_os_absolute_path_in_product_core"
    assert result.source_display_kind == "image_reference"
    assert result.capture_boundary == "image_metadata_only"
    assert result.preview_policy == "preview_metadata_only_no_thumbnail_generation"
    assert result.preview_boundary == "thumbnail_not_generated_preview_metadata_only"
    assert result.thumbnail_state == "not_generated"
    assert result.ocr_state == "disabled"
    assert result.extractor_state == "disabled"
    assert result.asset_id == result.source_id.replace("source-", "asset-", 1)
    assert result.asset_uri == f"crp-ref://default/assets/{result.asset_id}"
    assert result.asset_record_ref == f"crp://default/assets/{result.asset_id}"
    assert result.asset_uri_role == "original_image_reference_uri"
    assert result.asset_record_ref_role == "published_asset_record_ref"
    assert result.job_asset_output_ref == result.asset_record_ref
    assert result.asset_storage_mode == "reference"
    assert result.asset_availability == "unknown"
    assert result.asset_availability_reason == "metadata_only_image_reference_not_verified"
    assert result.asset_handoff_state == "image_reference_record_created"
    assert result.asset_handoff_copy == "image Asset reference created before OCR or visual extractor"
    assert result.library_selection_scope == "source_job_asset_image_reference_only"
    assert result.library_selection_copy == (
        "select Source, image Asset reference and capture Job evidence only; no image bytes, thumbnail, OCR, visual extractor output or Memory"
    )
    assert (
        result.library_selection_boundary
        == "source_job_image_asset_reference_only_no_derived_outputs"
    )
    assert result.no_binary_read_boundary == "image_bytes_not_read_or_copied"
    assert result.ocr_boundary == "ocr_disabled_until_explicit_image_extractor_slice"
    assert result.extractor_boundary == "extractor_disabled_until_asset_verification"
    assert result.derived_output_boundary == "no_thumbnail_ocr_visual_features_or_memory_published"
    assert result.job_id == f"job-capture-{result.source_id}"
    assert result.job_progress_message == (
        "captured workbench image metadata and asset reference without binary read"
    )
    assert result.step_names == ("persist_source", "persist_image_asset_reference")
    assert result.published_output_kinds == ("source", "asset")
    assert result.next_step == "audio_video_source_intake_planning_ready"
    assert result.trace_refs == (
        result.source_uri,
        result.asset_uri,
        f"crp://default/logs/jobs/{result.job_id}/persist_source.jsonl",
        f"crp://default/logs/jobs/{result.job_id}/persist_image_asset_reference.jsonl",
    )
    assert result.library_bridge_item.item_kind == "source_preview"
    assert result.library_bridge_item.media_type == "image/png"
    assert result.library_bridge_item.boundary == "source_job_image_asset_reference_selection_only"
    assert result.library_bridge_item.evidence_refs == result.trace_refs
    assert result.library_bridge_item.memory_publication_state == "not_started"
    assert source is not None
    assert job is not None
    assert source["storage_uri"] == result.source_uri
    assert source["original_url"] is None
    assert source["parser_version"] is None
    assert source["metadata"]["display_name"] == "screen.png"
    assert source["metadata"]["image_reference"] == "platform-image-ref-001"
    assert source["metadata"]["image_bytes_read"] is False
    assert source["metadata"]["thumbnail_generated"] is False
    assert source["metadata"]["ocr"] == "disabled"
    assert source["metadata"]["extractor"] == "disabled"
    assert source["metadata"]["content_snapshot"] is None
    assert result.asset_record["source_id"] == result.source_id
    assert result.asset_record["uri"] == result.asset_uri
    assert result.asset_record["metadata"]["image_bytes_read"] is False
    assert result.asset_record["metadata"]["thumbnail_generated"] is False
    assert result.asset_record["metadata"]["ocr"] == "disabled"
    assert result.asset_record["metadata"]["extractor"] == "disabled"
    assert job["published_outputs"] == [
        {
            "kind": "source",
            "uri": result.source_uri,
            "object_id": result.source_id,
            "published": True,
        },
        {
            "kind": "asset",
            "uri": result.asset_record_ref,
            "object_id": result.asset_id,
            "published": True,
        },
    ]
    assert validate_contract_instance("source.schema.json", _schema("source.schema.json"), source) == []
    assert validate_contract_instance("asset.schema.json", _schema("asset.schema.json"), result.asset_record) == []
    assert validate_contract_instance("job.schema.json", _schema("job.schema.json"), job) == []


def test_workbench_image_source_intake_rejects_paths_and_invalid_metadata_before_job_creation() -> None:
    use_case, _sources, jobs = _image_use_case()

    with pytest.raises(ValueError, match="display name"):
        use_case.execute(
            title="Bad",
            display_name="C:\\Users\\example\\screen.png",
            media_type="image/png",
            size_bytes=10,
            image_reference="platform-image-ref-001",
        )
    with pytest.raises(ValueError, match="platform-neutral"):
        use_case.execute(
            title="Bad",
            display_name="screen.png",
            media_type="image/png",
            size_bytes=10,
            image_reference="C:\\Users\\example\\screen.png",
        )
    with pytest.raises(ValueError, match="image media type"):
        use_case.execute(
            title="Bad",
            display_name="screen.png",
            media_type="application/pdf",
            size_bytes=10,
            image_reference="platform-image-ref-001",
        )
    with pytest.raises(ValueError, match="positive width_px"):
        use_case.execute(
            title="Bad",
            display_name="screen.png",
            media_type="image/png",
            size_bytes=10,
            image_reference="platform-image-ref-001",
            width_px=0,
        )

    assert jobs.all() == ()


def test_workbench_image_source_intake_serializer_returns_json_ready_payload() -> None:
    use_case, _sources, _jobs = _image_use_case()
    result = use_case.execute(
        title="Serializer Image",
        display_name="diagram.webp",
        media_type="image/webp",
        size_bytes=256,
        image_reference="platform-image-ref-diagram-001",
    )

    payload = serialize_workbench_image_source_intake(result)

    assert payload["status"] == "captured"
    assert payload["source_id"] == result.source_id
    assert payload["source_type"] == "image"
    assert payload["capture_mode"] == "reference"
    assert payload["media_type"] == "image/webp"
    assert payload["image_display_name"] == "diagram.webp"
    assert payload["image_reference"] == "platform-image-ref-diagram-001"
    assert payload["image_reference_role"] == "platform_neutral_original_image_reference"
    assert payload["binary_content_policy"] == "metadata_only_no_binary_read"
    assert payload["path_policy"] == "no_os_absolute_path_in_product_core"
    assert payload["preview_boundary"] == "thumbnail_not_generated_preview_metadata_only"
    assert payload["thumbnail_state"] == "not_generated"
    assert payload["ocr_state"] == "disabled"
    assert payload["extractor_state"] == "disabled"
    assert payload["asset_handoff_state"] == "image_reference_record_created"
    assert payload["asset_record_ref"] == result.asset_record_ref
    assert payload["asset_uri_role"] == "original_image_reference_uri"
    assert payload["asset_record_ref_role"] == "published_asset_record_ref"
    assert payload["job_asset_output_ref"] == result.asset_record_ref
    assert payload["asset_handoff_copy"] == "image Asset reference created before OCR or visual extractor"
    assert payload["library_selection_copy"] == (
        "select Source, image Asset reference and capture Job evidence only; no image bytes, thumbnail, OCR, visual extractor output or Memory"
    )
    assert payload["library_selection_boundary"] == (
        "source_job_image_asset_reference_only_no_derived_outputs"
    )
    assert payload["no_binary_read_boundary"] == "image_bytes_not_read_or_copied"
    assert payload["ocr_boundary"] == "ocr_disabled_until_explicit_image_extractor_slice"
    assert payload["extractor_boundary"] == "extractor_disabled_until_asset_verification"
    assert payload["derived_output_boundary"] == "no_thumbnail_ocr_visual_features_or_memory_published"
    assert payload["published_output_kinds"] == ["source", "asset"]
    assert payload["trace_refs"] == list(result.trace_refs)
    assert payload["library_bridge_item"]["selectable_evidence_refs"] == list(result.trace_refs)
    assert payload["asset_record"]["uri"] == result.asset_uri


def test_workbench_image_source_intake_endpoint_serves_narrow_post() -> None:
    use_case, _sources, _jobs = _image_use_case()
    endpoint = ServeWorkbenchImageSourceIntakeEndpoint()

    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/image-source-intake",
        body={
            "title": "Endpoint Image",
            "display_name": "photo.jpg",
            "media_type": "image/jpeg",
            "size_bytes": 2048,
            "image_reference": "platform-image-ref-photo-001",
            "width_px": 1024,
            "height_px": 768,
        },
        intake=use_case.execute,
    )

    assert response.status_code == 201
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.body["status"] == "captured"
    assert response.body["source_type"] == "image"
    assert response.body["image_reference_role"] == "platform_neutral_original_image_reference"
    assert response.body["asset_handoff_state"] == "image_reference_record_created"
    assert response.body["asset_handoff_copy"] == "image Asset reference created before OCR or visual extractor"
    assert response.body["asset_uri_role"] == "original_image_reference_uri"
    assert response.body["asset_record_ref_role"] == "published_asset_record_ref"
    assert response.body["no_binary_read_boundary"] == "image_bytes_not_read_or_copied"
    assert response.body["binary_content_policy"] == "metadata_only_no_binary_read"
    assert response.body["path_policy"] == "no_os_absolute_path_in_product_core"
    assert response.body["preview_boundary"] == "thumbnail_not_generated_preview_metadata_only"
    assert response.body["thumbnail_state"] == "not_generated"
    assert response.body["ocr_state"] == "disabled"
    assert response.body["extractor_state"] == "disabled"
    assert response.body["derived_output_boundary"] == "no_thumbnail_ocr_visual_features_or_memory_published"
    assert (
        response.body["library_selection_boundary"]
        == "source_job_image_asset_reference_only_no_derived_outputs"
    )
    assert response.body["published_output_kinds"] == ["source", "asset"]
    assert response.body["next_step"] == "audio_video_source_intake_planning_ready"
    assert response.body["library_bridge_item"]["memory_publication_state"] == "not_started"


def test_workbench_image_source_intake_endpoint_rejects_wrong_method_path_and_body() -> None:
    use_case, _sources, _jobs = _image_use_case()
    endpoint = ServeWorkbenchImageSourceIntakeEndpoint()

    wrong_method = endpoint.execute(
        method="GET",
        path="/api/rebuild/workbench/image-source-intake",
        body={
            "title": "Wrong",
            "display_name": "screen.png",
            "media_type": "image/png",
            "size_bytes": 1,
            "image_reference": "platform-image-ref-001",
        },
        intake=use_case.execute,
    )
    wrong_path = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/other",
        body={
            "title": "Wrong",
            "display_name": "screen.png",
            "media_type": "image/png",
            "size_bytes": 1,
            "image_reference": "platform-image-ref-001",
        },
        intake=use_case.execute,
    )
    wrong_body = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/image-source-intake",
        body={
            "title": "Wrong",
            "display_name": "screen.png",
            "media_type": "image/png",
            "size_bytes": 1,
            "image_reference": "C:\\Users\\example\\screen.png",
        },
        intake=use_case.execute,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "POST"
    assert wrong_path.status_code == 404
    assert wrong_body.status_code == 400
    assert wrong_body.body == {
        "detail": "workbench image Source intake rejected",
        "reason": "image source reference must be platform-neutral",
        "actionable": True,
    }


def test_workbench_audio_source_intake_captures_metadata_source_asset_and_job_trace() -> None:
    use_case, sources, jobs = _audio_use_case()

    result = use_case.execute(
        title="会议录音",
        display_name="meeting.mp3",
        media_type="audio/mpeg",
        size_bytes=65536,
        audio_reference="platform-audio-ref-001",
        duration_ms=120000,
    )

    source = sources.get(result.source_id)
    job = jobs.get(result.job_id)

    assert result.status == "captured"
    assert result.source_id.startswith("source-audio-")
    assert result.source_title == "会议录音"
    assert result.source_type == "audio"
    assert result.capture_mode == "reference"
    assert result.media_type == "audio/mpeg"
    assert result.size_bytes == 65536
    assert result.source_uri_role == "captured_audio_source_record_uri"
    assert result.audio_display_name == "meeting.mp3"
    assert result.audio_reference == "platform-audio-ref-001"
    assert result.audio_reference_role == "platform_neutral_original_audio_reference"
    assert result.duration_ms == 120000
    assert result.media_content_policy == "metadata_only_no_media_read"
    assert result.path_policy == "no_os_absolute_path_in_product_core"
    assert result.source_display_kind == "audio_reference"
    assert result.capture_boundary == "audio_metadata_only"
    assert result.transcription_state == "disabled"
    assert result.transcription_policy == "transcription_disabled_until_explicit_audio_slice"
    assert result.waveform_state == "not_generated"
    assert result.waveform_policy == "waveform_generation_disabled_until_media_processing_slice"
    assert result.remote_processing_state == "not_performed"
    assert result.remote_processing_policy == "remote_media_processing_not_performed"
    assert result.asset_id == result.source_id.replace("source-", "asset-", 1)
    assert result.asset_uri == f"crp-ref://default/assets/{result.asset_id}"
    assert result.asset_record_ref == f"crp://default/assets/{result.asset_id}"
    assert result.asset_uri_role == "original_audio_reference_uri"
    assert result.asset_record_ref_role == "published_asset_record_ref"
    assert result.job_source_output_ref == result.source_uri
    assert result.job_source_output_ref_role == "job_published_source_output_ref"
    assert result.job_asset_output_ref == result.asset_record_ref
    assert result.job_asset_output_ref_role == "job_published_audio_asset_output_ref"
    assert result.asset_storage_mode == "reference"
    assert result.asset_availability == "unknown"
    assert result.asset_availability_reason == "metadata_only_audio_reference_not_verified"
    assert result.asset_handoff_state == "audio_reference_record_created"
    assert result.asset_handoff_copy == "audio Asset reference created before transcription or waveform generation"
    assert result.library_selection_scope == "source_job_asset_audio_reference_only"
    assert result.library_selection_copy == (
        "select Source, audio Asset reference and capture Job evidence only; no audio bytes, transcript, waveform, remote processing output or Memory"
    )
    assert result.library_selection_boundary == "source_job_audio_asset_reference_only_no_derived_outputs"
    assert result.library_selection_role_summary == (
        "selected evidence contains captured Source URI, original audio Asset reference URI, published Asset record ref and capture Job logs only"
    )
    assert result.library_selection_excluded_outputs == (
        "audio_bytes",
        "transcript",
        "waveform",
        "remote_media_output",
        "memory_candidate",
        "memory_publication",
    )
    assert result.memory_selection_policy == "library_selection_cannot_publish_or_imply_memory"
    assert result.no_media_read_boundary == "audio_bytes_not_read_or_copied"
    assert result.derived_output_state == "not_created"
    assert result.derived_output_boundary == "no_transcript_waveform_remote_processing_or_memory_published"
    assert result.parser_readiness_state == "metadata_ready_parser_not_started"
    assert result.parser_readiness_boundary == (
        "audio_parser_not_started_until_asset_verification_and_explicit_parser_slice"
    )
    assert result.parser_readiness_copy == (
        "audio parser readiness can be reviewed from Source, Asset reference and capture Job evidence only; "
        "no audio bytes, transcript, waveform, remote output or Memory are available to parser"
    )
    assert result.trace_display_sections == (
        "source_record",
        "original_audio_asset_reference",
        "published_asset_record",
        "capture_job_outputs",
        "excluded_derived_outputs",
        "library_selection_boundary",
        "parser_readiness_boundary",
    )
    assert result.job_id == f"job-capture-{result.source_id}"
    assert result.job_progress_message == (
        "captured workbench audio metadata and asset reference without media read"
    )
    assert result.step_names == ("persist_source", "persist_audio_asset_reference")
    assert result.published_output_kinds == ("source", "asset")
    assert result.next_step == "audio_parser_boundary_readiness_ready"
    assert result.trace_refs == (
        result.source_uri,
        result.asset_uri,
        f"crp://default/logs/jobs/{result.job_id}/persist_source.jsonl",
        f"crp://default/logs/jobs/{result.job_id}/persist_audio_asset_reference.jsonl",
    )
    assert result.parser_required_evidence_refs == result.trace_refs
    assert result.parser_blocked_operations == (
        "audio_byte_read",
        "transcription",
        "waveform_generation",
        "remote_media_processing",
        "memory_candidate",
        "memory_publication",
    )
    assert result.library_bridge_item.item_kind == "source_preview"
    assert result.library_bridge_item.media_type == "audio/mpeg"
    assert result.library_bridge_item.boundary == "source_job_audio_asset_reference_selection_only"
    assert result.library_bridge_item.evidence_refs == result.trace_refs
    assert result.library_bridge_item.memory_publication_state == "not_started"
    assert source is not None
    assert job is not None
    assert source["storage_uri"] == result.source_uri
    assert source["original_url"] is None
    assert source["parser_version"] is None
    assert source["metadata"]["display_name"] == "meeting.mp3"
    assert source["metadata"]["audio_reference"] == "platform-audio-ref-001"
    assert source["metadata"]["audio_bytes_read"] is False
    assert source["metadata"]["transcription"] == "disabled"
    assert source["metadata"]["waveform_generated"] is False
    assert source["metadata"]["remote_processing"] == "not_performed"
    assert source["metadata"]["content_snapshot"] is None
    assert result.asset_record["source_id"] == result.source_id
    assert result.asset_record["uri"] == result.asset_uri
    assert result.asset_record["metadata"]["audio_bytes_read"] is False
    assert result.asset_record["metadata"]["transcription"] == "disabled"
    assert result.asset_record["metadata"]["waveform_generated"] is False
    assert result.asset_record["metadata"]["remote_processing"] == "not_performed"
    assert job["published_outputs"] == [
        {
            "kind": "source",
            "uri": result.source_uri,
            "object_id": result.source_id,
            "published": True,
        },
        {
            "kind": "asset",
            "uri": result.asset_record_ref,
            "object_id": result.asset_id,
            "published": True,
        },
    ]
    assert validate_contract_instance("source.schema.json", _schema("source.schema.json"), source) == []
    assert validate_contract_instance("asset.schema.json", _schema("asset.schema.json"), result.asset_record) == []
    assert validate_contract_instance("job.schema.json", _schema("job.schema.json"), job) == []


def test_workbench_audio_source_intake_rejects_paths_and_invalid_metadata_before_job_creation() -> None:
    use_case, _sources, jobs = _audio_use_case()

    with pytest.raises(ValueError, match="display name"):
        use_case.execute(
            title="Bad",
            display_name="C:\\Users\\example\\meeting.mp3",
            media_type="audio/mpeg",
            size_bytes=10,
            audio_reference="platform-audio-ref-001",
        )
    with pytest.raises(ValueError, match="platform-neutral"):
        use_case.execute(
            title="Bad",
            display_name="meeting.mp3",
            media_type="audio/mpeg",
            size_bytes=10,
            audio_reference="C:\\Users\\example\\meeting.mp3",
        )
    with pytest.raises(ValueError, match="audio media type"):
        use_case.execute(
            title="Bad",
            display_name="meeting.mp3",
            media_type="image/png",
            size_bytes=10,
            audio_reference="platform-audio-ref-001",
        )
    with pytest.raises(ValueError, match="positive duration_ms"):
        use_case.execute(
            title="Bad",
            display_name="meeting.mp3",
            media_type="audio/mpeg",
            size_bytes=10,
            audio_reference="platform-audio-ref-001",
            duration_ms=0,
        )

    assert jobs.all() == ()


def test_workbench_audio_source_intake_serializer_returns_json_ready_payload() -> None:
    use_case, _sources, _jobs = _audio_use_case()
    result = use_case.execute(
        title="Serializer Audio",
        display_name="voice.ogg",
        media_type="audio/ogg",
        size_bytes=512,
        audio_reference="platform-audio-ref-voice-001",
    )

    payload = serialize_workbench_audio_source_intake(result)

    assert payload["status"] == "captured"
    assert payload["source_id"] == result.source_id
    assert payload["source_type"] == "audio"
    assert payload["capture_mode"] == "reference"
    assert payload["media_type"] == "audio/ogg"
    assert payload["source_uri_role"] == "captured_audio_source_record_uri"
    assert payload["audio_display_name"] == "voice.ogg"
    assert payload["audio_reference"] == "platform-audio-ref-voice-001"
    assert payload["audio_reference_role"] == "platform_neutral_original_audio_reference"
    assert payload["media_content_policy"] == "metadata_only_no_media_read"
    assert payload["path_policy"] == "no_os_absolute_path_in_product_core"
    assert payload["transcription_state"] == "disabled"
    assert payload["waveform_state"] == "not_generated"
    assert payload["remote_processing_state"] == "not_performed"
    assert payload["asset_handoff_state"] == "audio_reference_record_created"
    assert payload["asset_record_ref"] == result.asset_record_ref
    assert payload["asset_uri_role"] == "original_audio_reference_uri"
    assert payload["asset_record_ref_role"] == "published_asset_record_ref"
    assert payload["job_source_output_ref"] == result.source_uri
    assert payload["job_source_output_ref_role"] == "job_published_source_output_ref"
    assert payload["job_asset_output_ref"] == result.asset_record_ref
    assert payload["job_asset_output_ref_role"] == "job_published_audio_asset_output_ref"
    assert payload["asset_handoff_copy"] == "audio Asset reference created before transcription or waveform generation"
    assert payload["library_selection_copy"] == (
        "select Source, audio Asset reference and capture Job evidence only; no audio bytes, transcript, waveform, remote processing output or Memory"
    )
    assert payload["library_selection_boundary"] == (
        "source_job_audio_asset_reference_only_no_derived_outputs"
    )
    assert payload["library_selection_role_summary"] == (
        "selected evidence contains captured Source URI, original audio Asset reference URI, published Asset record ref and capture Job logs only"
    )
    assert payload["library_selection_excluded_outputs"] == [
        "audio_bytes",
        "transcript",
        "waveform",
        "remote_media_output",
        "memory_candidate",
        "memory_publication",
    ]
    assert payload["memory_selection_policy"] == "library_selection_cannot_publish_or_imply_memory"
    assert payload["no_media_read_boundary"] == "audio_bytes_not_read_or_copied"
    assert payload["derived_output_state"] == "not_created"
    assert payload["derived_output_boundary"] == "no_transcript_waveform_remote_processing_or_memory_published"
    assert payload["parser_readiness_state"] == "metadata_ready_parser_not_started"
    assert payload["parser_readiness_boundary"] == (
        "audio_parser_not_started_until_asset_verification_and_explicit_parser_slice"
    )
    assert payload["parser_readiness_copy"] == (
        "audio parser readiness can be reviewed from Source, Asset reference and capture Job evidence only; "
        "no audio bytes, transcript, waveform, remote output or Memory are available to parser"
    )
    assert payload["parser_required_evidence_refs"] == list(result.trace_refs)
    assert payload["parser_blocked_operations"] == [
        "audio_byte_read",
        "transcription",
        "waveform_generation",
        "remote_media_processing",
        "memory_candidate",
        "memory_publication",
    ]
    assert payload["trace_display_sections"] == [
        "source_record",
        "original_audio_asset_reference",
        "published_asset_record",
        "capture_job_outputs",
        "excluded_derived_outputs",
        "library_selection_boundary",
        "parser_readiness_boundary",
    ]
    assert payload["published_output_kinds"] == ["source", "asset"]
    assert payload["trace_refs"] == list(result.trace_refs)
    assert payload["library_bridge_item"]["selectable_evidence_refs"] == list(result.trace_refs)
    assert payload["asset_record"]["uri"] == result.asset_uri


def test_workbench_audio_source_intake_endpoint_serves_narrow_post() -> None:
    use_case, _sources, _jobs = _audio_use_case()
    endpoint = ServeWorkbenchAudioSourceIntakeEndpoint()

    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/audio-source-intake",
        body={
            "title": "Endpoint Audio",
            "display_name": "meeting.mp3",
            "media_type": "audio/mpeg",
            "size_bytes": 65536,
            "audio_reference": "platform-audio-ref-001",
            "duration_ms": 120000,
        },
        intake=use_case.execute,
    )

    assert response.status_code == 201
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.body["status"] == "captured"
    assert response.body["source_type"] == "audio"
    assert response.body["source_uri_role"] == "captured_audio_source_record_uri"
    assert response.body["audio_reference_role"] == "platform_neutral_original_audio_reference"
    assert response.body["asset_handoff_state"] == "audio_reference_record_created"
    assert response.body["asset_handoff_copy"] == "audio Asset reference created before transcription or waveform generation"
    assert response.body["asset_uri_role"] == "original_audio_reference_uri"
    assert response.body["asset_record_ref_role"] == "published_asset_record_ref"
    assert response.body["job_source_output_ref"] == response.body["source_uri"]
    assert response.body["job_source_output_ref_role"] == "job_published_source_output_ref"
    assert response.body["job_asset_output_ref_role"] == "job_published_audio_asset_output_ref"
    assert response.body["no_media_read_boundary"] == "audio_bytes_not_read_or_copied"
    assert response.body["media_content_policy"] == "metadata_only_no_media_read"
    assert response.body["path_policy"] == "no_os_absolute_path_in_product_core"
    assert response.body["transcription_state"] == "disabled"
    assert response.body["waveform_state"] == "not_generated"
    assert response.body["remote_processing_state"] == "not_performed"
    assert response.body["derived_output_state"] == "not_created"
    assert response.body["derived_output_boundary"] == "no_transcript_waveform_remote_processing_or_memory_published"
    assert response.body["library_selection_boundary"] == "source_job_audio_asset_reference_only_no_derived_outputs"
    assert response.body["library_selection_excluded_outputs"] == [
        "audio_bytes",
        "transcript",
        "waveform",
        "remote_media_output",
        "memory_candidate",
        "memory_publication",
    ]
    assert response.body["memory_selection_policy"] == "library_selection_cannot_publish_or_imply_memory"
    assert response.body["parser_readiness_state"] == "metadata_ready_parser_not_started"
    assert response.body["parser_readiness_boundary"] == (
        "audio_parser_not_started_until_asset_verification_and_explicit_parser_slice"
    )
    assert response.body["parser_blocked_operations"] == [
        "audio_byte_read",
        "transcription",
        "waveform_generation",
        "remote_media_processing",
        "memory_candidate",
        "memory_publication",
    ]
    assert response.body["published_output_kinds"] == ["source", "asset"]
    assert response.body["next_step"] == "audio_parser_boundary_readiness_ready"
    assert response.body["library_bridge_item"]["memory_publication_state"] == "not_started"


def test_workbench_audio_source_intake_endpoint_rejects_wrong_method_path_and_body() -> None:
    use_case, _sources, _jobs = _audio_use_case()
    endpoint = ServeWorkbenchAudioSourceIntakeEndpoint()

    wrong_method = endpoint.execute(
        method="GET",
        path="/api/rebuild/workbench/audio-source-intake",
        body={
            "title": "Wrong",
            "display_name": "meeting.mp3",
            "media_type": "audio/mpeg",
            "size_bytes": 1,
            "audio_reference": "platform-audio-ref-001",
        },
        intake=use_case.execute,
    )
    wrong_path = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/other",
        body={
            "title": "Wrong",
            "display_name": "meeting.mp3",
            "media_type": "audio/mpeg",
            "size_bytes": 1,
            "audio_reference": "platform-audio-ref-001",
        },
        intake=use_case.execute,
    )
    wrong_body = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/audio-source-intake",
        body={
            "title": "Wrong",
            "display_name": "meeting.mp3",
            "media_type": "audio/mpeg",
            "size_bytes": 1,
            "audio_reference": "C:\\Users\\example\\meeting.mp3",
        },
        intake=use_case.execute,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "POST"
    assert wrong_path.status_code == 404
    assert wrong_body.status_code == 400
    assert wrong_body.body == {
        "detail": "workbench audio Source intake rejected",
        "reason": "audio source reference must be platform-neutral",
        "actionable": True,
    }


def test_workbench_video_source_intake_captures_metadata_source_asset_and_job_trace() -> None:
    use_case, sources, jobs = _video_use_case()

    result = use_case.execute(
        title="会议视频",
        display_name="meeting.mp4",
        media_type="video/mp4",
        size_bytes=262144,
        video_reference="platform-video-ref-001",
        duration_ms=180000,
        width_px=1920,
        height_px=1080,
    )

    source = sources.get(result.source_id)
    job = jobs.get(result.job_id)

    assert result.status == "captured"
    assert result.source_id.startswith("source-video-")
    assert result.source_title == "会议视频"
    assert result.source_type == "video"
    assert result.capture_mode == "reference"
    assert result.media_type == "video/mp4"
    assert result.size_bytes == 262144
    assert result.source_uri_role == "captured_video_source_record_uri"
    assert result.video_display_name == "meeting.mp4"
    assert result.video_reference == "platform-video-ref-001"
    assert result.video_reference_role == "platform_neutral_original_video_reference"
    assert result.duration_ms == 180000
    assert result.width_px == 1920
    assert result.height_px == 1080
    assert result.media_content_policy == "metadata_only_no_media_read"
    assert result.path_policy == "no_os_absolute_path_in_product_core"
    assert result.source_display_kind == "video_reference"
    assert result.capture_boundary == "video_metadata_only"
    assert result.frame_extraction_state == "disabled"
    assert result.frame_extraction_policy == "frame_extraction_disabled_until_explicit_video_slice"
    assert result.audio_track_extraction_state == "disabled"
    assert result.audio_track_extraction_policy == "audio_track_extraction_disabled_until_explicit_video_slice"
    assert result.thumbnail_state == "not_generated"
    assert result.thumbnail_policy == "thumbnail_generation_disabled_until_media_processing_slice"
    assert result.remote_processing_state == "not_performed"
    assert result.remote_processing_policy == "remote_media_processing_not_performed"
    assert result.asset_id == result.source_id.replace("source-", "asset-", 1)
    assert result.asset_uri == f"crp-ref://default/assets/{result.asset_id}"
    assert result.asset_record_ref == f"crp://default/assets/{result.asset_id}"
    assert result.asset_uri_role == "original_video_reference_uri"
    assert result.asset_record_ref_role == "published_asset_record_ref"
    assert result.job_source_output_ref == result.source_uri
    assert result.job_source_output_ref_role == "job_published_source_output_ref"
    assert result.job_asset_output_ref == result.asset_record_ref
    assert result.job_asset_output_ref_role == "job_published_video_asset_output_ref"
    assert result.asset_storage_mode == "reference"
    assert result.asset_availability == "unknown"
    assert result.asset_availability_reason == "metadata_only_video_reference_not_verified"
    assert result.asset_handoff_state == "video_reference_record_created"
    assert result.asset_handoff_copy == (
        "video Asset reference created before frame extraction, audio-track extraction or thumbnail generation"
    )
    assert result.library_selection_scope == "source_job_asset_video_reference_only"
    assert result.library_selection_copy == (
        "select Source, video Asset reference and capture Job evidence only; no video bytes, frames, audio track, thumbnail, remote processing output or Memory"
    )
    assert result.library_selection_boundary == "source_job_video_asset_reference_only_no_derived_outputs"
    assert result.library_selection_role_summary == (
        "selected evidence contains captured Source URI, original video Asset reference URI, published Asset record ref and capture Job logs only"
    )
    assert result.library_selection_excluded_outputs == (
        "video_bytes",
        "extracted_frames",
        "audio_track",
        "thumbnail",
        "remote_media_output",
        "memory_candidate",
        "memory_publication",
    )
    assert result.memory_selection_policy == "library_selection_cannot_publish_or_imply_memory"
    assert result.no_media_read_boundary == "video_bytes_not_read_or_copied"
    assert result.derived_output_state == "not_created"
    assert result.derived_output_boundary == "no_frames_audio_track_thumbnail_remote_processing_or_memory_published"
    assert result.parser_readiness_state == "metadata_ready_parser_not_started"
    assert result.parser_readiness_boundary == (
        "video_parser_not_started_until_asset_verification_and_explicit_parser_slice"
    )
    assert result.parser_readiness_copy == (
        "video parser readiness can be reviewed from Source, Asset reference and capture Job evidence only; "
        "no video bytes, extracted frames, audio track, thumbnail, remote output or Memory are available to parser"
    )
    assert result.trace_display_sections == (
        "source_record",
        "original_video_asset_reference",
        "published_asset_record",
        "capture_job_outputs",
        "excluded_derived_outputs",
        "library_selection_boundary",
        "parser_readiness_boundary",
    )
    assert result.trace_role_summary == (
        "video trace separates captured Source URI, platform-neutral original video Asset reference URI, published Asset record ref, Job Source output and Job Asset output"
    )
    assert result.job_output_role_summary == (
        "capture Job publishes the Source record and the video Asset record ref only; it does not publish video bytes, extracted frames, audio track, thumbnail, remote media output or Memory"
    )
    assert result.library_selection_evidence_roles == (
        "captured_source_uri",
        "original_video_asset_reference_uri",
        "published_asset_record_ref",
        "job_persist_source_log",
        "job_persist_video_asset_reference_log",
    )
    assert result.job_id == f"job-capture-{result.source_id}"
    assert result.job_progress_message == (
        "captured workbench video metadata and asset reference without media read"
    )
    assert result.step_names == ("persist_source", "persist_video_asset_reference")
    assert result.published_output_kinds == ("source", "asset")
    assert result.next_step == "video_parser_boundary_readiness_ready"
    assert result.trace_refs == (
        result.source_uri,
        result.asset_uri,
        f"crp://default/logs/jobs/{result.job_id}/persist_source.jsonl",
        f"crp://default/logs/jobs/{result.job_id}/persist_video_asset_reference.jsonl",
    )
    assert result.parser_required_evidence_refs == result.trace_refs
    assert result.parser_blocked_operations == (
        "video_byte_read",
        "frame_extraction",
        "audio_track_extraction",
        "thumbnail_generation",
        "remote_media_processing",
        "memory_candidate",
        "memory_publication",
    )
    assert result.library_bridge_item.item_kind == "source_preview"
    assert result.library_bridge_item.media_type == "video/mp4"
    assert result.library_bridge_item.boundary == "source_job_video_asset_reference_selection_only"
    assert result.library_bridge_item.evidence_refs == result.trace_refs
    assert result.library_bridge_item.memory_publication_state == "not_started"
    assert source is not None
    assert job is not None
    assert source["storage_uri"] == result.source_uri
    assert source["metadata"]["display_name"] == "meeting.mp4"
    assert source["metadata"]["video_reference"] == "platform-video-ref-001"
    assert source["metadata"]["video_bytes_read"] is False
    assert source["metadata"]["frame_extraction"] == "disabled"
    assert source["metadata"]["audio_track_extraction"] == "disabled"
    assert source["metadata"]["thumbnail_generated"] is False
    assert source["metadata"]["remote_processing"] == "not_performed"
    assert source["metadata"]["content_snapshot"] is None
    assert result.asset_record["source_id"] == result.source_id
    assert result.asset_record["uri"] == result.asset_uri
    assert result.asset_record["metadata"]["video_bytes_read"] is False
    assert result.asset_record["metadata"]["frame_extraction"] == "disabled"
    assert result.asset_record["metadata"]["audio_track_extraction"] == "disabled"
    assert result.asset_record["metadata"]["thumbnail_generated"] is False
    assert result.asset_record["metadata"]["remote_processing"] == "not_performed"
    assert job["published_outputs"] == [
        {
            "kind": "source",
            "uri": result.source_uri,
            "object_id": result.source_id,
            "published": True,
        },
        {
            "kind": "asset",
            "uri": result.asset_record_ref,
            "object_id": result.asset_id,
            "published": True,
        },
    ]
    assert validate_contract_instance("source.schema.json", _schema("source.schema.json"), source) == []
    assert validate_contract_instance("asset.schema.json", _schema("asset.schema.json"), result.asset_record) == []
    assert validate_contract_instance("job.schema.json", _schema("job.schema.json"), job) == []


def test_workbench_video_source_intake_rejects_paths_and_invalid_metadata_before_job_creation() -> None:
    use_case, _sources, jobs = _video_use_case()

    with pytest.raises(ValueError, match="display name"):
        use_case.execute(
            title="Bad",
            display_name="C:\\Users\\example\\meeting.mp4",
            media_type="video/mp4",
            size_bytes=10,
            video_reference="platform-video-ref-001",
        )
    with pytest.raises(ValueError, match="platform-neutral"):
        use_case.execute(
            title="Bad",
            display_name="meeting.mp4",
            media_type="video/mp4",
            size_bytes=10,
            video_reference="C:\\Users\\example\\meeting.mp4",
        )
    with pytest.raises(ValueError, match="video media type"):
        use_case.execute(
            title="Bad",
            display_name="meeting.mp4",
            media_type="audio/mpeg",
            size_bytes=10,
            video_reference="platform-video-ref-001",
        )
    with pytest.raises(ValueError, match="positive duration_ms"):
        use_case.execute(
            title="Bad",
            display_name="meeting.mp4",
            media_type="video/mp4",
            size_bytes=10,
            video_reference="platform-video-ref-001",
            duration_ms=0,
        )
    with pytest.raises(ValueError, match="positive width_px"):
        use_case.execute(
            title="Bad",
            display_name="meeting.mp4",
            media_type="video/mp4",
            size_bytes=10,
            video_reference="platform-video-ref-001",
            width_px=0,
        )
    with pytest.raises(ValueError, match="positive height_px"):
        use_case.execute(
            title="Bad",
            display_name="meeting.mp4",
            media_type="video/mp4",
            size_bytes=10,
            video_reference="platform-video-ref-001",
            height_px=0,
        )

    assert jobs.all() == ()


def test_workbench_video_source_intake_serializer_returns_json_ready_payload() -> None:
    use_case, _sources, _jobs = _video_use_case()
    result = use_case.execute(
        title="Serializer Video",
        display_name="clip.webm",
        media_type="video/webm",
        size_bytes=1024,
        video_reference="platform-video-ref-clip-001",
    )

    payload = serialize_workbench_video_source_intake(result)

    assert payload["status"] == "captured"
    assert payload["source_id"] == result.source_id
    assert payload["source_type"] == "video"
    assert payload["capture_mode"] == "reference"
    assert payload["media_type"] == "video/webm"
    assert payload["source_uri_role"] == "captured_video_source_record_uri"
    assert payload["video_display_name"] == "clip.webm"
    assert payload["video_reference"] == "platform-video-ref-clip-001"
    assert payload["video_reference_role"] == "platform_neutral_original_video_reference"
    assert payload["media_content_policy"] == "metadata_only_no_media_read"
    assert payload["path_policy"] == "no_os_absolute_path_in_product_core"
    assert payload["frame_extraction_state"] == "disabled"
    assert payload["audio_track_extraction_state"] == "disabled"
    assert payload["thumbnail_state"] == "not_generated"
    assert payload["remote_processing_state"] == "not_performed"
    assert payload["asset_handoff_state"] == "video_reference_record_created"
    assert payload["job_source_output_ref"] == result.source_uri
    assert payload["job_asset_output_ref"] == result.asset_record_ref
    assert payload["job_asset_output_ref_role"] == "job_published_video_asset_output_ref"
    assert payload["library_selection_excluded_outputs"] == [
        "video_bytes",
        "extracted_frames",
        "audio_track",
        "thumbnail",
        "remote_media_output",
        "memory_candidate",
        "memory_publication",
    ]
    assert payload["no_media_read_boundary"] == "video_bytes_not_read_or_copied"
    assert payload["derived_output_boundary"] == "no_frames_audio_track_thumbnail_remote_processing_or_memory_published"
    assert payload["parser_readiness_state"] == "metadata_ready_parser_not_started"
    assert payload["parser_readiness_boundary"] == (
        "video_parser_not_started_until_asset_verification_and_explicit_parser_slice"
    )
    assert payload["parser_readiness_copy"] == (
        "video parser readiness can be reviewed from Source, Asset reference and capture Job evidence only; "
        "no video bytes, extracted frames, audio track, thumbnail, remote output or Memory are available to parser"
    )
    assert payload["parser_required_evidence_refs"] == list(result.trace_refs)
    assert payload["parser_blocked_operations"] == [
        "video_byte_read",
        "frame_extraction",
        "audio_track_extraction",
        "thumbnail_generation",
        "remote_media_processing",
        "memory_candidate",
        "memory_publication",
    ]
    assert payload["trace_display_sections"] == [
        "source_record",
        "original_video_asset_reference",
        "published_asset_record",
        "capture_job_outputs",
        "excluded_derived_outputs",
        "library_selection_boundary",
        "parser_readiness_boundary",
    ]
    assert payload["trace_role_summary"] == (
        "video trace separates captured Source URI, platform-neutral original video Asset reference URI, published Asset record ref, Job Source output and Job Asset output"
    )
    assert payload["job_output_role_summary"] == (
        "capture Job publishes the Source record and the video Asset record ref only; it does not publish video bytes, extracted frames, audio track, thumbnail, remote media output or Memory"
    )
    assert payload["library_selection_evidence_roles"] == [
        "captured_source_uri",
        "original_video_asset_reference_uri",
        "published_asset_record_ref",
        "job_persist_source_log",
        "job_persist_video_asset_reference_log",
    ]
    assert payload["published_output_kinds"] == ["source", "asset"]
    assert payload["trace_refs"] == list(result.trace_refs)
    assert payload["library_bridge_item"]["selectable_evidence_refs"] == list(result.trace_refs)
    assert payload["asset_record"]["uri"] == result.asset_uri


def test_workbench_video_source_intake_endpoint_serves_narrow_post() -> None:
    use_case, _sources, _jobs = _video_use_case()
    endpoint = ServeWorkbenchVideoSourceIntakeEndpoint()

    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/video-source-intake",
        body={
            "title": "Endpoint Video",
            "display_name": "meeting.mp4",
            "media_type": "video/mp4",
            "size_bytes": 262144,
            "video_reference": "platform-video-ref-001",
            "duration_ms": 180000,
            "width_px": 1920,
            "height_px": 1080,
        },
        intake=use_case.execute,
    )

    assert response.status_code == 201
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.body["status"] == "captured"
    assert response.body["source_type"] == "video"
    assert response.body["source_uri_role"] == "captured_video_source_record_uri"
    assert response.body["video_reference_role"] == "platform_neutral_original_video_reference"
    assert response.body["asset_handoff_state"] == "video_reference_record_created"
    assert response.body["asset_uri_role"] == "original_video_reference_uri"
    assert response.body["job_source_output_ref"] == response.body["source_uri"]
    assert response.body["job_source_output_ref_role"] == "job_published_source_output_ref"
    assert response.body["job_asset_output_ref_role"] == "job_published_video_asset_output_ref"
    assert response.body["no_media_read_boundary"] == "video_bytes_not_read_or_copied"
    assert response.body["media_content_policy"] == "metadata_only_no_media_read"
    assert response.body["path_policy"] == "no_os_absolute_path_in_product_core"
    assert response.body["frame_extraction_state"] == "disabled"
    assert response.body["audio_track_extraction_state"] == "disabled"
    assert response.body["thumbnail_state"] == "not_generated"
    assert response.body["remote_processing_state"] == "not_performed"
    assert response.body["derived_output_state"] == "not_created"
    assert response.body["derived_output_boundary"] == "no_frames_audio_track_thumbnail_remote_processing_or_memory_published"
    assert response.body["library_selection_boundary"] == "source_job_video_asset_reference_only_no_derived_outputs"
    assert response.body["library_selection_excluded_outputs"] == [
        "video_bytes",
        "extracted_frames",
        "audio_track",
        "thumbnail",
        "remote_media_output",
        "memory_candidate",
        "memory_publication",
    ]
    assert response.body["trace_role_summary"] == (
        "video trace separates captured Source URI, platform-neutral original video Asset reference URI, published Asset record ref, Job Source output and Job Asset output"
    )
    assert response.body["job_output_role_summary"] == (
        "capture Job publishes the Source record and the video Asset record ref only; it does not publish video bytes, extracted frames, audio track, thumbnail, remote media output or Memory"
    )
    assert response.body["library_selection_evidence_roles"] == [
        "captured_source_uri",
        "original_video_asset_reference_uri",
        "published_asset_record_ref",
        "job_persist_source_log",
        "job_persist_video_asset_reference_log",
    ]
    assert response.body["memory_selection_policy"] == "library_selection_cannot_publish_or_imply_memory"
    assert response.body["parser_readiness_state"] == "metadata_ready_parser_not_started"
    assert response.body["parser_readiness_boundary"] == (
        "video_parser_not_started_until_asset_verification_and_explicit_parser_slice"
    )
    assert response.body["parser_blocked_operations"] == [
        "video_byte_read",
        "frame_extraction",
        "audio_track_extraction",
        "thumbnail_generation",
        "remote_media_processing",
        "memory_candidate",
        "memory_publication",
    ]
    assert response.body["published_output_kinds"] == ["source", "asset"]
    assert response.body["next_step"] == "video_parser_boundary_readiness_ready"
    assert response.body["library_bridge_item"]["memory_publication_state"] == "not_started"


def test_workbench_video_source_intake_endpoint_rejects_wrong_method_path_and_body() -> None:
    use_case, _sources, _jobs = _video_use_case()
    endpoint = ServeWorkbenchVideoSourceIntakeEndpoint()

    wrong_method = endpoint.execute(
        method="GET",
        path="/api/rebuild/workbench/video-source-intake",
        body={
            "title": "Wrong",
            "display_name": "meeting.mp4",
            "media_type": "video/mp4",
            "size_bytes": 1,
            "video_reference": "platform-video-ref-001",
        },
        intake=use_case.execute,
    )
    wrong_path = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/other",
        body={
            "title": "Wrong",
            "display_name": "meeting.mp4",
            "media_type": "video/mp4",
            "size_bytes": 1,
            "video_reference": "platform-video-ref-001",
        },
        intake=use_case.execute,
    )
    wrong_body = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/video-source-intake",
        body={
            "title": "Wrong",
            "display_name": "meeting.mp4",
            "media_type": "video/mp4",
            "size_bytes": 1,
            "video_reference": "C:\\Users\\example\\meeting.mp4",
        },
        intake=use_case.execute,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "POST"
    assert wrong_path.status_code == 404
    assert wrong_body.status_code == 400
    assert wrong_body.body == {
        "detail": "workbench video Source intake rejected",
        "reason": "video source reference must be platform-neutral",
        "actionable": True,
    }
