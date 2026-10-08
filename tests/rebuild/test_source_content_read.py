from __future__ import annotations

import locale
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from core.composition import (
    ObjectStoreLibraryOverviewReader,
    build_local_command_document_text_extractor,
    build_source_document_authorization,
)
from core.document_engine import ObjectStoreDocumentRepository
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.product_core import (
    AuthorizeLocalDocumentFileForSource,
    AuthorizeLocalTextFileForSource,
    GetLibraryOverview,
    LocalCommandDocumentTextExtractor,
    ReadLinkWebContent,
    ReadSourceTextContent,
    ServeSourceContentReadEndpoint,
    ServeSourceFileAuthorizationEndpoint,
    SourceFileAuthorizationError,
    SourceContentReadError,
    ConfirmSourceSeriesAssignment,
    CreateSeriesMemorySkillDraftsFromSourceStructure,
    CreateMemoryCandidateFromSourceTemplateDocument,
    CreateMediaOutputTemplateDocument,
    CreateSourceTemplateDocument,
    StructureSourceContent,
    serialize_library_overview,
    serialize_source_series_assignment_result,
    serialize_series_memory_skill_draft_result,
    serialize_source_content_read_result,
    serialize_source_template_document_result,
    serialize_source_template_memory_candidate_result,
    serialize_source_structuring_result,
    serialize_source_file_authorization_result,
    document_text_extractors_for_allowed_documents,
)
from core.storage_provider import JsonObjectStore




def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def test_source_text_content_read_persists_result_events_and_overview(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="会议记录",
            content="第一行：确认产品入口。\n第二行：资料库需要显示正文读取摘要。",
        )
    )

    result = ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    payload = serialize_source_content_read_result(result)
    updated_source = object_store.read("sources", str(source["id"]))
    read_record = object_store.read("source_content_reads", f"content-read-{source['id']}")
    activity_events = object_store.list("activity_events")
    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    source_item = next(item for item in overview["items"] if item["item_type"] == "source")

    assert payload["status"] == "completed"
    assert payload["content_read"] is True
    assert payload["char_count"] == len("第一行：确认产品入口。\n第二行：资料库需要显示正文读取摘要。")
    assert payload["read_ref"] == f"crp://default/source-content-reads/content-read-{source['id']}.json"
    assert updated_source is not None
    assert updated_source["processing_state"] == "ready"
    assert updated_source["metadata"]["content_read"]["status"] == "completed"
    assert updated_source["metadata"]["content_read"]["content_read"] is True
    assert read_record is not None
    assert read_record["text"] == "第一行：确认产品入口。\n第二行：资料库需要显示正文读取摘要。"
    assert {event["type"] for event in activity_events} == {
        "content_read_started",
        "content_read_completed",
    }
    assert source_item["content_read_status"] == "completed"
    assert source_item["content_read"] is True
    assert source_item["content_char_count"] == payload["char_count"]
    assert "资料库需要显示正文读取摘要" in source_item["content_preview"]
    assert f"{source['id']}#source:content" in source_item["source_refs"]
    assert "source_content_read" not in source_item["blocked_operations"]
    assert payload["read_ref"] in source_item["trace_refs"]
    assert any("/activity/event-content-read-completed-" in ref for ref in source_item["trace_refs"])


def test_source_content_structure_creates_paragraph_tags_and_series_candidate(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="产品记忆工作台讨论",
            content=(
                "产品设计需要让用户把资料加入个人 AI 记忆工作台。\n"
                "视频和录音资料需要完成音频转写、摘要和证据追溯。\n"
                "项目推进内容要形成验收任务和后续计划。"
            ),
        )
    )
    ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))

    result = StructureSourceContent(object_store).execute(source_id=str(source["id"]))
    payload = serialize_source_structuring_result(result)
    updated_source = object_store.read("sources", str(source["id"]))
    read_record = object_store.read("source_content_reads", f"content-read-{source['id']}")
    structure_record = object_store.read("source_structures", f"structure-{source['id']}")
    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    source_item = next(item for item in overview["items"] if item["item_id"] == source["id"])

    assert payload["status"] == "completed"
    assert payload["source_id"] == source["id"]
    assert payload["content_read_id"] == f"content-read-{source['id']}"
    assert {"Product", "Memory"}.issubset(set(payload["tags"]))
    assert payload["series_candidate"] == "个人 AI 记忆工作台"
    assert payload["memory_publication_state"] == "not_published"
    assert "long_term_memory_publication" in payload["blocked_operations"]
    assert "个人 AI 记忆工作台" in payload["summary"]
    assert len(payload["key_points"]) == 3
    assert "p001" in payload["structured_body"]
    assert len(payload["paragraph_tags"]) == 3
    assert payload["paragraph_tags"][0]["paragraph_id"] == "p001"
    assert "Product" in payload["paragraph_tags"][0]["tags"]

    assert structure_record is not None
    assert structure_record["series_candidate"] == "个人 AI 记忆工作台"
    assert structure_record["summary"] == payload["summary"]
    assert structure_record["key_points"] == payload["key_points"]
    assert read_record is not None
    assert read_record["structure_ref"] == payload["structure_ref"]
    assert read_record["structured_body"] == payload["structured_body"]
    assert updated_source is not None
    assert updated_source["metadata"]["content_structure"]["status"] == "completed"
    assert updated_source["metadata"]["content_structure"]["summary"] == payload["summary"]
    assert updated_source["metadata"]["content_structure"]["series_candidate"] == "个人 AI 记忆工作台"

    assert source_item["content_structure_status"] == "completed"
    assert {"Product", "Memory"}.issubset(set(source_item["content_tags"]))
    assert source_item["structured_summary"] == payload["summary"]
    assert source_item["structured_key_points"] == payload["key_points"]
    assert source_item["series_candidate"] == "个人 AI 记忆工作台"
    assert source_item["paragraph_tags"][0]["paragraph_id"] == "p001"
    assert payload["structure_ref"] in source_item["trace_refs"]
    assert any("/activity/event-content-structured-" in ref for ref in source_item["trace_refs"])


