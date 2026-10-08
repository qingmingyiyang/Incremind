from __future__ import annotations

from core.product_core import GenerateDailyReminders, serialize_daily_reminder_digest
from core.storage_provider import JsonObjectStore


def _store(tmp_path):
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def test_daily_reminders_collect_pending_reviews_and_blocked_workflows(tmp_path) -> None:
    store = _store(tmp_path)
    store.write(
        "memory_candidates",
        "candidate-1",
        {
            "id": "candidate-1",
            "status": "pending_review",
            "project_id": "project-alpha",
            "source_refs": ["source:note-1"],
        },
        expected_revision=None,
    )
    store.write(
        "external_agent_review_drafts",
        "draft-1",
        {
            "id": "draft-1",
            "status": "pending_review",
            "project_id": "project-alpha",
            "source_refs": ["source:agent-1"],
            "ref": "crp://default/external-agent-review-drafts/draft-1.json",
        },
        expected_revision=None,
    )
    store.write(
        "audio_auto_workflows",
        "audio-auto-workflow-source-audio",
        {
            "workflow_id": "audio-auto-workflow-source-audio",
            "status": "blocked",
            "project_id": "project-alpha",
            "source_id": "source-audio",
            "next_step": "configure_local_asr",
        },
        expected_revision=None,
    )
    store.write(
        "video_auto_workflows",
        "video-auto-workflow-source-video",
        {
            "workflow_id": "video-auto-workflow-source-video",
            "status": "blocked",
            "project_id": "project-alpha",
            "source_id": "source-video",
            "blocked_operations": ["transcript_summary_provider_readiness"],
            "summary_next_step": "enable_transcript_summary_provider",
        },
        expected_revision=None,
    )
    store.write(
        "sources",
        "source-unread-link",
        {
            "id": "source-unread-link",
            "type": "link",
            "title": "Unread link",
            "storage_uri": "crp://default/sources/source-unread-link",
            "metadata": {"project_id": "project-alpha", "remote_fetch": "not_performed"},
        },
        expected_revision=None,
    )
    store.write(
        "sources",
        "source-read-text",
        {
            "id": "source-read-text",
            "type": "text",
            "title": "Read text",
            "metadata": {
                "project_id": "project-alpha",
                "content_read": {"status": "completed", "content_read": True},
            },
        },
        expected_revision=None,
    )
    store.write(
        "sources",
        "source-doc-failed",
        {
            "id": "source-doc-failed",
            "type": "file",
            "title": "Failed document",
            "storage_uri": "crp://default/sources/source-doc-failed",
            "metadata": {
                "project_id": "project-alpha",
                "content_read": {"status": "failed", "content_read": False},
            },
        },
        expected_revision=None,
    )
    store.write(
        "source_content_reads",
        "content-read-source-doc-failed",
        {
            "id": "content-read-source-doc-failed",
            "source_id": "source-doc-failed",
            "status": "failed",
            "media_type": "application/pdf",
            "error": "local document extractor failed",
        },
        expected_revision=None,
    )
    store.write(
        "media_processing_outputs",
        "media-output-ocr-source-image-failed",
        {
            "id": "media-output-ocr-source-image-failed",
            "source_id": "source-doc-failed",
            "output_kind": "ocr_text",
            "status": "failed",
            "provider": "local-command-ocr",
            "error": "ocr command failed",
            "ref": "crp://default/media-processing-outputs/media-output-ocr-source-image-failed.json",
        },
        expected_revision=None,
    )
    store.write(
        "project_skills",
        "skill-missing-reading",
        {
            "id": "skill-missing-reading",
            "project_id": "project-alpha",
            "name": "缺默认阅读要求的 skill",
            "ref": "crp://default/project-skills/skill-missing-reading.json",
        },
        expected_revision=None,
    )
    store.write(
        "project_skills",
        "skill-ready",
        {
            "id": "skill-ready",
            "project_id": "project-alpha",
            "name": "已配置阅读要求的 skill",
            "default_reading_requirements": ["先读 series_summaries.json"],
        },
        expected_revision=None,
    )
    store.write(
        "memory_candidates",
        "candidate-other",
        {"id": "candidate-other", "status": "pending_review", "project_id": "project-beta"},
        expected_revision=None,
    )

    result = GenerateDailyReminders(store).execute(project_id="project-alpha")
    body = serialize_daily_reminder_digest(result)

    assert body["status"] == "ready"
    assert body["project_id"] == "project-alpha"
    assert body["counts"] == {
        "total": 8,
        "high": 2,
        "medium": 4,
        "low": 2,
        "pending_review": 2,
        "blocked_workflow": 2,
        "source_attention": 1,
        "failed_extraction": 1,
        "skill_attention": 1,
        "provider_attention": 1,
    }
    assert body["next_focus"] == "review_external_agent_drafts"
    assert {item["reminder_type"] for item in body["reminders"]} == {
        "memory_candidate_pending_review",
        "external_agent_review_draft_pending_review",
        "audio_auto_workflow_blocked",
        "video_auto_workflow_blocked",
        "source_content_read_needed",
        "extraction_failed_review_needed",
        "project_skill_reading_requirements_missing",
        "provider_local_readiness_needed",
    }
    unread = next(item for item in body["reminders"] if item["reminder_type"] == "source_content_read_needed")
    assert unread["related_ids"] == ["source-unread-link"]
    skill = next(
        item for item in body["reminders"] if item["reminder_type"] == "project_skill_reading_requirements_missing"
    )
    assert skill["related_ids"] == ["skill-missing-reading"]
    provider = next(item for item in body["reminders"] if item["reminder_type"] == "provider_local_readiness_needed")
    assert set(provider["related_ids"]) == {
        "local_ocr",
        "local_asr",
        "local_video",
        "local_document_text",
        "transcript_summary",
    }
    assert "disabled_until_explicit_enable" in provider["summary"]
    video = next(item for item in body["reminders"] if item["reminder_type"] == "video_auto_workflow_blocked")
    assert "enable_transcript_summary_provider" in video["summary"]
    extraction = next(item for item in body["reminders"] if item["reminder_type"] == "extraction_failed_review_needed")
    assert set(extraction["related_ids"]) == {
        "content-read-source-doc-failed",
        "media-output-ocr-source-image-failed",
        "source-doc-failed",
    }
    assert "ocr_text" in extraction["summary"]
    assert "application/pdf" in extraction["summary"]
    assert body["memory_publication_state"] == "not_published"
    assert "model_provider_execution" in body["blocked_operations"]
    assert "cookie_read" in body["blocked_operations"]


def test_daily_reminders_returns_quiet_digest_when_no_attention_needed(tmp_path) -> None:
    body = serialize_daily_reminder_digest(GenerateDailyReminders(_store(tmp_path)).execute(project_id="project-alpha"))

    assert body["status"] == "quiet"
    assert body["counts"]["total"] == 0
    assert body["reminders"] == []
    assert body["next_focus"] is None
