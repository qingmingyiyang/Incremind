"""retry@1: detached transport decisions; never owns a wire or permission."""
from __future__ import annotations

from collections.abc import Mapping
import math
import json
import re


_RETRY_BUDGETS = {'before_output': 10, 'thinking_error': 2,
                  'thinking_stall': 1, 'header': 1, 'fallback': 1}


def _retry_display(request):
    budget, reason, counts = request.get('budget'), request.get('reason'), request.get('counts')
    if (type(budget) is not str or budget not in _RETRY_BUDGETS
            or type(reason) is not str or reason not in {
                'server', 'connection', 'timeout', 'stalled', 'rate_limit',
                'header_timeout', 'malformed_stream'}
            or not isinstance(counts, Mapping)
            or any(type(value) is not int or value < 0 for value in counts.values())):
        raise ValueError('invalid_retry_display')
    used, limit = counts.get(budget, 0), _RETRY_BUDGETS[budget]
    if not 1 <= used <= limit:
        raise ValueError('invalid_retry_display')
    return {'reason': reason, 'used': used, 'limit': limit}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_task_decision_key')
        result[key] = value
    return result


def _pure_text_prefix(raw):
    if type(raw) is not str:
        return False
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, TypeError):
        # Conservatively recognize only a closed type followed by an open
        # summary string. Any unparsed key or invalid escape revokes it.
        return re.fullmatch(r'\s*\{\s*"type"\s*:\s*"complete"\s*,\s*"summary"\s*:\s*"'
            r'(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*', raw) is not None
    return (type(value) is dict and set(value) <= {'type', 'summary', 'payload_ref', 'evidence_refs'}
        and value.get('type') == 'complete' and type(value.get('summary')) is str
        and value.get('payload_ref') is None
        and type(value.get('evidence_refs', [])) is list
        and all(type(item) is str for item in value.get('evidence_refs', [])))


def decide(request: Mapping[str, object]) -> dict[str, object]:
    if request.get('kind') == 'retry_display':
        return _retry_display(request)
    if request.get('kind') == 'pure_text_prefix':
        return _pure_text_prefix(request.get('raw'))
    if request.get('kind') == 'continue_task_messages':
        return [*request['messages'], {'role': 'assistant', 'content': request['partial']},
                {'role': 'user', 'content': '连接已中断。接着已完成的段落继续写，不重复前文或已完成的工具；按原决策JSON格式返回，summary只含后续成果。'}]
    if request.get('kind') == 'continue_messages':
        return [*request['messages'], {'role': 'assistant', 'content': request['partial']},
                {'role': 'user', 'content': '连接已中断。接着已完成的段落继续写，不重复前文；仍按原JSON格式返回后续正文和引用编号。'}]
    if request.get('kind') == 'limits':
        return {'header_timeout': 180.0, 'idle_timeout': 20.0, 'total_timeout': 600.0}
    if request.get('kind') == 'partial_limits':
        return {'frame_characters': 400, 'frame_seconds': 1.0,
                'sleep_skew_seconds': 30.0, 'text_continuations': 3}
    if request.get('kind') == 'partial_output':
        text = str(request.get('text', ''))
        end, offset, fence = 0, 0, None
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            if stripped.startswith(('```', '~~~')):
                marker = stripped[:3]
                fence = None if fence == marker else marker if fence is None else fence
            offset += len(line)
            if not stripped and fence is None and line.endswith(('\n', '\r')):
                end = offset
        skew = float(request.get('wall_elapsed', 0)) - float(request.get('monotonic_elapsed', 0))
        return {'partial': text[:end], 'interruption': 'sleep' if skew > 30 else 'connection'}
    stopped = {'retry': False}
    phase = request.get('phase')
    kind = request.get('kind')
    if phase not in {'before_output', 'thinking'}:
        return stopped
    counts = request.get('counts', {})
    if not isinstance(counts, Mapping):
        raise ValueError('invalid_retry_counts')
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError('invalid_retry_counts')
    extra_budget = None
    if kind == 'header_timeout':
        budget = 'header'
        if phase == 'thinking':
            if counts.get('thinking_error', 0) >= _RETRY_BUDGETS['thinking_error']:
                return stopped
            extra_budget = 'thinking_error'
    elif kind == 'malformed_stream':
        if request.get('fallback_enabled', True) is not True:
            return stopped
        budget = 'fallback'
    elif phase == 'thinking' and kind == 'stalled':
        budget = 'thinking_stall'
    elif phase == 'thinking' and kind in {'server', 'connection', 'timeout', 'rate_limit'}:
        budget = 'thinking_error'
    elif kind in {'server', 'connection', 'timeout', 'stalled', 'rate_limit'}:
        budget = 'before_output'
    else:
        return stopped
    maximum = _RETRY_BUDGETS[budget]
    used = counts.get(budget, 0)
    if used >= maximum:
        return stopped
    jitter = request.get('jitter', 0)
    remaining = request.get('remaining', 0)
    if (not isinstance(jitter, (int, float)) or isinstance(jitter, bool)
            or not math.isfinite(jitter) or not 0 <= jitter <= 1
            or not isinstance(remaining, (int, float)) or isinstance(remaining, bool)
            or not math.isfinite(remaining)):
        raise ValueError('invalid_retry_time')
    delay = min(32, 2 ** used) * (1 + 0.25 * jitter)
    retry_after = request.get('retry_after')
    if (isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool)
            and math.isfinite(retry_after) and retry_after >= 0):
        delay = float(retry_after)
    if delay >= remaining:
        return stopped
    return {'retry': True, 'budget': budget, 'extra_budget': extra_budget,
            'delay': delay, 'non_stream': budget == 'fallback'}