def test_source_content_structure_records_developer_prompt_refs_without_prompt_content(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="灵感整理",
            content="这是一个关于灵感系统的想法，需要自动摘要、打标签并进入长期记忆。",
        )
    )
    ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))

    result = StructureSourceContent(object_store).execute(
        source_id=str(source["id"]),
        prompt_context=(
            {
                "id": "pt-summary",
                "revision": 3,
                "source": "developer_studio",
                "stage_id": "summary",
                "model_profile_id": "mp-memory",
                "content": "private prompt body must not be copied",
            },
            {
                "id": "pt-tags",
                "revision": 4,
                "source": "developer_studio",
                "stage_id": "tags",
                "model_profile_id": "mp-memory",
                "content": "private prompt body must not be copied either",
            },
        ),
    )
    payload = serialize_source_structuring_result(result)
    structure_record = object_store.read("source_structures", f"structure-{source['id']}")
    updated_source = object_store.read("sources", str(source["id"]))
    activity_event = object_store.read("activity_events", f"event-content-structured-{source['id']}")
    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    source_item = next(item for item in overview["items"] if item["item_id"] == source["id"])

    assert payload["organization_prompt_refs"] == [
        {
            "id": "pt-summary",
            "revision": 3,
            "source": "developer_studio",
            "stage_id": "summary",
            "model_profile_id": "mp-memory",
        },
        {
            "id": "pt-tags",
            "revision": 4,
            "source": "developer_studio",
            "stage_id": "tags",
            "model_profile_id": "mp-memory",
        },
    ]
    assert "content" not in payload["organization_prompt_refs"][0]
    assert structure_record is not None
    assert structure_record["organization_prompt_refs"] == payload["organization_prompt_refs"]
    assert updated_source is not None
    assert (
        updated_source["metadata"]["content_structure"]["organization_prompt_refs"]
        == payload["organization_prompt_refs"]
    )
    assert activity_event is not None
    assert activity_event["details"]["organization_prompt_refs"] == payload["organization_prompt_refs"]
    assert source_item["organization_prompt_refs"] == payload["organization_prompt_refs"]


def test_source_series_assignment_confirms_candidate_into_library_series(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="产品记忆工作台讨论",
            content=(
                "产品设计需要让用户把资料加入个人 AI 记忆工作台。\n"
                "系列归类需要用户确认，结构化整理需要可追溯。"
            ),
        )
    )
    ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    StructureSourceContent(object_store).execute(source_id=str(source["id"]))

    result = ConfirmSourceSeriesAssignment(object_store).execute(
        source_id=str(source["id"]),
        confirm=True,
    )
    payload = serialize_source_series_assignment_result(result)
    updated_source = object_store.read("sources", str(source["id"]))
    assignment = object_store.read("source_series_assignments", f"series-assignment-{source['id']}")
    series = object_store.read("library_series", result.series_id)
    event = object_store.read("activity_events", f"event-series-assigned-{source['id']}")
    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    source_item = next(item for item in overview["items"] if item["item_id"] == source["id"])

    assert payload["status"] == "confirmed"
    assert payload["series_name"] == "个人 AI 记忆工作台"
    assert payload["memory_publication_state"] == "not_published"
    assert "long_term_memory_publication" in payload["blocked_operations"]
    assert assignment is not None
    assert assignment["confirmed_by"] == "user"
    assert assignment["series_id"] == result.series_id
    assert series is not None
    assert series["name"] == "个人 AI 记忆工作台"
    assert series["source_ids"] == [source["id"]]
    assert event is not None
    assert event["type"] == "source_series_assigned"
    assert updated_source is not None
    assert updated_source["metadata"]["series_assignment"]["status"] == "confirmed"
    assert updated_source["metadata"]["series_assignment"]["series_name"] == "个人 AI 记忆工作台"
    assert source_item["series_assignment_status"] == "confirmed"
    assert source_item["series_name"] == "个人 AI 记忆工作台"
    assert payload["assignment_ref"] in source_item["trace_refs"]
    assert payload["series_ref"] in source_item["trace_refs"]


