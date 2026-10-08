"""remind@1 的本机确定时间解析；注册与服务接线由原策略入口负责。"""

from datetime import date, datetime, timedelta, tzinfo
from collections import Counter
from dataclasses import dataclass
import re

from .insight_time import MONTHS, UTC, instant


_NUMBER = r'(?:[0-9]{1,2}|[二两三四五六七八九]?十[一二三四五六七八九]?|[零〇一二两三四五六七八九])'
_DIGITS = {'零': 0, '〇': 0, '两': 2,
           **{word: value for word, value in MONTHS.items() if len(word) == 1 and value < 10}}
_BOUND = r'0-9零〇一二两三四五六七八九十百千万年月日号负上下昨前大个再-'
_ABSOLUTE = re.compile(r'(?:(?P<year>[0-9]{4})年)?(?P<month>' + _NUMBER
                       + r')月(?P<day>' + _NUMBER + r')[日号]')
_DATE = re.compile(
    r'(?<![' + _BOUND + r'])(?:'
    r'(?P<relative>今天|明天|后天)|(?P<days>' + _NUMBER + r')天后|'
    r'(?P<week>下(?:一?个)?|本|这(?:个)?)?(?:周|星期)(?P<weekday>[一二三四五六日天])(?![一二三四五六七八九十日天])|'
    r'(?P<absolute>(?:[0-9]{4}年)?' + _NUMBER + r'月' + _NUMBER + r'[日号]|'
    r'[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?![0-9TtZz])))')
_PERIOD = r'凌晨|早上|上午|中午|下午|傍晚|晚上'
_PERIODS = re.compile(_PERIOD)
_CLOCK = re.compile(
    r'(?<![0-9零〇一二两三四五六七八九十百千万负-])'
    r'(?P<period>' + _PERIOD + r')?(?P<hour>' + _NUMBER + r')'
    r'(?:(?:点|时)(?:(?P<half>半)|(?P<minute>' + _NUMBER + r')分)?|'
    r'[:：](?P<colon_minute>[0-9]{2}))'
    r'(?![0-9零〇一二两三四五六七八九十百千万负点分刻:：-])')
_TIME_FRAGMENT = re.compile(r'[0-9零〇一二两三四五六七八九十百千万]+[点时]|[0-9][:：]')
_UNCERTAIN = re.compile(r'每(?:天|日|周|星期|月|年|隔)|左右|大概|大约|约莫|差不多|'
                        r'约(?:在)?(?=今天|明天|后天|周|星期|下周|本周|这周|'
                        + _PERIOD + r'|' + _NUMBER + r'(?:年|月|天|点|时))')
_TIME_RANGE = re.compile(_NUMBER + r'(?:到|至|[-~～—])' + _NUMBER + r'(?:点|时|[:：])')
_OPEN_END = re.compile(r'^(?:之前|之后|以前|以后|前后|左右|(?:前|后)(?!往|勤)|'
                       r'(?:或者?|到|至|[-~～—])(?=' + _NUMBER + r'|' + _PERIOD + r'))')


def _number(value):
    if value.isascii() and value.isdigit():
        return int(value)
    if value in MONTHS:
        return MONTHS[value]
    if value in _DIGITS:
        return _DIGITS[value]
    left, _, right = value.partition('十')
    return _DIGITS.get(left, 1) * 10 + _DIGITS.get(right, 0)


def _calendar_day(match, local):
    if match['relative']:
        return local.date() + timedelta(days={'今天': 0, '明天': 1, '后天': 2}[match['relative']])
    if match['days']:
        return local.date() + timedelta(days=_number(match['days']))
    if match['weekday']:
        weekday = '一二三四五六日'.index(match['weekday'].replace('天', '日'))
        offset = weekday - local.weekday()
        if match['week'] and match['week'].startswith('下'):
            offset += 7
        elif match['week'] is None:
            offset %= 7
        return local.date() + timedelta(days=offset)
    value = match['absolute']
    if '-' in value:
        year, month, day = map(int, value.split('-'))
    else:
        parts = _ABSOLUTE.fullmatch(value)
        year = int(parts['year']) if parts['year'] else local.year
        month, day = _number(parts['month']), _number(parts['day'])
    return date(year, month, day)


