from __future__ import annotations

import heapq
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from typing import Protocol

from .errors import CompanionConflict, CompanionRepositoryError


class EventPriority(IntEnum):
    COSMETIC = 100
    AMBIENT = 200
    INTERACTIVE = 300
    CRITICAL = 400


_VISUAL_STATES = frozenset(
    {"idle", "happy", "attention", "working", "speaking", "offline", "sleeping", "warning"}
)
_RECOVERY_POLICIES = frozenset({"catch_up", "skip_missed"})


class SchedulerClock(Protocol):
    def wall_now(self) -> datetime: ...

    def monotonic(self) -> float: ...


@dataclass(frozen=True, slots=True)
class CompanionEvent:
    event_id: str
    kind: str
    priority: EventPriority
    created_at: datetime
    expires_at: datetime
    dedupe_key: str
    visual_state: str
    text: str = ""
    actions: tuple[str, ...] = ()
    requires_ack: bool = False
    sound_key: str | None = None

    def __post_init__(self) -> None:
        for label, value, limit in (
            ("event_id", self.event_id, 128),
            ("kind", self.kind, 64),
            ("dedupe_key", self.dedupe_key, 160),
        ):
            if not isinstance(value, str) or not value or len(value) > limit or any(ord(char) < 32 for char in value):
                raise CompanionRepositoryError(f"scheduler {label} is invalid")
        if not isinstance(self.priority, EventPriority):
            raise CompanionRepositoryError("scheduler event priority is invalid")
        _require_utc("created_at", self.created_at)
        _require_utc("expires_at", self.expires_at)
        if self.expires_at <= self.created_at:
            raise CompanionRepositoryError("scheduler event expiry is invalid")
        if self.visual_state not in _VISUAL_STATES:
            raise CompanionRepositoryError("scheduler visual state is invalid")
        if not isinstance(self.text, str) or len(self.text) > 400 or any(char == "\x00" for char in self.text):
            raise CompanionRepositoryError("scheduler event text is invalid")
        if not isinstance(self.actions, tuple) or len(self.actions) > 3:
            raise CompanionRepositoryError("scheduler event actions are invalid")
        if any(not isinstance(action, str) or not action or len(action) > 48 for action in self.actions):
            raise CompanionRepositoryError("scheduler event action is invalid")
        if self.requires_ack and not self.actions:
            raise CompanionRepositoryError("acknowledged scheduler event requires an action")
        if self.sound_key is not None and (
            not isinstance(self.sound_key, str) or not self.sound_key or len(self.sound_key) > 64
        ):
            raise CompanionRepositoryError("scheduler sound key is invalid")


@dataclass(frozen=True, slots=True)
class ScheduledTrigger:
    trigger_id: str
    next_run_at: datetime
    interval: timedelta
    event_factory: Callable[[datetime, str], CompanionEvent | Iterable[CompanionEvent] | None]
    dedupe_factory: Callable[[datetime], str]
    recovery_policy: str = "skip_missed"
    catch_up_window: timedelta = timedelta(minutes=5)
    max_attempts: int = 3
    backoff: timedelta = timedelta(seconds=5)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.trigger_id, str) or not self.trigger_id or len(self.trigger_id) > 96:
            raise CompanionRepositoryError("scheduler trigger id is invalid")
        _require_utc("next_run_at", self.next_run_at)
        if self.interval <= timedelta(0):
            raise CompanionRepositoryError("scheduler interval must be positive")
        if self.recovery_policy not in _RECOVERY_POLICIES:
            raise CompanionRepositoryError("scheduler recovery policy is invalid")
        if self.catch_up_window < timedelta(0) or self.catch_up_window > timedelta(days=7):
            raise CompanionRepositoryError("scheduler catch-up window is invalid")
        if not 1 <= self.max_attempts <= 10:
            raise CompanionRepositoryError("scheduler max attempts is invalid")
        if self.backoff < timedelta(0) or self.backoff > timedelta(hours=1):
            raise CompanionRepositoryError("scheduler backoff is invalid")


@dataclass(frozen=True, slots=True)
class SchedulerMode:
    quiet: bool = False
    game: bool = False
    sleep: bool = False

    def allows(self, priority: EventPriority) -> bool:
        if priority is EventPriority.CRITICAL:
            return True
        if self.sleep:
            return False
        if priority is EventPriority.INTERACTIVE:
            return True
        return not self.quiet and not self.game


@dataclass(slots=True)
class _TriggerState:
    trigger: ScheduledTrigger
    next_run_at: datetime
    pending_scheduled_for: datetime | None = None
    pending_dedupe_key: str | None = None
    resume_next_run_at: datetime | None = None


@dataclass(slots=True)
class _Lease:
    owner: str
    expires_at: datetime
    attempts: int
    next_attempt_at: datetime
    completed: bool = False


