from __future__ import annotations

from core.product_core.local_transcript_summary import CreateLocalTranscriptSummary
from core.product_core.transcript_ad_filter import (
    CreateAdFilteredTranscript,
    TRANSCRIPT_AD_FILTER_VERSION,
)
from core.storage_provider import JsonObjectStore


def test_ad_filter_preserves_raw_transcript_and_keeps_uncertain_topic_content(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    source_id = "source-video-ad-filter-1"
    transcript_id = "media-output-transcript-ad-filter-1"
    raw_text = (
        "今天讲解模型训练中的数据漂移。\n\n"
        "本期视频由示例品牌赞助，使用优惠码 SAVE20 下单。\n\n"
        "接下来讨论赞助研究和优惠策略如何影响平台经济，这部分属于课程正文。"
    )
    store.write("sources", source_id, {"id": source_id, "title": "数据漂移课程"}, expected_revision=0)
    store.write(
        "media_processing_outputs",
        transcript_id,
        {
            "schema_version": "1.0.0",
            "id": transcript_id,
            "job_id": "transcript-job-1",
            "source_id": source_id,
            "source_type": "video",
            "output_kind": "transcript",
            "status": "completed",
            "provider": "fixture",
            "title": "原始转写",
            "preview": raw_text[:100],
            "text": raw_text,
            "segments": [],
            "metadata": {},
            "memory_publication": "not_started",
            "created_at": "2026-09-05T00:00:00Z",
            "ref": f"crp://default/media-processing-outputs/{transcript_id}.json",
        },
        expected_revision=0,
    )

    filtered = CreateAdFilteredTranscript(store, namespace_id="default").execute(
        transcript_output_id=transcript_id
    )
    derived = store.read("media_processing_outputs", filtered.output_id)
    raw = store.read("media_processing_outputs", transcript_id)
    summary = CreateLocalTranscriptSummary(store, namespace_id="default").execute(
        transcript_output_id=filtered.output_id
    )
    summary_output = store.read("media_processing_outputs", summary.output_id)

    assert raw["text"] == raw_text
    assert "优惠码 SAVE20" not in derived["text"]
    assert "赞助研究和优惠策略" in derived["text"]
    assert derived["metadata"]["ad_filter_version"] == TRANSCRIPT_AD_FILTER_VERSION
    assert derived["metadata"]["excluded_ad_count"] == 1
    assert derived["metadata"]["uncertain_ad_count"] == 1
    assert "优惠码 SAVE20" not in summary_output["text"]
    assert summary_output["metadata"]["content_transcript_output_id"] == filtered.output_id


def test_all_advertising_transcript_produces_an_honest_empty_content_summary(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    transcript_id = "media-output-transcript-only-ad"
    store.write(
        "sources",
        "source-only-ad",
        {"id": "source-only-ad", "title": "纯广告片段"},
        expected_revision=0,
    )
    store.write(
        "media_processing_outputs",
        transcript_id,
        {
            "status": "completed",
            "output_kind": "transcript",
            "source_id": "source-only-ad",
            "source_type": "video",
            "title": "广告",
            "text": "本期视频由示例品牌赞助，使用优惠码 SAVE20 下单。",
            "segments": [],
            "created_at": "2026-09-05T00:00:00Z",
            "ref": f"crp://default/media-processing-outputs/{transcript_id}.json",
        },
        expected_revision=0,
    )

    filtered = CreateAdFilteredTranscript(store, namespace_id="default").execute(
        transcript_output_id=transcript_id
    )
    summary = CreateLocalTranscriptSummary(store, namespace_id="default").execute(
        transcript_output_id=filtered.output_id
    )

    derived = store.read("media_processing_outputs", filtered.output_id)
    summary_output = store.read("media_processing_outputs", summary.output_id)
    assert derived["text"] == ""
    assert derived["metadata"]["excluded_ad_count"] == 1
    assert "未检测到可整理的非广告正文" in summary_output["text"]