def _hour(hour, period, colon):
    if period is None:
        # 未说明上下午的十二小时说法有歧义；冒号格式按二十四小时理解。
        return hour if 0 <= hour <= 23 and (colon or hour == 0 or hour >= 13) else None
    if period in ('下午', '傍晚', '晚上') and 1 <= hour <= 11:
        hour += 12
    if period == '中午' and hour == 1:
        hour = 13
    if period == '凌晨' and hour == 12:
        hour = 0
    bounds = {'凌晨': (0, 5), '早上': (5, 11), '上午': (6, 11),
              '中午': (11, 13), '下午': (12, 18), '傍晚': (17, 19), '晚上': (18, 23)}
    left, right = bounds[period]
    return hour if left <= hour <= right else None


def _wall_time(day, hour, minute, local_timezone):
    wall = datetime(day.year, day.month, day.day, hour, minute)
    possibilities = set()
    for fold in (0, 1):
        at = wall.replace(tzinfo=local_timezone, fold=fold).astimezone(UTC)
        if at.astimezone(local_timezone).replace(tzinfo=None) == wall:
            possibilities.add(at)
    # 夏令时跳过或重复的时刻都不猜，由普通记住保留原话。
    return next(iter(possibilities)) if len(possibilities) == 1 else None


def remind_v1(text, reference, local_timezone: tzinfo, *, operation='parse'):
    """解析原话或核验改后的时间，时钟和本机时区始终由调用者提供。"""
    clock = instant(reference)
    if clock is None:
        raise ValueError('invalid_reminder_reference')
    if not isinstance(local_timezone, tzinfo):
        raise ValueError('invalid_reminder_timezone')
    try:
        local = clock.astimezone(local_timezone)
    except (TypeError, ValueError) as error:
        raise ValueError('invalid_reminder_timezone') from error
    if operation == 'at':
        at = instant(text)
        return at.isoformat() if at is not None and at > clock else None
    if operation != 'parse':
        raise ValueError('invalid_reminder_operation')
    if not isinstance(text, str) or not text.startswith('提醒我'):
        return None
    compact = re.sub(r'\s+', '', text[3:])
    if _UNCERTAIN.search(compact) or _TIME_RANGE.search(compact):
        return None
    dates = list(_DATE.finditer(compact))
    if len(dates) != 1:
        return None
    # 日期中的星期数字不属于小时数词；保留坐标后再检查时间边界。
    calendar = dates[0]
    clock_input = (compact[:calendar.start()] + ' ' * (calendar.end() - calendar.start())
                   + compact[calendar.end():])
    clocks = list(_CLOCK.finditer(clock_input))
    periods = list(_PERIODS.finditer(compact))
    if len(clocks) > 1 or len(periods) > 1:
        return None
    # 日期、时段和时钟都参与边界检查，默认九点不能抹去时段后的截止含义。
    if any(_OPEN_END.match(compact[match.end():]) for match in (*dates, *periods, *clocks)):
        return None
    hour, minute = 9, 0
    if clocks:
        match = clocks[0]
        hour = _hour(_number(match['hour']), match['period'], match['colon_minute'] is not None)
        minute = 30 if match['half'] else _number(match['minute'] or match['colon_minute'] or '0')
        if hour is None or minute > 59 or (periods and match['period'] is None):
            return None
        # 其余残留时间片段不能被合法的一段掩盖。
        remainder = compact[:match.start()] + compact[match.end():]
        if _TIME_FRAGMENT.search(remainder):
            return None
    elif _TIME_FRAGMENT.search(compact) or (periods and periods[0][0] != '早上'):
        return None
    try:
        day = _calendar_day(dates[0], local)
        at = _wall_time(day, hour, minute, local_timezone)
        if at is not None and at <= clock and dates[0]['weekday'] and dates[0]['week'] is None:
            # 未限定本周的星期取下一次；明确日期和相对日期不擅自顺延。
            at = _wall_time(day + timedelta(days=7), hour, minute, local_timezone)
    except (ValueError, OverflowError):
        return None
    if at is None or at <= clock:
        return None
    return {'at': at.isoformat(), 'text': text}


