from __future__ import annotations

from datetime import date

from backend.replay.contracts import KnowledgeIndex, KnowledgeItem, KnowledgeSource, KnowledgeStatus
from backend.replay.dashboard import ReplayDashboardService
from backend.replay.library import ReplayLibrary
from backend.replay.reports import ReportService


TODAY = date(2026, 6, 23)


def _note(item_id: str, day: str) -> KnowledgeItem:
    return KnowledgeItem(
        id=item_id,
        type="note",
        title=f"{day} 笔记",
        content="自动复盘状态测试。",
        summary="自动复盘状态测试。",
        created_at=f"{day}T10:00:00+08:00",
        updated_at=f"{day}T10:00:00+08:00",
        source=KnowledgeSource(kind="manual"),
        status=KnowledgeStatus(in_daily=True),
        index=KnowledgeIndex(available=True, keywords=["复盘"]),
    )


def test_dashboard_initializes_today_journal_without_generating_reports(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library")
    before_tasks = library.list_tasks()

    dashboard = ReplayDashboardService(library, today_provider=lambda: TODAY).build()

    assert dashboard.journal_ready is True
    assert dashboard.today == "2026-06-23"
    assert dashboard.today_item_count == 0
    assert {state.status for state in dashboard.report_states} == {"missing"}
    assert set(dashboard.missing_or_outdated) == {
        "today_daily",
        "yesterday_daily",
        "current_week",
        "current_month",
        "current_year",
    }
    assert library.list_reports() == []
    assert library.list_tasks() == before_tasks
    assert (tmp_path / "library" / "journals" / "2026" / "06" / "2026-06-23").is_dir()
    assert (tmp_path / "library" / "index" / "search.json").is_file()


def test_dashboard_marks_ready_chain_and_propagates_staleness(tmp_path) -> None:
    library = ReplayLibrary(tmp_path / "library")
    reports = ReportService(library)
    library.save_item(_note("item_yesterday", "2026-06-22"))
    library.save_item(_note("item_today", "2026-06-23"))
    reports.generate_daily("2026-06-22")
    reports.generate_daily("2026-06-23")
    reports.generate_weekly("2026-W26")
    reports.generate_monthly("2026-06")
    reports.generate_yearly("2026")
    service = ReplayDashboardService(library, today_provider=lambda: TODAY)

    ready = service.build()

    assert all(state.status == "ready" for state in ready.report_states)
    assert ready.missing_or_outdated == []
    assert ready.today_counts == {"note": 1}

    library.save_item(_note("item_today_new", "2026-06-23"))
    stale = service.build()
    states = {state.key: state.status for state in stale.report_states}

    assert states["yesterday_daily"] == "ready"
    assert states["today_daily"] == "outdated"
    assert states["current_week"] == "outdated"
    assert states["current_month"] == "outdated"
    assert states["current_year"] == "outdated"
    assert stale.today_item_count == 2
    assert stale.today_counts == {"note": 2}
