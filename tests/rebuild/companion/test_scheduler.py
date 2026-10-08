from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from core.companion_core.errors import CompanionRepositoryError
from core.companion_core.scheduler import (
    CompanionEvent,
    CompanionScheduler,
    EventPriority,
    InMemorySchedulerStore,
    ScheduledTrigger,
    SchedulerMode,
)


UTC = timezone.utc


class FakeClock:
    def __init__(self, wall: datetime) -> None:
        self.wall = wall
        self.mono = 0.0

    def wall_now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, delta: timedelta) -> None:
        self.wall += delta
        self.mono += delta.total_seconds()

    def set_wall(self, wall: datetime) -> None:
        self.wall = wall


def event_for(
    scheduled_for: datetime,
    dedupe_key: str,
    *,
    event_id: str | None = None,
    priority: EventPriority = EventPriority.AMBIENT,
    requires_ack: bool = False,
) -> CompanionEvent:
    return CompanionEvent(
        event_id=event_id or f"evt-{dedupe_key}",
        kind="test_due",
        priority=priority,
        created_at=scheduled_for,
        expires_at=scheduled_for + timedelta(hours=6),
        dedupe_key=f"event:{dedupe_key}",
        visual_state="attention",
        text="有界提醒",
        actions=("ack",) if requires_ack else (),
        requires_ack=requires_ack,
    )


def trigger_at(
    when: datetime,
    *,
    factory=event_for,
    interval: timedelta = timedelta(hours=1),
    recovery_policy: str = "catch_up",
    catch_up_window: timedelta = timedelta(hours=3),
    max_attempts: int = 3,
    backoff: timedelta = timedelta(seconds=5),
) -> ScheduledTrigger:
    return ScheduledTrigger(
        trigger_id="hourly",
        next_run_at=when,
        interval=interval,
        event_factory=factory,
        dedupe_factory=lambda due: f"hourly:{due.isoformat()}",
        recovery_policy=recovery_policy,
        catch_up_window=catch_up_window,
        max_attempts=max_attempts,
        backoff=backoff,
    )


def test_crosses_hour_and_day_once_with_duplicate_ticks() -> None:
    clock = FakeClock(datetime(2026, 7, 19, 23, 59, 59, tzinfo=UTC))
    scheduler = CompanionScheduler(clock=clock)
    scheduler.register(trigger_at(datetime(2026, 7, 20, 0, 0, tzinfo=UTC)))

    assert scheduler.tick() == 0
    clock.advance(timedelta(seconds=1))
    assert scheduler.tick() == 1
    assert scheduler.tick() == 0
    assert scheduler.next_event() is not None
    assert scheduler.next_event() is None


def test_dst_spring_forward_uses_utc_occurrence_identity() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    first = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)
    clock = FakeClock(first)
    seen: list[tuple[int, str]] = []

    def factory(scheduled_for: datetime, dedupe_key: str) -> CompanionEvent:
        seen.append((scheduled_for.astimezone(berlin).hour, dedupe_key))
        return event_for(scheduled_for, dedupe_key)

    scheduler = CompanionScheduler(clock=clock)
    scheduler.register(trigger_at(first, factory=factory))
    assert scheduler.tick() == 1
    clock.advance(timedelta(hours=1))
    assert scheduler.tick() == 1
    assert [hour for hour, _ in seen] == [1, 3]
    assert seen[0][1] != seen[1][1]


def test_two_hour_sleep_catches_up_once_but_skip_policy_drops_cosmetic() -> None:
    due = datetime(2026, 7, 19, 9, 0, tzinfo=UTC)
    clock = FakeClock(due - timedelta(seconds=1))
    catch_up = CompanionScheduler(clock=clock)
    catch_up.register(trigger_at(due, catch_up_window=timedelta(hours=3)))
    clock.advance(timedelta(hours=2, seconds=1))
    assert catch_up.tick() == 1
    assert catch_up.tick() == 0

    clock = FakeClock(due + timedelta(hours=2))
    skip = CompanionScheduler(clock=clock)
    skip.register(trigger_at(due, recovery_policy="skip_missed"))
    assert skip.tick() == 0


def test_wall_clock_rollback_cannot_repeat_a_completed_occurrence() -> None:
    due = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    clock = FakeClock(due)
    scheduler = CompanionScheduler(clock=clock)
    scheduler.register(trigger_at(due))
    assert scheduler.tick() == 1
    clock.advance(timedelta(hours=1))
    assert scheduler.tick() == 1
    clock.set_wall(due)
    clock.mono += 1
    assert scheduler.tick() == 0


def test_monotonic_clock_rollback_fails_closed() -> None:
    clock = FakeClock(datetime(2026, 7, 19, 10, 0, tzinfo=UTC))
    scheduler = CompanionScheduler(clock=clock)
    scheduler.tick()
    clock.mono = -1
    with pytest.raises(CompanionRepositoryError, match="monotonic"):
        scheduler.tick()


