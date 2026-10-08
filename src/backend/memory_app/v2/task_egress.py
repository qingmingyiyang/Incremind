"""Compatibility grouping of historical task roots using kernel receipt facts."""
from ..kernel.receipt_projection import kernel_call_groups


def task_call_groups(records, runtime_root, *, task_id=None, project=None):
    tasks = {row.object_id: {'project': row.payload.get('project_id'), 'turns': {row.payload.get('turn_id')}}
             for row in records.list('recognition_tasks') if isinstance(row.payload.get('turn_id'), str)
             and (task_id is None or row.object_id == task_id)
             and (project is None or row.payload.get('project_id') == project)}
    for row in records.list('v2_turns'):
        receipt = row.payload.get('receipt', {}).get('do', {})
        task = tasks.get(receipt.get('task_id'))
        if task and task['project'] == row.payload.get('project_id') and isinstance(receipt.get('research_turn_id'), str):
            task['turns'].add(receipt['research_turn_id'])
    groups = kernel_call_groups(runtime_root, project=project, records=records)
    result = []
    for task in tasks.values():
        calls = {(call['turn_id'], call['model_request_id']): call
                 for group in groups if group['turn_id'] in task['turns'] and group['project_id'] == task['project']
                 for call in group['calls']}
        if calls:
            result.append(tuple(calls.values()))
    return result
