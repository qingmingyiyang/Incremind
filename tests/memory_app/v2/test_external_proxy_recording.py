"""录制观察真实协议副本，不替代转发器或入库服务。"""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import importlib
import json
import zlib

import pytest


QUESTION = '请说明这个项目的交付目标、依赖关系和下一轮具体执行步骤。'
ANSWER = '先确认项目交付目标与现有边界，再核对直接依赖和验收证据。下一轮按任务拆分执行，保存完整结果，遇到失败保留原始记录并修复原因，最后汇总可验证的成果和剩余工作。'


def module():
    return importlib.import_module('backend.memory_app.v2.external_proxy_recording')


def request(protocol):
    key = 'input' if protocol == 'responses' else 'messages'
    return {key: [{'role': 'system', 'content': '只依据给定事实回答。'},
        {'role': 'user', 'content': QUESTION}]}


def response(protocol, text=ANSWER):
    if protocol == 'chat_completions':
        return {'id': 'chat-synthetic', 'choices': [{'index': 0, 'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': text}}]}
    if protocol == 'messages':
        return {'id': 'msg-synthetic', 'type': 'message', 'role': 'assistant',
            'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': text}]}
    return {'id': 'resp-synthetic', 'status': 'completed', 'output': [{'type': 'message',
        'id': 'out-synthetic', 'status': 'completed', 'role': 'assistant',
        'content': [{'type': 'output_text', 'text': text}]}]}


def event(value, *, name=None, newline='\n'):
    prefix = ('event: ' + name + newline) if name else ''
    return (prefix + 'data: ' + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        + newline * 2).encode('utf-8')


def stream(protocol, text=ANSWER, *, newline='\n'):
    pieces = [text[:9], text[9:]]
    if protocol == 'chat_completions':
        values = [event({'id': 'chat-synthetic', 'choices': [{'index': 0,
            'delta': {'role': 'assistant' if index == 0 else 'assistant', 'content': piece},
            'finish_reason': None}]}, newline=newline) for index, piece in enumerate(pieces)]
        values.append(event({'id': 'chat-synthetic', 'choices': [{'index': 0, 'delta': {},
            'finish_reason': 'stop'}]}, newline=newline))
        values.append(event({'id': 'chat-synthetic', 'choices': [], 'usage': {'completion_tokens': 100}}, newline=newline))
        return b''.join(values) + event('[DONE]', newline=newline)
    if protocol == 'messages':
        values = [
            {'type': 'message_start', 'message': {'id': 'msg-synthetic', 'type': 'message',
                'role': 'assistant', 'content': [], 'stop_reason': None}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
            *[{'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': piece}} for piece in pieces],
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}},
            {'type': 'message_stop'},
        ]
    else:
        values = [
            {'type': 'response.created', 'response': {'id': 'resp-synthetic', 'status': 'in_progress', 'output': []}},
            *[{'type': 'response.output_text.delta', 'item_id': 'out-synthetic', 'output_index': 0,
                'content_index': 0, 'delta': piece} for piece in pieces],
            {'type': 'response.output_text.done', 'item_id': 'out-synthetic', 'output_index': 0,
                'content_index': 0, 'text': text},
            {'type': 'response.completed', 'response': response(protocol, text)},
        ]
    return b''.join(event(value, name=value['type'], newline=newline) for value in values)


def record(protocol, body, *, payload=None, content_type='application/json', status=200,
           chunk=4096, credentials=(), content_encoding=''):
    recorder = module().ConversationRecorder(protocol, request(protocol) if payload is None else payload,
        credentials=credentials)
    relayed = []
    for start in range(0, len(body), chunk):
        original = body[start:start + chunk]
        assert recorder.feed(original) is None
        relayed.append(original)
    assert b''.join(relayed) == body
    options = {'content_encoding': content_encoding} if content_encoding else {}
    result = recorder.finish(status_code=status, content_type=content_type, **options)
    assert recorder.finish(status_code=status, content_type=content_type) is None
    return result


def compressed(body, encoding):
    compressor = zlib.compressobj(wbits=31 if encoding == 'gzip' else 15)
    return compressor.compress(body) + compressor.flush()


