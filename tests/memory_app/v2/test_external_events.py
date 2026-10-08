"""两个外部 CLI 的合成 JSONL 只经真实解析器归一化。"""
import importlib
import json

import pytest


def parser(executor, **options):
    module = importlib.import_module('backend.memory_app.v2.external_events')
    return module.ExternalEventParser(executor, **options)


def feed(owner, value):
    return owner.feed_line(json.dumps(value, ensure_ascii=False))


def test_codex_lifecycle_tools_message_and_usage():
    owner = parser('codex')
    assert feed(owner, {'type': 'thread.started', 'thread_id': 'hidden'}) == ({'kind': 'started'},)
    assert feed(owner, {'type': 'turn.started'}) == ({'kind': 'step', 'stage': 'turn'},)
    for name in ('command_execution', 'web_search', 'mcp_tool_call'):
        assert feed(owner, {'type': 'item.started', 'item': {'type': name,
            'command': 'private command', 'arguments': {'secret': 'private'},
            'query': 'private query', 'tool': 'private tool', 'server': 'private'}}) == (
                {'kind': 'tool', 'name': name},)
    assert feed(owner, {'type': 'item.completed', 'item': {
        'type': 'agent_message', 'text': '合成最终答复'}}) == ()
    result = feed(owner, {'type': 'turn.completed', 'usage': {
        'input_tokens': 12, 'cached_input_tokens': 3, 'output_tokens': 8}})
    assert result == ({'kind': 'message', 'text': '合成最终答复'},
        {'kind': 'finished', 'status': 'completed'})
    assert owner.usage == {'input_tokens': 12, 'cached_input_tokens': 3, 'output_tokens': 8}
    assert owner.failed is False
    assert feed(owner, {'type': 'error', 'message': 'late private error'}) == ()


def test_claude_complete_message_and_result_no_duplicate():
    owner = parser('claude-code')
    assert feed(owner, {'type': 'system', 'subtype': 'init', 'cwd': '/private/root',
        'env': {'TOKEN': 'secret'}}) == ({'kind': 'started'},)
    assert feed(owner, {'type': 'assistant', 'message': {'id': 'm1', 'content': [
        {'type': 'text', 'text': '合成答复'},
        {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'private command'}}]}}) == (
            {'kind': 'tool', 'name': 'command_execution'},)
    assert feed(owner, {'type': 'result', 'subtype': 'success', 'result': '合成答复',
        'usage': {'input_tokens': 10, 'output_tokens': 4, 'cache_read_input_tokens': 2}}) == (
            {'kind': 'message', 'text': '合成答复'}, {'kind': 'finished', 'status': 'completed'})
    assert owner.usage == {'input_tokens': 10, 'output_tokens': 4, 'cache_read_input_tokens': 2}


def test_claude_streamed_message_is_not_duplicated_by_assistant():
    owner = parser('claude-code')
    feed(owner, {'type': 'stream_event', 'event': {'type': 'message_start', 'message': {'id': 'm1'}}})
    for text in ('合成', '片段'):
        assert feed(owner, {'type': 'stream_event', 'event': {'type': 'content_block_delta',
            'index': 0, 'delta': {'type': 'text_delta', 'text': text}}}) == ()
    feed(owner, {'type': 'assistant', 'message': {'id': 'm1',
        'content': [{'type': 'text', 'text': '合成片段'}]}})
    assert feed(owner, {'type': 'result', 'subtype': 'success'}) == (
        {'kind': 'message', 'text': '合成片段'}, {'kind': 'finished', 'status': 'completed'})
    assert owner.usage is None


@pytest.mark.parametrize('executor', ['codex', 'claude-code'])
def test_secret_split_across_text_fragments_never_escapes(executor):
    secret = '-'.join(('synthetic', 'value', 'that', 'must', 'not', 'escape'))
    shaped = 'sk-' + 'S' * 30
    owner = parser(executor, secret_values=(secret,))
    emitted = []
    for text in ('正文 ' + secret[:9], secret[9:] + ' ' + shaped[:8], shaped[8:] + ' 末尾'):
        event = ({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}}
            if executor == 'codex' else {'type': 'stream_event', 'event': {
                'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': text}}})
        emitted.extend(feed(owner, event))
        assert not any(item['kind'] == 'message' for item in emitted)
    emitted.extend(feed(owner, {'type': 'turn.completed'} if executor == 'codex'
        else {'type': 'result', 'subtype': 'success'}))
    text = ''.join(item['text'] for item in emitted if item['kind'] == 'message')
    assert text == '正文 [REDACTED_SECRET] [REDACTED_SECRET] 末尾'
    assert secret not in repr(emitted) and shaped not in repr(emitted)


@pytest.mark.parametrize('value', [None, {}, {'input_tokens': 2},
    {'input_tokens': True, 'output_tokens': 2}, {'input_tokens': -1, 'output_tokens': 2},
    {'input_tokens': 1, 'output_tokens': 2, 'cached_input_tokens': False},
    {'input_tokens': '2', 'output_tokens': 1}])
@pytest.mark.parametrize('executor', ['codex', 'claude-code'])
def test_missing_or_invalid_usage_is_unknown(executor, value):
    owner = parser(executor)
    feed(owner, {'type': 'turn.completed', 'usage': value} if executor == 'codex'
        else {'type': 'result', 'subtype': 'success', 'usage': value})
    assert owner.usage is None


