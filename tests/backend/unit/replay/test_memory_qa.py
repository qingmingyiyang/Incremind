from __future__ import annotations

from datetime import date

import pytest

from backend.replay.contracts import (
    AssetMetadata,
    KnowledgeEvidence,
    KnowledgeIndex,
    KnowledgeItem,
    KnowledgeLinks,
    KnowledgeSource,
    KnowledgeStatus,
    MemoryQuestionRequest,
)
from backend.replay.library import ReplayLibrary
from backend.replay.memory_qa import MemoryQAService, NO_EVIDENCE, resolve_scope
from backend.replay.reports import ReportService


TODAY = date(2026, 6, 23)


def _video() -> KnowledgeItem:
    return KnowledgeItem(
        id="video_BV1MEMORY",
        type="video",
        title="Rust 桌面应用与 ASR",
        content="视频讨论 Rust、Electron 和 ASR。",
        summary="使用 Electron 复用 Web 前端，ASR 保留在本地服务。",
        tags=["Rust", "Electron", "ASR"],
        created_at="2026-06-22T09:00:00+08:00",
        updated_at="2026-06-22T09:00:00+08:00",
        source=KnowledgeSource(kind="bilibili", bvid="BV1MEMORY", local_path="library/BV1MEMORY"),
        evidence=KnowledgeEvidence(
            chunk_ids=["chunk-memory"],
            frame_ids=["frame-memory"],
            timestamps=["00:03:00"],
            quotes=["ASR 处理位于 Python 服务层。"],
        ),
        links=KnowledgeLinks(linked_video_ids=["BV1MEMORY"]),
        status=KnowledgeStatus(in_daily=True),
        index=KnowledgeIndex(available=True, keywords=["Rust", "Electron", "ASR"], chunk_ids=["chunk-memory"]),
    )


def _action() -> KnowledgeItem:
    return KnowledgeItem(
        id="item_action_memory",
        type="action",
        title="移动端行动",
        content="验证 Flutter Mobile 复用同一 API。",
        summary="验证 Flutter Mobile 复用同一 API。",
        tags=["移动端"],
        created_at="2026-06-22T10:00:00+08:00",
        updated_at="2026-06-22T10:00:00+08:00",
        source=KnowledgeSource(kind="manual"),
        status=KnowledgeStatus(in_daily=True),
        index=KnowledgeIndex(available=True, keywords=["Flutter", "API"]),
    )


def _prepared_library(tmp_path) -> ReplayLibrary:
    library = ReplayLibrary(tmp_path / "library")
    library.save_item(_video())
    library.save_item(_action())
    reports = ReportService(library)
    reports.generate_daily("2026-06-22")
    reports.generate_weekly("2026-W26")
    reports.generate_monthly("2026-06")
    reports.generate_yearly("2026")
    return library


@pytest.mark.parametrize(
    ("question", "scope", "value"),
    [
        ("我昨天学了什么？", "day", "2026-06-22"),
        ("这周看过什么？", "week", "2026-W26"),
        ("这个月的行动项？", "month", "2026-06"),
        ("今年最重要的主题？", "year", "2026"),
        ("BV1MEMORY 讲了什么？", "video", "BV1MEMORY"),
        ("最近哪些视频提到 ASR？", "all", ""),
    ],
)
def test_scope_resolution(question, scope, value) -> None:
    assert resolve_scope(question, "auto", "", today=TODAY) == (scope, value)


def test_memory_qa_searches_day_week_month_year_all_and_video_with_evidence(tmp_path) -> None:
    service = MemoryQAService(_prepared_library(tmp_path), today_provider=lambda: TODAY)
    questions = [
        "我昨天学了什么？",
        "这周 Rust 视频讲了什么？",
        "这个月我的行动项是什么？",
        "今年最重要的知识主题是什么？",
        "最近哪些视频提到 ASR？",
        "BV1MEMORY 的画面证据是什么？",
    ]

    answers = [service.ask(MemoryQuestionRequest(question=question, persist=False)) for question in questions]

    assert all(answer.evidence_sufficient for answer in answers)
    assert {answer.scope for answer in answers} == {"day", "week", "month", "year", "all", "video"}
    all_references = [reference for answer in answers for reference in answer.references]
    video_reference = next(reference for reference in all_references if reference.bvid == "BV1MEMORY")
    assert video_reference.video_id == "BV1MEMORY"
    assert video_reference.chunk_id == "chunk-memory"
    assert video_reference.frame_id == "frame-memory"
    assert video_reference.timestamp == "00:03:00"
    assert video_reference.quote == "ASR 处理位于 Python 服务层。"
    assert any(reference.report_id.startswith("daily_") for reference in all_references)
    assert any(reference.report_id.startswith("weekly_") for reference in all_references)
    assert any(reference.report_id.startswith("monthly_") for reference in all_references)
    assert any(reference.report_id.startswith("yearly_") for reference in all_references)
    assert "Flutter Mobile" in answers[2].answer


