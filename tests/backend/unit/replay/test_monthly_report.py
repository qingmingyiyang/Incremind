from __future__ import annotations

from backend.replay.library import ReplayLibrary
from backend.replay.reports import ReportService


def test_monthly_report_reads_weekly_reports_and_includes_overlapping_week(tmp_path, monkeypatch) -> None:
    library = ReplayLibrary(tmp_path / "library")
    service = ReportService(library)
    for week in ["2026-W26", "2026-W27", "2026-W28"]:
        service.generate_weekly(week)

    original_list_reports = library.list_reports
    calls: list[str | None] = []

    def track_report_reads(report_type=None):
        calls.append(report_type)
        if report_type in {"daily"}:
            raise AssertionError("monthly report must not read daily reports")
        return original_list_reports(report_type)

    def reject_journal_read(_day: str):
        raise AssertionError("monthly report must not read journal items")

    monkeypatch.setattr(library, "list_reports", track_report_reads)
    monkeypatch.setattr(library, "journal_items", reject_journal_read)
    monthly = service.generate_monthly("2026-06")

    assert monthly.type == "monthly"
    assert monthly.date_range.start == "2026-06-01"
    assert monthly.date_range.end == "2026-06-30"
    assert monthly.sources.previous_reports == ["weekly_2026-W27", "weekly_2026-W26"]
    assert monthly.index.linked_report_ids == monthly.sources.previous_reports
    assert "weekly" in calls
    assert "daily" not in calls
    assert library.get_item("report_monthly_2026-06").links.child_reports == monthly.sources.previous_reports
    assert library.list_tasks()[0].type == "monthly_report"
    assert library.list_tasks()[0].source_count == 2
    assert (tmp_path / "library" / "reports" / "monthly" / "2026-06.json").is_file()
    assert (tmp_path / "library" / "reports" / "monthly" / "2026-06.md").is_file()


def test_invalid_month_is_rejected_before_task_creation(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library")

    try:
        ReportService(library).generate_monthly("2026-13")
    except ValueError as error:
        assert "YYYY-MM" in str(error)
    else:
        raise AssertionError("invalid month should fail")

    assert library.list_tasks() == []