@pytest.mark.parametrize('executor,event', [('codex', {'type': 'turn.failed'}),
    ('codex', {'type': 'error'}), ('claude-code', {'type': 'result', 'is_error': True}),
    ('claude-code', {'type': 'result', 'subtype': 'error_during_execution'})])
def test_failure_never_preserves_error_details(executor, event):
    owner = parser(executor)
    event['message'] = 'private error at C:/secret/config'
    event['errors'] = ['private exception']
    assert feed(owner, event) == ({'kind': 'finished', 'status': 'failed'},)
    assert owner.failed and owner.usage is None


@pytest.mark.parametrize('line', ['not json private secret', 'null', '[]', '42',
    '{"type":"unknown","text":"private"}', '{"type":"item.completed","item":null}',
    '{"type":"assistant","message":{"content":42}}'])
def test_unknown_and_malformed_are_safely_ignored(line):
    for executor in ('codex', 'claude-code'):
        owner = parser(executor)
        assert owner.feed_line(line) == ()
        assert owner.usage is None and owner.failed is False


def test_long_unicode_message_is_split_after_redaction_and_paths_are_removed():
    owner = parser('codex')
    text = '汉字' * 5000 + ' C:\\private\\config /private/data/file'
    feed(owner, {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}})
    events = feed(owner, {'type': 'turn.completed'})
    messages = [item['text'] for item in events if item['kind'] == 'message']
    assert all(0 < len(item) <= 4096 for item in messages)
    assert ''.join(messages) == '汉字' * 5000 + ' [REDACTED_PATH] [REDACTED_PATH]'


def test_usage_property_cannot_mutate_parser_state():
    owner = parser('codex')
    feed(owner, {'type': 'turn.completed', 'usage': {'input_tokens': 2, 'output_tokens': 1}})
    owner.usage['input_tokens'] = 900
    assert owner.usage == {'input_tokens': 2, 'output_tokens': 1}


def test_invalid_executor_is_rejected_without_echoing_input():
    with pytest.raises(ValueError, match='^external_executor_unknown$'):
        parser('private executor')


@pytest.mark.parametrize('value', [{'type': []}, {'type': {}},
    {'type': 'item.completed', 'item': {'type': []}},
    {'type': 'item.completed', 'item': {'type': {}}}])
def test_nonstring_discriminators_are_unknown(value):
    for executor in ('codex', 'claude-code'):
        assert feed(parser(executor), value) == ()


def test_split_private_key_block_is_redacted_as_one_message():
    owner = parser('claude-code')
    header = '-----BEGIN ' + 'PRIVATE KEY-----'
    footer = '-----END ' + 'PRIVATE KEY-----'
    for text in ('前文 ' + header[:15], header[15:] + '\nsynthetic\n', footer + ' 后文'):
        feed(owner, {'type': 'stream_event', 'event': {'type': 'content_block_delta',
            'delta': {'type': 'text_delta', 'text': text}}})
    assert feed(owner, {'type': 'result', 'subtype': 'success'}) == (
        {'kind': 'message', 'text': '前文 [REDACTED_SECRET] 后文'},
        {'kind': 'finished', 'status': 'completed'})


def test_message_start_without_delta_keeps_complete_assistant_text():
    owner = parser('claude-code')
    feed(owner, {'type': 'stream_event', 'event': {'type': 'message_start', 'message': {'id': 'm1'}}})
    feed(owner, {'type': 'assistant', 'message': {'id': 'm1',
        'content': [{'type': 'text', 'text': '完整答复'}]}})
    assert feed(owner, {'type': 'result', 'subtype': 'success'}) == (
        {'kind': 'message', 'text': '完整答复'}, {'kind': 'finished', 'status': 'completed'})


@pytest.mark.parametrize('fields', [{'is_error': 'true'}, {'is_error': 1},
    {'is_error': None}, {'subtype': []}, {'subtype': {}}, {'subtype': None},
    {'subtype': 'unknown_future_result'}])
def test_malformed_result_flags_do_not_complete_the_run(fields):
    owner = parser('claude-code')
    assert feed(owner, {'type': 'result', 'result': 'private unknown text', **fields}) == ()
    assert owner.failed is False and owner.usage is None
    assert feed(owner, {'type': 'result', 'subtype': 'success'}) == (
        {'kind': 'finished', 'status': 'completed'},)


@pytest.mark.parametrize('executor', ['codex', 'claude-code'])
def test_failed_terminal_can_preserve_actual_reported_usage(executor):
    owner = parser(executor)
    event = {'type': 'turn.failed'} if executor == 'codex' else {
        'type': 'result', 'subtype': 'error_during_execution', 'is_error': True}
    event['usage'] = {'input_tokens': 6, 'output_tokens': 2}
    assert feed(owner, event) == ({'kind': 'finished', 'status': 'failed'},)
    assert owner.usage == {'input_tokens': 6, 'output_tokens': 2}


@pytest.mark.parametrize('executor', ['codex','claude-code'])
def test_finished_property_is_readonly_and_reports_only_protocol_terminal(executor):
    owner = parser(executor)
    assert owner.finished is False
    assert owner.feed_line('malformed') == () and owner.finished is False
    event = {'type':'turn.completed'} if executor == 'codex' else {'type':'result','subtype':'success'}
    feed(owner,event)
    assert owner.finished is True
    with pytest.raises(AttributeError):
        owner.finished = False
