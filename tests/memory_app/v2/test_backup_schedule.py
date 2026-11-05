from datetime import datetime, timezone

import pytest

from backend.memory_app.v2.policies import get


def _call(operation, now, settings=None, **extra):
    return get('backup_schedule', version='@1')({'operation': operation, 'now': now,
        'settings': settings, **extra})


def _settings(**values):
    return {**get('backup_schedule', version='@1')({'operation': 'settings'}), **values}


def test_default_daily_keeps_the_existing_utc_date_identity_and_off_has_no_due_slot():
    assert _call('slot', '2026-11-03T00:00:00+00:00') == {
        'key': '2026-11-03', 'scheduled_for': '2026-11-03T00:00:00+00:00', 'due': True, 'timezone': 'UTC'}
    assert _call('slot', '2026-11-03T12:00:00+00:00', _settings(frequency='off')) is None
    assert _call('next', '2026-11-03T12:00:00+00:00', completed=True) == '2026-11-04T00:00:00+00:00'


def test_weekly_uses_calendar_monday_and_monthly_clamps_to_the_real_last_day():
    weekly = _settings(frequency='weekly', weekday=6, time='03:00')
    assert _call('slot', '2026-11-02T03:00:00+00:00', weekly)['due'] is False
    assert _call('slot', '2026-11-08T03:00:00+00:00', weekly)['key'] == 'week-2026-11-02'
    assert _call('next', '2026-11-08T03:00:00+00:00', weekly, completed=True) == '2026-11-15T03:00:00+00:00'
    monthly = _settings(frequency='monthly', month_day=31, time='03:00')
    assert _call('slot', '2028-02-29T02:59:59+00:00', monthly)['due'] is False
    assert _call('slot', '2028-02-29T03:00:00+00:00', monthly)['key'] == 'month-2028-02'
    assert _call('next', '2028-02-29T03:00:00+00:00', monthly, completed=True) == '2028-03-31T03:00:00+00:00'
    assert _call('next', '2026-03-31T03:00:00+00:00', monthly, completed=True) == '2026-04-30T03:00:00+00:00'


def test_timezone_and_current_period_completion_are_independent_of_later_configuration_time():
    settings = _settings(timezone='Asia/Shanghai', time='03:00')
    slot = _call('slot', datetime(2026, 11, 2, 19, tzinfo=timezone.utc), settings)
    assert slot['key'] == '2026-11-03' and slot['due'] is True
    assert slot['scheduled_for'] == '2026-11-02T19:00:00+00:00'
    assert _call('next', '2026-11-03T04:00:00+00:00', _settings(time='05:00'), completed=True) == '2026-11-04T05:00:00+00:00'


@pytest.mark.parametrize('value', [{'frequency': []}, {'time': '25:00'}, {'timezone': []},
    {'weekday': True}, {'weekday': 7}, {'month_day': 0}, {'month_day': 32}])
def test_invalid_settings_are_refused(value):
    with pytest.raises(ValueError, match='backup_schedule_invalid'):
        get('backup_schedule', version='@1')({'operation': 'settings', 'settings': _settings(**value)})
