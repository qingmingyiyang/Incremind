"""Register the existing consolidation entry through caller injection."""
from .types import invoke_entry as v1


def v2(entrypoint=None, *args, operation='run', events=(), event_ids=(), expected_ids=(), kind='pattern', source_count=0, **kwargs):
    if operation == 'selected':
        return [event for event in events if event['event_id'] in event_ids]
    if operation == 'events':
        repeated = len({event['turn_id'] for event in events if event.get('type') == 'correction' and event.get('turn_id')}) > 1
        return [event for event in events if repeated or event.get('type') != 'correction' or not event.get('turn_id')
                or '以后' in event.get('after', '')]
    if operation == 'accept':
        chosen = v2(operation='selected', events=events, event_ids=event_ids)
        transient = chosen and all(event.get('type') == 'correction' and event.get('turn_id')
                                  and '以后' not in event.get('after', '') for event in chosen)
        return (set(event_ids) <= set(expected_ids) and bool(event_ids) == bool(expected_ids)
                and len(event_ids) == len(set(event_ids)) and kind in {'pattern', 'correction'}
                and (not transient or len({event['turn_id'] for event in chosen}) > 1)
                and (kind != 'pattern' or source_count >= 2 and (not event_ids or len(event_ids) >= 2)))
    if operation == 'messages':
        prompt = ('先核对哪些判断被改了，以后怎么做。保留条件，只提出待确认认识。'
                  '纠正必须引用给定 event_id，不把单次纠正当作规律。规律需至少两个不同原件来源。'
                  '只返回JSON {"text":"不超过40字的认识","conditions":[],"event_ids":[],"kind":"pattern或correction"}。')
        return [{'role': 'system', 'content': prompt + kwargs['constraints']},
                {'role': 'user', 'content': kwargs['text']}]
    return entrypoint(*args, use_corrections=True, **kwargs)
