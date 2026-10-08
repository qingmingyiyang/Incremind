from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.companion_core.errors import CompanionConflict, CompanionRepositoryError
from core.companion_core.reminders import CompanionReminderService, make_hourly_event
from core.companion_core.repository import CompanionRepository
from core.companion_core.scheduler import CompanionEvent, CompanionScheduler, ScheduledTrigger


class SystemSchedulerClock:
    def wall_now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


class CompanionSchedulerRuntime:
    """Owns the single business ticker and the bounded desktop event drain."""

    def __init__(self, data_root: Path, *, clock: Any | None = None) -> None:
        self.clock = clock or SystemSchedulerClock()
        self.repository = CompanionRepository.at_data_root(data_root)
        self.reminders = CompanionReminderService(self.repository, now=self.clock.wall_now)
        self.scheduler = CompanionScheduler(clock=self.clock, tick_seconds=1.0)
        self._lock = threading.RLock()
        self._revisions: dict[str, int] = {}
        self._last_actions: dict[str, str] = {}
        now = self.clock.wall_now()
        next_second = now.replace(microsecond=0) + timedelta(seconds=1)
        self.scheduler.register(
            ScheduledTrigger(
                trigger_id="companion-runtime-second",
                next_run_at=next_second,
                interval=timedelta(seconds=1),
                event_factory=self._events_for_second,
                dedupe_factory=lambda scheduled: f"tick:{scheduled.isoformat()}",
                recovery_policy="skip_missed",
            )
        )

    def start(self) -> bool:
        self.repository.initialize()
        return self.scheduler.start()

    def stop(self) -> bool:
        return self.scheduler.stop()

    def next_event(self) -> CompanionEvent | None:
        event = self.scheduler.next_event()
        if event is None or not event.kind.startswith("reminder_"):
            return event
        with self._lock:
            revision = self._revisions.get(event.event_id)
        if revision is None:
            self.scheduler.discard(event.event_id)
            return None
        try:
            saved = self.reminders.present(
                event_id=event.event_id, expected_revision=revision, requires_ack=event.requires_ack
            )
        except (CompanionConflict, CompanionRepositoryError):
            self.scheduler.discard(event.event_id)
            return None
        with self._lock:
            self._revisions[event.event_id] = saved.revision
        return event

    def act(self, event_id: str, action: str) -> dict[str, object]:
        if action not in {"acknowledge", "snooze_5m", "complete"}:
            raise CompanionRepositoryError("reminder action is invalid")
        with self._lock:
            if self._last_actions.get(event_id) == action:
                return {"status": "already_performed", "event_id": event_id, "action": action}
            revision = self._revisions.get(event_id)
        if revision is None:
            raise CompanionConflict("reminder event is not awaiting action")
        saved = self.reminders.act(event_id=event_id, action=action, expected_revision=revision)
        self.scheduler.discard(event_id)
        with self._lock:
            self._revisions[event_id] = saved.revision
            self._last_actions[event_id] = action
            if len(self._last_actions) > 512:
                oldest = next(iter(self._last_actions))
                self._last_actions.pop(oldest, None)
        return {"status": saved.state, "event_id": event_id, "action": action}

    def _events_for_second(self, scheduled_for: datetime, _dedupe_key: str) -> tuple[CompanionEvent, ...]:
        events: list[CompanionEvent] = []
        local = scheduled_for.astimezone()
        if local.minute == 0 and local.second == 0:
            events.append(make_hourly_event(local))
        dispatches = self.reminders.due_dispatches()
        with self._lock:
            for dispatch in dispatches:
                self._revisions[dispatch.event.event_id] = dispatch.occurrence_revision
                events.append(dispatch.event)
        return tuple(events)


def serialize_companion_event(event: CompanionEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "kind": event.kind,
        "priority": event.priority.name.lower(),
        "visual_state": event.visual_state,
        "text": event.text,
        "actions": list(event.actions),
        "requires_ack": event.requires_ack,
        "sound_key": event.sound_key,
    }