def test_memory_qa_persists_answer_as_question_knowledge_item(tmp_path) -> None:
    library = _prepared_library(tmp_path)
    answer = MemoryQAService(library, today_provider=lambda: TODAY).ask(
        MemoryQuestionRequest(question="最近哪些视频提到 ASR？")
    )

    saved = library.get_item(answer.saved_item_id)
    assert saved is not None
    assert saved.type == "question"
    assert saved.source.kind == "qa"
    assert saved.status.need_review is False
    assert "BV1MEMORY" in saved.links.linked_video_ids
    assert "chunk-memory" in saved.evidence.chunk_ids


def test_memory_qa_refuses_to_invent_when_evidence_is_missing(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library")
    answer = MemoryQAService(library, today_provider=lambda: TODAY).ask(
        MemoryQuestionRequest(question="火星农业有什么结论？")
    )

    assert answer.answer == NO_EVIDENCE
    assert answer.evidence_sufficient is False
    assert answer.references == []
    saved = library.get_item(answer.saved_item_id)
    assert saved.status.in_inbox is True
    assert saved.status.need_review is True


def test_memory_qa_searches_linked_asset_text_and_returns_asset_id(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library", series_id="default")
    asset_id = "asset_memory_text"
    extracted = library.root / "assets" / "extracted" / f"{asset_id}.txt"
    extracted.parent.mkdir(parents=True, exist_ok=True)
    extracted.write_text("海马缓存用于保持长期上下文。", encoding="utf-8")
    metadata = AssetMetadata(
        asset_id=asset_id,
        series_id="default",
        filename="记忆机制.txt",
        media_type="text/plain",
        size=42,
        sha256="abc123",
        relative_path=f"assets/files/{asset_id}-记忆机制.txt",
        extracted_text_path=f"assets/extracted/{asset_id}.txt",
        extraction_status="extracted",
    )
    metadata_path = library.root / "assets" / "metadata" / f"{asset_id}.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(metadata.model_dump_json(indent=2), encoding="utf-8")
    library.save_item(
        KnowledgeItem(
            id="item_asset_memory",
            series_id="default",
            type="clip",
            title="附件资料",
            content="",
            source=KnowledgeSource(kind="file"),
            links=KnowledgeLinks(asset_ids=[asset_id]),
            index=KnowledgeIndex(available=True),
        )
    )

    answer = MemoryQAService(library, today_provider=lambda: TODAY).ask(
        MemoryQuestionRequest(question="海马缓存有什么作用？", persist=False)
    )

    assert answer.evidence_sufficient is True
    reference = next(item for item in answer.references if item.asset_id == asset_id)
    assert reference.source_type == "asset"
    assert reference.item_id == "item_asset_memory"
    assert reference.title == "记忆机制.txt"
    assert "海马缓存" in reference.quote


def test_memory_qa_ignores_extracted_asset_paths_outside_the_series_asset_root(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library", series_id="default")
    asset_id = "asset_outside_root"
    outside = tmp_path / "private.txt"
    outside.write_text("越界秘密不能进入回忆证据。", encoding="utf-8")
    metadata = AssetMetadata(
        asset_id=asset_id,
        series_id="default",
        filename="普通附件.txt",
        media_type="text/plain",
        size=32,
        sha256="def456",
        relative_path=f"assets/files/{asset_id}-普通附件.txt",
        extracted_text_path="../../private.txt",
        extraction_status="extracted",
    )
    metadata_path = library.root / "assets" / "metadata" / f"{asset_id}.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(metadata.model_dump_json(indent=2), encoding="utf-8")
    library.save_item(
        KnowledgeItem(
            id="item_outside_asset",
            series_id="default",
            type="clip",
            title="普通资料",
            source=KnowledgeSource(kind="file"),
            links=KnowledgeLinks(asset_ids=[asset_id]),
            index=KnowledgeIndex(available=True),
        )
    )

    answer = MemoryQAService(library, today_provider=lambda: TODAY).ask(
        MemoryQuestionRequest(question="越界秘密是什么？", persist=False)
    )

    assert answer.evidence_sufficient is False
    assert answer.answer == NO_EVIDENCE
    assert answer.references == []


def test_memory_qa_rejects_blank_question_and_invalid_explicit_scope(tmp_path) -> None:
    service = MemoryQAService(ReplayLibrary(tmp_path / "library"), today_provider=lambda: TODAY)

    with pytest.raises(ValueError, match="不能为空"):
        service.ask(MemoryQuestionRequest(question="   "))
    with pytest.raises(ValueError, match="YYYY-Www"):
        service.ask(MemoryQuestionRequest(question="范围", scope="week", scope_value="2026-W99"))