def test_series_memory_skill_drafts_create_reviewable_l3_candidates(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="个人 AI 记忆工作台 MVP",
            content=(
                "个人 AI 记忆工作台需要把资料库、四层记忆、项目 Skill 和统一输出模板连起来。\n"
                "回答手册、复盘和项目总结必须先读取系列总览、结构化摘要和来源引用。\n"
                "长期记忆发布需要 review、staging、publication 和 rollback。"
            ),
        )
    )
    ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    StructureSourceContent(object_store).execute(source_id=str(source["id"]))
    ConfirmSourceSeriesAssignment(object_store).execute(source_id=str(source["id"]), confirm=True)

    result = CreateSeriesMemorySkillDraftsFromSourceStructure(object_store).execute(
        source_id=str(source["id"]),
        project_id="project-alpha",
    )
    payload = serialize_series_memory_skill_draft_result(result)
    updated_source = object_store.read("sources", str(source["id"]))
    event = object_store.read("activity_events", f"event-layered-drafts-created-{source['id']}")
    candidates = ObjectStoreMemoryCandidateRepository(object_store)
    saved_candidates = [candidates.get(item["candidate_id"]) for item in payload["candidates"]]

    assert payload["status"] == "candidates_created"
    assert payload["project_id"] == "project-alpha"
    assert payload["series_name"] == "个人 AI 记忆工作台"
    assert payload["memory_publication_state"] == "candidate_created_not_published"
    assert "automatic_memory_publication" in payload["blocked_operations"]
    assert {item["target_layer"] for item in payload["candidates"]} == {"series_memory", "project_skill"}
    assert "L3 Series Memory" in payload["series_memory_update_plan"]
    assert "项目 Skill 默认阅读要求" in payload["project_skill_update_plan"]
    assert updated_source is not None
    assert updated_source["metadata"]["layered_memory_drafts"]["status"] == "candidate_created"
    assert updated_source["metadata"]["layered_memory_drafts"]["candidate_ids"] == [
        item["candidate_id"] for item in payload["candidates"]
    ]
    assert event is not None
    assert event["type"] == "series_memory_skill_drafts_created"
    assert all(candidate is not None for candidate in saved_candidates)
    assert {candidate["target_layer"] for candidate in saved_candidates if candidate} == {
        "series_memory",
        "project_skill",
    }
    assert all(candidate["status"] == "pending_review" for candidate in saved_candidates if candidate)
    assert all(candidate["review"]["auto_promote_allowed"] is False for candidate in saved_candidates if candidate)
    assert all(
        candidate["series_id"] == payload["series_id"]
        for candidate in saved_candidates
        if candidate
    )
    assert object_store.list("memory_series_memory") == ()
    assert object_store.list("project_skills") == ()


def test_series_memory_skill_drafts_require_confirmed_series(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="未确认系列资料",
            content="资料库已经读取正文并完成结构化，但还没有用户确认系列。",
        )
    )
    ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    StructureSourceContent(object_store).execute(source_id=str(source["id"]))

    with pytest.raises(ValueError, match="confirmed series assignment"):
        CreateSeriesMemorySkillDraftsFromSourceStructure(object_store).execute(source_id=str(source["id"]))

    assert object_store.list("memory_candidates") == ()
    assert object_store.list("memory_series_memory") == ()
    assert object_store.list("project_skills") == ()


def test_source_template_document_creates_fixed_editable_draft_from_structure(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="产品记忆工作台讨论",
            content=(
                "产品设计需要让用户把资料加入个人 AI 记忆工作台。\n"
                "结构化整理需要形成摘要、关键点和可编辑正文。\n"
                "项目总结需要明确下一步和待确认内容。"
            ),
        )
    )
    ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    StructureSourceContent(object_store).execute(source_id=str(source["id"]))
    ConfirmSourceSeriesAssignment(object_store).execute(source_id=str(source["id"]), confirm=True)

    result = CreateSourceTemplateDocument(
        object_store=object_store,
        documents=ObjectStoreDocumentRepository(object_store),
    ).execute(
        source_id=str(source["id"]),
        template_type="project_summary",
        prompt_context=(
            {
                "id": "pt-detail-summary",
                "revision": 5,
                "source": "developer_studio",
                "stage_id": "detail-summary",
                "model_profile_id": "mp-memory",
                "content": "private template prompt body",
            },
        ),
    )
    payload = serialize_source_template_document_result(result)
    documents = ObjectStoreDocumentRepository(object_store)
    document = documents.read(result.document_id)
    markdown = documents.markdown(result.document_id)
    output = object_store.read("source_template_outputs", f"template-output-{source['id']}-project_summary")
    updated_source = object_store.read("sources", str(source["id"]))
    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    source_item = next(item for item in overview["items"] if item["item_id"] == source["id"])

    assert payload["status"] == "document_created"
    assert payload["template_type"] == "project_summary"
    assert payload["template_label"] == "项目总结"
    assert payload["document_type"] == "project_summary"
    assert payload["memory_publication_state"] == "not_published"
    assert "long_term_memory_publication" in payload["blocked_operations"]
    assert "不得编造不存在的事实" in payload["template_prompt"]
    assert payload["template_prompt_refs"] == [
        {
            "id": "pt-detail-summary",
            "revision": 5,
            "source": "developer_studio",
            "stage_id": "detail-summary",
            "model_profile_id": "mp-memory",
        }
    ]
    assert "content" not in payload["template_prompt_refs"][0]
    assert document is not None
    assert document["status"] == "draft"
    assert document["type"] == "project_summary"
    assert markdown is not None
    assert "## 生成提示词" in markdown
    assert "## 当前进展" in markdown
    assert "结构化整理需要形成摘要、关键点和可编辑正文。" in markdown
    assert "个人 AI 记忆工作台" in markdown
    assert output is not None
    assert output["document_id"] == result.document_id
    assert output["template_prompt_refs"] == payload["template_prompt_refs"]
    assert updated_source is not None
    assert updated_source["metadata"]["template_outputs"][0]["document_id"] == result.document_id
    assert updated_source["metadata"]["template_outputs"][0]["template_prompt_refs"] == payload["template_prompt_refs"]
    assert source_item["template_prompt_refs"] == payload["template_prompt_refs"]
    assert payload["output_ref"] in source_item["trace_refs"]
    assert result.document_id in {item["item_id"] for item in overview["items"]}


