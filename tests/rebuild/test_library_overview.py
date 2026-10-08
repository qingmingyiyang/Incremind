from __future__ import annotations

from pathlib import Path

import pytest

from core.composition import ObjectStoreLibraryOverviewReader, build_library_overview
from core.document_engine import ObjectStoreDocumentRepository
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.job_runner import ObjectStoreJobRepository
from core.memory_core import ObjectStoreMemoryCandidateRepository, ObjectStoreMemoryStore
from core.product_core import (
    CreateDocumentDraftFromWorkbenchSelection,
    CreateMemoryCandidateFromWorkbenchDocument,
    GetLibraryOverview,
    LibraryOverviewError,
    ServeLibraryOverviewEndpoint,
    WorkbenchDocumentDraftSelection,
    serialize_library_overview,
)
from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[2]


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _selection() -> WorkbenchDocumentDraftSelection:
    return WorkbenchDocumentDraftSelection(
        selection_id="library-overview-selection-001",
        source_id="source-text-001",
        source_title="Library overview source",
        source_uri="crp://default/sources/source-text-001",
        capture_job_id="job-library-overview-001",
        media_type="text/plain",
        selected_evidence_refs=(
            "crp://default/sources/source-text-001",
            "crp://default/logs/jobs/job-library-overview-001/persist_source.jsonl",
        ),
        project_id="project-alpha",
    )


def _populate_library(object_store: JsonObjectStore) -> None:
    ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="Library overview source",
            content="Source metadata is visible without reading private library content.",
        )
    )
    documents = ObjectStoreDocumentRepository(object_store)
    draft = CreateDocumentDraftFromWorkbenchSelection(documents=documents).execute(
        _selection(),
        summary="Library overview document keeps source refs.",
    )
    candidate = CreateMemoryCandidateFromWorkbenchDocument(
        documents=documents,
        candidates=ObjectStoreMemoryCandidateRepository(object_store),
    ).execute(
        draft.document_id,
        expected_revision=draft.document_revision,
        proposed_content="Library overview candidate remains pending review.",
        created_at="2026-07-01T12:00:00+08:00",
    )
    candidate_record = object_store.read("memory_candidates", candidate.candidate_id)
    assert candidate_record is not None
    object_store.write(
        "memory_candidates",
        candidate.candidate_id,
        {**candidate_record, "import_batch_id": "roundtrip-library-001"},
        expected_revision=object_store.revision("memory_candidates", candidate.candidate_id),
    )
    ObjectStoreMemoryStore(object_store).publish(
        "atom",
        {
            "id": "atom-library-overview-001",
            "project_id": "project-alpha",
            "layer": "atom",
            "content": "Published Atom remains traceable to the selected Source.",
            "trust_status": "user_confirmed",
            "source_refs": [
                {
                    "source_id": "source-text-001",
                    "locator": "source:metadata",
                    "quote": "Library overview source",
                }
            ],
            "updated_at": "2026-07-01T12:05:00+08:00",
        },
    )


