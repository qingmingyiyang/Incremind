from __future__ import annotations

from backend.video_intake.models import LibraryRecord, RecordDetail
from backend.video_intake.service import _build_question_references


def test_question_references_include_chunk_time_and_frame_ids() -> None:
    detail = RecordDetail(
        record=LibraryRecord(
            id="BV1xx411c7mD",
            bvid="BV1xx411c7mD",
            title="问答测试",
            source_url="https://www.bilibili.com/video/BV1xx411c7mD/",
            relative_dir="2026/06/23/test",
        ),
        structured={
            "chunks": [
                {"chunk_id": "chunk-0001", "start": 10, "end": 30, "text": "安卓开发流程包含设计和编码。", "keywords": ["安卓", "开发流程"]}
            ],
            "timeline": [{"start": 0, "end": 40, "frame_ids": ["frame-0001"]}],
            "visual_analysis": {"keyframes": [{"frame_id": "frame-0001", "timestamp": 20}]},
        },
    )

    references = _build_question_references(detail, "安卓开发流程")

    assert references[0]["chunk_id"] == "chunk-0001"
    assert references[0]["timestamp"] == "00:10–00:30"
    assert references[0]["frame_ids"] == ["frame-0001"]
