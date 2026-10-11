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


# Not yet calibrated on real EmbeddingGemma 2 scores (V0 did not record the
# similarity distribution); calibrate with memory_eval's synonym questions.
MIN_SIMILARITY = 0.55


def meaning(keyword, similarity):
    """Recall score for an insight or a document's selected passages, or None.

    Keyword hits stay first: a hit scores its matched term count plus the
    similarity as a fraction, so meaning orders hits with equal counts.
    Without a keyword hit, material whose similarity reaches MIN_SIMILARITY
    is recalled below every keyword hit, ordered by similarity. Without
    vectors (similarity None) the score is the keyword count alone.
    """
    if similarity is None:
        return keyword or None
    similarity = max(0.0, min(float(similarity), 0.999))
    if keyword:
        return keyword + similarity
    return similarity if similarity >= MIN_SIMILARITY else None


def v3(request: RankInput) -> float:
    """Version two's insight ranking, plus keyword-and-meaning recall."""
    return v2(request)


for _name in ('freshness', 'adjust', 'describe', 'decorate', 'rescore', 'instruction', 'profile_instruction'):
    setattr(v3, _name, getattr(v2, _name))
v3.meaning = meaning


_FUNCTION_WORDS = frozenset('a an are as at be been being by can could did do from had has have her his how in into is it its of on or s she should that their them they this to was were when where which who why will with would'.split())


def contextual(chunks, terms, title, summary, vectors, max_chars, trim):
    """按实义词稀缺性和语义排序，保留同主题的互补原文窗口。"""
    from dataclasses import replace
    from math import log
    def contains(term, text):
        if not term.isascii():
            return term in text
        # 只处理简单单复数，不让 art 误命中 started，同时允许 park/parks。
        stem = term[:-1] if len(term) > 3 and term.endswith('s') and not term.endswith(('ss', 'us')) else term
        word = re.escape(stem) + (r's?' if stem.isalpha() and len(stem) > 2 else '')
        return bool(re.search(r'(?<!\w)' + word + r'(?!\w)', text))
    terms = [(term, weight) for term, weight in terms if term not in _FUNCTION_WORDS]
    bodies = [chunk.text.casefold() for chunk in chunks]
    # 在一份资料中重复出现的人名和主题词不应压倒只出现一次的事实词。
    weights = {term: weight * (1 + log((len(chunks) + 1) / (1 + sum(contains(term, body) for body in bodies))))
               for term, weight in terms}
    total = sum(weights.values()) or 1
    prefix = (title + '\n' + summary[:120]).casefold()
    ranked = []
    for index, (chunk, body) in enumerate(zip(chunks, bodies)):
        body_hit = sum(weight for term, weight in weights.items() if contains(term, body))
        prefix_hit = sum(weight for term, weight in weights.items() if contains(term, prefix) and not contains(term, body))
        lexical = (body_hit + .2 * prefix_hit) / total
        similarity = max(0.0, min(float(vectors.get(index, 0)), 1.0))
        if not body_hit and not prefix_hit and similarity < MIN_SIMILARITY:
            continue
        score = .45 * lexical + .55 * similarity if vectors else lexical
        ranked.append((score, body_hit, -index, chunk))
    selected, used, best_score = [], 0, 0.0
    for score, body_hit, _index, chunk in sorted(ranked, key=lambda row: row[:3], reverse=True):
        if len(selected) == 3:
            break
        if selected and (score < best_score * .5 or not body_hit and not vectors):
            continue
        # 分块自带末句重叠；只去掉重复交集，互补事实不必带来新的问题词。
        segments = [(chunk.start, chunk.end)]
        for old in selected:
            segments = [(a, min(b, old.start)) for a, b in segments if a < old.start] + [
                (max(a, old.end), b) for a, b in segments if b > old.end]
        for start, end in segments:
            room = max_chars - used - (3 if selected else 0)
            if room <= 0 or end <= start:
                continue
            text = chunk.text[start-chunk.start:end-chunk.start]
            piece = replace(chunk, start=start, end=end, text=text)
            if len(text) > room:
                piece = trim(piece, room)
            if piece is None or not piece.text.strip():
                continue
            selected.append(piece)
            used += len(piece.text) + (3 if len(selected) > 1 else 0)
            best_score = max(best_score, score)
            if len(selected) == 3:
                break
    selected.sort(key=lambda window: window.start)
    return selected, best_score, 'content' if selected else 'none'


def v4(request: RankInput) -> float:
    """认识排序保持原规则，文段使用可独立对照的新排序。"""
    return v3(request)


for _name in ('freshness', 'adjust', 'describe', 'decorate', 'rescore', 'instruction', 'profile_instruction', 'meaning'):
    setattr(v4, _name, getattr(v3, _name))
v4.contextual = contextual
from . import register
register('rank', '@4')(v4)
