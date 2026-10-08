"""Versioned visibility and stable preference among equally ranked memories."""
from .types import ScopeInput


def v1(request: ScopeInput) -> bool:
    return request.requested is None or request.assigned == request.requested


def v2(request: ScopeInput) -> bool:
    return request.requested is None or request.assigned in (None, request.requested)


def v3(request: ScopeInput) -> bool:
    """保留项目和场景可见性，灵感补入由独立操作控制。"""
    return v2(request)


_IDEAS = ('点子', '想法', '创意', '灵感', '怎么玩', '好主意', '脑暴')


def inspiration_rules(question):
    """灵感补入独立于四层充分判断，归类原话只为构思补入。"""
    brainstorming = any(word in question for word in _IDEAS)
    return {'limit': 5 if brainstorming else 2, 'include_project': brainstorming}


v3.inspirations = inspiration_rules


from . import register
register('scope', '@3')(v3)


def scene_priority(request: ScopeInput, visibility) -> int:
    """Keep @1 neutral; an inheriting scope prefers its own scene on ties."""
    return int(request.requested is not None and request.assigned != request.requested
               and visibility(ScopeInput(request.requested, None)))


def prefer_scene_ties(rows, *, score_key):
    """Exchange equal-score slots stably, preferring the requested scene."""
    result = list(rows)
    if not any(row.get('scope_priority', 0) for row in result):
        return result
    positions = {}
    for index, row in enumerate(result):
        positions.setdefault(score_key(row), []).append(index)
    for indices in positions.values():
        ordered = sorted((result[index] for index in indices),
                         key=lambda row: row.get('scope_priority', 0))
        for index, row in zip(indices, ordered):
            result[index] = row
    return result
