"""Pure calendar interpretation and validity selection, with an explicit clock."""
from datetime import datetime, timedelta, timezone
import re

LOCAL = timezone(timedelta(hours=8))
UTC = timezone.utc
MONTHS = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6,
          '七': 7, '八': 8, '九': 9, '十': 10, '十一': 11, '十二': 12}
MONTH = r'(?:[1-9]|1[0-2]|十一|十二|十|[一二三四五六七八九])'
DATE = re.compile(r'(?:(\d{4})年)?(' + MONTH + r')月(?:(\d{1,2})[日号])?')
SIGNALS = ('现在', '目前', '上周', '当时', '以前', '什么时候', '何时', '哪天')
REMOVE = re.compile(r'现在|目前|上周|当时|以前|什么时候|何时|哪天|先后做了什么|怎么想的|怎么认为|采用什么决定')


def instant(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(UTC) if result.tzinfo else None
    except ValueError:
        return None


def parse(question, reference):
    matches = list(DATE.finditer(question))
    if not matches and not any(word in question for word in SIGNALS):
        return None
    clock = instant(reference)
    if clock is None:
        raise ValueError('invalid_time_reference')
    local = clock.astimezone(LOCAL)
    mode, start, end = 'current', clock, clock
    if matches:
        dates = []
        year = local.year
        for match in matches:
            year = int(match[1]) if match[1] else year
            month = MONTHS.get(match[2]) or int(match[2])
            try:
                left = datetime(year, month, int(match[3] or 1), tzinfo=LOCAL)
                right = (left + timedelta(days=1) if match[3] else
                         datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=LOCAL))
            except ValueError:
                return None
            dates.append((left, right))
        start, end = dates[0][0], dates[-1][1]
        if end <= start:
            return None
        mode = 'interval'
    elif '上周' in question:
        end = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())
        start, mode = end - timedelta(days=7), 'interval'
    elif any(word in question for word in ('什么时候', '何时', '哪天')):
        start, end, mode = None, clock, 'event'
    elif any(word in question for word in ('以前', '当时')):
        start, end, mode = None, clock, 'past'
    search = REMOVE.sub('', DATE.sub('', question))
    search = re.sub(r'从\s*到|在\s*时', '', search).strip(' ？?')
    return {'mode': mode, 'start': start.astimezone(UTC).isoformat() if start else None,
            'end': end.astimezone(UTC).isoformat(), 'reference': reference, 'query': search or question}


def valid(marker):
    start, end = instant(marker.get('valid_from')), instant(marker.get('valid_until'))
    return start is not None and (marker.get('valid_until') is None or end is not None and end >= start)


def applies(marker, scope):
    if not valid(marker):
        return False
    start, end = instant(marker['valid_from']), instant(marker['valid_until'])
    left, right = instant(scope['start']), instant(scope['end'])
    if scope['mode'] == 'current':
        return start <= right and (end is None or right < end)
    if scope['mode'] == 'past':
        return start < right
    if scope['mode'] == 'event':
        return start <= right
    return start < right and (end is None or left is None or end > left)


def select(candidates, scope, documents):
    """Keep history relevant to this scope; shared evidence retains any valid owner."""
    if scope is None:
        return candidates
    insights = [row for row in candidates if row['kind'] == 'recognition'
                and applies(row.get('validity', {}), scope)]
    if scope['mode'] == 'event' and insights:
        best = max(row['time_match_score'] for row in insights)
        insights = [row for row in insights if row['time_match_score'] == best]
    allowed = {row['id'] for row in insights}
    result = []
    for row in candidates:
        if row['kind'] == 'recognition':
            if row['id'] not in allowed:
                continue
            row = {**row, 'time_scope': scope, 'temporal': scope['mode'] != 'current',
                   'score': row['time_match_score']}
        else:
            linked = set(row.get('document_ids', ())) | {row.get('document_id')}
            owners = {identity for document in linked for identity in documents.get(document, ())}
            if owners and not owners & allowed:
                continue
            at = instant(row.get('sort_time'))
            right = instant(scope['end'])
            if at is not None and (at > right if scope['mode'] == 'current' else at >= right):
                continue
        result.append(row)
    return result


def describe(marker):
    start = marker['valid_from'].split('T')[0]
    end = marker['valid_until'].split('T')[0] if marker['valid_until'] else '至今'
    return f'当时有效（{start} 至 {end}）：\n'


def dedup_comparison(candidate, chosen):
    return [row for row in chosen if not (candidate.get('temporal') and row.get('temporal')
            and candidate.get('validity') != row.get('validity'))]


INSTRUCTION = '涉及过去的认识时说明当时的有效时间，与现在的判断区分；时间不明时说明缺少日期依据。'
