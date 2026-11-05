from .consolidate import v2


def _eligible(events):
    directions = {}
    for event in events:
        if event.get('via') in {'steer', 'followup'} and event.get('type') == 'correction':
            directions.setdefault(event.get('direction'), set()).add(event.get('turn_id'))
    return [event for event in events if event.get('via') not in {'steer', 'followup'}
        or event.get('type') == 'future'
        or (event.get('type') == 'correction' and event.get('direction')
            and len(directions[event['direction']] - {None}) >= 2)]


def v3(entrypoint=None, *args, operation='run', events=(), event_ids=(), expected_ids=(),
       kind='pattern', source_count=0, **kwargs):
    if operation == 'conditions':
        conditions = kwargs['conditions']
        if not isinstance(conditions, list) or any(not isinstance(item, str) for item in conditions):
            raise ValueError('steer_learning_conditions_invalid')
        normalized = list(dict.fromkeys(item.strip() for item in conditions if item.strip()))
        fallback = kwargs.get('situation') or kwargs['question']
        if not isinstance(fallback, str) or not fallback.strip():
            raise ValueError('steer_learning_condition_context_invalid')
        return normalized or [fallback.strip()]
    if operation == 'events':
        legacy = [event for event in events if event.get('via') not in {'steer', 'followup'}]
        eligible = v2(operation='events', events=legacy)
        return [event for event in _eligible(events) if event.get('via') in {'steer', 'followup'} or event in eligible]
    if operation == 'accept':
        selected = [event for event in events if event['event_id'] in event_ids]
        if (not set(event_ids) <= set(expected_ids) or bool(event_ids) != bool(expected_ids)
                or len(event_ids) != len(set(event_ids)) or kind not in {'pattern', 'correction'}
                or any(event not in _eligible(selected) for event in selected)):
            return False
        if kind == 'pattern' and (source_count < 2 or event_ids and len(event_ids) < 2):
            return False
        legacy = [event for event in selected if event.get('via') not in {'steer', 'followup'}]
        return (not legacy or v2(operation='accept', events=legacy,
            event_ids=[event['event_id'] for event in legacy], expected_ids=[event['event_id'] for event in legacy],
            kind='correction', source_count=source_count))
    if operation == 'messages':
        prompt = ('依据实际用户反馈提出待确认的新方法，保留适用条件。future 是用户明确要求今后采用的做法。'
            '输入 type=correction 至少需要两个不同对话提出相同方向，不能把相反要求合并。'
            '一条 type=future 即可提出 kind=correction 的新方法，不能把单条反馈标为 pattern。'
            '只使用给定 event_id，不能引用历史正文中的旧编号。pattern 还需要两个不同原件根。'
            '依据了 context_recognitions 中某条认识时，target_id 选其真实编号，relation 使用 supplement 或 differs；'
            '没有对应认识时 target_id 为 null 且 relation 为 new。适用条件取当前情境，没有情境时取原问题。'
            '只返回JSON {"text":"不超过40字的认识","conditions":[],"event_ids":[],"kind":"pattern或correction",'
            '"target_id":null,"relation":"new或supplement或differs"}。')
        return [{'role': 'system', 'content': prompt + kwargs['constraints']},
            {'role': 'user', 'content': kwargs['text']}]
    if operation == 'selected':
        return [event for event in events if event['event_id'] in event_ids]
    return entrypoint(*args, use_corrections=True, **kwargs)
