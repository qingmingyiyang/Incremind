import calendar
from datetime import datetime, time, timedelta, timezone
import re


_DEFAULT = {'frequency': 'daily', 'time': '00:00', 'timezone': 'UTC', 'weekday': 0, 'month_day': 1}
_ZONES = {'UTC': timezone.utc, 'Asia/Shanghai': timezone(timedelta(hours=8), 'Asia/Shanghai')}
_TIME = re.compile(r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')


def _settings(value):
    if value is None:
        return dict(_DEFAULT)
    if (not isinstance(value, dict) or not _DEFAULT.keys() <= value.keys()
            or not isinstance(value['frequency'], str) or value['frequency'] not in {'daily', 'weekly', 'monthly', 'off'}
            or not isinstance(value['time'], str) or not _TIME.fullmatch(value['time'])
            or not isinstance(value['timezone'], str) or value['timezone'] not in _ZONES or type(value['weekday']) is not int
            or not 0 <= value['weekday'] <= 6 or type(value['month_day']) is not int
            or not 1 <= value['month_day'] <= 31):
        raise ValueError('backup_schedule_invalid')
    return {key: value[key] for key in _DEFAULT}


def _instant(value, settings):
    instant = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(instant, datetime) or instant.tzinfo is None:
        raise ValueError('backup_schedule_clock_invalid')
    return instant.astimezone(_ZONES[settings['timezone']])


def _scheduled(day, settings):
    return datetime.combine(day, time.fromisoformat(settings['time']), _ZONES[settings['timezone']])


def v1(request):
    settings = _settings(request.get('settings'))
    if request['operation'] == 'settings':
        return settings
    now = _instant(request['now'], settings)
    frequency = settings['frequency']
    if frequency == 'off':
        return None
    day = now.date()
    if frequency == 'daily':
        key = day.isoformat()
    elif frequency == 'weekly':
        monday = day - timedelta(days=day.weekday())
        key, day = 'week-' + monday.isoformat(), monday + timedelta(days=settings['weekday'])
    else:
        day = day.replace(day=min(settings['month_day'], calendar.monthrange(day.year, day.month)[1]))
        key = 'month-' + day.strftime('%Y-%m')
    scheduled = _scheduled(day, settings)
    if request['operation'] == 'slot':
        return {'key': key, 'scheduled_for': scheduled.astimezone(timezone.utc).isoformat(),
                'due': now >= scheduled, 'timezone': settings['timezone']}
    if request['operation'] != 'next':
        raise ValueError('backup_schedule_operation_invalid')
    if request.get('completed'):
        if frequency == 'daily':
            day += timedelta(days=1)
        elif frequency == 'weekly':
            day += timedelta(days=7)
        else:
            year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
            day = day.replace(year=year, month=month, day=min(settings['month_day'], calendar.monthrange(year, month)[1]))
        scheduled = _scheduled(day, settings)
    return scheduled.astimezone(timezone.utc).isoformat()
