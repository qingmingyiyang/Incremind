from datetime import date, datetime, time, timedelta, timezone
import re


TIMEZONE = 'Asia/Shanghai'
_ZONE = timezone(timedelta(hours=8), TIMEZONE)
_TIME = re.compile(r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')


def _clock(value):
    instant = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(instant, datetime) or instant.tzinfo is None:
        raise ValueError('inspiration_schedule_clock_invalid')
    return instant.astimezone(_ZONE)


def _settings(value):
    if value is None:
        return {'enabled': True, 'time': '09:00', 'timezone': TIMEZONE}
    if (not isinstance(value, dict) or not {'enabled', 'time', 'timezone'} <= set(value)
            or type(value['enabled']) is not bool or not isinstance(value['time'], str)
            or not _TIME.fullmatch(value['time']) or value['timezone'] != TIMEZONE):
        raise ValueError('inspiration_schedule_settings_invalid')
    return {key: value[key] for key in ('enabled', 'time', 'timezone')}


def v1(request):
    operation = request['operation']
    if operation == 'settings':
        return _settings(request.get('settings'))
    if operation == 'limits':
        return {'batches': 1, 'documents': 3, 'insights': 5, 'wire_attempts': 1}
    if operation == 'covered':
        return any(source['revision'] == request['revision'] or source['markdown'] == request['markdown']
            for source in request['previous'])
    if operation == 'date':
        return _clock(request['now']).date().isoformat()
    if operation == 'job_id':
        value = request['date']
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError('inspiration_schedule_date_invalid')
        return 'automatic-inspiration-' + value
    if operation in {'slot', 'due'}:
        settings = _settings(request.get('settings'))
        now = _clock(request['now'])
        scheduled = datetime.combine(now.date(), time.fromisoformat(settings['time']), _ZONE)
        job = request.get('job')
        due = (settings['enabled'] and now >= scheduled
            and (job is None or job.get('status') == 'running'))
        if operation == 'due':
            return due
        return {'date': now.date().isoformat(), 'key': 'automatic-inspiration-' + now.date().isoformat(),
            'scheduled_for': scheduled.astimezone(timezone.utc).isoformat(), 'due': due}
    if operation == 'select':
        groups = {}
        for row in request['candidates']:
            groups.setdefault((row['project_id'], row['scene']), []).append(row)
        if not groups:
            return []
        selected = min(groups, key=lambda group: (
            min(row.get('last_auto_at', '') for row in groups[group]),
            min(row.get('created_at') or '' for row in groups[group]), group[0], group[1] or ''))
        result, bodies = [], []
        for row in sorted(groups[selected], key=lambda item: (item.get('created_at') or '', item['document_id'])):
            if row['markdown'] in bodies:
                continue
            result.append(row); bodies.append(row['markdown'])
            if len(result) == 3:
                break
        return result
    raise ValueError('inspiration_schedule_operation_invalid')
