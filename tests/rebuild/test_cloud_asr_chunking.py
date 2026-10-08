from core.product_core.cloud_asr_chunking import (
    CloudAsrChunkPlan,
    merge_cloud_asr_chunk_outputs,
    plan_cloud_asr_chunks,
)


def test_cloud_asr_planner_uses_five_second_overlap() -> None:
    plans = plan_cloud_asr_chunks(130.0)

    assert [(item.start_seconds, item.end_seconds) for item in plans] == [
        (0.0, 60.0),
        (55.0, 115.0),
        (110.0, 130.0),
    ]
    assert [item.overlap_before_seconds for item in plans] == [0.0, 5.0, 5.0]


def test_cloud_asr_merge_assigns_overlap_to_one_side_and_offsets_timestamps() -> None:
    first = CloudAsrChunkPlan(0, 0.0, 60.0, 0.0)
    second = CloudAsrChunkPlan(1, 55.0, 90.0, 5.0)

    merged = merge_cloud_asr_chunk_outputs([
        (first, {
            "text": "第一句。衔接内容。",
            "language": "zh",
            "segments": [
                {"start_seconds": 0.0, "end_seconds": 56.0, "text": "第一句。"},
                {"start_seconds": 56.0, "end_seconds": 60.0, "text": "衔接内容。"},
            ],
        }),
        (second, {
            "text": "衔接内容。第二句。",
            "language": "zh",
            "segments": [
                {"start_seconds": 0.0, "end_seconds": 5.0, "text": "衔接内容。"},
                {"start_seconds": 5.0, "end_seconds": 35.0, "text": "第二句。"},
            ],
        }),
    ])

    assert merged["text"] == "第一句。衔接内容。第二句。"
    assert [segment["text"] for segment in merged["segments"]] == [
        "第一句。",
        "衔接内容。",
        "第二句。",
    ]
    assert merged["segments"][1]["start_seconds"] == 55.0


def test_cloud_asr_merge_deduplicates_text_when_provider_has_no_segments() -> None:
    first = CloudAsrChunkPlan(0, 0.0, 60.0, 0.0)
    second = CloudAsrChunkPlan(1, 55.0, 90.0, 5.0)

    merged = merge_cloud_asr_chunk_outputs([
        (first, {"text": "前文需要保证衔接内容", "segments": []}),
        (second, {"text": "衔接内容继续后文", "segments": []}),
    ])

    assert merged["text"] == "前文需要保证衔接内容继续后文"
