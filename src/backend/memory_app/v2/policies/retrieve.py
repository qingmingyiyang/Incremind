"""Register the existing retrieval entry through caller injection."""
from .types import invoke_entry as v1
from . import register
from .method_situation import condition_score


def v2(entrypoint, project, question, *, scene=None, situation=None):
    return entrypoint(project, question, scene=scene, method_query=situation or question)


register('retrieve', '@2')(v2)


def v3(entrypoint, /, *args, **kwargs):
    from .insight_time import parse, select, valid, DATE, SIGNALS
    operation = kwargs.pop('operation', None)
    if operation == 'time':
        return parse(*args)
    if operation == 'time_candidates':
        return select(*args)
    if operation == 'validity':
        return valid(*args)
    project, question = args
    original = kwargs.get('situation') or question
    if not DATE.search(original) and not any(word in original for word in SIGNALS):
        return v2(entrypoint, *args, **kwargs)
    return entrypoint(project, question, scene=kwargs.get('scene'), method_query=original, time_query=original)


register('retrieve', '@3')(v3)


_GAP_COVERAGE = 0.6
_GAP_QUERY_LIMIT = 3
_GAP_QUERY_CHARS = 200


def _gap_queries(question, output):
    if not isinstance(output, dict) or set(output) != {'queries'}:
        raise ValueError('invalid_gap_queries')
    values = output['queries']
    if not isinstance(values, list) or len(values) > _GAP_QUERY_LIMIT:
        raise ValueError('invalid_gap_queries')
    queries, seen = [], {question.strip().casefold()}
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > _GAP_QUERY_CHARS:
            raise ValueError('invalid_gap_queries')
        value = value.strip()
        if value.casefold() not in seen:
            queries.append(value)
            seen.add(value.casefold())
    return tuple(queries)


def _lower_order(rows):
    # Preserve the original connected/scene/recency order within an exact RRF
    # rank. Without fused queries, the ordinary ladder remains untouched.
    return sorted(rows, key=lambda row: row.get('rrf_rank', float('inf')))


def v4(entrypoint, /, *args, **kwargs):
    operation = kwargs.get('operation')
    if operation == 'gap_needed':
        plan, = args
        coverage = next((row['coverage'] for row in reversed(plan['trace'])
                         if row['layer'] in {'L3', 'L2'} and 'expanded_from' not in row), 0.0)
        return coverage < _GAP_COVERAGE
    if operation == 'gap_messages':
        question, materials = args
        return [
            {'role': 'system', 'content':
             '根据问题与已选材料列出回答还缺哪几点，最多3条短问句，每条不超过200字。'
             '只问尚缺的具体信息，不回答、不改写已覆盖的内容。只返回JSON {"queries":["问句"]}。'},
            {'role': 'user', 'content': '问题：' + question + '\n已选材料：\n' + '\n\n'.join(materials)},
        ]
    if operation == 'gap_queries':
        return _gap_queries(*args)
    if operation == 'lower_order':
        return _lower_order
    return v3(entrypoint, *args, **kwargs)


register('retrieve', '@4')(v4)
