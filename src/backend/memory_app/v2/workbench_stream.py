"""Read-only turn stream projection and SSE delivery validation."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from fastapi import HTTPException

from backend.recognition import RecognitionConflict
from backend.recognition.external_turn_facts import completed_answer_facts
from core.ai_kernel.contracts import validate_event_transition
from ..kernel.answer_continuations import COLLECTION as ANSWER_CONTINUATIONS, PLAN_KIND, PREPARATION_KIND
from ..kernel.receipt_projection import frozen_answer_request, frozen_task_request
from ..kernel.route_source_snapshot import native_source_connection
from ..transaction_records import TransactionRecords
from .turn_frames import (
    FRAMES,
    MAX_SEQUENCE,
    STREAMS,
    _read_json,
    failed_stream_event,
    frame_text,
    read_stream_records,
    stream_status,
)
from .workbench_transport import read_stream_response
from .external_progress import progress_frame


def _steer_event(event):
    if (set(event) != {'sequence', 'kind', 'id', 'text', 'at', 'state', 'applied_to', 'part'}
            or not isinstance(event['id'], str) or not event['id'].strip()
            or not isinstance(event['text'], str) or not event['text'].strip()
            or not isinstance(event['at'], str) or event['state'] not in {'queued', 'applied', 'unapplied'}
            or not isinstance(event['applied_to'], dict) or set(event['applied_to']) != {'part', 'step'}):
        raise ValueError('invalid_stream_steer')
    part, target = event['part'], event['applied_to']
    if ((part is not None and (type(part) is not int or part < 0))
            or (target['part'] is not None and type(target['part']) is not int)
            or target['part'] != part or target['step'] is not None and not isinstance(target['step'], str)):
        raise ValueError('invalid_stream_steer_target')
    at = datetime.fromisoformat(event['at'])
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('invalid_stream_steer_time')
    return {key: event[key] for key in ('id', 'text', 'at', 'state', 'applied_to', 'part')}


def _reset_source(event):
    source = event.get('source')
    fields = {'sequence', 'kind', 'source'} | ({'run_generation'} if source == 'steer' else set())
    if (source not in {'terminal_partial', 'frame_prefix', 'steer'} or set(event) != fields
            or source == 'steer' and (type(event['run_generation']) is not int or event['run_generation'] < 1)):
        raise ValueError('invalid_stream_reset')
    return source


def _legacy_answer_done(records, product, project, request):
    with records.begin_read() as transaction:
        reader = TransactionRecords(transaction)
        if reader.read('v2_turns', product.object_id) != product:
            raise ValueError('invalid_stream_answer_part')
        completed_answer_facts(reader, project_id=project, turn_id=product.object_id, bound_child_request=request)


def _legacy_answer_partial(records, reader, product, request):
    identity, project = product.object_id, product.payload['project_id']
    row = reader.read(ANSWER_CONTINUATIONS, identity)
    if row is None or (row.payload['state'], row.payload['project_id'], row.payload['question']) != (
            'paused', project, request['input']['text']):
        raise ValueError('invalid_stream_answer_pause')
    saved, public = row.payload['result']['receipt']['ask'], product.payload['receipt']['ask']
    if (saved['answer'] is not None or type(saved['partial']) is not str
            or saved['interruption'] not in {'stopped', 'connection', 'sleep'} or 'error' in public
            or any(saved[key] != public[key] for key in ('answer', 'partial', 'interruption', 'citations'))):
        raise ValueError('invalid_stream_answer_pause')

    # 从 Actor 原生数据库读取原暂停事件和不可变回执，GET 不初始化执行运行时。
    with native_source_connection(records) as connection:
        raw = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?', (identity,)).fetchone()
        if raw is None or _read_json(raw[0]) != request:
            raise ValueError('invalid_stream_answer_pause')
        events, prior = [], None
        for sequence, body in connection.execute(
                'SELECT sequence,event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence', (identity,)):
            prior = validate_event_transition(prior, _read_json(body))
            if (prior['sequence'] != sequence or prior['turn_id'] != identity
                    or prior['session_id'] != request['session_id'] or prior['schema_version'] != '1.0.0'
                    or prior['correlation']['operation_id'] != request['operation_id']):
                raise ValueError('invalid_stream_answer_pause')
            events.append(prior)
        if not events or prior['data']['status'] != 'waiting_approval':
            raise ValueError('invalid_stream_answer_pause')

        def capsule(reference):
            value = connection.execute('SELECT kind,payload_json FROM ai_turn_immutable_payloads '
                'WHERE turn_id=? AND payload_ref=?', (identity, reference)).fetchone()
            if value is None:
                raise ValueError('invalid_stream_answer_pause')
            return value[0], _read_json(value[1])

        plan_ref = row.payload['plan_ref']
        kind, plan = capsule(plan_ref)
        if kind not in {PLAN_KIND, PREPARATION_KIND} or plan['request'] != request:
            raise ValueError('invalid_stream_answer_pause')
        invocation = row.payload['invocation_id']
        boundary = row.payload.get('boundary_ref')
        if boundary:
            kind, proof = capsule(boundary)
            if (prior['type'] != 'turn.cancel.requested' or prior['actor'] != 'user'
                    or prior['event_id'] != row.payload['boundary_event_id']
                    or prior['data']['evidence_refs'] != [boundary] or not kind.startswith('product-answer-stop-boundary-')
                    or proof['kind'] != kind or proof['request'] != request):
                raise ValueError('invalid_stream_answer_pause')
        else:
            close = row.payload['close_refs'][-1]
            kind, proof = capsule(close)
            outcomes = [event for event in events[:-1] if event['type'] == 'tool.outcome.recorded'
                and event['correlation']['tool_call_id'] == invocation
                and set(event['data']['evidence_refs']) == {plan_ref, close}]
            if (prior['type'] != 'tool.failed' or prior['correlation']['tool_call_id'] != invocation
                    or prior['data']['capability_id'] != 'workbench.answer.execute' or len(outcomes) != 1
                    or kind != 'product-answer-closed-' + proof['model_request_id']
                    or proof['invocation_id'] != invocation or proof['question'] != request['input']['text']):
                raise ValueError('invalid_stream_answer_pause')
            raw_outcome = connection.execute('SELECT payload_json FROM ai_turn_payloads WHERE turn_id=? AND payload_ref=?',
                (identity, outcomes[0]['data']['payload_ref'])).fetchone()
            outcome = _read_json(raw_outcome[0]) if raw_outcome is not None else {}
            expected = {'turn_id': identity, 'invocation_id': invocation, 'capability_id': 'workbench.answer.execute',
                'status': 'failed', 'effect_certainty': 'confirmed_none'}
            if any(outcome.get(key) != value for key, value in expected.items()):
                raise ValueError('invalid_stream_answer_pause')
        if (proof['turn_id'] != identity or proof['project_id'] != project
                or proof['plan_ref'] != plan_ref or proof['result'] != row.payload['result']):
            raise ValueError('invalid_stream_answer_pause')
    return saved['partial']


def read_turn_stream(records, root, identity, project, start, *, view, safe_errors, accepted=None, scope_for_turn=None,
                     project_result=None, answer_part=None, uncommitted_answer_visible=None):
    announced = False
    visible_request = None
    def started(turn, frozen):
        value = {key: turn[key] for key in ('id', 'intent', 'user_text')}
        if scope_for_turn is not None:
            value['scope_kind'] = scope_for_turn(turn, frozen)
        return {'thread_id': turn['thread_id'], 'turn': value}
    def rejected(failure, turn, position):
        nonlocal announced
        values = []
        if turn['intent'] == 'do' and position == 0 and not announced:
            # 干活失败也先交付已核对的真实接受身份，不创建持久游标。
            announced = True
            values.append((None, 'started', started(turn, None)))
        values.append((None, 'error', failure))
        return values, True

    def snapshot(position):
        try:
            with read_stream_records(records) as reader:
                return read_snapshot(position, reader)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(404, 'workbench_not_found') from None

    def read_snapshot(position, reader):
        nonlocal announced, visible_request
        visible_request = None
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
                    return [(None, 'started', started(turn, sealed))], False
                return [], False
        if head is None:
            # Historical/best-effort missing heads have no provable cursor.
            # Read a terminal only; never manufacture or persist sequence 1.
            if product is None or product.payload.get('intent') != 'ask':
                raise HTTPException(404, 'workbench_not_found')
            sealed = frozen_answer_request(root, identity, project,
                question=product.payload.get('user_text'))
            if sealed is None and product.payload.get('parent_turn_id') is not None and answer_part is not None:
                request, public_text = answer_part({**product.payload, 'id': identity})
                sealed = frozen_answer_request(root, identity, project, question=request['input']['text'])
                if sealed != request or public_text != product.payload['user_text']:
                    raise ValueError('invalid_stream_answer_part')
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
            turn = payload['turn']
            if turn['intent'] == 'ask':
                visible_request = sealed
            legacy_partial = None
            if payload['turn']['intent'] == 'ask':
                if sealed['turn_id'] != identity:
                    raise ValueError('invalid_stream_binding')
                native_text = sealed['input']['text']
                part = None
                if answer_part is not None and (product is None or (product.payload.get('parent_turn_id') is not None
                        and (product.payload['user_text'] != native_text or turn['user_text'] != native_text))):
                    candidate = turn if product is None else {**turn, 'parent_turn_id': product.payload['parent_turn_id']}
                    part = answer_part(candidate, sealed)
                if part is None:
                    if turn['user_text'] != native_text:
                        raise ValueError('invalid_stream_binding')
                else:
                    request, public_text = part
                    if request != sealed:
                        raise ValueError('invalid_stream_binding')
                    if turn['user_text'] != public_text:
                        # 旧头保留原生指令。只在已核对分项中投影公开片段，不补造终态游标。
                        if turn['user_text'] != native_text or payload['terminal'] is not None:
                            raise ValueError('invalid_stream_binding')
                        if product is not None:
                            ask = product.payload['receipt']['ask']
                            if 'interruption' in ask:
                                try:
                                    legacy_partial = _legacy_answer_partial(records, reader, product, request)
                                except RecognitionConflict as error:
                                    raise ValueError('invalid_stream_answer_pause') from error
                            else:
                                _legacy_answer_done(records, product, project, request)
                        turn = {**turn, 'user_text': public_text}
            if payload['turn']['intent'] == 'do' and (product is None
                    or sealed['session_id'] != 'session-' + identity
                    or product.payload.get('receipt', {}).get('do', {}).get('kernel_turn_id') != sealed['turn_id']
                    or json.loads(sealed['input']['text']).get('task') != payload['turn']['user_text']):
                raise ValueError('invalid_stream_binding')
            if product is not None and any(product.payload[key] != turn[key]
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
                    {'started', 'delta', 'reset', 'status', 'task_progress', 'steer', 'done'}
                    for index, event in enumerate(payload['events'], 1)):
                raise ValueError('invalid_stream_events')
            for event in payload['events']:
                if event['kind'] == 'status':
                    stream_status(event)
                elif event['kind'] == 'steer':
                    _steer_event(event)
                elif event['kind'] == 'reset':
                    _reset_source(event)
            failure = failed_stream_event(root,
                payload['binding']['kernel_turn_id'], sealed, safe_errors=safe_errors)
            final = {'thread_id': payload['thread_id'], 'turn': view(product, read_records=reader)} if product is not None else None
            answer = product.payload.get('receipt', {}).get('ask', {}) if product is not None else {}
            task = product.payload.get('receipt', {}).get('do', {}) if product is not None else {}
            external_failure = failure is not None and payload['turn']['intent'] == 'do' and bool(task.get('executor'))
            if failure is not None and not external_failure:
                return rejected(failure, payload['turn'], position)
            if (final is not None and final['turn'].get('scope_kind') == 'shared'
                    and final['turn']['receipt'].get('ask', {}).get('interruption') == 'stopped'):
                # 恢复连接只交付已提交的停止回执，旧传输片段不再发送。
                committed = terminal is not None and payload['events'][-3:] == [
                    {'sequence': terminal - 2, 'kind': 'reset', 'source': 'terminal_partial'},
                    {'sequence': terminal - 1, 'kind': 'status', 'state': 'interrupted'},
                    {'sequence': terminal, 'kind': 'done'}]
                return [(terminal if committed else None, 'done', final)], True
            if (terminal is None and turn['intent'] == 'ask' and uncommitted_answer_visible is not None
                    and not uncommitted_answer_visible(sealed)):
                return [], False
            if not external_failure and ((payload['turn']['intent'] == 'ask' and isinstance(answer.get('answer'), str)
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
            legacy_next_frame = legacy_frame_end = 1
            legacy_prefix_safe = True
            if legacy_partial is not None and frames is not None:
                if frames.payload.get('turn') != payload['turn']:
                    raise ValueError('invalid_stream_answer_frames')
                prefix = frames.payload.get('text_prefix', '')
                legacy_next_frame = legacy_frame_end = frames.payload.get('text_from', 1)
                legacy_prefix_safe = legacy_partial.startswith(prefix)
                if legacy_prefix_safe:
                    offset = len(prefix)
                    for frame in frames.payload['frames'][legacy_next_frame - 1:]:
                        if not legacy_partial.startswith(frame['text'], offset):
                            break
                        offset += len(frame['text'])
                        legacy_frame_end += 1
            values = []
            segment = max((event['sequence'] for event in payload['events']
                if event['kind'] == 'reset' and (event.get('source') == 'frame_prefix'
                    or legacy_partial is not None and event.get('source') == 'steer')), default=0)
            for event in payload['events']:
                sequence, kind = event['sequence'], event['kind']
                if sequence < segment or sequence <= position and (legacy_partial is None or kind != 'delta'):
                    continue
                if external_failure and kind not in {'started', 'task_progress'}:
                    continue
                if kind == 'delta':
                    fields = {'sequence', 'kind', 'frame_sequence', 'offset', 'length'}
                    if (set(event) not in (fields, fields | {'run_generation'}) or frames is None
                            or 'run_generation' in event and (type(event['run_generation']) is not int
                                or event['run_generation'] < 1)):
                        raise ValueError('invalid_stream_delta')
                    number, offset, length = (event[key] for key in ('frame_sequence', 'offset', 'length'))
                    if (any(type(value) is not int for value in (number, offset, length))
                            or not 1 <= number <= len(frames.payload['frames']) or offset != 0 or length < 1):
                        raise ValueError('invalid_stream_delta')
                    text = frames.payload['frames'][number - 1]['text']
                    if len(text) != length:
                        raise ValueError('invalid_stream_delta')
                    if legacy_partial is not None:
                        if number != legacy_next_frame:
                            raise ValueError('invalid_stream_answer_frame_order')
                        legacy_next_frame += 1
                        if sequence <= position or number >= legacy_frame_end:
                            continue
                    data = {'text': text}
                    if values and values[-1][1] == 'delta':
                        data['text'] = values.pop()[2]['text'] + text
                elif kind == 'started':
                    if set(event) != {'sequence', 'kind'}:
                        raise ValueError('invalid_stream_started')
                    data = started({**turn, 'thread_id': payload['thread_id']}, sealed)
                elif kind == 'reset':
                    source = _reset_source(event)
                    if source == 'frame_prefix':
                        if frames is None or type(frames.payload.get('text_prefix')) is not str:
                            raise ValueError('invalid_stream_prefix')
                        if legacy_partial is not None and not legacy_prefix_safe:
                            continue
                        data = {'text': frames.payload['text_prefix']}
                    elif source == 'steer':
                        data = {'text': ''}
                    else:
                        if legacy_partial is not None:
                            raise ValueError('invalid_stream_answer_pause')
                        data = {'text': product.payload['receipt'][payload['turn']['intent']]['partial']}
                elif kind == 'steer':
                    data = _steer_event(event)
                elif kind == 'status':
                    data = stream_status(event)
                elif kind == 'task_progress':
                    if (payload['turn']['intent'] != 'do' or product is None
                            or event.get('kernel_turn_id') != sealed['turn_id']):
                        raise ValueError('invalid_external_progress_binding')
                    data = progress_frame(event, turn_id=identity, project=project, executor=task.get('executor'))
                else:
                    if set(event) != {'sequence', 'kind'} or sequence != terminal or final is None:
                        raise ValueError('invalid_stream_done')
                    data = final
                values.append((sequence, kind, data))
            if external_failure:
                values.append((None, 'error', failure))
                return values, True
            if legacy_partial is not None:
                ask = final['turn']['receipt']['ask']
                if any(ask.get(key) != answer.get(key) for key in ('answer', 'interruption', 'partial')) or 'error' in ask:
                    raise ValueError('invalid_stream_answer_pause')
                # 只交付原暂停回执，保留已有帧游标及原生待继续状态。
                values.append((None, 'done', final))
                return values, True
            if terminal is not None and not values:
                values.append((terminal, 'done', final))
            return values, terminal is not None
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            raise HTTPException(404, 'workbench_not_found') from None
    return read_stream_response(snapshot, start, project_result=project_result,
        uncommitted_visible=(lambda: visible_request is None or uncommitted_answer_visible(visible_request))
        if uncommitted_answer_visible is not None else None)