def test_source_template_document_keeps_project_skill_body_title_when_outline_is_overridden(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="项目级模板材料",
            content="项目 Skill 自定义章节应保留，并承载结构化正文。",
        )
    )
    ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    StructureSourceContent(object_store).execute(source_id=str(source["id"]))
    ConfirmSourceSeriesAssignment(object_store).execute(source_id=str(source["id"]), confirm=True)

    result = CreateSourceTemplateDocument(
        object_store=object_store,
        documents=ObjectStoreDocumentRepository(object_store),
    ).execute(
        source_id=str(source["id"]),
        template_type="project_summary",
        outline_override=(
            {"section_id": "project_body", "title": "项目定制正文", "kind": "body", "required": True},
        ),
    )

    markdown = ObjectStoreDocumentRepository(object_store).markdown(result.document_id)

    assert markdown is not None
    assert "## 项目定制正文" in markdown
    assert "## 当前进展" not in markdown




def test_media_output_template_document_creates_editable_draft_from_summary_output(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="video",
            title="视频记忆工作台说明",
            display_name="memory-workbench.mp4",
            media_type="video/mp4",
            size_bytes=4096,
            video_reference="bilibili/BV1memory/p1",
            duration_ms=8000,
        )
    )
    output_id = f"media-output-summary-{source['id']}"
    job_id = f"media-job-summary-media-output-template-{source['id']}"
    object_store.write(
        "media_processing_jobs",
        job_id,
        {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source["id"],
            "source_type": "video",
            "required_capability": "transcript_summary",
            "status": "completed",
            "disabled_reason": None,
            "input_refs": [],
            "expected_output_refs": [],
            "adapter_contract": {},
            "error": None,
            "activity_refs": [],
            "output_refs": [f"crp://default/media-processing-outputs/{output_id}.json"],
            "created_at": "2026-07-02T19:20:00+08:00",
            "updated_at": "2026-07-02T19:20:00+08:00",
        },
        expected_revision=None,
    )
    object_store.write(
        "media_processing_outputs",
        output_id,
        {
            "schema_version": "1.0.0",
            "id": output_id,
            "job_id": job_id,
            "source_id": source["id"],
            "source_type": "video",
            "output_kind": "summary",
            "status": "completed",
            "provider": "local-command-summary",
            "title": "视频记忆工作台说明",
            "preview": "总结输出：视频讨论了四层记忆、资料库和项目 Skill。",
            "text": "## 摘要\n视频讨论了四层记忆、资料库和项目 Skill。",
            "markdown": "## 摘要\n视频讨论了四层记忆、资料库和项目 Skill。",
            "summary_data": {
                "title": "视频记忆工作台说明",
                "thirty_second_summary": "视频说明资料库视频输出要进入四层记忆前先形成可编辑草稿。",
                "chapters": [
                    {
                        "title": "视频到记忆",
                        "summary": "先生成总结，再形成统一模板和待审候选。",
                    }
                ],
                "key_takeaways": ["模板草稿必须可编辑", "发布长期 Memory 前必须 review"],
            },
            "metadata": {"memory_publication": "not_started"},
            "memory_publication": "not_started",
            "created_at": "2026-07-02T19:20:00+08:00",
            "ref": f"crp://default/media-processing-outputs/{output_id}.json",
        },
        expected_revision=None,
    )

    result = CreateMediaOutputTemplateDocument(
        object_store=object_store,
        documents=ObjectStoreDocumentRepository(object_store),
    ).execute(output_id=output_id, template_type="answer_manual")
    payload = serialize_source_template_document_result(result)
    documents = ObjectStoreDocumentRepository(object_store)
    markdown = documents.markdown(result.document_id)
    output = object_store.read("media_template_outputs", f"media-template-output-{output_id}-answer_manual")
    updated_source = object_store.read("sources", str(source["id"]))

    assert payload["status"] == "document_created"
    assert payload["template_type"] == "answer_manual"
    assert payload["template_label"] == "回答手册"
    assert payload["document_type"] == "answer_manual"
    assert payload["media_output_id"] == output_id
    assert payload["media_output_kind"] == "summary"
    assert "long_term_memory_publication" in payload["blocked_operations"]
    assert markdown is not None
    assert "## 适用边界" in markdown
    assert "视频到记忆：先生成总结" in markdown
    assert "Media output" in markdown
    assert output is not None
    assert output["document_id"] == result.document_id
    assert output["media_output_id"] == output_id
    assert updated_source is not None
    assert updated_source["metadata"]["media_template_outputs"][0]["document_id"] == result.document_id
    assert "sk-" not in str(payload).lower()