@pytest.mark.parametrize('protocol', ['chat_completions', 'messages', 'responses'])
@pytest.mark.parametrize('encoding', ['gzip', 'deflate'])
@pytest.mark.parametrize('media', ['application/json', 'text/event-stream'])
def test_compressed_complete_conversation_preserves_original_wire(protocol, encoding, media):
    body = (json.dumps(response(protocol), ensure_ascii=False).encode()
        if media == 'application/json' else stream(protocol))
    wire = compressed(body, encoding)
    text = record(protocol, wire, content_type=media, content_encoding=encoding, chunk=1)
    assert text is not None and QUESTION in text and ANSWER in text


@pytest.mark.parametrize('encoding', ['gzip', 'deflate'])
def test_compressed_bomb_abandons_recording_without_partial_text(encoding):
    from backend.memory_app.v2.policies import get
    maximum = get('proxy_record', version='@1').max_response_bytes
    body = json.dumps(response('chat_completions'), ensure_ascii=False).encode()
    # 空白不改变合法终态；没有解压限额时，这份正文会被错误录制。
    wire = compressed(body + b' ' * maximum, encoding)
    assert len(wire) < maximum
    recorder = module().ConversationRecorder('chat_completions', request('chat_completions'))
    recorder.feed(wire)
    assert recorder.finish(status_code=200, content_type='application/json', content_encoding=encoding) is None
    assert recorder._buffer == b'' and recorder._history is None and recorder._credentials == ()


@pytest.mark.parametrize('encoding', ['gzip', 'deflate'])
@pytest.mark.parametrize('damage', ['truncated', 'trailing', 'second_member', 'checksum'])
def test_compressed_incomplete_or_trailing_bytes_never_record(encoding, damage):
    body = json.dumps(response('chat_completions'), ensure_ascii=False).encode()
    wire = compressed(body, encoding)
    if damage == 'truncated':
        wire = wire[:-1]
    elif damage == 'trailing':
        wire += b'junk'
    elif damage == 'second_member':
        wire += compressed(b' ', encoding)
    else:
        damaged = bytearray(wire)
        damaged[-1] ^= 1
        wire = bytes(damaged)
    assert record('chat_completions', wire, content_encoding=encoding, chunk=7) is None


@pytest.mark.parametrize('encoding', ['br', 'gzip, deflate', None, 1])
def test_unknown_content_encoding_cannot_treat_raw_text_as_complete(encoding):
    body = json.dumps(response('chat_completions'), ensure_ascii=False).encode()
    recorder = module().ConversationRecorder('chat_completions', request('chat_completions'))
    recorder.feed(body)
    assert recorder.finish(status_code=200, content_type='application/json', content_encoding=encoding) is None


@pytest.mark.parametrize('encoding', ['', 'identity', ' Identity '])
def test_explicit_identity_content_encoding_preserves_original_recording(encoding):
    body = json.dumps(response('chat_completions'), ensure_ascii=False).encode()
    recorder = module().ConversationRecorder('chat_completions', request('chat_completions'))
    recorder.feed(body)
    text = recorder.finish(status_code=200, content_type='application/json', content_encoding=encoding)
    assert text is not None and QUESTION in text and ANSWER in text


@pytest.mark.parametrize('protocol', ['chat_completions', 'messages', 'responses'])
def test_json_complete_history_and_final_answer(protocol):
    payload = request(protocol)
    key = 'input' if protocol == 'responses' else 'messages'
    payload[key][1:1] = [{'role': 'user', 'content': '之前讨论过交付文档的范围。'},
        {'role': 'assistant', 'content': '已核对原文与修订，保留需要的边界。'}]
    original = deepcopy(payload)
    text = record(protocol, json.dumps(response(protocol), ensure_ascii=False).encode(), payload=payload)
    assert payload == original
    assert text is not None and QUESTION in text and ANSWER in text
    assert '之前讨论过' in text and '已核对原文' in text
    assert text.index('之前讨论过') < text.index(QUESTION) < text.index(ANSWER)


@pytest.mark.parametrize('protocol', ['chat_completions', 'messages', 'responses'])
@pytest.mark.parametrize('chunk,newline', [(1, '\n'), (7, '\r\n'), (4096, '\r')])
def test_arbitrary_sse_chunks_preserve_unicode_and_wire(protocol, chunk, newline):
    data = b'\xef\xbb\xbf' + (': keepalive' + newline * 2).encode() + stream(protocol, newline=newline)
    text = record(protocol, data, content_type='text/event-stream; charset=utf-8', chunk=chunk)
    assert text is not None and QUESTION in text and ANSWER in text


