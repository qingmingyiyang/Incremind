import asyncio
from time import monotonic

from fastapi import HTTPException


def attachment_ids(body):
    if 'item_ids' not in body:
        return [body['item_id']] if body.get('item_id') else []
    values = body['item_ids']
    if ('item_id' in body or not isinstance(values, list) or not 1 <= len(values) <= 10
            or any(not isinstance(value, str) or not value.strip() for value in values)):
        raise HTTPException(400, 'invalid_turn_fields')
    return values


def expand_parts(parts, items):
    expanded, mapping = [], {}
    for index, part in enumerate(parts):
        values = items if part.intent == 'remember' and items else [None]
        mapping[index] = list(range(len(expanded), len(expanded) + len(values)))
        expanded.extend((part, item) for item in values)
    return [(part.model_copy(update={'depends_on': [target for dependency in part.depends_on
             for target in mapping[dependency]]}), item) for part, item in expanded]


def allocate_attachment_windows(contents, question, *, titles, admission):
    from .contextual_chunks import dynamic_contextual_windows
    if len(contents) != len(titles):
        raise ValueError('attachment_window_titles_changed')
    selected = []
    for content, title in zip(contents, titles):
        minimum = None
        def reserve(candidate):
            nonlocal minimum
            if candidate.windows and (minimum is None or len(candidate.excerpt) < len(minimum.excerpt)):
                minimum = candidate
            # 只观察原算法允许的完整候选，先为全部附件保留证据。
            return False
        dynamic_contextual_windows(content, question, title=title, admission=reserve)
        if minimum is None:
            return None
        selected.append(minimum)
    if not admission(tuple(selected)):
        return None
    for index, (content, title) in enumerate(zip(contents, titles)):
        def admit(candidate):
            proposed = list(selected)
            proposed[index] = candidate
            return admission(tuple(proposed))
        grown = dynamic_contextual_windows(content, question, title=title, admission=admit)
        if grown.windows:
            selected[index] = grown
    return tuple(selected)


class OriginalReadiness:
    def __init__(self):
        self.waiters = {}

    def notify(self, project, identity):
        for event in tuple(self.waiters.get((project, identity), ())):
            event.set()

    async def wait(self, records, project, identity, deadline, *, turn_id=None, active=None):
        key, event = (project, identity), asyncio.Event()
        self.waiters.setdefault(key, set()).add(event)
        try:
            while True:
                # 先登记再读；清除通知后重读，状态变化不会丢失。
                event.clear()
                row = records.read('workspace_items', identity)
                if row is None or row.payload.get('project_id') != project or row.payload.get('status') == 'failed':
                    raise HTTPException(409, 'dependency_failed')
                if row.payload.get('status') in {'ready', 'confirming', 'confirmed'}:
                    if not row.payload.get('source_text'):
                        raise HTTPException(409, 'dependency_failed')
                    return
                turn = records.read('v2_turns', turn_id) if turn_id is not None else None
                if ((turn is not None and turn.payload.get('receipt', {}).get('remember', {}).get('state') == 'failed')
                        or active is not None and not active()):
                    raise HTTPException(409, 'dependency_failed')
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise HTTPException(409, 'dependency_timeout')
                try:
                    await asyncio.wait_for(event.wait(), remaining)
                except TimeoutError:
                    raise HTTPException(409, 'dependency_timeout') from None
        finally:
            self.waiters[key].discard(event)
            if not self.waiters[key]:
                self.waiters.pop(key)
