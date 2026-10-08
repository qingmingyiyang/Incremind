from datetime import datetime, timedelta, timezone

import pytest

from core.companion_core import CompanionConflict, CompanionRepository, CompanionRepositoryError
from core.companion_core.reminders import CompanionReminderService, hourly_key, hourly_text, make_hourly_event


NOW = datetime(2026, 3, 27, 0, 0, tzinfo=timezone.utc)


def service_for(tmp_path):
    return CompanionReminderService(
        CompanionRepository.at_data_root(tmp_path), now=lambda: NOW, id_factory=lambda: "rem_test",
    )


def test_one_time_reminder_generates_distinct_advance_and_due_occurrences(tmp_path) -> None:
    service = service_for(tmp_path)
    item = service.create(
        title="  项目复盘  ", scheduled_at="2026-03-27T08:00:00+00:00", timezone_name="Asia/Shanghai",
        advance_minutes=5, recurrence="once", repeat_count=1,
    )
    occurrences = service.repository.list_reminder_occurrences(item.reminder_id)
    assert item.schedule["title"] == "项目复盘"
    assert item.next_fire_at == "2026-03-27T07:55:00+00:00"
    assert [(value.phase, value.fire_at) for value in occurrences] == [
        ("advance", "2026-03-27T07:55:00+00:00"), ("due", "2026-03-27T08:00:00+00:00"),
    ]
    assert len({value.occurrence_id for value in occurrences}) == 2


def test_daily_reminder_preserves_local_wall_time_across_dst(tmp_path) -> None:
    service = service_for(tmp_path)
    item = service.create(
        title="晨间计划", scheduled_at="2026-03-27T08:30:00+00:00", timezone_name="Europe/Berlin",
        advance_minutes=0, recurrence="daily", repeat_count=3,
    )
    occurrences = service.repository.list_reminder_occurrences(item.reminder_id)
    assert [value.scheduled_for for value in occurrences] == [
        "2026-03-27T08:30:00+00:00", "2026-03-28T08:30:00+00:00", "2026-03-29T07:30:00+00:00",
    ]


@pytest.mark.parametrize("changes", [
    {"title": ""}, {"title": "x\nsecret"}, {"timezone_name": "Mars/Base"},
    {"scheduled_at": "2026-03-26T08:00:00+00:00"}, {"scheduled_at": "2026-03-28T08:00:00"},
    {"advance_minutes": 7}, {"recurrence": "weekly"}, {"recurrence": "daily", "repeat_count": 1},
])
def test_invalid_reminders_fail_closed(tmp_path, changes) -> None:
    values = dict(title="事项", scheduled_at="2026-03-28T08:00:00+00:00", timezone_name="UTC", advance_minutes=5, recurrence="once", repeat_count=1)
    values.update(changes)
    with pytest.raises(CompanionRepositoryError):
        service_for(tmp_path).create(**values)


def test_cancel_is_cas_and_cancels_pending_occurrences(tmp_path) -> None:
    service = service_for(tmp_path)
    item = service.create(title="事项", scheduled_at="2026-03-28T08:00:00+00:00", timezone_name="UTC", advance_minutes=5, recurrence="once", repeat_count=1)
    with pytest.raises(CompanionConflict):
        service.cancel(reminder_id=item.reminder_id, expected_revision=2)
    cancelled = service.cancel(reminder_id=item.reminder_id, expected_revision=1)
    assert cancelled.ack_state == "cancelled" and cancelled.next_fire_at is None
    assert {value.state for value in service.repository.list_reminder_occurrences(item.reminder_id)} == {"cancelled"}
    assert service.list() == ()


def test_hourly_templates_are_local_deterministic_and_model_independent() -> None:
    local = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    assert hourly_key(local) == "hour:2026-07-20-12"
    assert "吃饭" in hourly_text(local)
    event = make_hourly_event(local)
    assert event.event_id == event.dedupe_key == "hour:2026-07-20-12"
    assert event.sound_key == "hourly-default"
    assert event.text and event.requires_ack is False


def test_due_events_are_bounded_and_actions_are_cas_idempotent(tmp_path) -> None:
    clock = [NOW]
    service = CompanionReminderService(
        CompanionRepository.at_data_root(tmp_path), now=lambda: clock[0], id_factory=lambda: "rem_runtime",
    )
    service.create(
        title="站会", scheduled_at="2026-03-27T00:10:00+00:00", timezone_name="UTC",
        advance_minutes=5, recurrence="once", repeat_count=1,
    )
    clock[0] = NOW + timedelta(minutes=5)
    advance = service.due_events()
    assert len(advance) == 1 and advance[0].kind == "reminder_advance"
    occurrence = service.repository.list_reminder_occurrences("rem_runtime")[0]
    service.present(event_id=advance[0].event_id, expected_revision=occurrence.revision, requires_ack=False)
    assert service.due_events() == ()

    clock[0] = NOW + timedelta(minutes=10)
    due = service.due_events()
    assert len(due) == 1
    assert due[0].priority.name == "CRITICAL"
    assert due[0].actions == ("acknowledge", "snooze_5m", "complete")
    occurrence = service.repository.list_reminder_occurrences("rem_runtime")[1]
    presented = service.present(event_id=due[0].event_id, expected_revision=occurrence.revision, requires_ack=True)
    snoozed = service.act(event_id=due[0].event_id, action="snooze_5m", expected_revision=presented.revision)
    assert snoozed.state == "snoozed"
    assert snoozed.snooze_until == "2026-03-27T00:15:00+00:00"
    assert service.act(event_id=due[0].event_id, action="snooze_5m", expected_revision=presented.revision) == snoozed


def test_due_scan_expires_occurrences_older_than_24_hours(tmp_path) -> None:
    clock = [NOW]
    service = CompanionReminderService(
        CompanionRepository.at_data_root(tmp_path), now=lambda: clock[0], id_factory=lambda: "rem_expired",
    )
    service.create(
        title="旧事项", scheduled_at="2026-03-27T00:01:00+00:00", timezone_name="UTC",
        advance_minutes=0, recurrence="once", repeat_count=1,
    )
    clock[0] = NOW + timedelta(hours=25)
    assert service.due_events() == ()
    occurrence = service.repository.list_reminder_occurrences("rem_expired")[0]
    assert occurrence.state == "expired"