def test_source_template_document_creates_reviewable_project_skill_candidate(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="项目 Skill 更新材料",
            content="项目总结需要沉淀默认阅读要求、输出结构和下一步行动。",
        )
    )
    ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    StructureSourceContent(object_store).execute(source_id=str(source["id"]))
    ConfirmSourceSeriesAssignment(object_store).execute(source_id=str(source["id"]), confirm=True)
    document_result = CreateSourceTemplateDocument(
        object_store=object_store,
        documents=ObjectStoreDocumentRepository(object_store),
    ).execute(source_id=str(source["id"]), template_type="project_summary")

    result = CreateMemoryCandidateFromSourceTemplateDocument(
        documents=ObjectStoreDocumentRepository(object_store),
        candidates=ObjectStoreMemoryCandidateRepository(object_store),
    ).execute(document_result.document_id, expected_revision=document_result.document_revision)
    payload = serialize_source_template_memory_candidate_result(result)
    candidate = object_store.read("memory_candidates", result.candidate_id)
    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )

    assert payload["status"] == "candidate_created"
    assert payload["document_type"] == "project_summary"
    assert payload["target_layer"] == "project_skill"
    assert payload["candidate_type"] == "document_takeaway"
    assert payload["memory_publication_state"] == "candidate_created_not_published"
    assert "候选必须等待用户审核" in payload["template_prompt"]
    assert candidate is not None
    assert candidate["status"] == "pending_review"
    assert candidate["review"]["requires_user_confirmation"] is True
    assert candidate["review"]["auto_promote_allowed"] is False
    assert candidate["provenance"]["document_id"] == document_result.document_id
    assert candidate["provenance"]["document_revision"] == document_result.document_revision
    assert "目标记忆层：project_skill" in candidate["proposed_content"]
    assert object_store.read("project_skills", result.candidate_id) is None
    assert result.candidate_id in {item["item_id"] for item in overview["items"]}


def test_link_web_content_read_fetches_html_into_source_content_read(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="link",
            title="产品设计网页",
            original_url="https://example.com/product-design",
        )
    )

    result = ReadLinkWebContent(
        object_store,
        fetch_url=lambda url: (
            "<html><head><style>.x{}</style><script>hidden()</script></head>"
            "<body><h1>产品设计网页正文</h1><p>资料库需要保存网页内容并生成可读写资料。</p></body></html>"
        ),
    ).execute(source_id=str(source["id"]))
    updated_source = object_store.read("sources", str(source["id"]))
    read_record = object_store.read("source_content_reads", f"content-read-{source['id']}")
    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    source_item = next(item for item in overview["items"] if item["item_id"] == source["id"])

    assert result.status == "completed"
    assert result.media_type == "text/html"
    assert result.content_read is True
    assert "产品设计网页正文" in result.preview
    assert read_record is not None
    assert read_record["original_url"] == "https://example.com/product-design"
    assert read_record["media_type"] == "text/html"
    assert "hidden()" not in read_record["text"]
    assert "资料库需要保存网页内容" in read_record["text"]
    assert updated_source is not None
    assert updated_source["metadata"]["content_read"]["reader"] == "link_web_content"
    assert source_item["content_read_status"] == "completed"
    assert source_item["content_read"] is True
    assert "source_content_read" not in source_item["blocked_operations"]
    assert f"{source['id']}#source:content" in source_item["source_refs"]


def test_source_text_content_read_records_failed_attempt_for_unsupported_media(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="file",
            title="PDF",
            display_name="report.pdf",
            media_type="application/pdf",
            size_bytes=4096,
            file_reference="platform-ref-report",
        )
    )

    result = ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    updated_source = object_store.read("sources", str(source["id"]))
    activity_events = object_store.list("activity_events")

    assert result.status == "failed"
    assert result.content_read is False
    assert result.error == "source has no document authorization"
    assert updated_source is not None
    assert updated_source["metadata"]["content_read"]["status"] == "failed"
    assert updated_source["metadata"]["content_read"]["error"] == result.error
    assert {event["type"] for event in activity_events} == {
        "content_read_started",
        "content_read_failed",
    }