@dataclass(order=True, slots=True)
class _QueuedEvent:
    sort_key: tuple[int, float, int]
    event: CompanionEvent = field(compare=False)
    state: str = field(default="queued", compare=False)


class InMemorySchedulerStore:
    """Thread-safe persistence seam used by the scheduler.

    A long-lived application composes one store with one scheduler. A future
    SQLite adapter may implement the same operations without changing trigger
    or queue semantics.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._leases: dict[str, _Lease] = {}
        self._events: dict[str, _QueuedEvent] = {}
        self._dedupe_to_event: dict[str, str] = {}
        self._heap: list[_QueuedEvent] = []
        self._sequence = 0

    def claim(self, dedupe_key: str, *, owner: str, now: datetime, lease_for: timedelta) -> int | None:
        with self._lock:
            current = self._leases.get(dedupe_key)
            if current is not None:
                if current.completed or current.next_attempt_at > now:
                    return None
                if current.expires_at > now and current.owner != owner:
                    return None
                current.owner = owner
                current.expires_at = now + lease_for
                current.attempts += 1
                return current.attempts
            self._leases[dedupe_key] = _Lease(
                owner=owner,
                expires_at=now + lease_for,
                attempts=1,
                next_attempt_at=now,
            )
            return 1

    def complete(self, dedupe_key: str, *, owner: str) -> None:
        with self._lock:
            lease = self._owned_lease(dedupe_key, owner)
            lease.completed = True

    def fail(self, dedupe_key: str, *, owner: str, now: datetime, retry_at: datetime, terminal: bool) -> None:
        with self._lock:
            lease = self._owned_lease(dedupe_key, owner)
            lease.completed = terminal
            lease.expires_at = now
            lease.next_attempt_at = retry_at

    def enqueue(self, event: CompanionEvent) -> bool:
        with self._lock:
            if event.event_id in self._events or event.dedupe_key in self._dedupe_to_event:
                return False
            self._sequence += 1
            queued = _QueuedEvent(
                sort_key=(-int(event.priority), event.created_at.timestamp(), self._sequence),
                event=event,
            )
            self._events[event.event_id] = queued
            self._dedupe_to_event[event.dedupe_key] = event.event_id
            heapq.heappush(self._heap, queued)
            return True

    def next_event(self, *, now: datetime, mode: SchedulerMode) -> CompanionEvent | None:
        with self._lock:
            deferred: list[_QueuedEvent] = []
            selected: _QueuedEvent | None = None
            while self._heap:
                queued = heapq.heappop(self._heap)
                if queued.state != "queued":
                    continue
                if queued.event.expires_at <= now:
                    queued.state = "expired"
                    continue
                if mode.allows(queued.event.priority):
                    selected = queued
                    break
                deferred.append(queued)
            for queued in deferred:
                heapq.heappush(self._heap, queued)
            if selected is None:
                return None
            selected.state = "presented" if selected.event.requires_ack else "completed"
            return selected.event

    def acknowledge(self, event_id: str) -> bool:
        with self._lock:
            queued = self._events.get(event_id)
            if queued is None or queued.state != "presented":
                return False
            queued.state = "completed"
            return True

    def discard(self, event_id: str) -> bool:
        with self._lock:
            queued = self._events.pop(event_id, None)
            if queued is None:
                return False
            queued.state = "completed"
            self._dedupe_to_event.pop(queued.event.dedupe_key, None)
            return True

    def recover_pending(self, *, now: datetime) -> tuple[CompanionEvent, ...]:
        with self._lock:
            recovered = []
            for queued in self._events.values():
                if queued.state == "presented" and queued.event.requires_ack and queued.event.expires_at > now:
                    queued.state = "queued"
                    heapq.heappush(self._heap, queued)
                    recovered.append(queued.event)
            return tuple(recovered)

    def event_state(self, event_id: str) -> str | None:
        with self._lock:
            queued = self._events.get(event_id)
            return queued.state if queued is not None else None

    def _owned_lease(self, dedupe_key: str, owner: str) -> _Lease:
        lease = self._leases.get(dedupe_key)
        if lease is None or lease.owner != owner:
            raise CompanionConflict("scheduler lease is not owned by caller")
        return lease


class CompanionScheduler:
    def __init__(
        self,
        *,
        clock: SchedulerClock,
        store: InMemorySchedulerStore | None = None,
        owner_id: str = "scheduler-main",
        tick_seconds: float = 1.0,
        lease_for: timedelta = timedelta(seconds=30),
        mode_provider: Callable[[], SchedulerMode] = SchedulerMode,
    ) -> None:
        if not owner_id or len(owner_id) > 96:
            raise CompanionRepositoryError("scheduler owner id is invalid")
        if tick_seconds <= 0 or tick_seconds > 60:
            raise CompanionRepositoryError("scheduler tick interval is invalid")
        self.clock = clock
        self.store = store or InMemorySchedulerStore()
        self.owner_id = owner_id
        self.tick_seconds = tick_seconds
        self.lease_for = lease_for
        self.mode_provider = mode_provider
        self._triggers: dict[str, _TriggerState] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_wall: datetime | None = None
        self._last_monotonic: float | None = None

    def register(self, trigger: ScheduledTrigger) -> None:
        with self._lock:
            if trigger.trigger_id in self._triggers:
                raise CompanionConflict("scheduler trigger is already registered")
            self._triggers[trigger.trigger_id] = _TriggerState(trigger, trigger.next_run_at)

    def unregister(self, trigger_id: str) -> bool:
        with self._lock:
            return self._triggers.pop(trigger_id, None) is not None

    def tick(self) -> int:
        now = self.clock.wall_now()
        monotonic_now = self.clock.monotonic()
        _require_utc("clock.wall_now", now)
        if self._last_monotonic is not None and monotonic_now < self._last_monotonic:
            raise CompanionRepositoryError("scheduler monotonic clock moved backwards")
        self._last_monotonic = monotonic_now
        self._last_wall = now
        emitted = 0
        with self._lock:
            states = tuple(self._triggers.values())
        for state in states:
            emitted += self._run_due(state, now)
        return emitted

    def next_event(self) -> CompanionEvent | None:
        return self.store.next_event(now=self.clock.wall_now(), mode=self.mode_provider())

    def acknowledge(self, event_id: str) -> bool:
        return self.store.acknowledge(event_id)

    def discard(self, event_id: str) -> bool:
        return self.store.discard(event_id)

    def recover_pending(self) -> tuple[CompanionEvent, ...]:
        return self.store.recover_pending(now=self.clock.wall_now())

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, name="companion-scheduler", daemon=True)
            self._thread.start()
            return True

    def stop(self, *, timeout: float = 3.0) -> bool:
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, timeout))
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                # A trigger failure is recorded by _run_due. Clock or invariant
                # failures keep the single loop alive for the next bounded tick.
                pass
            self._stop_event.wait(self.tick_seconds)

    def _run_due(self, state: _TriggerState, now: datetime) -> int:
        trigger = state.trigger
        if not trigger.enabled or state.next_run_at > now:
            return 0
        if state.pending_scheduled_for is not None:
            scheduled_for = state.pending_scheduled_for
            dedupe_key = state.pending_dedupe_key
            resume_next = state.resume_next_run_at
            if dedupe_key is None or resume_next is None:
                raise CompanionRepositoryError("scheduler retry state is incomplete")
        else:
            scheduled_for = state.next_run_at
            lateness = now - scheduled_for
            resume_next = state.next_run_at
            while resume_next <= now:
                resume_next += trigger.interval
            state.next_run_at = resume_next
            if trigger.recovery_policy == "skip_missed" and lateness >= trigger.interval:
                return 0
            if trigger.recovery_policy == "catch_up" and lateness > trigger.catch_up_window:
                return 0
            dedupe_key = trigger.dedupe_factory(scheduled_for)
        attempt = self.store.claim(dedupe_key, owner=self.owner_id, now=now, lease_for=self.lease_for)
        if attempt is None:
            return 0
        try:
            produced = trigger.event_factory(scheduled_for, dedupe_key)
            events = _normalize_events(produced)
            emitted = sum(1 for event in events if self.store.enqueue(event))
            self.store.complete(dedupe_key, owner=self.owner_id)
            state.pending_scheduled_for = None
            state.pending_dedupe_key = None
            state.resume_next_run_at = None
            state.next_run_at = resume_next
            return emitted
        except Exception:
            terminal = attempt >= trigger.max_attempts
            retry_at = now + trigger.backoff * (2 ** (attempt - 1))
            self.store.fail(
                dedupe_key,
                owner=self.owner_id,
                now=now,
                retry_at=retry_at,
                terminal=terminal,
            )
            if not terminal:
                state.pending_scheduled_for = scheduled_for
                state.pending_dedupe_key = dedupe_key
                state.resume_next_run_at = resume_next
                state.next_run_at = retry_at
            else:
                state.pending_scheduled_for = None
                state.pending_dedupe_key = None
                state.resume_next_run_at = None
                state.next_run_at = resume_next
            return 0


def _normalize_events(value: CompanionEvent | Iterable[CompanionEvent] | None) -> tuple[CompanionEvent, ...]:
    if value is None:
        return ()
    if isinstance(value, CompanionEvent):
        return (value,)
    events = tuple(value)
    if len(events) > 16 or any(not isinstance(event, CompanionEvent) for event in events):
        raise CompanionRepositoryError("scheduler trigger returned invalid events")
    return events


def _require_utc(label: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise CompanionRepositoryError(f"{label} must use UTC")
