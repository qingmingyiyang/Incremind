from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import CompanionRepositoryError
from .models import CompanionReminder, CompanionReminderOccurrence
from .repository import CompanionRepository
from .scheduler import CompanionEvent, EventPriority


_TITLE_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class ReminderDispatch:
    event: CompanionEvent
    occurrence_revision: int


def hourly_key(local_time: datetime) -> str:
    if not isinstance(local_time, datetime) or local_time.tzinfo is None:
        raise CompanionRepositoryError("hourly time must be timezone-aware")
    return f"hour:{local_time.strftime('%Y-%m-%d-%H')}"


def hourly_text(local_time: datetime) -> str:
    hour = local_time.hour
    if hour == 0:
        return "午夜十二点了。该休息的话，就把今天轻轻放下吧。"
    if 1 <= hour < 6:
        return f"凌晨 {hour} 点。夜已经很深了，别忘记照顾自己。"
    if 6 <= hour < 9:
        return f"早上 {hour} 点。新的一天，慢慢开始吧。"
    if 9 <= hour < 12:
        return f"上午 {hour} 点。现在的节奏还舒服吗？"
    if hour == 12:
        return "中午十二点了。记得吃饭，也让眼睛休息一下。"
    if 13 <= hour < 18:
        return f"下午 {hour - 12} 点。喝口水，再继续也不迟。"
    if 18 <= hour < 22:
        return f"晚上 {hour - 12} 点。今天已经走过很长一段啦。"
    return f"深夜 {hour - 12} 点。该收尾的话，我陪你一起。"


def make_hourly_event(local_time: datetime) -> CompanionEvent:
    key = hourly_key(local_time)
    created_at = local_time.astimezone(timezone.utc)
    return CompanionEvent(
        event_id=key,
        kind="hourly_chime",
        priority=EventPriority.AMBIENT,
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=2),
        dedupe_key=key,
        visual_state="speaking",
        text=hourly_text(local_time),
        sound_key="hourly-default",
    )


class CompanionReminderService:
    def __init__(
        self,
        repository: CompanionRepository,
        *,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.repository = repository
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.id_factory = id_factory or (lambda: f"rem_{uuid.uuid4().hex}")

    def create(
        self,
        *,
        title: object,
        scheduled_at: object,
        timezone_name: object,
        advance_minutes: object,
        recurrence: object,
        repeat_count: object,
    ) -> CompanionReminder:
        clean_title = _title(title)
        first = _utc_datetime("scheduled_at", scheduled_at)
        now = self.now()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now) or first <= now:
            raise CompanionRepositoryError("reminder must be scheduled in the future")
        zone = _zone(timezone_name)
        if advance_minutes not in {0, 5} or recurrence not in {"once", "daily"}:
            raise CompanionRepositoryError("reminder schedule options are invalid")
        if not isinstance(repeat_count, int) or isinstance(repeat_count, bool):
            raise CompanionRepositoryError("reminder repeat count is invalid")
        if recurrence == "once" and repeat_count != 1:
            raise CompanionRepositoryError("one-time reminder count must be one")
        if recurrence == "daily" and not 2 <= repeat_count <= 365:
            raise CompanionRepositoryError("daily reminder count must be between 2 and 365")
        reminder_id = self.id_factory()
        local_first = first.astimezone(zone)
        occurrences: list[dict[str, str]] = []
        for index in range(repeat_count):
            local_date = local_first.date() + timedelta(days=index)
            local_due = datetime.combine(local_date, time(local_first.hour, local_first.minute), tzinfo=zone)
            due = local_due.astimezone(timezone.utc)
            scheduled = due.isoformat()
            if advance_minutes:
                occurrences.append(_occurrence(reminder_id, scheduled, due - timedelta(minutes=advance_minutes), "advance"))
            occurrences.append(_occurrence(reminder_id, scheduled, due, "due"))
        next_fire_at = min(item["fire_at"] for item in occurrences)
        schedule = {
            "schema_version": 1,
            "title": clean_title,
            "timezone": zone.key,
            "scheduled_at": first.isoformat(),
            "recurrence": recurrence,
            "repeat_count": repeat_count,
        }
        self.repository.initialize()
        return self.repository.create_reminder(
            reminder_id=reminder_id,
            schedule=schedule,
            advance_minutes=advance_minutes,
            next_fire_at=next_fire_at,
            occurrences=tuple(occurrences),
            updated_at=now.isoformat(),
        )

    def list(self) -> tuple[CompanionReminder, ...]:
        self.repository.initialize()
        return self.repository.list_reminders()

    def cancel(self, *, reminder_id: str, expected_revision: int) -> CompanionReminder:
        self.repository.initialize()
        return self.repository.cancel_reminder(
            reminder_id=reminder_id, expected_revision=expected_revision, updated_at=self.now().isoformat()
        )

    def due_events(self) -> tuple[CompanionEvent, ...]:
        return tuple(item.event for item in self.due_dispatches())

    def due_dispatches(self) -> tuple[ReminderDispatch, ...]:
        now = self.now()
        self.repository.initialize()
        due = self.repository.list_due_reminder_occurrences(
            now=now.isoformat(), oldest_meaningful=(now - timedelta(hours=24)).isoformat()
        )
        return tuple(
            ReminderDispatch(_reminder_event(occurrence, reminder, now), occurrence.revision)
            for occurrence, reminder in due
        )

    def present(self, *, event_id: str, expected_revision: int, requires_ack: bool) -> CompanionReminderOccurrence:
        self.repository.initialize()
        return self.repository.present_reminder_occurrence(
            occurrence_id=event_id,
            expected_revision=expected_revision,
            requires_ack=requires_ack,
            updated_at=self.now().isoformat(),
        )

    def act(self, *, event_id: str, action: str, expected_revision: int) -> CompanionReminderOccurrence:
        self.repository.initialize()
        return self.repository.act_on_reminder_occurrence(
            occurrence_id=event_id, action=action, expected_revision=expected_revision, now=self.now().isoformat()
        )


