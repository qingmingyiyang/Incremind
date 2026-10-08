from __future__ import annotations

from backend.replay.contracts import KnowledgeEvidence, KnowledgeIndex, KnowledgeItem, KnowledgeLinks, KnowledgeSource, KnowledgeStatus
from backend.replay.library import ReplayLibrary
from backend.replay.reports import ReportService


def _video(day: str, suffix: str) -> KnowledgeItem:
    return KnowledgeItem(
        id=f"video_{suffix}",
        type="video",
        title=f"视频 {suffix}",
        content="完整文本保存在视频资料。",
        summary=f"{suffix} 的摘要",
        tags=["Rust", "桌面"],
        created_at=f"{day}T10:00:00+08:00",
        updated_at=f"{day}T10:00:00+08:00",
        source=KnowledgeSource(kind="bilibili", bvid=suffix, local_path=f"library/{suffix}"),
        evidence=KnowledgeEvidence(
            chunk_ids=[f"chunk-{suffix}"],
            frame_ids=[f"frame-{suffix}"],
            timestamps=["00:01:00"],
            quotes=[f"{suffix} 的证据"],
        ),
        links=KnowledgeLinks(linked_video_ids=[suffix]),
        status=KnowledgeStatus(in_daily=True, high_value=True),
        index=KnowledgeIndex(available=True, keywords=["Rust", "桌面"], chunk_ids=[f"chunk-{suffix}"]),
    )


def test_weekly_report_reads_daily_reports_without_reloading_journal_items(tmp_path, monkeypatch) -> None:
    library = ReplayLibrary(tmp_path / "library")
    service = ReportService(library)
    for day, suffix in [("2026-06-22", "BV1A"), ("2026-06-23", "BV1B")]:
        library.save_item(_video(day, suffix))
        service.generate_daily(day)

    def reject_raw_read(_day: str):
        raise AssertionError("weekly report must not read journal items")

    monkeypatch.setattr(library, "journal_items", reject_raw_read)
    weekly = service.generate_weekly("2026-W26")

    assert weekly.type == "weekly"
    assert weekly.date_range.start == "2026-06-22"
    assert weekly.date_range.end == "2026-06-28"
    assert weekly.sources.previous_reports == ["daily_2026-06-23", "daily_2026-06-22"]
    assert {video.video_id for video in weekly.video_knowledge} == {"BV1A", "BV1B"}
    assert weekly.index.linked_report_ids == weekly.sources.previous_reports
    assert {item.source_id for item in weekly.evidence if item.source_type == "report"} == set(weekly.sources.previous_reports)
    original = next(item for item in weekly.evidence if item.bvid == "BV1A")
    assert original.chunk_id == "chunk-BV1A"
    assert original.frame_id == "frame-BV1A"
    assert original.timestamp == "00:01:00"
    assert "Rust" in weekly.summary.main_topics
    assert library.get_item("report_weekly_2026-W26").links.child_reports == weekly.sources.previous_reports
    assert library.list_tasks()[0].type == "weekly_report"
    assert library.list_tasks()[0].source_count == 2
    assert (tmp_path / "library" / "reports" / "weekly" / "2026-W26.json").is_file()
    assert (tmp_path / "library" / "reports" / "weekly" / "2026-W26.md").is_file()


def test_weekly_report_ignores_daily_reports_outside_requested_week(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library")
    service = ReportService(library)
    for day in ["2026-06-21", "2026-06-22", "2026-06-29"]:
        service.generate_daily(day)

    weekly = service.generate_weekly("2026-W26")

    assert weekly.sources.previous_reports == ["daily_2026-06-22"]


def test_invalid_week_is_rejected_before_task_creation(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library")

    try:
        ReportService(library).generate_weekly("2026-W99")
    except ValueError as error:
        assert "YYYY-Www" in str(error)
    else:
        raise AssertionError("invalid ISO week should fail")

    assert library.list_tasks() == []
