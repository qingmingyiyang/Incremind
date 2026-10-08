from __future__ import annotations

import json
from pathlib import Path

from backend.video_intake.models import LibraryRecord
from backend.video_intake.structured import TOP_LEVEL_KEYS, build_structured_document, retrieve_chunks


def test_structured_document_has_fixed_contract_and_complete_chunks(tmp_path: Path) -> None:
    record = LibraryRecord(
        id="BV1xx411c7mD",
        bvid="BV1xx411c7mD",
        title="结构化测试",
        source_url="https://www.bilibili.com/video/BV1xx411c7mD/",
        uploader="作者",
        published_at="2026-06-23",
        tags=["测试"],
        cover_url="https://example.test/cover.jpg",
        relative_dir="2026/06/23/test",
    )
    segments = [
        {"start_seconds": 0.0, "end_seconds": 10.0, "text": "第一段介绍核心问题。"},
        {"start_seconds": 10.0, "end_seconds": 20.0, "text": "第二段给出重要结论和行动建议。"},
    ]
    (tmp_path / "transcript.cleaned.json").write_text(
        json.dumps({"source": "whisper", "language": "zh", "segments": segments}, ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "summary.json").write_text(
        json.dumps(
            {
                "thirty_second_summary": "简要总结",
                "core_problem": "核心问题是什么",
                "key_takeaways": ["重要结论"],
                "detailed_notes": ["详细笔记"],
                "chapters": [{"title": "开场", "start_seconds": 0, "end_seconds": 20, "summary": "章节摘要", "evidence_ids": ["ev-1"]}],
                "evidence": [{"id": "ev-1", "statement": "结论", "quote": "重要结论", "start_seconds": 10, "end_seconds": 20, "confidence": "high"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    document, search_index = build_structured_document(record, tmp_path)

    assert tuple(document) == TOP_LEVEL_KEYS
    assert document["metadata"]["cover_url"] == "https://example.test/cover.jpg"
    assert document["index"]["available"] is True
    assert document["chunks"][0]["chunk_id"] == "chunk-0001-0000000000"
    assert "第一段介绍核心问题" in document["retrieval_text"]
    assert "第二段给出重要结论" in document["retrieval_text"]
    assert search_index["chunk_order"] == ["chunk-0001-0000000000"]
    assert document["claims"][0]["chunk_id"] == "chunk-0001-0000000000"
    assert document["sources"]["cleaned_transcript"]["source"] == "whisper"


def test_retrieve_chunks_returns_evidence_with_stable_ids(tmp_path: Path) -> None:
    structured = {
        "chunks": [
            {"chunk_id": "chunk-0001", "start": 0, "end": 20, "text": "苹果介绍", "keywords": ["苹果"]},
            {"chunk_id": "chunk-0002", "start": 20, "end": 40, "text": "安卓开发流程", "keywords": ["安卓", "开发流程"]},
        ]
    }

    result = retrieve_chunks(structured, "安卓开发流程", limit=1)

    assert result[0]["chunk_id"] == "chunk-0002"
