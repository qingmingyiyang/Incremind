from __future__ import annotations

from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.product_core import CreateMemoryCandidateFromSourceOutput
from core.storage_provider import JsonObjectStore
from tests.rebuild.memory_candidate_saga_review_testlib import (
    publish_staging_user_confirmed,
    review_candidate_to_staging,
    saga_records,
)


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _video_source(object_store: JsonObjectStore) -> dict[str, object]:
    return dict(
        ObjectStoreSourceRegistrar(object_store).register(
            SourceSubmission(
                kind="video",
                title="Summary memory source",
                display_name="video.mp4",
                media_type="video/mp4",
                size_bytes=4096,
                video_reference="bilibili/BV1xx411c7mD/p1",
                duration_ms=30000,
            )
        )
    )


def _summary_output(object_store: JsonObjectStore, source_id: str) -> str:
    job_id = f"media-job-summary-media-output-transcript-{source_id}"
    output_id = f"media-output-summary-{source_id}"
    output_ref = f"crp://default/media-processing-outputs/{output_id}.json"
    object_store.write(
        "media_processing_jobs",
        job_id,
        {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source_id,
            "source_type": "video",
            "required_capability": "transcript_summary",
            "status": "completed",
            "disabled_reason": None,
            "input_refs": [f"crp://default/media-processing-outputs/media-output-transcript-{source_id}.json"],
            "expected_output_refs": [f"crp://default/media-processing/{source_id}/summary.json"],
            "adapter_contract": {
                "capability": "transcript_summary",
                "provider": "local_command_summary",
                "memory_publication": "not_started",
            },
            "error": None,
            "activity_refs": [],
            "output_refs": [output_ref],
            "output_preview": "本地 summary provider 已读取 transcript JSON，并生成结构化总结。",
            "created_at": "2026-07-02T05:00:00+08:00",
            "updated_at": "2026-07-02T05:00:00+08:00",
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
            "source_id": source_id,
            "source_type": "video",
            "output_kind": "summary",
            "status": "completed",
            "provider": "local-json-summary-smoke",
            "title": "Round55 转写总结",
            "preview": "本地 summary provider 已读取 transcript JSON，并生成结构化总结。",
            "text": "# Round55 转写总结\n\n## 30 秒摘要\n本地 summary provider 已读取 transcript JSON。\n",
            "markdown": "# Round55 转写总结\n\n## 30 秒摘要\n本地 summary provider 已读取 transcript JSON。\n",
            "summary_data": {
                "title": "Round55 转写总结",
                "core_problem": "如何把已完成转写变成可回看的总结输出。",
                "key_takeaways": ["总结输出保持 not_started 的 Memory 边界"],
                "evidence": [
                    {
                        "id": "ev-1",
                        "statement": "provider 读取了转写文本",
                        "quote": "转写输出可以进入结构化总结输出。",
                        "start_seconds": 0,
                        "end_seconds": 3,
                    }
                ],
            },
            "metadata": {
                "local_processing": True,
                "remote_processing": False,
                "transcript_output_id": f"media-output-transcript-{source_id}",
                "auto_memory_candidate": False,
                "memory_publication": "not_started",
            },
            "memory_publication": "not_started",
            "created_at": "2026-07-02T05:00:00+08:00",
            "ref": output_ref,
        },
        expected_revision=None,
    )
    return output_id


def test_summary_output_requires_explicit_candidate_review_and_publication(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    output_id = _summary_output(object_store, str(source["id"]))
    candidate_use_case = CreateMemoryCandidateFromSourceOutput(
        object_store,
        now="2026-07-02T05:05:00+08:00",
    )

    candidate_result = candidate_use_case.execute_from_media_output(
        output_id=output_id,
        project_id="project-alpha",
        proposed_content="总结确认：转写输出可以进入结构化总结输出。",
        target_layer="atom",
        candidate_type="answer_summary",
        created_at="2026-07-02T05:05:00+08:00",
    )
    candidate_repo = ObjectStoreMemoryCandidateRepository(object_store)
    candidate = candidate_repo.get(candidate_result.candidate_id)
    summary_output = object_store.read("media_processing_outputs", output_id)

    assert candidate_result.status == "candidate_created"
    assert candidate_result.candidate_status == "pending_review"
    assert candidate is not None
    assert candidate["status"] == "pending_review"
    assert candidate["candidate_type"] == "answer_summary"
    assert candidate["provenance"]["media_processing_output_id"] == output_id
    assert candidate["source_refs"][0]["locator"] == "media:summary"
    assert candidate["review"]["auto_promote_allowed"] is False
    assert summary_output is not None
    assert summary_output["memory_publication"] == "candidate_created"
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()

    operation = review_candidate_to_staging(
        object_store,
        tmp_path,
        candidate_result.candidate_id,
        review_reason="用户确认该 summary output 可作为草稿 Atom。",
        reviewed_at="2026-07-02T05:06:00+08:00",
        tags=("summary", "video"),
    )
    records = saga_records(tmp_path)
    staged_atom = records.read("staging_atoms", operation.evidence.draft_id)

    assert operation.state == "finalized"
    assert staged_atom is not None
    assert staged_atom.payload["content"] == "总结确认：转写输出可以进入结构化总结输出。"
    assert staged_atom.payload["source_refs"][0]["locator"] == "media:summary"
    assert records.list("memory_atoms") == ()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()

    published = publish_staging_user_confirmed(
        tmp_path,
        layer="atom",
        staged_id=operation.evidence.draft_id,
        published_at="2026-07-02T05:07:00+08:00",
    )
    long_term_atom = records.read("memory_atoms", operation.evidence.draft_id)
    publication = records.read("memory_publications", published.publication_id)

    assert long_term_atom is not None
    assert long_term_atom.payload["trust_status"] == "user_confirmed"
    assert publication is not None
    assert publication.payload["status"] == "published"
    assert publication.payload["source_refs"][0]["locator"] == "media:summary"
    assert records.read("staging_atoms", operation.evidence.draft_id) is None
    assert not (tmp_path / "library").exists()
