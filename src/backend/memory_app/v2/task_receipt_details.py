"""Small read projection of a child's immutable tool history."""
from collections.abc import Mapping


def task_receipt_details(turn_id, events, load_payload):
    tools, recalled = {}, []
    states = {'tool.intent.recorded':'waiting','tool.started':'running',
              'tool.completed':'done','tool.failed':'failed','tool.outcome.unknown':'failed'}
    for event in events:
        state = states.get(event['type'])
        data, identity = event.get('data', {}), event.get('correlation', {}).get('tool_call_id')
        if state is None or not identity or not isinstance(data.get('capability_id'), str):
            continue
        tools[identity] = {'id':identity, 'capability_id':data['capability_id'], 'state':state}
        ref = data.get('payload_ref')
        if (event['type'] == 'tool.completed' and data['capability_id'] == 'memory.recall'
                and isinstance(ref, str) and ref.startswith(f'crp://session/{turn_id}/')):
            items = load_payload(ref)
            for item in items if isinstance(items, list) else ():
                if not isinstance(item, Mapping):
                    continue
                title, text = item.get('title'), item.get('content') or item.get('summary')
                recalled.append({'title':title[:160] if isinstance(title,str) else '认识',
                                 'text':text[:2000] if isinstance(text,str) else ''})
    return {'tools':list(tools.values()), 'recalled':recalled}