@pytest.mark.parametrize('protocol', ['chat_completions', 'messages', 'responses'])
@pytest.mark.parametrize('bad', ['tool', 'truncated', 'error', 'wrong_role', 'empty'])
def test_json_incomplete_or_tool_output_never_records(protocol, bad):
    value = response(protocol)
    if protocol == 'chat_completions':
        item = value['choices'][0]
        if bad == 'tool': item['message']['tool_calls'] = [{'id': 'tool-synthetic'}]
        elif bad == 'truncated': item['finish_reason'] = 'length'
        elif bad == 'wrong_role': item['message']['role'] = 'user'
        elif bad == 'empty': item['message']['content'] = ''
    elif protocol == 'messages':
        if bad == 'tool': value['content'].append({'type': 'tool_use', 'id': 'tool-synthetic'})
        elif bad == 'truncated': value['stop_reason'] = 'max_tokens'
        elif bad == 'wrong_role': value['role'] = 'user'
        elif bad == 'empty': value['content'] = []
    else:
        if bad == 'tool': value['output'].append({'type': 'function_call', 'name': 'synthetic'})
        elif bad == 'truncated': value['status'] = 'incomplete'
        elif bad == 'wrong_role': value['output'][0]['role'] = 'user'
        elif bad == 'empty': value['output'] = []
    if bad == 'error': value['error'] = {'message': 'synthetic failure'}
    assert record(protocol, json.dumps(value).encode()) is None


@pytest.mark.parametrize('protocol', ['chat_completions', 'messages', 'responses'])
def test_sse_missing_terminal_or_error_after_terminal_never_records(protocol):
    data = stream(protocol)
    ending = event('[DONE]') if protocol == 'chat_completions' else (
        event({'type': 'message_stop'}, name='message_stop') if protocol == 'messages'
        else event({'type': 'response.completed', 'response': response(protocol)}, name='response.completed'))
    assert data.endswith(ending)
    assert record(protocol, data[:-len(ending)], content_type='text/event-stream') is None
    late_error = event({'type': 'error', 'error': {'type': 'synthetic_error'}}, name='error')
    assert record(protocol, data + late_error, content_type='text/event-stream') is None


@pytest.mark.parametrize('protocol', ['chat_completions', 'messages', 'responses'])
def test_short_error_and_unknown_content_type_skip_without_relay_changes(protocol):
    body = json.dumps(response(protocol, '好。')).encode()
    assert record(protocol, body) is None
    assert record(protocol, json.dumps(response(protocol)).encode(), status=500) is None
    assert record(protocol, json.dumps(response(protocol)).encode(), content_type='text/plain') is None


@pytest.mark.parametrize('protocol', ['chat_completions', 'messages', 'responses'])
def test_original_history_snapshot_and_secret_redaction(protocol):
    key = 'input' if protocol == 'responses' else 'messages'
    synthetic = 'sk-' + 'Q' * 28
    literal = 'synthetic-nonpattern-credential'
    payload = request(protocol)
    payload[key][-1]['content'] += ' 原始片段 ' + synthetic + ' ' + literal
    recorder = module().ConversationRecorder(protocol, payload, credentials=(literal,))
    payload[key][-1]['content'] = 'INJECTED_MEMORY_MUST_NOT_BE_RECORDED'
    data = stream(protocol, ANSWER + synthetic + ' ' + literal)
    for byte in data: recorder.feed(bytes([byte]))
    text = recorder.finish(status_code=200, content_type='text/event-stream')
    assert text is not None and '原始片段' in text
    assert synthetic not in text and literal not in text
    assert 'INJECTED_MEMORY_MUST_NOT_BE_RECORDED' not in text
    assert '[REDACTED_SECRET]' in text


def test_chat_multiple_choices_and_function_call_are_not_final_conversations():
    value = response('chat_completions')
    value['choices'].append(deepcopy(value['choices'][0]))
    assert record('chat_completions', json.dumps(value).encode()) is None
    value = response('chat_completions')
    value['choices'][0]['message']['function_call'] = {'name': 'synthetic'}
    assert record('chat_completions', json.dumps(value).encode()) is None


