"""Version one of published recognition ranking."""
from .types import RankInput


def v1(request: RankInput) -> float:
    return (request.score + 1) * request.weight


from datetime import datetime, timedelta
import re

from .insight_time import LOCAL, MONTHS, instant

_PERIOD = re.compile(r'(\d{4})(?:年(?:(\d{1,2}|十一|十二|十|[一二三四五六七八九])月(?:(\d{1,2})[日号])?)?|[-/](\d{1,2})(?:[-/](\d{1,2}))?)?')
_PREFIX = re.compile(r'^(?:截至|截止(?:到)?|适用于|适用期[:：]?|有效期[:：]?)')
_RANGE = re.compile(r'至|到|~|～|—|–')
_INDEFINITE = re.compile(r'今年|去年|明年|每年|长期|永久|至今|以后|起$')
_UNKNOWN = {'stale': False, 'expires_at': None}
_STALE_FACTOR = 0.5


def _period(text):
    match = _PERIOD.fullmatch(text)
    if match is None:
        return None
    year = int(match[1])
    raw_month = match[2] or match[4]
    month = MONTHS.get(raw_month) or int(raw_month or 1)
    raw_day = match[3] or match[5]
    try:
        start = datetime(year, month, int(raw_day or 1), tzinfo=LOCAL)
        if raw_day:
            end = start + timedelta(days=1)
        elif raw_month:
            end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=LOCAL)
        else:
            end = datetime(year + 1, 1, 1, tzinfo=LOCAL)
    except (ValueError, OverflowError):
        return None
    return start, end


def freshness(request: RankInput) -> dict:
    """只有明确的有限日历适用期和冻结时钟能证明到期。"""
    clock = instant(request.reference)
    if clock is None:
        return dict(_UNKNOWN)
    periods = []
    for condition in request.conditions:
        if not isinstance(condition, str):
            return dict(_UNKNOWN)
        text = re.sub(r'\s+', '', condition)
        if _INDEFINITE.search(text):
            return dict(_UNKNOWN)
        if not re.search(r'\d{4}', text):
            continue
        text = _PREFIX.sub('', text)
        ends = _RANGE.split(text)
        parsed = [_period(end) for end in ends]
        if len(parsed) not in (1, 2) or any(period is None for period in parsed):
            return dict(_UNKNOWN)
        left, right = parsed[0][0], parsed[-1][1]
        if parsed[-1][0] < left:
            return dict(_UNKNOWN)
        periods.append((left, right))
    if len(set(periods)) != 1:
        return dict(_UNKNOWN)
    end = periods[0][1]
    return {'stale': clock >= end, 'expires_at': end.isoformat()}


def adjust(score: float, request: RankInput) -> float:
    """原调用方已有独立分数时，也沿用同一个到期折扣。"""
    return score * (_STALE_FACTOR if freshness(request)['stale'] else 1)


def describe(text: str, request: RankInput) -> str:
    return '过时：' + text if freshness(request)['stale'] else text


def decorate(candidate, request: RankInput):
    """标注展示标题，证据正文和原始坐标保持完整。"""
    details = freshness(request)
    return ({**candidate, 'stale': True, 'expires_at': details['expires_at'],
             'title': describe(candidate['title'], request)} if details['stale'] else candidate)


def rescore(score, candidate):
    """时间范围和方法匹配沿冻结候选的同一到期事实重排。"""
    return score * (_STALE_FACTOR if candidate.get('stale') is True else 1)


def instruction(chosen):
    return '用到标为过时的资料时，明确说明其适用期已过，不把它当作当前事实。' if any(c.get('stale') is True for c in chosen) else ''


def profile_instruction(items):
    """画像没有编号与排名，只在原回答指令中指明过期的背景条目。"""
    expired = [item['content'] for item in items if item.get('stale') is True]
    return ('以下已确认画像的适用期已过；使用这些背景时明确说明过时：\n'
            + '\n'.join(expired)) if expired else ''


def v2(request: RankInput) -> float:
    return adjust(v1(request), request)


v2.freshness = freshness
v2.adjust = adjust
v2.describe = describe
v2.decorate = decorate
v2.rescore = rescore
v2.instruction = instruction
v2.profile_instruction = profile_instruction
