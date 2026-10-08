from pathlib import Path

import pytest

from backend.api.bilibili_media_postprocess_runtime import (
    build_bilibili_postprocess_domain,
    readmit_bilibili_postprocess,
)
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.document_engine import DocumentDraft
from core.effect_log import EffectState
from core.job_runner import SQLiteJobAdmissionCommand
from core.product_core.bilibili_media_postprocess import (
    BilibiliPostprocessAdmissionFactory,
    BilibiliPostprocessEffectHandler,
    BilibiliPostprocessEffectProbe,
)
from core.source_processing import SourceManifestArtifactRepository, SourceManifestCodec


def _job() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": "bilibili-postprocess:media-job-1",
        "source_id": "source-bili-1",
        "project_id": "default",
        "job_type": "bilibili_media_postprocess",
        "execution_version": "effect-v2",
        "status": "pending",
        "attempt": 0,
        "max_attempts": 1,
        "postprocess_input": {
            "parent_job_id": "media-job-1",
            "parent_operation_id": "media-operation-1",
            "parent_receipt_ref": "receipt:media-hands/media-operation-1",
            "source_id": "source-bili-1",
            "project_id": "default",
            "manifest_ref": "crp://default/source-manifests/bili-1",
            "manifest_revision": "manifest-r1",
            "document_id": "document-bili-1",
            "document_revision": 1,
            "document_uri": "crp://default/documents/document-bili-1.md",
        },
        "created_at": "2026-09-04T00:00:00Z",
        "updated_at": "2026-09-04T00:00:00Z",
        "published_outputs": [],
    }