def _nudge_clock(now, local_timezone):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError('invalid_nudge_reference')
    if not isinstance(local_timezone, tzinfo):
        raise ValueError('invalid_nudge_timezone')
    return now.astimezone(UTC), now.astimezone(local_timezone)


@dataclass(frozen=True)
class NudgePolicy:
    """对脱离存储的提醒候选排序，所有频率和时间规则集中在本版本。"""
    window_days: int = 14
    repeat_days: int = 7
    default_limit: int = 2
    maximum_limit: int = 5
    conversation_days: int = 60
    minimum_rounds: int = 20
    default_hour: int = 9

    def __call__(self, items, *, now, local_timezone, limit=None, history=()):
        clock, local = _nudge_clock(now, local_timezone)
        limit = self.default_limit if limit is None else limit
        if type(limit) is not int or not 0 <= limit <= self.maximum_limit:
            raise ValueError('invalid_nudge_limit')
        past = [(event, at) for event in history
                if (at := instant(event.get('at'))) is not None and at <= clock]
        repeated = {event['id'] for event, at in past
                    if clock - timedelta(days=self.repeat_days) < at <= clock}
        penalties = Counter()
        broken = set()
        for event, _ in sorted(past, key=lambda pair: pair[1], reverse=True):
            kind = event.get('kind')
            if kind in broken or event.get('action') not in {'opened', 'ignored', 'closed'}:
                continue
            if event['action'] == 'closed':
                penalties[kind] += 1
            else:
                broken.add(kind)
        selected, automatic, seen = [], [], set()
        for row in items:
            if row.get('private') is True or row.get('state', 'active') != 'active':
                continue
            identity, kind = row['id'], row['kind']
            if identity in seen:
                continue
            seen.add(identity)
            at = instant(row.get('at'))
            if kind == 'reminder':
                # 用户指定的事实不接受自动投放的频率和反馈规则。
                if at is not None and at <= clock:
                    selected.append(row)
                continue
            if kind not in {'date', 'place'} or identity in repeated:
                continue
            if kind == 'date':
                if at is None:
                    raise ValueError('invalid_nudge_date')
                day = at.astimezone(local_timezone).date()
                if not local.date() <= day < local.date() + timedelta(days=self.window_days):
                    continue
            else:
                if not isinstance(row.get('city'), str) or not row['city']:
                    raise ValueError('invalid_nudge_city')
                day = local.date()
            automatic.append((row, day))
        counts = Counter()
        # 同优先级保持已冻结候选顺序；关掉同类越多，排序越靠后。
        for row, day in sorted(automatic, key=lambda pair: penalties[pair[0]['kind']]):
            if counts[day] < limit:
                counts[day] += 1
                selected.append(row)
        return selected

    def delivery_hour(self, turns, *, now, local_timezone, weekend, recording_enabled=True):
        if not recording_enabled:
            return self.default_hour
        clock, _ = _nudge_clock(now, local_timezone)
        hours, identities = Counter(), set()
        for turn in turns:
            if turn.get('intent') not in {'remember', 'ask'} or turn.get('parent_turn_id'):
                continue
            at = instant(turn.get('created_at'))
            if at is None or not clock - timedelta(days=self.conversation_days) <= at <= clock:
                continue
            local = at.astimezone(local_timezone)
            if (local.weekday() >= 5) != weekend or turn['id'] in identities:
                continue
            identities.add(turn['id'])
            hours[local.hour] += 1
        if sum(hours.values()) < self.minimum_rounds:
            return self.default_hour
        return min(hours, key=lambda hour: (-hours[hour], hour))