def test_overview_exposes_video_link_semantics_without_changing_mime(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("sources", "source-video-link", {
        "id": "source-video-link", "title": "Video link", "type": "link",
        "project_id": "project-alpha", "media_type": "text/uri-list",
        "workspace_item_id": "workspace-video-link",
        "original_url": "https://www.bilibili.com/video/BV1test",
        "metadata": {"content_kind": "video", "platform": "bilibili"},
    }, expected_revision=0)
    overview = GetLibraryOverview(ObjectStoreLibraryOverviewReader(store)).execute(project_id="project-alpha")
    item = serialize_library_overview(overview)["items"][0]
    assert item["source_media_type"] == "text/uri-list"
    assert item["source_content_kind"] == "video"
    assert item["source_platform"] == "bilibili"
    assert item["source_workspace_item_id"] == "workspace-video-link"
    assert item["source_original_url"] == "https://www.bilibili.com/video/BV1test"


def test_library_overview_unifies_source_document_candidate_and_memory(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    _populate_library(object_store)
    overview = GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    payload = serialize_library_overview(overview)

    assert overview.status == "ready"
    assert overview.scope == "all"
    assert overview.project_id is None
    assert overview.counts["total"] == 4
    assert overview.counts["source"] == 1
    assert overview.counts["document"] == 1
    assert overview.counts["memory_candidate"] == 1
    assert overview.counts["atom"] == 1
    assert "source_content_read" in overview.blocked_operations
    assert "legacy_library_write" in overview.blocked_operations
    assert not (tmp_path / "library").exists()
    item_types = {item["item_type"] for item in payload["items"]}
    assert item_types == {"source", "document", "memory_candidate", "atom"}
    document_item = next(item for item in payload["items"] if item["item_type"] == "document")
    assert document_item["project_id"] == "project-alpha"
    assert document_item["source_refs"] == [
        "source-text-001#source:metadata",
        "source-text-001#job:job-library-overview-001",
    ]
    candidate_item = next(item for item in payload["items"] if item["item_type"] == "memory_candidate")
    assert candidate_item["status"] == "pending_review"
    assert candidate_item["candidate_revision"] == 2
    assert candidate_item["import_batch_id"] == "roundtrip-library-001"
    assert "memory_publication" in candidate_item["blocked_operations"]


def test_library_overview_surfaces_expert_proposal_id_for_expert_candidates(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    _populate_library(object_store)
    object_store.write(
        "memory_candidates",
        "memory-candidate-expert-001",
        {
            "id": "memory-candidate-expert-001",
            "project_id": "project-alpha",
            "status": "pending_review",
            "target_layer": "atom",
            "provenance": {"external_agent_proposal_id": "expert-abc123def4567890"},
        },
        expected_revision=None,
    )
    overview = GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    payload = serialize_library_overview(overview)
    candidate_items = [item for item in payload["items"] if item["item_type"] == "memory_candidate"]
    expert_item = next(item for item in candidate_items if item["item_id"] == "memory-candidate-expert-001")
    assert expert_item["expert_proposal_id"] == "expert-abc123def4567890"
    ordinary_items = [item for item in candidate_items if item["item_id"] != "memory-candidate-expert-001"]
    assert ordinary_items, "expected the fixture candidate to remain present"
    assert all(item["expert_proposal_id"] is None for item in ordinary_items)


def test_library_overview_exposes_only_authority_verified_source_jobs(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="Capture Job Source",
            content="Authority-linked capture source.",
        )
    )
    source_id = str(source["id"])
    job_id = f"job-capture-{source_id}"
    jobs = ObjectStoreJobRepository(object_store)
    jobs.save({"id": job_id, "source_id": source_id, "job_type": "capture", "status": "pending"})

    reader = ObjectStoreLibraryOverviewReader(object_store, job_repository=jobs)
    payload = serialize_library_overview(GetLibraryOverview(reader).execute())
    assert payload["items"][0]["capture_job_id"] == job_id
    assert payload["items"][0]["user_job_id"] == job_id

    intake_job_id = f"job-intake-{source_id}"
    jobs.save(
        {
            "id": intake_job_id,
            "source_id": source_id,
            "job_type": "workbench_auto_intake",
            "status": "waiting_user",
        }
    )
    payload = serialize_library_overview(GetLibraryOverview(reader).execute())
    assert payload["items"][0]["capture_job_id"] == job_id
    assert payload["items"][0]["user_job_id"] == intake_job_id

    jobs.save({"id": job_id, "source_id": "source-other", "job_type": "capture", "status": "pending"})
    jobs.save(
        {
            "id": intake_job_id,
            "source_id": "source-other",
            "job_type": "workbench_auto_intake",
            "status": "waiting_user",
        }
    )
    payload = serialize_library_overview(GetLibraryOverview(reader).execute())
    assert payload["items"][0]["capture_job_id"] is None
    assert payload["items"][0]["user_job_id"] is None


def test_library_overview_keeps_job_lookup_optional_for_read_only_readers(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="Read-only Source",
            content="No Job authority is attached.",
        )
    )
    base = ObjectStoreLibraryOverviewReader(object_store)

    class ReadOnlyLibraryReader:
        sources = base.sources
        documents = base.documents
        memory_candidates = base.memory_candidates
        external_agent_review_drafts = base.external_agent_review_drafts
        memory_objects = base.memory_objects

    payload = serialize_library_overview(GetLibraryOverview(ReadOnlyLibraryReader()).execute())

    assert payload["items"][0]["capture_job_id"] is None
    assert payload["items"][0]["user_job_id"] is None


def test_library_overview_surfaces_external_agent_review_drafts(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    object_store.write(
        "external_agent_review_drafts",
        "draft-external-agent-series",
        {
            "schema_version": "1.0.0",
            "id": "draft-external-agent-series",
            "proposal_id": "proposal-series",
            "proposal_type": "series_update_proposal",
            "draft_type": "series_update",
            "status": "pending_review",
            "project_id": "project-alpha",
            "target_id": "inspiration_series",
            "summary": "外部 Agent 建议更新灵感系列。",
            "source_refs": [{"source_id": "source-export", "locator": "whitebox:series"}],
            "evidence_refs": [{"locator": "crp://default/exports/tags.json"}],
            "review": {"state": "pending_review"},
            "application": {
                "state": "not_applied",
                "writes_long_term_memory": False,
                "writes_staging_memory": False,
            },
            "ref": "crp://default/external-agent-review-drafts/draft-external-agent-series.json",
        },
        expected_revision=None,
    )

    payload = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute(project_id="project-alpha")
    )

    item = payload["items"][0]
    assert payload["counts"]["external_agent_review_draft"] == 1
    assert item["item_type"] == "external_agent_review_draft"
    assert item["title"] == "外部 Agent 建议更新灵感系列。"
    assert item["source_refs"] == ["source-export#whitebox:series"]
    assert item["external_agent_proposal_id"] == "proposal-series"
    assert item["external_agent_draft_type"] == "series_update"
    assert item["external_agent_target_id"] == "inspiration_series"
    assert item["external_agent_review_state"] == "pending_review"
    assert item["external_agent_application_state"] == "not_applied"
    assert item["external_agent_writes_long_term_memory"] is False
    assert item["external_agent_writes_staging_memory"] is False
    assert item["external_agent_memory_publication_state"] == "not_published"
    assert item["external_agent_applied_target_kind"] is None
    assert item["external_agent_applied_target_id"] is None
    assert item["external_agent_applied_revision"] is None
    assert item["external_agent_applied_ref"] is None
    assert "direct_long_term_memory_write" in item["blocked_operations"]
    assert "crp://default/exports/tags.json" in item["trace_refs"]


def test_library_overview_surfaces_applied_external_agent_review_draft_result(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    object_store.write(
        "external_agent_review_drafts",
        "draft-external-agent-skill",
        {
            "schema_version": "1.0.0",
            "id": "draft-external-agent-skill",
            "proposal_id": "proposal-skill",
            "proposal_type": "project_skill_update_proposal",
            "draft_type": "project_skill_update",
            "status": "applied",
            "project_id": "project-alpha",
            "target_id": "skill-project-alpha",
            "summary": "外部 Agent 建议更新项目 Skill。",
            "source_refs": [{"locator": "project_skills.json#project-alpha"}],
            "evidence_refs": [{"locator": "crp://default/exports/project_skills.json"}],
            "review": {"state": "reviewed", "reviewed_by": "user"},
            "application": {
                "state": "applied",
                "writes_long_term_memory": False,
                "writes_staging_memory": False,
                "applied_project_id": "project-alpha",
                "applied_project_skill_id": "skill-project-alpha",
                "applied_project_skill_revision": 2,
            },
        },
        expected_revision=None,
    )

    payload = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute(project_id="project-alpha")
    )

    item = payload["items"][0]
    assert item["item_type"] == "external_agent_review_draft"
    assert item["status"] == "applied"
    assert item["external_agent_review_state"] == "reviewed"
    assert item["external_agent_application_state"] == "applied"
    assert item["external_agent_applied_target_kind"] == "project_skill"
    assert item["external_agent_applied_target_id"] == "skill-project-alpha"
    assert item["external_agent_applied_revision"] == 2
    assert item["external_agent_applied_ref"] == "crp://default/project-skills/skill-project-alpha.json"
    assert item["external_agent_memory_publication_state"] == "not_published"


def test_library_overview_project_filter_keeps_project_items_and_global_sources(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    _populate_library(object_store)
    object_store.write(
        "documents",
        "document-project-beta-001",
        {
            "id": "document-project-beta-001",
            "title": "Beta document",
            "project_id": "project-beta",
            "status": "draft",
            "source_refs": [{"source_id": "source-beta", "locator": "source:metadata"}],
        },
        expected_revision=0,
    )

    overview = GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute(
        project_id="project-alpha"
    )

    assert overview.scope == "project"
    assert overview.project_id == "project-alpha"
    assert all(item.project_id in {None, "project-alpha"} for item in overview.items)
    assert not any(item.item_id == "document-project-beta-001" for item in overview.items)
    assert overview.counts["document"] == 1


def test_library_overview_exposes_source_inspiration_metadata(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = dict(
        ObjectStoreSourceRegistrar(object_store).register(
            SourceSubmission(
                kind="text",
                title="灵感资料",
                content="灵感：资料库可以把点子作为默认系列继续参与项目构思。",
            )
        )
    )
    updated = dict(source)
    metadata = dict(source["metadata"])
    metadata["inspiration"] = {
        "status": "recorded",
        "inspiration_id": "inspiration-source-idea",
        "inspiration_ref": "crp://default/inspirations/inspiration-source-idea.json",
        "series_id": "inspiration-series-default",
        "series_name": "灵感系列",
        "themes": ["产品", "记忆"],
        "summary": "资料库可以把点子作为默认系列继续参与项目构思。",
        "activity_refs": ["crp://default/events/event-inspiration-recorded-source-idea.json"],
        "memory_publication": "not_started",
    }
    updated["metadata"] = metadata
    object_store.write("sources", str(source["id"]), updated, expected_revision=None)

    payload = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    item = payload["items"][0]

    assert item["inspiration_status"] == "recorded"
    assert item["inspiration_id"] == "inspiration-source-idea"
    assert item["inspiration_ref"] == "crp://default/inspirations/inspiration-source-idea.json"
    assert item["inspiration_series_id"] == "inspiration-series-default"
    assert item["inspiration_series_name"] == "灵感系列"
    assert item["inspiration_themes"] == ["产品", "记忆"]
    assert item["inspiration_summary"] == "资料库可以把点子作为默认系列继续参与项目构思。"
    assert item["inspiration_memory_publication"] == "not_started"
    assert "crp://default/inspirations/inspiration-source-idea.json" in item["trace_refs"]
    assert "crp://default/events/event-inspiration-recorded-source-idea.json" in item["trace_refs"]


def test_library_overview_empty_store_is_ready_without_fake_items(tmp_path: Path) -> None:
    object_store = _store(tmp_path)

    overview = GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    payload = serialize_library_overview(overview)

    assert overview.status == "ready"
    assert overview.items == ()
    assert overview.counts == {"total": 0}
    assert payload["items"] == []
    assert payload["next_step_boundary"] == "library_all_items_ready_without_content_read"


def test_library_overview_exposes_video_workflow_ids_without_local_paths(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = dict(
        ObjectStoreSourceRegistrar(object_store).register(
            SourceSubmission(
                kind="video",
                title="Video workflow source",
                display_name="video.mp4",
                media_type="video/mp4",
                size_bytes=1024,
                video_reference="bilibili/BV1abcDEF234/p1",
            )
        )
    )
    updated = dict(source)
    metadata = dict(source["metadata"])
    metadata["audio_track_extraction"] = {
        "status": "completed",
        "output_id": "media-output-audio-track-source-video-001",
        "output_ref": "crp://default/media-processing-outputs/media-output-audio-track-source-video-001.json",
        "output_preview": "已抽取音频。",
        "audio_asset_id": "audio-track-source-video-001",
        "audio_asset_ref": "crp-ref://default/assets/audio-track-source-video-001",
        "transcript_output_id": "media-output-transcript-source-video-001",
        "transcript_output_ref": "crp://default/media-processing-outputs/media-output-transcript-source-video-001.json",
        "summary_output_id": "media-output-summary-source-video-001",
        "summary_output_ref": "crp://default/media-processing-outputs/media-output-summary-source-video-001.json",
        "summary_preview": "总结输出。",
        "memory_publication": "not_started",
        "path_stored_in_source": False,
    }
    metadata["video_auto_workflow"] = {
        "status": "completed",
        "workflow_id": "video-auto-workflow-source-video-001",
        "workflow_ref": "crp://default/video-auto-workflows/video-auto-workflow-source-video-001.json",
        "steps": [
            {"name": "create_memory_candidate", "status": "candidate_created", "candidate_id": "memory-candidate-video-001"},
            {"name": "publish_memory", "status": "published", "candidate_id": "memory-candidate-video-001"},
        ],
        "publication_id": "memory-publication-atom-video-001",
        "published_ref": "crp://default/memory/atom/atom-video-001.json",
        "rollback_ref": "crp://default/memory-publications/memory-publication-atom-video-001/rollback",
        "memory_publication": "published_with_rollback_ref",
    }
    updated["metadata"] = metadata
    object_store.write("sources", str(source["id"]), updated, expected_revision=None)

    payload = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    item = payload["items"][0]

    assert item["video_audio_asset_id"] == "audio-track-source-video-001"
    assert item["video_audio_asset_ref"] == "crp-ref://default/assets/audio-track-source-video-001"
    assert item["video_transcript_output_id"] == "media-output-transcript-source-video-001"
    assert item["video_summary_output_id"] == "media-output-summary-source-video-001"
    assert item["media_output_ids"] == ["media-output-summary-source-video-001"]
    assert item["media_output_preview"] == "总结输出。"
    assert item["video_auto_workflow_status"] == "completed"
    assert item["video_auto_workflow_id"] == "video-auto-workflow-source-video-001"
    assert item["memory_publication_id"] == "memory-publication-atom-video-001"
    assert item["memory_published_ref"] == "crp://default/memory/atom/atom-video-001.json"
    assert item["memory_rollback_ref"] == "crp://default/memory-publications/memory-publication-atom-video-001/rollback"
    assert "crp://default/video-auto-workflows/video-auto-workflow-source-video-001.json" in item["trace_refs"]
    assert "path" not in item


def test_library_overview_exposes_audio_auto_workflow_ids_without_local_paths(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = dict(
        ObjectStoreSourceRegistrar(object_store).register(
            SourceSubmission(
                kind="audio",
                title="Audio workflow source",
                display_name="meeting.mp3",
                media_type="audio/mpeg",
                size_bytes=4096,
                audio_reference="platform-audio-ref",
                duration_ms=60000,
            )
        )
    )
    updated = dict(source)
    metadata = dict(source["metadata"])
    metadata["audio_transcription"] = {
        "asr_state": "completed",
        "transcript_output_id": "media-output-transcript-source-audio-001",
        "transcript_output_ref": "crp://default/media-processing-outputs/media-output-transcript-source-audio-001.json",
        "transcript_preview": "会议第一段转写。",
        "summary_state": "not_started",
        "memory_publication": "not_started",
        "path_stored_in_source": False,
    }
    metadata["audio_auto_workflow"] = {
        "status": "completed",
        "workflow_id": "audio-auto-workflow-source-audio-001",
        "workflow_ref": "crp://default/audio-auto-workflows/audio-auto-workflow-source-audio-001.json",
        "steps": [
            {
                "name": "transcribe_audio",
                "status": "completed",
                "job_id": "media-job-transcript-audio-asset-001",
                "output_id": "media-output-transcript-source-audio-001",
                "audio_asset_id": "audio-asset-001",
            }
        ],
        "transcript_output_id": "media-output-transcript-source-audio-001",
        "memory_publication": "not_started",
    }
    updated["metadata"] = metadata
    object_store.write("sources", str(source["id"]), updated, expected_revision=None)

    payload = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    item = payload["items"][0]

    assert item["audio_auto_workflow_status"] == "completed"
    assert item["audio_auto_workflow_id"] == "audio-auto-workflow-source-audio-001"
    assert item["audio_auto_workflow_steps"][0]["name"] == "transcribe_audio"
    assert item["audio_auto_workflow_steps"][0]["status"] == "completed"
    assert item["audio_transcript_output_id"] == "media-output-transcript-source-audio-001"
    assert item["media_required_capability"] == "audio_transcription"
    assert item["media_processing_status"] == "completed"
    assert item["media_output_ids"] == ["media-output-transcript-source-audio-001"]
    assert item["media_output_preview"] == "会议第一段转写。"
    assert "crp://default/audio-auto-workflows/audio-auto-workflow-source-audio-001.json" in item["trace_refs"]
    assert "path" not in item


def test_library_overview_rejects_empty_project_filter(tmp_path: Path) -> None:
    object_store = _store(tmp_path)

    with pytest.raises(LibraryOverviewError, match="project_id"):
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute(project_id=" ")


def test_library_overview_composition_uses_temp_storage(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    _populate_library(object_store)
    use_case = build_library_overview(ROOT, runtime_root=tmp_path)

    overview = use_case.execute(project_id="project-alpha")

    assert overview.status == "ready"
    assert overview.counts["total"] >= 3
    assert not (tmp_path / "library").exists()


def test_library_overview_endpoint_returns_json_ready_overview(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    _populate_library(object_store)
    use_case = GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store))

    response = ServeLibraryOverviewEndpoint().execute(
        method="GET",
        path="/api/rebuild/library/overview",
        get_library_overview=use_case.execute,
    )

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.body["status"] == "ready"
    assert response.body["counts"]["total"] == 4
    assert {item["item_type"] for item in response.body["items"]} == {
        "source",
        "document",
        "memory_candidate",
        "atom",
    }
    assert "source_content_read" in response.body["blocked_operations"]
    assert "legacy_library_write" in response.body["blocked_operations"]


def test_library_overview_endpoint_applies_project_filter(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    _populate_library(object_store)
    use_case = GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store))

    response = ServeLibraryOverviewEndpoint().execute(
        method="GET",
        path="/api/rebuild/library/overview?project_id=project-alpha",
        get_library_overview=use_case.execute,
    )

    assert response.status_code == 200
    assert response.body["scope"] == "project"
    assert response.body["project_id"] == "project-alpha"
    assert response.body["counts"]["document"] == 1


def test_library_overview_endpoint_rejects_wrong_method_path_and_query(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    use_case = GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store))
    endpoint = ServeLibraryOverviewEndpoint()

    wrong_method = endpoint.execute(
        method="POST",
        path="/api/rebuild/library/overview",
        get_library_overview=use_case.execute,
    )
    wrong_path = endpoint.execute(
        method="GET",
        path="/api/rebuild/library/items",
        get_library_overview=use_case.execute,
    )
    bad_query = endpoint.execute(
        method="GET",
        path="/api/rebuild/library/overview?project_id=",
        get_library_overview=use_case.execute,
    )
    unknown_query = endpoint.execute(
        method="GET",
        path="/api/rebuild/library/overview?read_content=true",
        get_library_overview=use_case.execute,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "GET"
    assert wrong_method.body["detail"] == "library overview endpoint only supports GET"
    assert wrong_path.status_code == 404
    assert bad_query.status_code == 400
    assert bad_query.body["detail"] == "project_id cannot be empty"
    assert unknown_query.status_code == 400
    assert unknown_query.body["detail"] == "unsupported query parameter: read_content"