@pytest.mark.parametrize('protocol', ['chat_completions', 'messages', 'responses'])
def test_original_tool_history_and_multimodal_history_are_not_silently_lost(protocol):
    key = 'input' if protocol == 'responses' else 'messages'
    payload = request(protocol)
    payload[key].insert(1, {'role': 'tool', 'content': 'tool result'})
    assert record(protocol, json.dumps(response(protocol)).encode(), payload=payload) is None
    payload = request(protocol)
    payload[key][-1]['content'] = [{'type': 'input_text' if protocol == 'responses' else 'text',
        'text': QUESTION}, {'type': 'image_url', 'image_url': {'url': 'synthetic'}}]
    assert record(protocol, json.dumps(response(protocol)).encode(), payload=payload) is None


@pytest.mark.parametrize('name', ['previous_response_id', 'conversation'])
def test_responses_server_side_history_is_not_claimed_complete(name):
    payload = request('responses')
    payload[name] = 'server-side-synthetic'
    assert record('responses', json.dumps(response('responses')).encode(), payload=payload) is None


def test_malformed_json_utf8_duplicate_keys_and_incomplete_sse_frame_skip():
    for data in (b'{', b'\xff', b'{"choices":[],"choices":[]}'):
        assert record('chat_completions', data) is None
    assert record('chat_completions', stream('chat_completions')[:-1], content_type='text/event-stream') is None


def test_policy_is_pure_frozen_explicit_version_and_limits_abandon_only_recording(monkeypatch):
    from backend.memory_app.v2.policies import ACTIVE, get
    policy = get('proxy_record', version='@1')
    with pytest.raises(FrozenInstanceError): policy.max_response_bytes = 1
    monkeypatch.setitem(ACTIVE, 'proxy_record', '@999')
    body = b'x' * (policy.max_response_bytes + 1)
    assert record('chat_completions', body, chunk=257) is None
    payload = request('chat_completions')
    payload['messages'][-1]['content'] = 'x' * (policy.max_history_bytes + 1)
    assert record('chat_completions', json.dumps(response('chat_completions')).encode(), payload=payload) is None


def test_invalid_credentials_object_or_feed_type_cannot_enter_recording():
    assert record('chat_completions', json.dumps(response('chat_completions')).encode(),
        credentials={'Authorization': 'must-not-be-retained'}) is None
    recorder = module().ConversationRecorder('chat_completions', request('chat_completions'))
    assert recorder.feed('not bytes') is None
    assert recorder.finish(status_code=200, content_type='application/json') is None


def test_original_intake_limit_and_redacted_only_question_do_not_create_partial_records():
    from backend.memory_app.v2.policies import get
    policy = get('proxy_record', version='@1')
    payload = request('chat_completions')
    payload['messages'][-1]['content'] = 'x' * policy.max_record_characters
    assert record('chat_completions', json.dumps(response('chat_completions')).encode(), payload=payload) is None
    literal = 'synthetic-secret-only-question'
    payload['messages'][-1]['content'] = literal
    assert record('chat_completions', json.dumps(response('chat_completions')).encode(),
        payload=payload, credentials=(literal,)) is None


@pytest.mark.parametrize('bad', ['foreign_item', 'foreign_part', 'wrong_part_done', 'duplicate_done', 'unknown_reasoning'])
def test_responses_complete_snapshot_cannot_replace_conflicting_observed_structure(bad):
    protocol = 'responses'
    if bad == 'duplicate_done':
        data = stream(protocol) + event('[DONE]') * 2
    else:
        if bad == 'foreign_item':
            conflict = {'type': 'response.output_item.added', 'output_index': 0,
                'item': {'type': 'message', 'id': 'foreign', 'role': 'assistant', 'status': 'in_progress', 'content': []}}
        elif bad == 'foreign_part':
            conflict = {'type': 'response.content_part.added', 'output_index': 0,
                'content_index': 0, 'item_id': 'foreign', 'part': {'type': 'output_text', 'text': ''}}
        elif bad == 'wrong_part_done':
            conflict = {'type': 'response.content_part.done', 'output_index': 0,
                'content_index': 0, 'item_id': 'out-synthetic', 'part': {'type': 'output_text', 'text': 'wrong final text'}}
        else:
            conflict = {'type': 'response.reasoning_unknown_tool_call', 'call_id': 'synthetic'}
        data = event(conflict, name=conflict['type']) + stream(protocol)
    assert record(protocol, data, content_type='text/event-stream') is None