def test_failed_trigger_retries_same_dedupe_with_exponential_backoff() -> None:
    due = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    clock = FakeClock(due)
    calls: list[str] = []

    def flaky(scheduled_for: datetime, dedupe_key: str) -> CompanionEvent:
        calls.append(dedupe_key)
        if len(calls) < 3:
            raise RuntimeError("temporary")
        return event_for(scheduled_for, dedupe_key)

    scheduler = CompanionScheduler(clock=clock)
    scheduler.register(trigger_at(due, factory=flaky, backoff=timedelta(seconds=5)))
    assert scheduler.tick() == 0
    clock.advance(timedelta(seconds=4))
    assert scheduler.tick() == 0
    clock.advance(timedelta(seconds=1))
    assert scheduler.tick() == 0
    clock.advance(timedelta(seconds=9))
    assert scheduler.tick() == 0
    clock.advance(timedelta(seconds=1))
    assert scheduler.tick() == 1
    assert len(set(calls)) == 1


def test_concurrent_lease_has_one_winner_and_expired_lease_can_be_reclaimed() -> None:
    store = InMemorySchedulerStore()
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    barrier = threading.Barrier(3)
    results: list[tuple[str, int | None]] = []

    def claim(owner: str) -> None:
        barrier.wait()
        results.append((owner, store.claim("same", owner=owner, now=now, lease_for=timedelta(seconds=10))))

    threads = [threading.Thread(target=claim, args=(owner,)) for owner in ("one", "two")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sum(attempt is not None for _, attempt in results) == 1
    loser = next(owner for owner, attempt in results if attempt is None)
    assert store.claim("same", owner=loser, now=now + timedelta(seconds=11), lease_for=timedelta(seconds=10)) == 2


def test_event_storm_presents_highest_priority_then_fifo() -> None:
    store = InMemorySchedulerStore()
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    for index, priority in enumerate(
        [EventPriority.COSMETIC, EventPriority.AMBIENT, EventPriority.CRITICAL, EventPriority.INTERACTIVE]
    ):
        assert store.enqueue(event_for(now, f"key-{index}", event_id=f"evt-{index}", priority=priority))
    order = []
    while (event := store.next_event(now=now, mode=SchedulerMode())) is not None:
        order.append(event.priority)
    assert order == [
        EventPriority.CRITICAL,
        EventPriority.INTERACTIVE,
        EventPriority.AMBIENT,
        EventPriority.COSMETIC,
    ]


@pytest.mark.parametrize(
    ("mode", "allowed"),
    [
        (SchedulerMode(quiet=True), {EventPriority.CRITICAL, EventPriority.INTERACTIVE}),
        (SchedulerMode(game=True), {EventPriority.CRITICAL, EventPriority.INTERACTIVE}),
        (SchedulerMode(sleep=True), {EventPriority.CRITICAL}),
    ],
)
def test_modes_suppress_only_their_disallowed_priorities(mode: SchedulerMode, allowed: set[EventPriority]) -> None:
    store = InMemorySchedulerStore()
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    for priority in EventPriority:
        store.enqueue(event_for(now, priority.name, event_id=f"evt-{priority.name}", priority=priority))
    visible = []
    while (event := store.next_event(now=now, mode=mode)) is not None:
        visible.append(event.priority)
    assert set(visible) == allowed


def test_unacknowledged_critical_recovers_but_completed_cosmetic_does_not() -> None:
    store = InMemorySchedulerStore()
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    critical = event_for(now, "reminder", event_id="evt-reminder", priority=EventPriority.CRITICAL, requires_ack=True)
    cosmetic = event_for(now, "idle", event_id="evt-idle", priority=EventPriority.COSMETIC)
    store.enqueue(critical)
    store.enqueue(cosmetic)
    assert store.next_event(now=now, mode=SchedulerMode()).event_id == "evt-reminder"
    assert store.next_event(now=now, mode=SchedulerMode()).event_id == "evt-idle"
    assert store.recover_pending(now=now) == (critical,)
    assert store.next_event(now=now, mode=SchedulerMode()).event_id == "evt-reminder"
    assert store.acknowledge("evt-reminder") is True
    assert store.recover_pending(now=now) == ()
    assert store.event_state("evt-idle") == "completed"


def test_scheduler_recreation_with_same_authority_recovers_only_unacknowledged_event() -> None:
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    clock = FakeClock(now)
    store = InMemorySchedulerStore()
    first = CompanionScheduler(clock=clock, store=store, owner_id="first")
    store.enqueue(event_for(now, "persisted-reminder", event_id="evt-persisted", priority=EventPriority.CRITICAL, requires_ack=True))
    assert first.next_event().event_id == "evt-persisted"

    restarted = CompanionScheduler(clock=clock, store=store, owner_id="restarted")
    assert restarted.recover_pending()[0].event_id == "evt-persisted"
    assert restarted.next_event().event_id == "evt-persisted"
    assert restarted.acknowledge("evt-persisted") is True
    assert restarted.recover_pending() == ()


def test_event_envelope_rejects_unbounded_or_sensitive_shapes() -> None:
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    with pytest.raises(CompanionRepositoryError, match="text"):
        replace(event_for(now, "long"), text="x" * 401)


def test_single_owned_thread_start_and_bounded_stop() -> None:
    clock = FakeClock(datetime(2026, 7, 19, 10, 0, tzinfo=UTC))
    scheduler = CompanionScheduler(clock=clock, tick_seconds=0.01)
    assert scheduler.start() is True
    assert scheduler.start() is False
    assert scheduler.stop(timeout=1) is True
    assert scheduler.stop(timeout=1) is True
