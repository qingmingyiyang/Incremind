from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.composition import ObjectStoreLibraryOverviewReader, build_source_output_memory_candidate_handoff
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.memory_core import ObjectStoreMemoryCandidateRepository, SQLiteMemoryReader
from core.product_core import (
    CreateMediaProcessingQueueJob,
    CreateMemoryCandidateFromSourceOutput,
    GetLibraryOverview,
    MediaOcrAdapterResult,
    ReadSourceTextContent,
    RunImageOcrAdapterForMediaJob,
    ServeSourceOutputMemoryCandidateEndpoint,
    SourceOutputMemoryCandidateError,
    serialize_library_overview,
    serialize_source_output_memory_candidate,
)
from core.storage_provider import JsonObjectStore
from tests.rebuild.memory_candidate_saga_review_testlib import (
    publish_staging_user_confirmed,
    review_candidate_to_staging,
    saga_records,
)
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


class StubOcrAdapter:
    def extract_text(
        self,
        *,
        source: dict[str, object],
        job: dict[str, object],
    ) -> MediaOcrAdapterResult:
        return MediaOcrAdapterResult(
            text="白板内容：Memory 候选必须先待审，不能自动写入长期记忆。",
            provider="stub-local-ocr",
            confidence=0.92,
        )


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _image_source(object_store: JsonObjectStore) -> dict[str, object]:
    return dict(
        ObjectStoreSourceRegistrar(object_store).register(
            SourceSubmission(
                kind="image",
                title="Memory whiteboard",
                display_name="whiteboard.png",
                media_type="image/png",
                size_bytes=4096,
                image_reference="platform-image-ref-memory-whiteboard",
                width_px=1280,
                height_px=720,
            )
        )
    )


def test_content_read_output_creates_reviewable_memory_candidate_without_publication(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="Memory source note",
            content="正文读取完成后，只能先生成待审记忆候选，不能自动发布长期记忆。",
        )
    )
    ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    use_case = CreateMemoryCandidateFromSourceOutput(
        object_store,
        now="2026-07-01T18:40:00+08:00",
    )

    result = use_case.execute_from_content_read(
        source_id=str(source["id"]),
        project_id="project-alpha",
    )
    payload = serialize_source_output_memory_candidate(result)
    candidate = ObjectStoreMemoryCandidateRepository(object_store).get(result.candidate_id)
    read_record = object_store.read("source_content_reads", f"content-read-{source['id']}")
    updated_source = object_store.read("sources", str(source["id"]))
    events = object_store.list("activity_events")
    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    candidate_item = next(item for item in overview["items"] if item["item_id"] == result.candidate_id)

    assert payload["status"] == "candidate_created"
    assert payload["candidate_status"] == "pending_review"
    assert payload["memory_publication_state"] == "candidate_created_not_published"
    assert candidate is not None
    assert validate_contract_instance(
        "memory_candidate.schema.json",
        _schema("memory_candidate.schema.json"),
        candidate,
    ) == []
    assert candidate["project_id"] == "project-alpha"
    assert candidate["candidate_type"] == "other"
    assert candidate["status"] == "pending_review"
    assert candidate["source_refs"] == [
        {
            "source_id": source["id"],
            "locator": "source:content",
            "quote": "正文读取完成后，只能先生成待审记忆候选，不能自动发布长期记忆。",
        }
    ]
    assert candidate["provenance"]["source_content_read_id"] == f"content-read-{source['id']}"
    assert candidate["provenance"]["media_processing_output_id"] is None
    assert [ref["kind"] for ref in candidate["provenance"]["input_refs"]] == [
        "source",
        "source_content_read",
    ]
    assert candidate["review"]["requires_user_confirmation"] is True
    assert candidate["review"]["auto_promote_allowed"] is False
    assert read_record is not None
    assert read_record["memory_publication"] == "candidate_created"
    assert read_record["memory_candidate_id"] == result.candidate_id
    assert updated_source is not None
    assert updated_source["metadata"]["content_read"]["memory_publication"] == "candidate_created"
    assert any(event["type"] == "memory_candidate_created" for event in events)
    assert candidate_item["item_type"] == "memory_candidate"
    assert f"crp://default/source-content-reads/content-read-{source['id']}.json" in candidate_item["trace_refs"]
    assert "memory_publication" in candidate_item["blocked_operations"]