def test_authorized_pdf_read_requires_configured_document_extractor(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.7 fake local document")
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="file",
            title="PDF report",
            display_name="report.pdf",
            media_type="application/pdf",
            size_bytes=pdf_path.stat().st_size,
            file_reference="platform-ref-pdf",
        )
    )
    authorization = AuthorizeLocalDocumentFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(pdf_path),
    )

    result = ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    updated_source = object_store.read("sources", str(source["id"]))
    auth_record = object_store.read("authorized_file_refs", authorization.authorization_id)
    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    source_item = next(item for item in overview["items"] if item["item_type"] == "source")

    assert result.status == "failed"
    assert result.content_read is False
    assert result.error == "document text extractor is not configured for media_type: application/pdf"
    assert auth_record is not None
    assert auth_record["path"] == str(pdf_path.resolve(strict=False))
    assert updated_source is not None
    assert updated_source["metadata"]["document_authorization"]["path_stored_in_source"] is False
    assert "path" not in updated_source["metadata"]["document_authorization"]
    assert authorization.authorization_ref in source_item["trace_refs"]


def test_authorized_docx_read_uses_injected_extractor_and_overview(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    docx_path = tmp_path / "brief.docx"
    docx_path.write_bytes(b"fake docx bytes")
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="file",
            title="Word brief",
            display_name="brief.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=docx_path.stat().st_size,
            file_reference="platform-ref-docx",
        )
    )
    authorization = AuthorizeLocalDocumentFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(docx_path),
    )

    def extract_document_text(path: Path, media_type: str) -> str:
        assert path == docx_path.resolve(strict=False)
        assert media_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return "Word 文档正文：这里是经过安全提取器返回的内容。"

    result = ReadSourceTextContent(
        object_store,
        document_extractors={
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_document_text,
        },
    ).execute(source_id=str(source["id"]))
    updated_source = object_store.read("sources", str(source["id"]))
    read_record = object_store.read("source_content_reads", f"content-read-{source['id']}")
    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    source_item = next(item for item in overview["items"] if item["item_type"] == "source")

    assert result.status == "completed"
    assert result.content_read is True
    assert result.read_ref == f"crp://default/source-content-reads/content-read-{source['id']}.json"
    assert result.preview == "Word 文档正文：这里是经过安全提取器返回的内容。"
    assert updated_source is not None
    assert updated_source["metadata"]["document_authorization"]["authorization_id"] == (
        authorization.authorization_id
    )
    assert read_record is not None
    assert read_record["media_type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert read_record["text"] == "Word 文档正文：这里是经过安全提取器返回的内容。"
    assert source_item["content_read_status"] == "completed"
    assert source_item["content_read"] is True
    assert "经过安全提取器返回" in source_item["content_preview"]
    assert authorization.authorization_ref in source_item["trace_refs"]


def test_authorized_pdf_read_uses_local_command_document_extractor(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    pdf_path = tmp_path / "extractable.pdf"
    pdf_path.write_text("PDF正文：真实命令返回的正文。", encoding="utf-8")
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="file",
            title="Extractable PDF",
            display_name="extractable.pdf",
            media_type="application/pdf",
            size_bytes=pdf_path.stat().st_size,
            file_reference="platform-ref-extractable-pdf",
        )
    )
    AuthorizeLocalDocumentFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(pdf_path),
    )
    extractor = LocalCommandDocumentTextExtractor(
        command=(
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text(encoding='utf-8'))",
            "{document_path}",
        ),
        enabled=True,
    )

    result = ReadSourceTextContent(
        object_store,
        document_extractors=document_text_extractors_for_allowed_documents(extractor),
    ).execute(source_id=str(source["id"]))
    read_record = object_store.read("source_content_reads", f"content-read-{source['id']}")

    assert result.status == "completed"
    assert result.content_read is True
    assert result.preview == "PDF正文：真实命令返回的正文。"
    assert read_record is not None
    assert read_record["text"] == "PDF正文：真实命令返回的正文。"


def test_local_command_document_extractor_accepts_windows_code_page_output(tmp_path: Path) -> None:
    if locale.getpreferredencoding(False).lower().replace("-", "") in {"utf8", "utf8mb4"}:
        pytest.skip("code-page decode path requires a non-UTF-8 system locale")
    document_path = tmp_path / "windows-code-page.pdf"
    document_path.write_bytes(b"%PDF placeholder")
    extractor = LocalCommandDocumentTextExtractor(
        command=(
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write('中文资料提取成功'.encode('gbk'))",
            "{document_path}",
        ),
        enabled=True,
    )

    assert extractor.extract(document_path, "application/pdf") == "中文资料提取成功"


