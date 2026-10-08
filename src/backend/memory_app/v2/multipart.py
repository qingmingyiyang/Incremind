"""Persist routed parts and reuse the existing single-intent executors."""
import asyncio
from uuid import uuid4

from fastapi import HTTPException
from ..workspace_contracts import _now
from .turn_execution import TurnExecutionService, safe_failure
from .intent import parse_scope_tag


def part_state(turn):
    intent, receipt = turn['intent'], turn['receipt']
    if intent in {'remember', 'do'}:
        return receipt[intent]['state']
    return 'done'


def project_parts(records, parent, view):
    receipt = parent['receipt']
    parts = []
    for part, identity in zip(receipt['parts'], parent['part_turn_ids']):
        child = records.read('v2_turns', identity)
        if child is not None and child.payload.get('parent_turn_id') == parent['id']:
            turn = view(child)
            part = {**part, 'receipt':turn['receipt'], 'state':part_state(turn),
                    'turn_id':child.object_id, 'project_id':child.payload['project_id']}
        parts.append(part)
    return {**receipt, 'parts':parts}


async def execute_parts(body, plan, *, records, instance, execute, project, scene,
                        request_key, complete, on_started=None, on_delta=None):
    now = _now()
    identity = 'turn-' + uuid4().hex
    thread_id = body.get('thread_id') or 'thread-' + uuid4().hex
    children = ['turn-' + uuid4().hex for _ in plan.parts]
    inherited_tag = parse_scope_tag(body.get('text', ''))[0]
    remember = next((index for index, part in enumerate(plan.parts) if part.intent == 'remember'), None)
    if body.get('item_id') and remember is None:
        raise HTTPException(400, 'multipart_attachment_without_remember')
    route = {key:getattr(plan, key) for key in ('mode', 'usage', 'egress_receipt_id')}
    parts = [{'index':index, 'intent':part.intent, 'span':part.span,
              'instruction':part.instruction, 'depends_on':part.depends_on,
              'state':'waiting', 'receipt':{}, 'turn_id':None, 'project_id':None} for index, part in enumerate(plan.parts)]
    parent = {'id':identity, 'project_id':project, 'thread_id':thread_id,
              'intent':'multi', 'user_text':body.get('text', ''), 'created_at':now,
              'updated_at':now, 'instance':instance, 'receipt':{'parts':parts, 'route':route},
              'part_turn_ids':children, 'route_plan':plan.model_dump(), 'scene':scene}
    with records.begin() as tx:
        thread = tx.read('v2_threads', thread_id)
        if thread is not None and thread.payload['project_id'] != project:
            raise HTTPException(404, 'workbench_not_found')
        if thread is None:
            tx.put('v2_threads', thread_id, {'project_id':project, 'title':body.get('text', '')[:40],
                   'created_at':now, 'updated_at':now}, expected_revision=0)
        tx.put('v2_turns', identity, parent, expected_revision=0)
        tx.commit()
    if on_started:
        on_started({'thread_id':thread_id, 'turn':{'id':identity, 'intent':'multi',
            'user_text':parent['user_text']}, 'route':route, 'parts':[{'index':index, 'intent':part.intent,
            'span':part.span, 'depends_on':part.depends_on, 'state':'waiting'} for index, part in enumerate(plan.parts)]})
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
                    dependency_turns=[results[dependency]['turn']['id'] for dependency in part.depends_on])
            executor = TurnExecutionService(records, execute_child, instance=instance, internal_keys=True,
                internal_identity='part-' + children[index])
            result = await executor.run(child_body, f'{request_key}:{index}' if request_key else None,
                on_delta=(lambda text: on_delta({'part':index, 'text':text})) if on_delta else None)
            state = part_state(result['turn'])
            results[index] = None if state == 'failed' else result
            update(index, state=state, receipt=result['turn']['receipt'], turn_id=result['turn']['id'],
                   project_id=records.read('v2_turns', result['turn']['id']).payload['project_id'])
        except Exception as error:
            results[index] = None
            update(index, state='failed', receipt={}, error=safe_failure(error))
        finally:
            ready[index].set()
    await asyncio.gather(*(run(index, part) for index, part in enumerate(plan.parts)))
    with records.begin() as tx:
        row = tx.read('v2_turns', identity)
        payload = {**row.payload, 'updated_at':_now()}
        response = {'thread_id':thread_id, 'turn':{key:payload[key] for key in
            ('id', 'thread_id', 'intent', 'user_text', 'created_at', 'receipt')}}
        tx.put('v2_turns', identity, payload, expected_revision=row.revision)
        complete(tx, request_key, response)
        tx.commit()
    return response