def test_effect_v2_candidate_has_operation_scoped_identity_and_recovery_markers(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="Effect-bound candidate",
            content="This proposal belongs to one immutable Effect execution.",
        )
    )
    ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    execution_ref = f"facts:effect/eff2_{'a' * 64}"
    use_case = CreateMemoryCandidateFromSourceOutput(object_store)

    bound = use_case.execute_from_content_read(
        source_id=str(source["id"]),
        project_id="project-alpha",
        execution_ref=execution_ref,
    )
    replay = use_case.execute_from_content_read(
        source_id=str(source["id"]),
        project_id="project-alpha",
        execution_ref=execution_ref,
    )

    assert replay.candidate_id == bound.candidate_id
    assert len(object_store.list("memory_candidates")) == 1
    candidate = object_store.read("memory_candidates", bound.candidate_id)
    assert candidate is not None
    assert validate_contract_instance(
        "memory_candidate.schema.json",
        _schema("memory_candidate.schema.json"),
        candidate,
    ) == []
    read_id = f"content-read-{source['id']}"
    read_record = object_store.read("source_content_reads", read_id)
    current_source = object_store.read("sources", str(source["id"]))
    event = object_store.read(
        "activity_events",
        f"event-memory-candidate-created-{source['id']}-{read_id}",
    )
    assert read_record is not None
    assert read_record["memory_candidate_execution_ref"] == execution_ref
    assert current_source is not None
    assert current_source["metadata"]["content_read"]["memory_candidate_execution_ref"] == execution_ref
    assert event is not None
    assert event["details"]["execution_ref"] == execution_ref

    with pytest.raises(SourceOutputMemoryCandidateError, match="effect-v2"):
        use_case.execute_from_content_read(
            source_id=str(source["id"]),
            project_id="project-alpha",
            execution_ref="crp://default/not-an-effect",
        )
    with pytest.raises(SourceOutputMemoryCandidateError, match="effect-v2"):
        use_case.execute_from_content_read(
            source_id=str(source["id"]),
            project_id="project-alpha",
            execution_ref="facts:effect/eff2_not-a-real-operation-id",
        )
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms").exists()
    assert not (tmp_path / "library").exists()


def test_source_output_memory_candidate_endpoint_serves_content_read_post(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="Endpoint candidate source",
            content="资料库详情手动生成候选，仍然不能自动发布长期记忆。",
        )
    )
    ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    use_case = CreateMemoryCandidateFromSourceOutput(object_store)
    endpoint = ServeSourceOutputMemoryCandidateEndpoint()

    response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/sources/{source['id']}/memory-candidate",
        body={
            "evidence_kind": "source_content_read",
            "project_id": "project-alpha",
            "target_layer": "atom",
            "candidate_type": "other",
        },
        create_from_content_read=use_case.execute_from_content_read,
        create_from_media_output=use_case.execute_from_media_output,
    )
    candidate = ObjectStoreMemoryCandidateRepository(object_store).get(str(response.body["candidate_id"]))

    assert response.status_code == 200
    assert response.body["status"] == "candidate_created"
    assert response.body["candidate_status"] == "pending_review"
    assert response.body["memory_publication_state"] == "candidate_created_not_published"
    assert candidate is not None
    assert candidate["status"] == "pending_review"
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()