def test_local_command_document_extractor_is_default_disabled(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    pdf_path = tmp_path / "disabled.pdf"
    pdf_path.write_text("disabled extractor should not read this", encoding="utf-8")
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="file",
            title="Disabled PDF",
            display_name="disabled.pdf",
            media_type="application/pdf",
            size_bytes=pdf_path.stat().st_size,
            file_reference="platform-ref-disabled-pdf",
        )
    )
    AuthorizeLocalDocumentFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(pdf_path),
    )
    extractor = LocalCommandDocumentTextExtractor(
        command=(sys.executable, "-c", "print('should not run')"),
    )

    result = ReadSourceTextContent(
        object_store,
        document_extractors=document_text_extractors_for_allowed_documents(extractor),
    ).execute(source_id=str(source["id"]))

    assert result.status == "failed"
    assert result.content_read is False
    assert result.error == "local document text extractor is disabled"


def test_source_document_authorization_composition_uses_temp_storage(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    pdf_path = tmp_path / "composed.pdf"
    pdf_path.write_bytes(b"%PDF composed")
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="file",
            title="Composed PDF",
            display_name="composed.pdf",
            media_type="application/pdf",
            size_bytes=pdf_path.stat().st_size,
            file_reference="platform-ref-composed-pdf",
        )
    )
    use_case = build_source_document_authorization(Path.cwd(), runtime_root=tmp_path)

    result = use_case.execute(source_id=str(source["id"]), file_path=str(pdf_path))

    assert result.status == "authorized"
    assert result.authorization_ref == (
        f"crp://default/authorized-documents/authorized-document-{source['id']}.json"
    )
    assert not (tmp_path / "library").exists()


def test_local_command_document_extractor_composition_is_default_off(tmp_path: Path) -> None:
    extractor = build_local_command_document_text_extractor(
        Path.cwd(),
        runtime_root=tmp_path,
        command=(sys.executable, "-c", "print('configured')"),
    )

    assert extractor.enabled is False
    assert extractor.provider_name == "local-command-document-text"
    assert not (tmp_path / "library").exists()


def test_authorized_text_file_read_keeps_os_path_out_of_source_contract(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    note_path = tmp_path / "notes.md"
    note_path.write_text("# 会议记录\n\n授权文件正文可以进入资料库读取链路。", encoding="utf-8")
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="file",
            title="会议记录文件",
            display_name="notes.md",
            media_type="text/markdown",
            size_bytes=note_path.stat().st_size,
            file_reference="platform-ref-notes",
        )
    )

    authorization = AuthorizeLocalTextFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(note_path),
    )
    auth_payload = serialize_source_file_authorization_result(authorization)
    result = ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    updated_source = object_store.read("sources", str(source["id"]))
    auth_record = object_store.read("authorized_file_refs", authorization.authorization_id)
    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    source_item = next(item for item in overview["items"] if item["item_type"] == "source")

    assert auth_payload["status"] == "authorized"
    assert auth_payload["authorization_ref"] == (
        f"crp://default/authorized-files/authorized-file-{source['id']}.json"
    )
    assert auth_record is not None
    assert auth_record["path"] == str(note_path.resolve(strict=False))
    assert updated_source is not None
    assert updated_source["metadata"]["file_authorization"]["status"] == "authorized"
    assert updated_source["metadata"]["file_authorization"]["path_stored_in_source"] is False
    assert "path" not in updated_source["metadata"]["file_authorization"]
    assert result.status == "completed"
    assert result.content_read is True
    assert result.preview == "# 会议记录 授权文件正文可以进入资料库读取链路。"
    assert source_item["content_read"] is True
    assert source_item["content_read_status"] == "completed"
    assert "授权文件正文可以进入资料库读取链路" in source_item["content_preview"]
    assert authorization.authorization_ref in source_item["trace_refs"]


def test_authorized_text_file_read_records_missing_file_failure(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    note_path = tmp_path / "deleted.txt"
    note_path.write_text("soon gone", encoding="utf-8")
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="file",
            title="Deleted",
            display_name="deleted.txt",
            media_type="text/plain",
            size_bytes=note_path.stat().st_size,
            file_reference="platform-ref-deleted",
        )
    )
    AuthorizeLocalTextFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(note_path),
    )
    note_path.unlink()

    result = ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))

    assert result.status == "failed"
    assert result.content_read is False
    assert result.error == "authorized file does not exist"


def test_authorized_text_file_read_records_max_size_failure(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    note_path = tmp_path / "large.txt"
    note_path.write_text("123456789", encoding="utf-8")
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="file",
            title="Large",
            display_name="large.txt",
            media_type="text/plain",
            size_bytes=note_path.stat().st_size,
            file_reference="platform-ref-large",
        )
    )
    AuthorizeLocalTextFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(note_path),
    )

    result = ReadSourceTextContent(object_store, max_bytes=4).execute(source_id=str(source["id"]))

    assert result.status == "failed"
    assert result.error == "source content exceeds max_bytes 4"