def test_responses_original_item_and_part_events_bind_to_final_snapshot():
    added = {'type': 'response.output_item.added', 'output_index': 0,
        'item': {'type': 'message', 'id': 'out-synthetic', 'role': 'assistant', 'status': 'in_progress', 'content': []}}
    part = {'type': 'response.content_part.added', 'output_index': 0, 'content_index': 0,
        'item_id': 'out-synthetic', 'part': {'type': 'output_text', 'text': ''}}
    final_part = deepcopy(part)
    final_part['type'] = 'response.content_part.done'
    final_part['part']['text'] = ANSWER
    done = {'type': 'response.output_item.done', 'output_index': 0, 'item': response('responses')['output'][0]}
    ending = event({'type': 'response.completed', 'response': response('responses')}, name='response.completed')
    middle = stream('responses')[:-len(ending)]
    data = event(added, name=added['type']) + event(part, name=part['type']) + middle + \
        event(final_part, name=final_part['type']) + event(done, name=done['type']) + ending
    assert ANSWER in record('responses', data, content_type='text/event-stream')


@pytest.mark.parametrize('protocol', ['chat_completions', 'messages', 'responses'])
def test_stream_tool_or_conflicting_finish_is_not_recorded(protocol):
    if protocol == 'chat_completions':
        conflict = {'id': 'chat-synthetic', 'choices': [{'index': 0, 'delta': {'tool_calls': [{'id': 'synthetic'}]},
            'finish_reason': None}]}
        prefix = event(conflict)
    elif protocol == 'messages':
        start = {'type': 'message_start', 'message': {'type': 'message', 'role': 'assistant', 'content': [], 'stop_reason': None}}
        conflict = {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': 'synthetic'}}
        prefix = event(start, name=start['type']) + event(conflict, name=conflict['type'])
    else:
        conflict = {'type': 'response.output_item.added', 'output_index': 0,
            'item': {'id': 'synthetic', 'type': 'function_call', 'name': 'synthetic'}}
        prefix = event(conflict, name=conflict['type'])
    assert record(protocol, prefix + stream(protocol), content_type='text/event-stream') is None


def test_explicit_cancel_clears_all_observed_text_and_literal_credentials():
    recorder = module().ConversationRecorder('chat_completions', request('chat_completions'),
        credentials=('synthetic-literal',))
    recorder.feed(stream('chat_completions'))
    recorder.close()
    recorder.feed(stream('chat_completions'))
    assert recorder.finish(status_code=200, content_type='text/event-stream') is None
    assert recorder._history is None and recorder._credentials == () and recorder._buffer == b''


def test_credential_capacity_is_policy_owned_and_unknown_protocol_object_is_not_retained():
    from backend.memory_app.v2.policies import get
    policy = get('proxy_record', version='@1')
    assert record('chat_completions', json.dumps(response('chat_completions')).encode(),
        credentials=('x' * (policy.max_credential_characters + 1),)) is None
    private = {'Authorization': 'must-not-be-retained'}
    recorder = module().ConversationRecorder(private, request('chat_completions'))
    assert recorder._protocol is None and recorder._credentials == ()


@pytest.mark.parametrize('protocol', ['chat_completions', 'messages', 'responses'])
def test_eof_keepalive_comment_and_multiline_sse_json_are_not_new_messages(protocol):
    data = stream(protocol) + b': final keepalive'
    assert ANSWER in record(protocol, data, content_type='text/event-stream')
    body = stream(protocol).replace(b'data: {', b'data: {\ndata: ', 1)
    assert ANSWER in record(protocol, body, content_type='text/event-stream', chunk=1)


@pytest.mark.parametrize('protocol', ['chat_completions', 'messages', 'responses'])
def test_policy_event_cap_and_credentials_cannot_escape_after_abandoning(protocol):
    from backend.memory_app.v2.policies import get
    policy = get('proxy_record', version='@1')
    recorder = module().ConversationRecorder(protocol, request(protocol), credentials=('synthetic-literal',))
    recorder.feed(b'x' * (policy.max_response_bytes + 1))
    assert recorder._history is None and recorder._credentials == () and recorder._buffer == b''
    ping = event({'type': 'ping'}, name='ping')
    assert record(protocol, ping * (policy.max_events + 1) + stream(protocol), content_type='text/event-stream') is None
