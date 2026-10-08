from __future__ import annotations

from pathlib import Path

import pytest

from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.product_core import (
    AutoMemoryPublicationError,
    AutoPublishMemoryCandidate,
    GetAutoMemoryPublicationSettings,
    SaveAutoMemoryPublicationSettings,
    serialize_auto_memory_publication_settings,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _candidate(object_store: JsonObjectStore, candidate_id: str = "memory-candidate-auto-publish-001") -> str:
    ObjectStoreMemoryCandidateRepository(object_store).save(
        {
            "schema_version": "1.0.0",
            "id": candidate_id,
            "project_id": "project-alpha",
            "target_layer": "atom",
            "candidate_type": "answer_summary",
            "status": "pending_review",
            "proposed_content": "用户授权后，系统可以把该视频总结沉淀为长期 Atom Memory。",
            "source_refs": [
                {
                    "source_id": "source-video-alpha",
                    "locator": "media:summary",
                    "quote": "视频总结说明了自动发布边界。",
                }
            ],
            "provenance": {
                "model_result_id": None,
                "model_request_id": None,
                "recall_result_id": None,
                "document_id": None,
                "document_revision": None,
                "source_content_read_id": None,
                "media_processing_output_id": "media-output-summary-source-video-alpha",
                "media_processing_job_id": "media-job-summary-source-video-alpha",
                "input_refs": [
                    {
                        "kind": "source",
                        "object_id": "source-video-alpha",
                        "uri": "crp://default/sources/source-video-alpha.json",
                    },
                    {
                        "kind": "media_processing_job",
                        "object_id": "media-job-summary-source-video-alpha",
                        "uri": "crp://default/media-processing-jobs/media-job-summary-source-video-alpha.json",
                    },
                    {
                        "kind": "media_processing_output",
                        "object_id": "media-output-summary-source-video-alpha",
                        "uri": "crp://default/media-processing-outputs/media-output-summary-source-video-alpha.json",
                    },
                ],
            },
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": "用户已授权系统自动发布前仍需本地审计记录。",
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "created_at": "2026-07-02T10:00:00+08:00",
            "updated_at": "2026-07-02T10:00:00+08:00",
        }
    )
    return candidate_id


def test_auto_memory_publication_is_disabled_by_default(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    candidate_id = _candidate(object_store)

    settings = serialize_auto_memory_publication_settings(GetAutoMemoryPublicationSettings(object_store).execute())
    result = AutoPublishMemoryCandidate(object_store).execute(candidate_id=candidate_id)

    assert settings["status"] == "disabled"
    assert settings["enabled"] is False
    assert settings["quarantined"] is False
    assert result.status == "skipped"
    assert result.skipped_reason == "auto memory publication is disabled"
    assert ObjectStoreMemoryCandidateRepository(object_store).get(candidate_id)["status"] == "pending_review"
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()


def test_auto_memory_publication_requires_explicit_enable_confirmation(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    settings = SaveAutoMemoryPublicationSettings(object_store)

    with pytest.raises(AutoMemoryPublicationError, match="confirm_enable=true"):
        settings.execute(enabled=True, confirm_enable=False)

    saved = settings.execute(enabled=True, confirm_enable=True, allowed_layers=("atom",))

    assert saved.status == "quarantined"
    assert saved.enabled is True
    assert saved.allowed_layers == ("atom",)
    assert saved.quarantined is True

    with pytest.raises(AutoMemoryPublicationError, match="limited to atom"):
        settings.execute(enabled=True, confirm_enable=True, allowed_layers=("scenario",))


def test_auto_memory_publication_quarantines_enabled_setting_without_writing_memory(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    candidate_id = _candidate(object_store)
    SaveAutoMemoryPublicationSettings(object_store).execute(
        enabled=True,
        confirm_enable=True,
        allowed_layers=("atom",),
    )

    result = AutoPublishMemoryCandidate(
        object_store,
        now="2026-07-02T10:45:00+08:00",
    ).execute(
        candidate_id=candidate_id,
        reason="用户已授权视频 summary candidate 自动发布到长期 Atom Memory。",
    )
    candidate = ObjectStoreMemoryCandidateRepository(object_store).get(candidate_id)
    assert result.status == "skipped"
    assert result.memory_publication_state == "not_published"
    assert result.skipped_reason == "automatic memory publication is quarantined pending a low-risk Atom policy"
    assert candidate is not None
    assert candidate["status"] == "pending_review"
    assert object_store.list("staging_atoms") == ()
    assert object_store.list("memory_atoms") == ()
    assert object_store.list("memory_publications") == ()
    assert object_store.list("memory_transitions") == ()
    assert object_store.list("auto_memory_publications") == ()


def test_auto_memory_publication_reads_legacy_enabled_wide_scope_as_quarantined(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    candidate_id = _candidate(object_store)
    object_store.write(
        "auto_memory_publication_settings",
        "default",
        {
            "schema_version": "1.0.0",
            "id": "default",
            "enabled": True,
            "allowed_layers": ["atom", "scenario", "series_memory", "project_skill"],
            "reviewer": "system",
            "publisher": "system",
            "rollback_required": True,
        },
        expected_revision=0,
    )

    settings = GetAutoMemoryPublicationSettings(object_store).execute()
    result = AutoPublishMemoryCandidate(object_store).execute(candidate_id=candidate_id)

    assert settings.status == "quarantined"
    assert settings.enabled is True
    assert settings.allowed_layers == ("atom", "scenario", "series_memory", "project_skill")
    assert result.status == "skipped"
    assert result.skipped_reason == "automatic memory publication is quarantined pending a low-risk Atom policy"
    assert ObjectStoreMemoryCandidateRepository(object_store).get(candidate_id)["status"] == "pending_review"
