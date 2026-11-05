"""Persist routed parts and reuse the existing single-intent executors."""
import asyncio
from uuid import uuid4

from fastapi import HTTPException
from ..workspace_contracts import _now
from .turn_execution import TurnExecutionService, safe_failure
from .intent import parse_scope_tag, scope_tag_match
from .attachments import attachment_ids, expand_parts
from .route import internal_route_dump


def part_state(turn):
    intent, receipt = turn['intent'], turn['receipt']
    if intent in {'remember', 'do'}:
        return receipt[intent]['state']
    return 'done'


def part_finished(turn):
    # 仅公开结束状态可供后续纠正复用；未知和中断继续保留恢复入口。
    return part_state(turn) in {'done', 'partial', 'failed', 'not_started', 'stopped', 'cancelled'}


def grouped_remember(records, identities, view):
    from .route_reuse import dependency_parent
    results, states = [], []
    for parent_id, identity in identities:
        dependency_parent(records, parent_id, identity)
        row = records.read('v2_turns', identity)
        turn = view(row)
        results.append({'turn_id': identity, 'project_id': row.payload['project_id'], 'receipt': turn['receipt']})
        states.append(part_state(turn))
    state = ('done' if all(value == 'done' for value in states) else
        'processing' if any(value in {'running', 'processing', 'waiting'} for value in states) else
        'partial' if any(value in {'done', 'partial'} for value in states) else 'failed')
    return {'remember': {'state': state, 'item_id': None, 'items': results}}


def project_parts(records, parent, view, *, instance=None):
    receipt = parent['receipt']
    parts = []
    for part, identity in zip(receipt['parts'], parent['part_turn_ids']):
        if part.get('source_turn_ids'):
            grouped = grouped_remember(records, [(parent['id'], member_id) for member_id in part['source_turn_ids']], view)
            parts.append({**part, 'receipt': grouped, 'state': grouped['remember']['state']})
            continue
        child = records.read('v2_turns', identity)
        if child is not None:
            from .route_reuse import dependency_parent
            dependency_parent(records, parent['id'], identity)
            turn = view(child)
            part = {**part, 'receipt':turn['receipt'], 'state':part_state(turn),
                    'turn_id':child.object_id, 'project_id':child.payload['project_id']}
        if (instance is not None and parent.get('instance') != instance
                and part['state'] in {'waiting', 'running', 'processing'}):
            part = {**part, 'state': 'interrupted', 'error': 'turn_interrupted'}
        parts.append(part)
    return {**receipt, 'parts':parts}


