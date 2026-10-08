"""Read-only projection of retained legacy task receipts."""
from ..task_status import get_task_status


def task_receipt(status, previous=None):
    previous = previous or {}
    state = status['status']
    state = 'done' if state == 'completed' else 'waiting_approval' if state == 'waiting_approval' else 'running' if state in {'queued', 'running'} else 'failed'
    research = 'research_turn_id' in previous
    total = 4 if research else 3
    done = 3 if state == 'done' else max(previous.get('progress', {}).get('done', 0), 1 if state == 'waiting_approval' else 0)
    if research:
        done = 4 if state == 'done' else max(previous.get('progress', {}).get('done', 0), 2 if state == 'waiting_approval' else 0)
    return {**({key: previous[key] for key in ('research_turn_id', 'experts')} if research else {}), 'task_id': status['task_id'], 'title': status['title'], 'state': state,
        'progress': {'done': done, 'total': total}, 'document_id': status.get('document_id')}


def read_task(records, scope, documents, previous, *, models=None, usage_reader=None):
    if previous.get('task_id') is None:
        if previous.get('kernel_turn_id'):
            from .do_context import kernel_task_context
            return {**previous, **kernel_task_context(records, scope.project_id, previous['kernel_turn_id'])}
        return previous
    status = get_task_status(records, scope, previous['task_id'], document_namespace=documents.namespace_id)
    from .do_context import task_context
    return {**task_receipt(status, previous), **task_context(records, scope, status, previous, models, usage_reader)}
