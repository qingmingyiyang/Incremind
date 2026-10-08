from __future__ import annotations

from backend.replay.contracts import KnowledgeEvidence, KnowledgeIndex, KnowledgeItem, KnowledgeLinks, KnowledgeSource, KnowledgeStatus
from backend.replay.library import ReplayLibrary
from backend.replay.reports import ReportService


def _video() -> KnowledgeItem:
    return KnowledgeItem(
        id="video_BV1CHAIN",
        type="video",
        title="四级报告证据视频",
        content="完整文本保存在视频资料。",
        summary="证据应穿透四级报告。",
        tags=["证据链", "Electron"],
        created_at="2026-06-23T10:00:00+08:00",
        updated_at="2026-06-23T10:00:00+08:00",
        source=KnowledgeSource(kind="bilibili", bvid="BV1CHAIN", local_path="library/BV1CHAIN"),
        evidence=KnowledgeEvidence(
            chunk_ids=["chunk-chain"],
            frame_ids=["frame-chain"],
            timestamps=["00:02:00"],
            quotes=["证据来自视频原文与画面。"],
        ),
        links=KnowledgeLinks(linked_video_ids=["BV1CHAIN"]),
        status=KnowledgeStatus(in_daily=True, high_value=True),
        index=KnowledgeIndex(available=True, keywords=["证据链", "Electron"], chunk_ids=["chunk-chain"]),
    )


def test_yearly_report_reads_monthly_and_preserves_complete_four_level_chain(tmp_path, monkeypatch) -> None:
    library = ReplayLibrary(tmp_path / "library")
    service = ReportService(library)
    library.save_item(_video())
    daily = service.generate_daily("2026-06-23")
    weekly = service.generate_weekly("2026-W26")
    monthly = service.generate_monthly("2026-06")

    original_list_reports = library.list_reports
    calls: list[str | None] = []

    def monthly_only(report_type=None):
        calls.append(report_type)
        if report_type in {"daily", "weekly"}:
            raise AssertionError("yearly report must only read monthly reports")
        return original_list_reports(report_type)

    def reject_journal_read(_day: str):
        raise AssertionError("yearly report must not read journal items")

    monkeypatch.setattr(library, "list_reports", monthly_only)
    monkeypatch.setattr(library, "journal_items", reject_journal_read)
    yearly = service.generate_yearly("2026")

    assert yearly.type == "yearly"
    assert yearly.date_range.start == "2026-01-01"
    assert yearly.date_range.end == "2026-12-31"
    assert yearly.sources.previous_reports == [monthly.report_id]
    assert yearly.index.linked_report_ids == [monthly.report_id]
    assert yearly.sources.videos == ["video_BV1CHAIN"]
    assert yearly.video_knowledge[0].video_id == "BV1CHAIN"
    original = next(item for item in yearly.evidence if item.bvid == "BV1CHAIN")
    assert original.chunk_id == "chunk-chain"
    assert original.frame_id == "frame-chain"
    assert original.timestamp == "00:02:00"
    assert original.quote == "证据来自视频原文与画面。"
    report_sources = {item.source_id for item in yearly.evidence if item.source_type == "report"}
    assert {daily.report_id, weekly.report_id, monthly.report_id}.issubset(report_sources)
    assert "monthly" in calls
    assert "daily" not in calls and "weekly" not in calls
    assert library.get_item("report_yearly_2026").links.child_reports == [monthly.report_id]
    assert library.list_tasks()[0].type == "yearly_report"
    assert library.list_tasks()[0].source_count == 1
    assert (tmp_path / "library" / "reports" / "yearly" / "2026.json").is_file()
    assert (tmp_path / "library" / "reports" / "yearly" / "2026.md").is_file()


def test_yearly_report_excludes_months_from_other_years(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library")
    service = ReportService(library)
    service.generate_monthly("2025-12")
    service.generate_monthly("2026-01")
    service.generate_monthly("2027-01")

    yearly = service.generate_yearly("2026")

    assert yearly.sources.previous_reports == ["monthly_2026-01"]


def test_invalid_year_is_rejected_before_task_creation(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library")

    try:
        ReportService(library).generate_yearly("26")
    except ValueError as error:
        assert "YYYY" in str(error)
    else:
        raise AssertionError("invalid year should fail")

    assert library.list_tasks() == []
