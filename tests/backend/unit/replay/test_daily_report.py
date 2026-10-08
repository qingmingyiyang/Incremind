from __future__ import annotations

import json

import pytest

from backend.replay.contracts import (
    KnowledgeEvidence,
    KnowledgeIndex,
    KnowledgeItem,
    KnowledgeLinks,
    KnowledgeSource,
    KnowledgeStatus,
)
from backend.replay.library import ReplayLibrary
from backend.replay.reports import ReportService


DAY = "2026-06-23"


def _item(item_id: str, item_type: str, title: str, content: str) -> KnowledgeItem:
    return KnowledgeItem(
        id=item_id,
        type=item_type,
        title=title,
        content=content,
        summary=content,
        tags=["桌面", "复盘"],
        created_at=f"{DAY}T10:00:00+08:00",
        updated_at=f"{DAY}T10:00:00+08:00",
        source=KnowledgeSource(kind="manual"),
        status=KnowledgeStatus(in_daily=True),
        index=KnowledgeIndex(available=True, keywords=["桌面", "复盘"]),
    )


def test_daily_report_contains_video_personal_items_and_full_evidence_chain(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library")
    video = KnowledgeItem(
        id="video_BV1TEST",
        type="video",
        title="Electron 架构视频",
        content="完整视频文本保存在原视频文件中。",
        summary="Electron 只负责桌面系统能力。",
        tags=["Electron", "Rust"],
        created_at=f"{DAY}T09:00:00+08:00",
        updated_at=f"{DAY}T09:00:00+08:00",
        source=KnowledgeSource(
            kind="bilibili",
            url="https://www.bilibili.com/video/BV1TEST/",
            bvid="BV1TEST",
            local_path=str(tmp_path / "library" / "2026" / "video"),
        ),
        evidence=KnowledgeEvidence(
            chunk_ids=["chunk-0001"],
            frame_ids=["frame-0001"],
            timestamps=["00:01:20"],
            quotes=["业务保留在 Python 服务层。"],
        ),
        links=KnowledgeLinks(linked_video_ids=["BV1TEST"]),
        status=KnowledgeStatus(in_daily=True, high_value=True, need_review=True),
        index=KnowledgeIndex(available=True, keywords=["Electron", "Rust"], chunk_ids=["chunk-0001"]),
    )
    items = [
        video,
        _item("item_note", "note", "架构笔记", "记录 API 边界。"),
        _item("item_action", "action", "下一步", "实现周报。"),
        _item("item_question", "question", "未解决问题", "如何同步到移动端？"),
    ]
    for item in items:
        library.save_item(item)

    report = ReportService(library).generate_daily(DAY)

    assert report.report_id == f"daily_{DAY}"
    assert report.sources.videos == [video.id]
    assert report.sources.notes == ["item_note"]
    assert report.sources.actions == ["item_action"]
    assert report.sources.questions == ["item_question"]
    assert report.video_knowledge[0].video_id == "BV1TEST"
    assert report.video_knowledge[0].bvid == "BV1TEST"
    video_evidence = next(item for item in report.evidence if item.source_type == "video")
    assert video_evidence.video_id == "BV1TEST"
    assert video_evidence.bvid == "BV1TEST"
    assert video_evidence.chunk_id == "chunk-0001"
    assert video_evidence.frame_id == "frame-0001"
    assert video_evidence.timestamp == "00:01:20"
    assert video_evidence.quote == "业务保留在 Python 服务层。"
    assert report.index.linked_item_ids == [item.id for item in items]

    journal = tmp_path / "library" / "journals" / "2026" / "06" / DAY
    for name in ["daily.json", "daily.md", "notes.md", "videos.json", "clips.json", "actions.json", "review.md"]:
        assert (journal / name).is_file(), name
    committed = json.loads((journal / "daily.json").read_text(encoding="utf-8"))
    assert committed == report.model_dump(mode="json")
    markdown = (journal / "daily.md").read_text(encoding="utf-8")
    assert "BV1TEST" in markdown
    assert "chunk-0001" in markdown
    assert "frame-0001" in markdown
    assert "实现周报" in markdown

    report_item = library.get_item(f"report_{report.report_id}")
    assert report_item is not None
    assert report_item.type == "report"
    assert report_item.source.report_id == report.report_id
    assert library.list_tasks()[0].status == "success"
    assert library.list_tasks()[0].source_count == 4
    assert not (tmp_path / "library" / "inbox" / "raw" / f"{video.id}.json").exists()
    assert not (tmp_path / "library" / "inbox" / "raw" / f"{report_item.id}.json").exists()


def test_daily_report_is_idempotent_and_empty_day_is_valid(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library")
    service = ReportService(library)

    first = service.generate_daily(DAY)
    second = service.generate_daily(DAY)

    assert first.report_id == second.report_id
    assert first.created_at == second.created_at
    assert second.summary.one_sentence.startswith("当天沉淀 0 条知识")
    assert library.list_reports("daily") == [second]
    assert len(library.list_tasks()) == 2


def test_invalid_daily_date_is_rejected_without_writing_task(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library")

    try:
        ReportService(library).generate_daily("2026-6-23")
    except ValueError as error:
        assert "YYYY-MM-DD" in str(error)
    else:
        raise AssertionError("invalid date should fail")

    assert library.list_tasks() == []


def test_daily_report_failure_is_recorded_with_clear_error(tmp_path, monkeypatch) -> None:
    library = ReplayLibrary(tmp_path / "library")

    def fail_to_write(*_args, **_kwargs):
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(library, "save_report", fail_to_write)

    with pytest.raises(OSError, match="磁盘写入失败"):
        ReportService(library).generate_daily(DAY)

    task = library.list_tasks()[0]
    assert task.status == "failed"
    assert task.error == "磁盘写入失败"