def test_source_output_memory_candidate_endpoint_rejects_invalid_requests(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    use_case = CreateMemoryCandidateFromSourceOutput(object_store)
    endpoint = ServeSourceOutputMemoryCandidateEndpoint()

    wrong_method = endpoint.execute(
        method="GET",
        path="/api/rebuild/sources/source-001/memory-candidate",
        body={},
        create_from_content_read=use_case.execute_from_content_read,
        create_from_media_output=use_case.execute_from_media_output,
    )
    missing_media_output_id = endpoint.execute(
        method="POST",
        path="/api/rebuild/sources/source-001/memory-candidate",
        body={"evidence_kind": "media_processing_output"},
        create_from_content_read=use_case.execute_from_content_read,
        create_from_media_output=use_case.execute_from_media_output,
    )
    wrong_path = endpoint.execute(
        method="POST",
        path="/api/rebuild/sources/source-001/candidate",
        body={},
        create_from_content_read=use_case.execute_from_content_read,
        create_from_media_output=use_case.execute_from_media_output,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "POST"
    assert missing_media_output_id.status_code == 400
    assert missing_media_output_id.body["reason"] == "evidence_id is required for media processing output"
    assert wrong_path.status_code == 404


def test_media_processing_output_creates_reviewable_memory_candidate_and_can_be_promoted_to_draft(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    queued = CreateMediaProcessingQueueJob(
        object_store,
        enabled_capabilities=("ocr",),
    ).execute(source_id=str(source["id"]))
    RunImageOcrAdapterForMediaJob(object_store).execute(
        job_id=queued.job_id,
        adapter=StubOcrAdapter(),
    )
    output_id = f"media-output-ocr-{source['id']}"
    use_case = CreateMemoryCandidateFromSourceOutput(object_store)

    result = use_case.execute_from_media_output(
        output_id=output_id,
        project_id="project-alpha",
        proposed_content="白板确认：Memory 候选必须先待审。",
        created_at="2026-07-01T18:45:00+08:00",
    )
    candidate = ObjectStoreMemoryCandidateRepository(object_store).get(result.candidate_id)
    output = object_store.read("media_processing_outputs", output_id)
    updated_job = object_store.read("media_processing_jobs", queued.job_id)

    assert candidate is not None
    assert validate_contract_instance(
        "memory_candidate.schema.json",
        _schema("memory_candidate.schema.json"),
        candidate,
    ) == []
    assert candidate["proposed_content"] == "白板确认：Memory 候选必须先待审。"
    assert candidate["source_refs"][0]["locator"] == "media:ocr_text"
    assert candidate["provenance"]["media_processing_output_id"] == output_id
    assert candidate["provenance"]["media_processing_job_id"] == queued.job_id
    assert [ref["kind"] for ref in candidate["provenance"]["input_refs"]] == [
        "source",
        "media_processing_job",
        "media_processing_output",
    ]
    assert output is not None
    assert output["memory_publication"] == "candidate_created"
    assert output["memory_candidate_id"] == result.candidate_id
    assert updated_job is not None
    assert updated_job["memory_publication"] == "candidate_created"
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()

    operation = review_candidate_to_staging(
        object_store,
        tmp_path,
        result.candidate_id,
        review_reason="用户确认该 OCR 输出可以作为草稿 Atom。",
        reviewed_at="2026-07-01T18:46:00+08:00",
    )
    staged = saga_records(tmp_path).read("staging_atoms", operation.evidence.draft_id)

    assert operation.state == "finalized"
    assert staged is not None
    assert staged.payload["project_id"] == "project-alpha"
    assert staged.payload["source_id"] == source["id"]
    assert staged.payload["content"] == "白板确认：Memory 候选必须先待审。"
    assert staged.payload["source_refs"] == candidate["source_refs"]
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()


def test_long_media_output_identity_keeps_candidate_activity_event_addressable(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("ocr",)).execute(source_id=str(source["id"]))
    output_id = f"media-output-local-summary-ad_filter_v1-extended-media-evidence-{source['id']}"
    object_store.write("media_processing_outputs", output_id, {
        "id": output_id, "job_id": queued.job_id, "source_id": source["id"],
        "status": "completed", "output_kind": "summary", "preview": "真实媒体摘要",
        "ref": f"crp://default/media-processing-outputs/{output_id}.json",
    }, expected_revision=None)
    use_case = CreateMemoryCandidateFromSourceOutput(object_store)
    result = use_case.execute_from_media_output(output_id=output_id, project_id="project-alpha")
    replay = use_case.execute_from_media_output(output_id=output_id, project_id="project-alpha")
    event_id = f"event-memory-candidate-created-{output_id}"
    assert len(f"event-memory-candidate-created-{source['id']}-{output_id}") > 128
    assert replay.candidate_id == result.candidate_id
    assert object_store.read("activity_events", event_id)["details"]["candidate_id"] == result.candidate_id


def test_source_output_atom_publication_is_recalled_only_from_reviewed_project(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(kind="text", title="Scoped fact", content="Project Alpha 的受控事实。")
    )
    ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    candidate = CreateMemoryCandidateFromSourceOutput(object_store).execute_from_content_read(
        source_id=str(source["id"]), project_id="project-alpha",
    )
    operation = review_candidate_to_staging(
        object_store, tmp_path, candidate.candidate_id,
        review_reason="用户确认该 Source 事实属于 Alpha 项目。",
        reviewed_at="2026-09-06T14:40:00+00:00",
    )
    published = publish_staging_user_confirmed(
        tmp_path, layer="atom", staged_id=operation.evidence.draft_id,
        published_at="2026-09-06T14:41:00+00:00",
    )
    records = saga_records(tmp_path)
    with records.begin() as transaction:
        transaction.put("memory_atoms", "atom-missing-id", {
            "project_id": "project-alpha", "trust_status": "user_confirmed",
        }, expected_revision=0)
        transaction.put("memory_atoms", "atom-project-beta", {
            "id": "atom-project-beta", "project_id": "project-beta",
            "trust_status": "user_confirmed",
        }, expected_revision=0)
        transaction.commit()
    reader = SQLiteMemoryReader(records)

    assert published.layer == "atom"
    assert published.object_id == operation.evidence.draft_id
    assert [item["id"] for item in reader.list_by_project("project-alpha")] == [
        operation.evidence.draft_id
    ]
    assert [item["id"] for item in reader.list_by_project("project-beta")] == [
        "atom-project-beta"
    ]


def test_source_output_memory_candidate_rejects_incomplete_evidence(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(kind="text", title="Unread", content="未读取正文不能生成候选。")
    )
    use_case = CreateMemoryCandidateFromSourceOutput(object_store)

    with pytest.raises(SourceOutputMemoryCandidateError, match="record not found"):
        use_case.execute_from_content_read(
            source_id=str(source["id"]),
            project_id="project-alpha",
        )

    object_store.write(
        "source_content_reads",
        f"content-read-{source['id']}",
        {
            "schema_version": "1.0.0",
            "id": f"content-read-{source['id']}",
            "source_id": source["id"],
            "status": "failed",
            "preview": "读取失败。",
        },
        expected_revision=None,
    )
    with pytest.raises(SourceOutputMemoryCandidateError, match="must be completed"):
        use_case.execute_from_content_read(
            source_id=str(source["id"]),
            project_id="project-alpha",
        )


def test_source_output_memory_candidate_composition_uses_temp_storage_only(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="Composed source output",
            content="组合入口也只能生成待审记忆候选。",
        )
    )
    ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    use_case = build_source_output_memory_candidate_handoff(ROOT, runtime_root=tmp_path)

    result = use_case.execute_from_content_read(
        source_id=str(source["id"]),
        project_id="project-alpha",
        created_at="2026-07-01T18:50:00+08:00",
    )
    candidate = ObjectStoreMemoryCandidateRepository(object_store).get(result.candidate_id)

    assert candidate is not None
    assert candidate["status"] == "pending_review"
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_candidates").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / "library").exists()