def test_bilibili_postprocess_is_an_independent_receipted_effect(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    job = _job()
    admission = BilibiliPostprocessAdmissionFactory(admitted_at=100).build(job_payload=job)
    admitted = SQLiteJobAdmissionCommand(database).admit(
        payload=job,
        authorization=admission.authorization,
        intent=admission.intent,
    )
    expected = {
        "transcript_output_id": "transcript-1",
        "content_transcript_output_id": "content-transcript-1",
        "summary_output_id": "summary-1",
        "candidate_id": "candidate-1",
        "candidate_status": "pending_review",
        "execution_ref": f"facts:effect/{admitted.effect.operation_id}",
    }
    calls = []

    def execute(effect, item, checkpoint):
        checkpoint()
        assert effect.operation_id == admitted.effect.operation_id
        assert item["parent_receipt_ref"] == "receipt:media-hands/media-operation-1"
        return expected

    def verify(_effect, _item, output):
        assert output == expected
        calls.append(output["candidate_id"])

    handler = BilibiliPostprocessEffectHandler(
        database, execute, verify, lambda _effect: (lambda: None),
    )
    receipt = handler(admitted.effect)

    assert receipt.receipt_kind == "bilibili-media-postprocess.receipt"
    assert BilibiliPostprocessEffectProbe(database, verify)(admitted.effect)[0] is EffectState.SETTLED_OK
    assert calls


def test_bilibili_document_becomes_summary_and_pending_review_candidate(tmp_path: Path) -> None:
    store, settings = build_rebuild_object_store(tmp_path)
    documents = AggregateRepositoryFactory(
        runtime_root=tmp_path,
        namespace_id=settings.namespace_id,
        json_store=store,
    ).document_repository()
    document = documents.create_or_replay_generated(DocumentDraft(
        title="B站字幕：公开课程视频",
        document_type="media_transcript",
        markdown=(
            "# 公开课程视频\n\n这是一段用于验证本地摘要和待审记忆候选的真实转写正文。\n\n"
            "本期视频由示例品牌赞助，使用优惠码 SAVE20 下单。\n"
        ),
        source_refs=({
            "source_id": "source-bili-1",
            "locator": "crp://default/source-manifests/bili-1",
            "quote": "manifest-r1",
        },),
        project_id="default",
    ))
    job = _job()
    item = dict(job["postprocess_input"])
    item.update({
        "document_id": document["id"],
        "document_uri": document["markdown_uri"],
    })
    job["postprocess_input"] = item
    database = tmp_path / ".rebuild-data" / "jobs.sqlite3"
    admission = BilibiliPostprocessAdmissionFactory(admitted_at=100).build(job_payload=job)
    effect = SQLiteJobAdmissionCommand(database).admit(
        payload=job,
        authorization=admission.authorization,
        intent=admission.intent,
    ).effect
    domain = build_bilibili_postprocess_domain(tmp_path, store, settings.namespace_id)

    BilibiliPostprocessEffectHandler(
        database, domain.execute, domain.verify, lambda _effect: (lambda: None),
    )(effect)

    summary = store.read(
        "media_processing_outputs",
        "media-output-local-summary-ad_filter_v1-source-bili-1",
    )
    content_transcript = store.read(
        "media_processing_outputs",
        "media-output-content-transcript-ad_filter_v1-source-bili-1",
    )
    raw_transcript = store.read(
        "media_processing_outputs",
        f"media-output-bilibili-transcript-{document['id']}",
    )
    source = store.read("sources", "source-bili-1")
    assert source is not None
    assert source["capture_mode"] == "verified_source_manifest"
    assert source["metadata"]["document_id"] == document["id"]
    assert summary is not None
    assert summary["provider"] == "builtin-local-extractive-summary"
    assert "优惠码 SAVE20" not in summary["text"]
    assert content_transcript["metadata"]["excluded_ad_count"] == 1
    assert "优惠码 SAVE20" not in content_transcript["text"]
    assert "优惠码 SAVE20" in raw_transcript["text"]
    candidates = store.list("memory_candidates")
    assert len(candidates) == 1
    assert candidates[0]["status"] == "pending_review"
    assert candidates[0]["review"]["requires_user_confirmation"] is True
    assert candidates[0]["review"]["auto_promote_allowed"] is False


def test_failed_bilibili_postprocess_rebuilds_from_current_manifest_and_document(
    tmp_path: Path,
) -> None:
    store, settings = build_rebuild_object_store(tmp_path)
    manifest = SourceManifestCodec.decode({
        "schema_version": "1.0.0",
        "source_id": "source-bili-1",
        "source_ref": "crp://default/sources/source-bili-1",
        "platform": "bilibili",
        "input_identity": "https://www.bilibili.com/video/BV1xx411c7mD/",
        "resolver_revision": "bilibili-view-api-v1",
        "normalizer_revision": "bilibili-manifest-v1",
        "content_kind": "video",
        "body": None,
        "metadata": {"title": "fixture"},
        "permission": {
            "decision": "granted",
            "evidence_refs": ["crp://default/source-resolution-evidence/bili-1"],
        },
        "provenance_refs": ["crp://default/source-resolution-evidence/bili-1"],
        "assets": [{
            "asset_id": "video-source-bili-1",
            "ordinal": 0,
            "kind": "video",
            "media_type": None,
            "role": "primary",
            "locator": "https://www.bilibili.com/video/BV1xx411c7mD/",
            "source_ref": "crp://default/sources/source-bili-1/assets/video-source-bili-1",
            "relations": [],
            "evidence_refs": ["crp://default/source-resolution-evidence/bili-1"],
        }],
    })
    artifact = SourceManifestArtifactRepository(
        store, namespace_id=settings.namespace_id,
    ).put(project_id="default", manifest_id="source-bili-1--initial", manifest=manifest)
    documents = AggregateRepositoryFactory(
        runtime_root=tmp_path,
        namespace_id=settings.namespace_id,
        json_store=store,
    ).document_repository()
    document = documents.create_or_replay_generated(DocumentDraft(
        title="B站字幕",
        document_type="media_transcript",
        markdown="# B站字幕\n\n内容。\n",
        source_refs=({
            "source_id": "source-bili-1",
            "locator": artifact.public_ref,
            "quote": artifact.revision,
        },),
        project_id="default",
    ))
    previous = _job()
    previous.update({"status": "failed", "error": {"code": "postprocess_failed"}})
    previous["postprocess_input"] = {
        **previous["postprocess_input"],
        "manifest_ref": artifact.public_ref,
        "manifest_revision": artifact.revision,
        "document_id": document["id"],
        "document_uri": document["markdown_uri"],
    }

    rebuilt = readmit_bilibili_postprocess(
        database_path=tmp_path / ".rebuild-data" / "jobs.sqlite3",
        runtime_root=tmp_path,
        object_store=store,
        namespace_id=settings.namespace_id,
        previous_job=previous,
        command_id="retry-command-0001",
    )

    assert rebuilt["id"] == "bilibili-postprocess-retry:retry-command-0001"
    assert rebuilt["rebuilt_from_job_id"] == previous["id"]
    assert rebuilt["postprocess_input"] == previous["postprocess_input"]
    assert previous["status"] == "failed"

    with pytest.raises(ValueError, match="not rebuildable"):
        readmit_bilibili_postprocess(
            database_path=tmp_path / ".rebuild-data" / "jobs.sqlite3",
            runtime_root=tmp_path,
            object_store=store,
            namespace_id=settings.namespace_id,
            previous_job={**previous, "status": "running"},
            command_id="retry-command-0002",
        )