def _reminder_event(
    occurrence: CompanionReminderOccurrence, reminder: CompanionReminder, now: datetime
) -> CompanionEvent:
    title = reminder.schedule.get("title")
    if not isinstance(title, str) or not title:
        raise CompanionRepositoryError("stored reminder title is invalid")
    requires_ack = occurrence.phase == "due"
    return CompanionEvent(
        event_id=occurrence.occurrence_id,
        kind="reminder_due" if requires_ack else "reminder_advance",
        priority=EventPriority.CRITICAL if requires_ack else EventPriority.INTERACTIVE,
        created_at=now,
        expires_at=now + (timedelta(hours=24) if requires_ack else timedelta(minutes=5)),
        dedupe_key=occurrence.occurrence_id,
        visual_state="attention" if requires_ack else "speaking",
        text=f"时间到了：{title}" if requires_ack else f"还有 5 分钟：{title}",
        actions=("acknowledge", "snooze_5m", "complete") if requires_ack else (),
        requires_ack=requires_ack,
        sound_key="reminder-default" if requires_ack else None,
    )


def _title(value: object) -> str:
    title = value.strip() if isinstance(value, str) else ""
    if not title or len(title) > 200 or _TITLE_CONTROL.search(title):
        raise CompanionRepositoryError("reminder title is invalid")
    return title


def _utc_datetime(label: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise CompanionRepositoryError(f"{label} must be UTC ISO 8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CompanionRepositoryError(f"{label} must be UTC ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise CompanionRepositoryError(f"{label} must be UTC ISO 8601")
    return parsed


def _zone(value: object) -> ZoneInfo:
    if not isinstance(value, str) or not value or len(value) > 80 or "\x00" in value:
        raise CompanionRepositoryError("reminder timezone is invalid")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CompanionRepositoryError("reminder timezone is invalid") from exc


def _occurrence(reminder_id: str, scheduled_for: str, fire_at: datetime, phase: str) -> dict[str, str]:
    digest = hashlib.sha256(f"{reminder_id}|{scheduled_for}|{phase}".encode("utf-8")).hexdigest()[:32]
    return {
        "occurrence_id": f"occ_{digest}",
        "scheduled_for": scheduled_for,
        "fire_at": fire_at.isoformat(),
        "phase": phase,
    }