def test_authorized_text_file_read_records_encoding_failure(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    note_path = tmp_path / "bad.txt"
    note_path.write_bytes(b"\xff\xfe\x00")
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="file",
            title="Bad encoding",
            display_name="bad.txt",
            media_type="text/plain",
            size_bytes=note_path.stat().st_size,
            file_reference="platform-ref-bad",
        )
    )
    AuthorizeLocalTextFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(note_path),
    )

    result = ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))

    assert result.status == "failed"
    assert result.error == "authorized file is not valid utf-8 text"


def test_authorized_empty_text_file_records_explicit_empty_failure(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    note_path = tmp_path / "empty.txt"
    note_path.write_bytes(b"")
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="file",
            title="Empty text",
            display_name="empty.txt",
            media_type="text/plain",
            size_bytes=0,
            file_reference="platform-ref-empty",
        )
    )
    AuthorizeLocalTextFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(note_path),
    )

    result = ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))

    assert result.status == "failed"
    assert result.content_read is False
    assert result.error == "source content is empty"
    assert object_store.read("source_content_reads", f"content-read-{source['id']}") is None


def test_local_text_file_authorization_rejects_unsupported_media(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_text("not a pdf", encoding="utf-8")
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="file",
            title="PDF",
            display_name="report.pdf",
            media_type="application/pdf",
            size_bytes=pdf_path.stat().st_size,
            file_reference="platform-ref-report",
        )
    )

    with pytest.raises(SourceFileAuthorizationError, match="unsupported media_type"):
        AuthorizeLocalTextFileForSource(object_store).execute(
            source_id=str(source["id"]),
            file_path=str(pdf_path),
        )


def test_source_text_content_read_rejects_missing_source(tmp_path: Path) -> None:
    object_store = _store(tmp_path)

    with pytest.raises(SourceContentReadError, match="source not found"):
        ReadSourceTextContent(object_store).execute(source_id="source-missing")


def test_source_content_read_endpoint_serves_narrow_post(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(kind="text", title="Endpoint Source", content="Endpoint reads text.")
    )
    use_case = ReadSourceTextContent(object_store)
    endpoint = ServeSourceContentReadEndpoint()

    response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/sources/{source['id']}/content-read",
        read_content=use_case.execute,
    )

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.body["status"] == "completed"
    assert response.body["source_id"] == source["id"]
    assert response.body["content_read"] is True
    assert response.body["preview"] == "Endpoint reads text."


def test_source_content_read_endpoint_rejects_wrong_method_path_and_missing_source(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    use_case = ReadSourceTextContent(object_store)
    endpoint = ServeSourceContentReadEndpoint()

    wrong_method = endpoint.execute(
        method="GET",
        path="/api/rebuild/sources/source-text-001/content-read",
        read_content=use_case.execute,
    )
    wrong_path = endpoint.execute(
        method="POST",
        path="/api/rebuild/source-content-read",
        read_content=use_case.execute,
    )
    missing_source = endpoint.execute(
        method="POST",
        path="/api/rebuild/sources/source-missing/content-read",
        read_content=use_case.execute,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "POST"
    assert wrong_path.status_code == 404
    assert missing_source.status_code == 404
    assert missing_source.body["detail"] == "source content read rejected"
    assert missing_source.body["reason"] == "source not found"


def test_source_file_authorization_endpoint_serves_narrow_post(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    note_path = tmp_path / "endpoint.txt"
    note_path.write_text("Endpoint authorized file.", encoding="utf-8")
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="file",
            title="Endpoint File",
            display_name="endpoint.txt",
            media_type="text/plain",
            size_bytes=note_path.stat().st_size,
            file_reference="platform-ref-endpoint",
        )
    )
    endpoint = ServeSourceFileAuthorizationEndpoint()
    use_case = AuthorizeLocalTextFileForSource(object_store)

    response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/sources/{source['id']}/file-authorization",
        body={"file_path": str(note_path)},
        authorize_file=use_case.execute,
    )

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/json"
    assert response.body["status"] == "authorized"
    assert response.body["source_id"] == source["id"]
    assert response.body["authorization_id"] == f"authorized-file-{source['id']}"


def test_source_file_authorization_endpoint_rejects_wrong_method_path_and_body(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    endpoint = ServeSourceFileAuthorizationEndpoint()
    use_case = AuthorizeLocalTextFileForSource(object_store)

    wrong_method = endpoint.execute(
        method="GET",
        path="/api/rebuild/sources/source-file-001/file-authorization",
        body={"file_path": "note.txt"},
        authorize_file=use_case.execute,
    )
    wrong_path = endpoint.execute(
        method="POST",
        path="/api/rebuild/sources/source-file-001/authorize-file",
        body={"file_path": "note.txt"},
        authorize_file=use_case.execute,
    )
    wrong_body = endpoint.execute(
        method="POST",
        path="/api/rebuild/sources/source-file-001/file-authorization",
        body={"file_path": ""},
        authorize_file=use_case.execute,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "POST"
    assert wrong_path.status_code == 404
    assert wrong_body.status_code == 400
    assert wrong_body.body["detail"] == "file_path must be a non-empty string"