v1 = NudgePolicy()


@dataclass(frozen=True)
class DeliveryNudgePolicy(NudgePolicy):
    """候选版本区分事件时间与实际送达日，并为问的管线提供总证据预算。"""
    evidence_limit: int = 3
    profile_limit: int = 1
    maximum_text: int = 60

    def __call__(self, items, *, now, local_timezone, limit=None, history=()):
        clock, local = _nudge_clock(now, local_timezone)
        limit = self.default_limit if limit is None else limit
        if type(limit) is not int or not 0 <= limit <= self.maximum_limit:
            raise ValueError('invalid_nudge_limit')
        past = [(row, at) for row in history if row.get('state') != 'failed'
                and (at := instant(row.get('delivery_at', row.get('at')))) is not None and at <= clock]
        delivered_today = {row['id'] for row, at in past
                           if row.get('kind') != 'reminder' and at.astimezone(local_timezone).date() == local.date()}
        repeated = {row.get('event_id', row['id']) for row, at in past
                    if clock - timedelta(days=self.repeat_days) < at <= clock}
        penalties, broken = Counter(), set()
        feedback = sorted(past, key=lambda pair: instant(pair[0].get('action_at')) or pair[1], reverse=True)
        for row, _ in feedback:
            kind, action = row.get('kind'), row.get('action')
            if kind in broken or action not in {'opened', 'ignored', 'closed'}:
                continue
            if action == 'closed':
                penalties[kind] += 1
            else:
                broken.add(kind)
        selected, automatic, seen = [], [], set()
        for row in items:
            if row.get('private') is True or row.get('state', 'active') != 'active' or row['id'] in seen:
                continue
            seen.add(row['id'])
            at = instant(row.get('at'))
            if row['kind'] == 'reminder':
                if at is not None and at <= clock:
                    selected.append(row)
                continue
            if row['kind'] not in {'date', 'place'} or row['id'] in repeated:
                continue
            delivery = instant(row.get('delivery_at'))
            if delivery is None:
                raise ValueError('invalid_nudge_delivery_at')
            if delivery > clock or delivery.astimezone(local_timezone).date() != local.date():
                continue
            if row['kind'] == 'date' and (at is None or not self.in_window(at.isoformat(), now=now, local_timezone=local_timezone)):
                continue
            if row['kind'] == 'place' and not row.get('city'):
                raise ValueError('invalid_nudge_city')
            automatic.append(row)
        available = max(0, limit - len(delivered_today))
        automatic.sort(key=lambda row: (penalties[row['kind']], instant(row.get('at')) or clock, row['id']))
        return selected + automatic[:available]

    def in_window(self, at, *, now, local_timezone):
        _, local = _nudge_clock(now, local_timezone)
        at = instant(at)
        return at is not None and local.date() <= at.astimezone(local_timezone).date() < local.date() + timedelta(days=self.window_days)

    def capture_reference(self, originals):
        """只有唯一规范原件提供参照日期，不用最早日期猜测多源事实的归属。"""
        if len(originals) != 1:
            return None
        if instant(originals[0]) is None:
            raise ValueError('invalid_nudge_source_reference')
        return originals[0]

    def dates(self, text, *, source_at, now, local_timezone, annual=False):
        clock, local = _nudge_clock(now, local_timezone)
        anchor = instant(source_at)
        if source_at is not None and anchor is None:
            raise ValueError('invalid_nudge_source_reference')
        if not isinstance(text, str):
            raise ValueError('invalid_nudge_source_text')
        result = []
        for match in _DATE.finditer(re.sub(r'\s+', '', text)):
            if anchor is None:
                absolute = match['absolute']
                if not absolute or ('-' not in absolute and not _ABSOLUTE.fullmatch(absolute)['year']):
                    continue
            try:
                # 无参照时只接受完整年月日，此分支不使用当前年份或星期推断事件。
                reference = anchor.astimezone(local_timezone) if anchor is not None else local
                day = _calendar_day(match, reference)
                recurring = annual
                if recurring:
                    # 出生年份不限制周年出现；相对日期先由原资料日期求出月日。
                    day = day.replace(year=local.year)
                    if day < local.date():
                        day = day.replace(year=day.year + 1)
                at = _wall_time(day, self.default_hour, 0, local_timezone)
            except (ValueError, OverflowError):
                continue
            if at is not None and self.in_window(at.isoformat(), now=clock, local_timezone=local_timezone):
                result.append({'at': at.isoformat(), 'span': match[0], 'annual': bool(recurring)})
        return result

    def annual_source(self, text, source_kind):
        return source_kind == 'recognition' and any(word in text for word in ('生日', '纪念日'))

    def delivery_hour(self, turns, *, now, local_timezone, weekend, recording_enabled=True):
        qualified = []
        for turn in turns:
            if turn.get('by') == 'admin' or turn.get('parent_turn_id'):
                continue
            intent = turn.get('intent')
            if intent == 'multi':
                intent = 'remember' if any(part.get('intent') in {'remember', 'ask', 'inspiration'}
                                          for part in turn.get('receipt', {}).get('parts', [])) else None
            elif intent == 'inspiration':
                intent = 'remember'
            if intent in {'remember', 'ask'}:
                qualified.append({**turn, 'intent': intent})
        return super().delivery_hour(qualified, now=now, local_timezone=local_timezone,
                                     weekend=weekend, recording_enabled=recording_enabled)

    def delivery_at(self, turns, *, now, local_timezone, recording_enabled=True):
        _, local = _nudge_clock(now, local_timezone)
        hour = self.delivery_hour(turns, now=now, local_timezone=local_timezone,
                                  weekend=local.weekday() >= 5, recording_enabled=recording_enabled)
        at = _wall_time(local.date(), hour, 0, local_timezone)
        return at.isoformat() if at is not None else None

    def situation(self, row, *, now, local_timezone):
        if row['kind'] == 'place':
            context = '到了 ' + row['city']
        else:
            _, local = _nudge_clock(now, local_timezone)
            days = (instant(row['at']).astimezone(local_timezone).date() - local.date()).days
            context = row['title'] + ' 还有 ' + str(days) + ' 天'
        return context + '。结合当前资料给我一句提醒，不超过' + str(self.maximum_text) + '字。'

    def fallback(self, row, *, now, local_timezone):
        if row['kind'] == 'place':
            prefix = '到了' + row['city'] + '：'
        else:
            _, local = _nudge_clock(now, local_timezone)
            days = (instant(row['at']).astimezone(local_timezone).date() - local.date()).days
            prefix = ('今天：' if days == 0 else str(days) + '天后：')
        return (prefix + row['title'])[:self.maximum_text]

    def response_text(self, text):
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > self.maximum_text:
            raise ValueError('invalid_nudge_generated_text')
        return text.strip()

    def city_mentions(self, text, scene, resource):
        names, lengths, selected = resource['names'], resource['lengths'], {}
        def accept(name):
            choices = names.get(name.casefold(), {})
            if len(choices) == 1:
                city = next(iter(choices.values()))
                selected[city['id']] = {**city, 'display': name}
        # 场景的完整段名可直接匹配；同名多个驻地的别名始终拒绝。
        for part in (scene or '').split('/'):
            accept(part.strip())
        folded = text.casefold()
        for start in range(len(folded)):
            for size in lengths.get(folded[start:start + 2], ()):
                end = start + size
                name = folded[start:end]
                if name not in names:
                    continue
                if name[0].isascii() and name[0].isalpha():
                    if ((start and folded[start - 1].isalnum()) or
                            (end < len(folded) and folded[end].isalnum())):
                        continue
                    # 英文正文的小写常用词不足以证明地名，完整场景仍支持小写。
                    if text[start:end].islower():
                        continue
                accept(text[start:end])
        return [selected[identity] for identity in sorted(selected)]


v2 = DeliveryNudgePolicy()
