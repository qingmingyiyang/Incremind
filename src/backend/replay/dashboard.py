from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Callable

from backend.replay.contracts import DashboardReportState, DateRange, ReplayDashboard, Report
from backend.replay.library import ReplayLibrary


class ReplayDashboardService:
    def __init__(self, library: ReplayLibrary, *, today_provider: Callable[[], date] | None = None) -> None:
        self.library = library
        self._today_provider = today_provider or date.today

    def build(self) -> ReplayDashboard:
        today = self._today_provider()
        today_text = today.isoformat()
        today_items = self.library.journal_items(today_text)
        self.library.refresh_indexes()
        yesterday = today - timedelta(days=1)
        iso = today.isocalendar()
        week_start = date.fromisocalendar(iso.year, iso.week, 1)
        week_end = date.fromisocalendar(iso.year, iso.week, 7)
        states = [
            self._state("today_daily", "daily", f"daily_{today_text}", today, today),
            self._state("yesterday_daily", "daily", f"daily_{yesterday.isoformat()}", yesterday, yesterday),
            self._state("current_week", "weekly", f"weekly_{iso.year}-W{iso.week:02d}", week_start, week_end),
            self._state(
                "current_month",
                "monthly",
                f"monthly_{today.strftime('%Y-%m')}",
                today.replace(day=1),
                _month_end(today),
            ),
            self._state("current_year", "yearly", f"yearly_{today.year}", date(today.year, 1, 1), date(today.year, 12, 31)),
        ]
        tasks = self.library.list_tasks()
        counts = Counter(item.type for item in today_items)
        return ReplayDashboard(
            series_id=self.library.series_id or "default",
            today=today_text,
            journal_path=str(self.library.journal_dir(today_text)),
            journal_ready=True,
            today_item_count=len(today_items),
            today_counts=dict(counts),
            report_states=states,
            missing_or_outdated=[state.key for state in states if state.status != "ready"],
            recent_items=self.library.list_items()[:10],
            active_tasks=[task for task in tasks if task.status in {"pending", "running"}],
            failed_tasks=[task for task in tasks if task.status == "failed"][:10],
        )

    def _state(self, key: str, report_type: str, report_id: str, start: date, end: date) -> DashboardReportState:
        report = self.library.get_report(report_id)
        sources = self._direct_sources(report_type, start, end)
        if report is None:
            status = "missing"
            updated_at = ""
        else:
            status = "outdated" if self._outdated(report, sources) else "ready"
            updated_at = report.updated_at
        return DashboardReportState(
            key=key,
            type=report_type,
            report_id=report_id,
            status=status,
            date_range=DateRange(start=start.isoformat(), end=end.isoformat()),
            source_count=len(sources),
            updated_at=updated_at,
        )

    def _direct_sources(self, report_type: str, start: date, end: date):
        if report_type == "daily":
            return self.library.journal_items(start.isoformat())
        upstream = {"weekly": "daily", "monthly": "weekly", "yearly": "monthly"}[report_type]
        return [
            report
            for report in self.library.list_reports(upstream)
            if date.fromisoformat(report.date_range.start) <= end
            and date.fromisoformat(report.date_range.end) >= start
        ]

    def _outdated(self, report: Report, sources: list) -> bool:
        if report.type == "daily":
            expected = {item.id for item in sources}
            if expected != set(report.index.linked_item_ids):
                return True
        else:
            expected = [item.report_id for item in sources]
            if expected != report.sources.previous_reports:
                return True
        if any(item.updated_at > report.updated_at for item in sources):
            return True
        for source in sources:
            if isinstance(source, Report):
                nested_start = date.fromisoformat(source.date_range.start)
                nested_end = date.fromisoformat(source.date_range.end)
                if self._outdated(source, self._direct_sources(source.type, nested_start, nested_end)):
                    return True
        return False


def _month_end(value: date) -> date:
    next_month = value.replace(day=28) + timedelta(days=4)
    return next_month.replace(day=1) - timedelta(days=1)
