import json

from ..kernel.receipt_projection import frozen_task_assignment
from ..original_sources import source_store


def product_task_display(records, project, turn_id, receipt):
    if not turn_id or not receipt.get('kernel_turn_id'):
        return receipt
    execution = records.read('v2_task_executions', turn_id)
    product = records.read('v2_turns', turn_id)
    if execution is None or product is None:
        return receipt
    request = execution.payload['request']
    text = execution.payload.get('task_text')
    if (execution.payload.get('project_id') != project or product.payload.get('project_id') != project
            or product.payload.get('intent') != 'do' or not isinstance(text, str)
            or text != product.payload.get('user_text') or request.get('session_id') != 'session-' + turn_id
            or request.get('scope', {}).get('project_id') != project
            or request.get('turn_id') != receipt['kernel_turn_id']
            or product.payload.get('receipt', {}).get('do', {}).get('kernel_turn_id') != request['turn_id']
            or request.get('execution_policy', {}).get('template_version') != 3):
        return receipt
    host_input = request['input']['text']
    try:
        host = json.loads(host_input)
    except (ValueError, TypeError):
        return receipt
    if not isinstance(host, dict) or host.get('task') != text:
        return receipt
    projected = dict(receipt)
    root = source_store(records).root.parent
    for field in ('division', 'experts'):
        if not receipt.get(field):
            continue
        rows = []
        for row in receipt[field]:
            # 只改真实主轮次派发的host封装显示，用户正文及不可变任务保持原样。
            assignment = (frozen_task_assignment(root, request, row.get('assignment_id'))
                if row.get('goal') == host_input else None)
            rows.append({**row, 'goal': text} if assignment is not None else row)
        projected[field] = rows
    return projected