async def execute_parts(body, plan, *, records, instance, execute, project, scene,
                        request_key, complete, on_started=None, on_delta=None, on_frame=None, learning=None,
                        message_accepted_at=None, acceptance=None):
    now = _now()
    items = attachment_ids(body)
    expanded = expand_parts(plan.parts, items) if 'item_ids' in body else [(part, None) for part in plan.parts]
    execution_parts = [part for part, _ in expanded]
    identity = acceptance['public_id'] if acceptance is not None else 'turn-' + uuid4().hex
    thread_id = body.get('thread_id') or (acceptance['thread_id'] if acceptance is not None else 'thread-' + uuid4().hex)
    children = ['turn-' + uuid4().hex for _ in execution_parts]
    inherited_tag = parse_scope_tag(body.get('text', ''))[0]
    scope = scope_tag_match(body.get('text', ''))
    title = parse_scope_tag(body.get('text', ''))[2] if scope and scope[4] else body.get('text', '')
    remember = next((index for index, part in enumerate(execution_parts) if part.intent == 'remember'), None)
    if items and remember is None:
        raise HTTPException(400, 'multipart_attachment_without_remember')
    route = {key:getattr(plan, key) for key in ('mode', 'usage', 'egress_receipt_id')}
    parts = [{'index':index, 'intent':part.intent, 'span':part.span,
              'instruction':part.instruction, 'depends_on':part.depends_on,
              'state':'waiting', 'receipt':{}, 'turn_id':None, 'project_id':None} for index, part in enumerate(execution_parts)]
    parent = {'id':identity, 'project_id':project, 'thread_id':thread_id,
              'intent':'multi', 'user_text':body.get('text', ''), 'created_at':now,
              'updated_at':now, 'instance':instance, 'receipt':{'parts':parts, 'route':route},
              'part_turn_ids':children, 'route_plan':internal_route_dump(plan), 'scene':scene}
    if 'item_ids' in body:
        parent['attachment_item_ids'] = items
    with records.begin() as tx:
        thread = tx.read('v2_threads', thread_id)
        if thread is not None and thread.payload['project_id'] != project:
            raise HTTPException(404, 'workbench_not_found')
        if thread is None:
            tx.put('v2_threads', thread_id, {'project_id':project, 'title':title[:40],
                   'created_at':now, 'updated_at':now}, expected_revision=0)
        tx.put('v2_turns', identity, parent, expected_revision=0)
        if learning is not None:
            learning.record_message(tx, public_id=identity, project_id=project, thread_id=thread_id,
                text=parent['user_text'], accepted_at=message_accepted_at or now)
        tx.commit()
    if on_started:
        on_started({'thread_id':thread_id, 'turn':{'id':identity, 'intent':'multi',
            'user_text':parent['user_text']}, 'route':route, 'parts':[{'index':index, 'intent':part.intent,
            'span':part.span, 'depends_on':part.depends_on, 'state':'waiting'} for index, part in enumerate(execution_parts)]})
    results, ready = {}, [asyncio.Event() for _ in parts]

    def update(index, **changes):
        with records.begin() as tx:
            row = tx.read('v2_turns', identity)
            receipt = row.payload['receipt']
            current = list(receipt['parts'])
            current[index] = {**current[index], **changes}
            tx.put('v2_turns', identity, {**row.payload, 'updated_at':_now(),
                'receipt':{**receipt, 'parts':current}}, expected_revision=row.revision)
            tx.commit()

    async def run(index, part):
        try:
            for dependency in part.depends_on:
                await ready[dependency].wait()
            failed = [dependency for dependency in part.depends_on if results.get(dependency) is None]
            if failed:
                update(index, state='not_started', receipt={}, error='dependency_failed')
                results[index] = None
                return
            update(index, state='running')
            child_body = {'project_id':project, 'thread_id':thread_id, 'intent':part.intent, 'text':part.span}
            if expanded[index][1] is not None:
                child_body['item_id'] = expanded[index][1]
            if body.get('item_id') and index == remember:
                child_body['item_id'] = body['item_id']
            if part.intent == 'inspiration':
                # Inspirations retain the existing inbox owner, not a thread
                # borrowed from the ordinary project.
                child_body.pop('thread_id')
            async def execute_child(value, **options):
                return await execute(value, **options, forced_turn_id=children[index], parent_turn_id=identity,
                    forced_scene=scene, forced_project=project if inherited_tag else None,
                    instruction=part.instruction, situation=part.situation,
                    data_operation=getattr(part, 'data_operation', None),
                    dependency_turns=[results[dependency]['turn']['id'] for dependency in part.depends_on])
            executor = TurnExecutionService(records, execute_child, instance=instance, internal_keys=True,
                internal_identity='part-' + children[index])
            result = await executor.run(child_body, f'{request_key}:{index}' if request_key else None,
                on_delta=(lambda text: on_delta({'part':index, 'text':text})) if on_delta else None,
                on_frame=(lambda sequence, event, value: on_frame(None, event, {**value, 'part': index})
                    if event != 'started' else None) if on_frame else None)
            state = part_state(result['turn'])
            results[index] = None if state == 'failed' else result
            update(index, state=state, receipt=result['turn']['receipt'], turn_id=result['turn']['id'],
                   project_id=records.read('v2_turns', result['turn']['id']).payload['project_id'])
        except Exception as error:
            results[index] = None
            update(index, state='failed', receipt={}, error=safe_failure(error))
        finally:
            ready[index].set()
    await asyncio.gather(*(run(index, part) for index, part in enumerate(execution_parts)))
    with records.begin() as tx:
        row = tx.read('v2_turns', identity)
        payload = {**row.payload, 'updated_at':_now()}
        response = {'thread_id':thread_id, 'turn':{key:payload[key] for key in
            ('id', 'thread_id', 'intent', 'user_text', 'created_at', 'receipt')}}
        tx.put('v2_turns', identity, payload, expected_revision=row.revision)
        complete(tx, request_key, response)
        tx.commit()
    if learning is not None:
        learning.observe_public(identity)
    return response
