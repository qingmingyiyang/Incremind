"""Read-only turn stream projection and SSE delivery validation."""
from __future__ import annotations

import json
import sqlite3

from fastapi import HTTPException

from ..kernel.receipt_projection import frozen_answer_request, frozen_task_request
from .turn_frames import (
    FRAMES,
    MAX_SEQUENCE,
    STREAMS,
    failed_stream_event,
    frame_text,
    read_stream_records,
    stream_status,
)
from .workbench_transport import read_stream_response


def read_turn_stream(records, root, identity, project, start, *, view, safe_errors, accepted=None):
    announced = False
    def rejected(failure, turn, position):
        nonlocal announced
        values = []
        if turn['intent'] == 'do' and position == 0 and not announced:
            # 干活失败也先交付已核对的真实接受身份，不创建持久游标。
            announced = True
            values.append((None, 'started', {'thread_id': turn['thread_id'], 'turn':
                {key: turn[key] for key in ('id', 'intent', 'user_text')}}))
        values.append((None, 'error', failure))
        return values, True

    def snapshot(position):
        try:
            with read_stream_records(records) as reader:
                return read_snapshot(position, reader)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(404, 'workbench_not_found') from None

    def read_snapshot(position, reader):
        nonlocal announced
        head = reader.read(STREAMS, identity)
        product = reader.read('v2_turns', identity)
        if product is not None and product.payload.get('project_id') != project:
            raise HTTPException(404, 'workbench_not_found')
        binding = accepted
        if (binding is None and head is None and product is not None
                and product.payload.get('intent') == 'do'):
            # 沿原执行和冻结请求回读，观察请求不重新派发任务。
            execution = records.read('v2_task_executions', identity)
            thread = records.read('v2_threads', product.payload['thread_id'])
            if (execution is None or execution.payload.get('project_id') != project
                    or execution.payload.get('task_text') != product.payload['user_text']
                    or thread is None or thread.payload.get('project_id') != project):
                raise ValueError('invalid_headless_task_binding')
            binding = {'request': execution.payload['request'], 'turn': {'id': identity,
                **{key: product.payload[key] for key in ('thread_id', 'intent', 'user_text', 'created_at')}}}
        if binding is not None:
            original = binding['request']
            turn = binding['turn']
            if (product is None or turn['id'] != identity or turn['intent'] != 'do'
                    or any(product.payload[key] != turn[key] for key in
                        ('thread_id', 'intent', 'user_text', 'created_at'))
                    or original['turn_id'] != product.payload['receipt']['do']['kernel_turn_id']
                    or original['session_id'] != 'session-' + identity
                    or original['scope']['project_id'] != project
                    or original['desired_outcome'] != 'project.task'
                    or json.loads(original['input']['text']).get('task') != turn['user_text']):
                raise ValueError('invalid_accepted_stream_binding')
            sealed = frozen_task_request(root,
                original['turn_id'], project, question=original['input']['text'])
            if accepted is None and sealed is None:
                raise ValueError('unsealed_headless_task_binding')
            if sealed is not None and any(original.get(key) != sealed.get(key) for key in
                    ('turn_id', 'session_id', 'operation_id', 'idempotency_key', 'scope', 'input',
                     'desired_outcome', 'privacy', 'policy_versions', 'created_at')):
                raise ValueError('invalid_accepted_stream_request')
            if head is None:
                if position != 0:
                    raise HTTPException(400, 'invalid_stream_cursor')
                state = product.payload['receipt']['do']['state']
                failure = (failed_stream_event(root,
                    original['turn_id'], sealed, safe_errors=safe_errors) if sealed is not None else None)
                if failure is not None or state == 'failed':
                    return rejected(failure or {'code': 'answer_generation_failed'}, turn, position)
                if state in {'done', 'partial', 'interrupted', 'stopped'}:
                    if sealed is None:
                        raise ValueError('unsealed_task_stream_terminal')
                    return [(None, 'done', {'thread_id': turn['thread_id'],
                        'turn': view(product, read_records=reader)})], True
                if not announced:
                    # This carries already accepted metadata, not a made-up
                    # persisted cursor. The first real head owns sequence 1.
                    announced = True
                    return [(None, 'started', {'thread_id': turn['thread_id'], 'turn':
                        {key: turn[key] for key in ('id', 'intent', 'user_text')}})], False
                return [], False
        if head is None:
            # Historical/best-effort missing heads have no provable cursor.
            # Read a terminal only; never manufacture or persist sequence 1.
            if product is None or product.payload.get('intent') != 'ask':
                raise HTTPException(404, 'workbench_not_found')
            sealed = frozen_answer_request(root, identity, project,
                question=product.payload.get('user_text'))
            if sealed is None or product.payload.get('receipt', {}).get('ask') is None:
                raise HTTPException(404, 'workbench_not_found')
            if position != 0:
                raise HTTPException(400, 'invalid_stream_cursor')
            return [(None, 'done', {'thread_id': product.payload['thread_id'],
                'turn': view(product, read_records=reader)})], True
        payload = head.payload
        try:
            if (set(payload) != {'project_id', 'thread_id', 'turn', 'binding', 'sequence', 'events', 'terminal'}
                    or payload['project_id'] != project or payload['turn']['id'] != identity
                    or payload['turn']['thread_id'] != payload['thread_id']
                    or payload['turn']['intent'] not in {'ask', 'do'}
                    or type(payload['sequence']) is not int or not 1 <= payload['sequence'] <= MAX_SEQUENCE
                    or not isinstance(payload['events'], list) or len(payload['events']) != payload['sequence']
                    or set(payload['binding']) != {'kernel_turn_id', 'request'}):
                raise ValueError('invalid_stream_projection')
            frozen = payload['binding']['request']
            sealed_reader = frozen_answer_request if payload['turn']['intent'] == 'ask' else frozen_task_request
            sealed = sealed_reader(root, payload['binding']['kernel_turn_id'], project,
                question=frozen['input']['text'])
            if sealed != frozen or sealed['turn_id'] != payload['binding']['kernel_turn_id']:
                raise ValueError('invalid_stream_binding')
            if payload['turn']['intent'] == 'ask' and (sealed['turn_id'] != identity
                    or sealed['input']['text'] != payload['turn']['user_text']):
                raise ValueError('invalid_stream_binding')
            if payload['turn']['intent'] == 'do' and (product is None
                    or sealed['session_id'] != 'session-' + identity
                    or product.payload.get('receipt', {}).get('do', {}).get('kernel_turn_id') != sealed['turn_id']
                    or json.loads(sealed['input']['text']).get('task') != payload['turn']['user_text']):
                raise ValueError('invalid_stream_binding')
            if product is not None and any(product.payload[key] != payload['turn'][key]
                    for key in ('thread_id', 'intent', 'user_text')):
                raise ValueError('invalid_stream_result')
            if position > payload['sequence']:
                raise HTTPException(400, 'invalid_stream_cursor')
            terminal = payload['terminal']
            if terminal is not None and (type(terminal) is not int or terminal != payload['sequence']
                    or product is None):
                raise ValueError('invalid_stream_terminal')
            if any(not isinstance(event, dict) or type(event.get('sequence')) is not int
                    or event['sequence'] != index or event.get('kind') not in
                    {'started', 'delta', 'reset', 'status', 'done'}
                    for index, event in enumerate(payload['events'], 1)):
                raise ValueError('invalid_stream_events')
            for event in payload['events']:
                if event['kind'] == 'status':
                    stream_status(event)
            failure = failed_stream_event(root,
                payload['binding']['kernel_turn_id'], sealed, safe_errors=safe_errors)
            if failure is not None:
                return rejected(failure, payload['turn'], position)
            final = {'thread_id': payload['thread_id'], 'turn': view(product, read_records=reader)} if product is not None else None
            answer = product.payload.get('receipt', {}).get('ask', {}) if product is not None else {}
            task = product.payload.get('receipt', {}).get('do', {}) if product is not None else {}
            if ((payload['turn']['intent'] == 'ask' and isinstance(answer.get('answer'), str)
                    and 'interruption' not in answer) or (payload['turn']['intent'] == 'do'
                    and task.get('state') in {'done', 'partial'} and task.get('document_id'))):
                # The actual completed result survives a best-effort head
                # failure. An older interrupted segment cannot label the
                # new answer with its already delivered terminal cursor.
                committed = terminal is not None and payload['events'][-2:] == [
                    {'sequence': terminal - 1, 'kind': 'status', 'state': 'completed'},
                    {'sequence': terminal, 'kind': 'done'}]
                return [(terminal if committed else None, 'done', final)], True
            frames = reader.read(FRAMES, identity)
            if reader.read(STREAMS, identity) != head:
                raise HTTPException(503, 'stream_snapshot_changed')
            if frames is not None and (frames.payload.get('project_id') != project
                    or frames.payload.get('thread_id') != payload['thread_id']
                    or frame_text(frames.payload) is None):
                raise ValueError('invalid_stream_frames')
            values = []
            segment = max((event['sequence'] for event in payload['events']
                if event['kind'] == 'reset' and event.get('source') == 'frame_prefix'), default=0)
            for event in payload['events']:
                sequence, kind = event['sequence'], event['kind']
                if sequence <= position or sequence < segment:
                    continue
                if kind == 'delta':
                    if set(event) != {'sequence', 'kind', 'frame_sequence', 'offset', 'length'} or frames is None:
                        raise ValueError('invalid_stream_delta')
                    number, offset, length = (event[key] for key in ('frame_sequence', 'offset', 'length'))
                    if (any(type(value) is not int for value in (number, offset, length))
                            or not 1 <= number <= len(frames.payload['frames']) or offset != 0 or length < 1):
                        raise ValueError('invalid_stream_delta')
                    text = frames.payload['frames'][number - 1]['text']
                    if len(text) != length:
                        raise ValueError('invalid_stream_delta')
                    data = {'text': text}
                    if values and values[-1][1] == 'delta':
                        data['text'] = values.pop()[2]['text'] + text
                elif kind == 'started':
                    if set(event) != {'sequence', 'kind'}:
                        raise ValueError('invalid_stream_started')
                    data = {'thread_id': payload['thread_id'], 'turn':
                        {key: payload['turn'][key] for key in ('id', 'intent', 'user_text')}}
                elif kind == 'reset':
                    if set(event) != {'sequence', 'kind', 'source'} or event['source'] not in {'terminal_partial', 'frame_prefix'}:
                        raise ValueError('invalid_stream_reset')
                    if event['source'] == 'frame_prefix':
                        if frames is None or type(frames.payload.get('text_prefix')) is not str:
                            raise ValueError('invalid_stream_prefix')
                        data = {'text': frames.payload['text_prefix']}
                    else:
                        data = {'text': product.payload['receipt'][payload['turn']['intent']]['partial']}
                elif kind == 'status':
                    data = stream_status(event)
                else:
                    if set(event) != {'sequence', 'kind'} or sequence != terminal or final is None:
                        raise ValueError('invalid_stream_done')
                    data = final
                values.append((sequence, kind, data))
            if terminal is not None and not values:
                values.append((terminal, 'done', final))
            return values, terminal is not None
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            raise HTTPException(404, 'workbench_not_found') from None
    return read_stream_response(snapshot, start)
