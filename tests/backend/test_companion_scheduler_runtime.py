from datetime import datetime, timedelta, timezone

from backend.companion_scheduler_runtime import CompanionSchedulerRuntime, serialize_companion_event


class FakeClock:
    def __init__(self, current: datetime) -> None:
        self.current = current
        self.elapsed = 0.0

    def wall_now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, delta: timedelta) -> None:
        self.current += delta
        self.elapsed += delta.total_seconds()


def test_runtime_uses_one_second_trigger_and_does_not_replay_missed_hour(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 7, 20, 7, 59, 58, tzinfo=timezone.utc))
    runtime = CompanionSchedulerRuntime(tmp_path, clock=clock)
    clock.advance(timedelta(seconds=1))
    assert runtime.scheduler.tick() == 0
    assert runtime.scheduler.tick() == 0
    clock.advance(timedelta(seconds=1))
    assert runtime.scheduler.tick() == 1
    event = runtime.next_event()
    assert event is not None and event.kind == "hourly_chime"
    assert runtime.next_event() is None

    clock.advance(timedelta(hours=2, seconds=5))
    assert runtime.scheduler.tick() == 0
    clock.advance(timedelta(seconds=1))
    assert runtime.scheduler.tick() == 0


def test_runtime_recovers_due_reminder_and_snoozes_exactly_five_minutes(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 7, 20, 8, 0, 0, tzinfo=timezone.utc))
    runtime = CompanionSchedulerRuntime(tmp_path, clock=clock)
    runtime.reminders.create(
        title="提交日报", scheduled_at="2026-07-20T08:01:00+00:00", timezone_name="UTC",
        advance_minutes=0, recurrence="once", repeat_count=1,
    )
    clock.advance(timedelta(minutes=1))
    assert runtime.scheduler.tick() == 0
    clock.advance(timedelta(seconds=1))
    assert runtime.scheduler.tick() == 1
    event = runtime.next_event()
    assert event is not None and event.requires_ack
    assert serialize_companion_event(event)["actions"] == ["acknowledge", "snooze_5m", "complete"]
    result = runtime.act(event.event_id, "snooze_5m")
    assert result["status"] == "snoozed"
    assert runtime.act(event.event_id, "snooze_5m")["status"] == "already_performed"

    clock.advance(timedelta(minutes=5))
    assert runtime.scheduler.tick() == 0
    clock.advance(timedelta(seconds=1))
    assert runtime.scheduler.tick() == 1
    repeated = runtime.next_event()
    assert repeated is not None and repeated.event_id == event.event_id


def test_runtime_restart_recovers_presented_reminder_within_window(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 7, 20, 9, 0, 58, tzinfo=timezone.utc))
    first = CompanionSchedulerRuntime(tmp_path, clock=clock)
    first.reminders.create(
        title="喝水", scheduled_at="2026-07-20T09:01:00+00:00", timezone_name="UTC",
        advance_minutes=0, recurrence="once", repeat_count=1,
    )
    clock.advance(timedelta(seconds=1))
    assert first.scheduler.tick() == 0
    clock.advance(timedelta(seconds=1))
    assert first.scheduler.tick() == 1
    event = first.next_event()
    assert event is not None

    clock.advance(timedelta(seconds=1))
    restarted = CompanionSchedulerRuntime(tmp_path, clock=clock)
    clock.advance(timedelta(seconds=1))
    assert restarted.scheduler.tick() == 1
    recovered = restarted.next_event()
    assert recovered is not None and recovered.event_id == event.event_id
