"""Private synthetic capture observes the original wire without changing it."""
import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI
from openai.types.chat import ChatCompletionChunk

from backend.memory_app import model_config
from backend.shared.llm.model_transport import ModelTransportTimeout, _OwnedModelStream
from tools import rag_holdout as runner


CONTENT = '合成原始正文 \n {"answer":"private-only"} '
REASONING = 'SYNTHETIC_REASONING_MUST_NOT_BE_CAPTURED'
SECRET = 'SYNTHETIC_CREDENTIAL_MUST_NOT_BE_CAPTURED'


@pytest.mark.parametrize('retrieve,compose', [('@9', '@34'), ('@12', '@36'), ('@13', '@36')])
def test_cli_accepts_expected_candidate_versions_without_selecting_or_running_them(tmp_path, monkeypatch, retrieve, compose):
    received = []
    arguments = ['rag_holdout']
    for name in ('index', 'output', 'data-root', 'config-root', 'config-db', 'models-root'):
        arguments.extend(['--' + name, str(tmp_path / name)])
    arguments.extend(['--max-calls', '1', '--expected-retrieve', retrieve,
        '--expected-compose', compose, '--capture-private-responses'])
    monkeypatch.setattr(sys, 'argv', arguments)
    monkeypatch.setattr(runner, 'run', received.append)
    for name, value in {'LITELLM_LOCAL_MODEL_COST_MAP': 'True', 'LITELLM_LOG': 'ERROR',
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}.items():
        monkeypatch.setenv(name, value)
    runner.main()
    assert len(received) == 1
    assert (received[0].expected_retrieve, received[0].expected_compose) == (retrieve, compose)
    assert received[0].capture_private_responses is True
    assert not list(tmp_path.iterdir())


def chunk(content=None, *, finish=None, usage=None):
    value = {'choices': [{'index': 0, 'delta': {'content': content,
        'reasoning_content': REASONING}, 'finish_reason': finish}]}
    if usage is not None:
        value['usage'] = usage
    return value


def artifact(output, number=1):
    return json.loads((output / 'private-responses' / f'completion-{number}.json').read_text())


def observer(tmp_path, monkeypatch, provider, *, enabled=True):
    monkeypatch.setattr(model_config, '_load_litellm_completion', provider)
    return runner.CompletionObserver(tmp_path, 20, capture_private_responses=enabled)


class Stream:
    def __init__(self, chunks, *, error=None, close_error=None):
        self.chunks, self.error, self.close_error = iter(chunks), error, close_error
        self.next_calls, self.close_calls = 0, 0
        self.correlations = []
        self.response = object()

    def __iter__(self):
        return self

    def __next__(self):
        self.next_calls += 1
        self.correlations.append(dict(runner.CALL.get()))
        try:
            return next(self.chunks)
        except StopIteration:
            if self.error is not None:
                raise self.error
            raise

    def close(self):
        self.close_calls += 1
        self.correlations.append(dict(runner.CALL.get()))
        if self.close_error is not None:
            raise self.close_error


@pytest.mark.parametrize('stream', [False, True])
def test_capture_is_default_off_and_preserves_return_identity(tmp_path, monkeypatch, stream):
    result = Stream([chunk(CONTENT)]) if stream else {'choices': [{'message': {'content': CONTENT}}]}
    monkeypatch.setattr(model_config, '_load_litellm_completion', lambda **_request: result)
    completion = runner.CompletionObserver(tmp_path, 20)
    assert completion.complete(stream=stream, api_key=SECRET) is result
    assert not (tmp_path / 'private-responses').exists()
    assert CONTENT not in (tmp_path / 'wires.json').read_text()


@pytest.mark.parametrize('raw', ['{"answer":"ok"}', '{invalid JSON',
    '{"answer":["invalid schema"]}', '', CONTENT, '\ud800'])
def test_nonstream_capture_does_not_parse_or_repair_json(tmp_path, monkeypatch, capsys, raw):
    result = {'choices': [{'message': {'content': raw, 'reasoning_content': REASONING},
        'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 9, 'completion_tokens': 3,
        'total_tokens': 12, 'api_key': SECRET}, 'headers': {'Authorization': SECRET}}
    completion = observer(tmp_path, monkeypatch, lambda **_request: result)
    assert completion.complete(messages=[], api_key=SECRET) is result
    saved = artifact(tmp_path)
    assert saved['content'] == raw and saved['state'] == 'returned'
    assert saved['finish_reasons'] == ['stop']
    assert saved['usage'] == {'prompt_tokens': 9, 'completion_tokens': 3, 'total_tokens': 12}
    assert saved['content_truncated'] is False
    serialized = json.dumps(saved)
    assert REASONING not in serialized and SECRET not in serialized
    assert raw not in (tmp_path / 'wires.json').read_text() if raw else True
    assert capsys.readouterr() == ('', '')


def test_sdk_chunks_lazy_iteration_identity_usage_and_context_are_preserved(tmp_path, monkeypatch):
    first = ChatCompletionChunk(id='synthetic', created=0, model='synthetic', object='chat.completion.chunk',
        choices=[{'index': 0, 'delta': {'content': CONTENT, 'reasoning_content': REASONING},
                  'finish_reason': None}])
    last = ChatCompletionChunk(id='synthetic', created=0, model='synthetic', object='chat.completion.chunk',
        choices=[{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
        usage={'prompt_tokens': 7, 'completion_tokens': 4, 'total_tokens': 11})
    stream = Stream([first, last])
    completion = observer(tmp_path, monkeypatch, lambda **_request: stream)
    wrapped = completion.complete(stream=True, messages=[])
    assert stream.next_calls == 0 and stream.close_calls == 0
    assert wrapped.response is stream.response
    assert next(wrapped) is first
    assert not (tmp_path / 'private-responses').exists()
    assert next(wrapped) is last
    with pytest.raises(StopIteration):
        next(wrapped)
    wrapped.close()
    assert stream.close_calls == 1 and stream.next_calls == 3
    saved = artifact(tmp_path)
    assert saved['state'] == 'eof' and saved['content'] == CONTENT
    assert saved['usage'] == {'prompt_tokens': 7, 'completion_tokens': 4, 'total_tokens': 11}
    assert all(value['number'] == 1 for value in stream.correlations)
    assert runner.CALL.get() == {}


@pytest.mark.parametrize('finish', ['length', None])
def test_length_and_eof_are_observed_without_claiming_answer_success(tmp_path, monkeypatch, finish):
    stream = Stream([chunk(CONTENT), chunk(finish=finish)])
    completion = observer(tmp_path, monkeypatch, lambda **_request: stream)
    assert len(list(completion.complete(stream=True))) == 2
    saved = artifact(tmp_path)
    assert saved['state'] == 'eof' and saved['content'] == CONTENT
    assert saved['finish_reasons'] == ([finish] if finish else [])
    assert saved['usage'] is None


def test_stream_exception_is_original_and_partial_capture_contains_no_error_text(tmp_path, monkeypatch):
    failure = ConnectionError(SECRET)
    stream = Stream([chunk(CONTENT)], error=failure)
    completion = observer(tmp_path, monkeypatch, lambda **_request: stream)
    wrapped = completion.complete(stream=True)
    next(wrapped)
    with pytest.raises(ConnectionError) as caught:
        next(wrapped)
    assert caught.value is failure and stream.close_calls == 0
    saved = artifact(tmp_path)
    assert saved['state'] == 'stream_error' and saved['content'] == CONTENT
    assert saved['error_type'] == 'ConnectionError'
    assert SECRET not in json.dumps(saved)


def test_early_close_is_not_drained_and_close_error_is_not_replaced(tmp_path, monkeypatch):
    failure = OSError(SECRET)
    stream = Stream([chunk(CONTENT), chunk('not consumed')], close_error=failure)
    completion = observer(tmp_path, monkeypatch, lambda **_request: stream)
    wrapped = completion.complete(stream=True)
    next(wrapped)
    with pytest.raises(OSError) as caught:
        wrapped.close()
    assert caught.value is failure
    assert stream.next_calls == 1 and stream.close_calls == 1
    assert artifact(tmp_path)['state'] == 'close_error'
    assert artifact(tmp_path)['content'] == CONTENT


def test_normal_early_close_writes_closed_and_missing_close_stays_missing(tmp_path, monkeypatch):
    stream = Stream([chunk(CONTENT), chunk('unconsumed')])
    completion = observer(tmp_path, monkeypatch, lambda **_request: stream)
    wrapped = completion.complete(stream=True)
    next(wrapped)
    wrapped.close()
    assert artifact(tmp_path)['state'] == 'closed'
    assert stream.next_calls == 1 and stream.close_calls == 1
    monkeypatch.setattr(model_config, '_load_litellm_completion', lambda **_request: iter([chunk('next')]))
    wrapped = completion.complete(stream=True)
    assert not hasattr(wrapped, 'close')
    list(wrapped)
    assert artifact(tmp_path, 2)['content'] == 'next'


def test_owned_stream_keeps_timeout_identity_and_original_cleanup_owner(tmp_path, monkeypatch):
    provider = Stream([chunk(CONTENT), chunk('unconsumed')])
    closed = []
    attempt = SimpleNamespace(close=lambda: closed.append(True))
    original = _OwnedModelStream(provider, attempt)
    completion = observer(tmp_path, monkeypatch, lambda **_request: original)
    wrapped = completion.complete(stream=True)
    assert isinstance(wrapped, _OwnedModelStream)
    timeout = ModelTransportTimeout(remaining=lambda: 1,
        limits={'header_timeout': 1, 'idle_timeout': 1})
    assert timeout.bind_completion(wrapped) is wrapped
    assert wrapped.completion_stream is original.completion_stream
    next(wrapped)
    wrapped.close()
    assert original.closed and closed == [True] and provider.close_calls == 1
    assert provider.next_calls == 1 and artifact(tmp_path)['state'] == 'closed'


def test_public_completion_stream_close_is_observed_without_extra_close(tmp_path, monkeypatch):
    provider = Stream([chunk(CONTENT)])
    outer = SimpleNamespace(completion_stream=provider)

    class Wrapper:
        completion_stream = outer.completion_stream
        def __iter__(self):
            return iter(provider)
    completion = observer(tmp_path, monkeypatch, lambda **_request: Wrapper())
    wrapped = completion.complete(stream=True)
    assert wrapped.completion_stream.response is provider.response
    next(wrapped)
    wrapped.completion_stream.close()
    assert provider.close_calls == 1 and provider.next_calls == 1
    assert artifact(tmp_path)['state'] == 'closed'


def test_real_openai_sse_objects_capture_without_reading_ahead(tmp_path, monkeypatch):
    events = [chunk(CONTENT), chunk(finish='stop', usage={'prompt_tokens': 8,
        'completion_tokens': 2, 'total_tokens': 10})]
    body = ''.join('data: ' + json.dumps({'id': 'synthetic', 'created': 0, 'model': 'synthetic',
        'object': 'chat.completion.chunk', **event}) + '\n\n' for event in events) + 'data: [DONE]\n\n'
    client = OpenAI(api_key=SECRET, base_url='https://synthetic.invalid/v1',
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, text=body, headers={'Content-Type': 'text/event-stream'}))))
    sdk_stream = client.chat.completions.create(model='synthetic', messages=[], stream=True)
    completion = observer(tmp_path, monkeypatch, lambda **_request: sdk_stream)
    wrapped = completion.complete(stream=True)
    assert wrapped.response is sdk_stream.response
    with wrapped:
        received = list(wrapped)
    assert len(received) == 2 and received[0].choices[0].delta.content == CONTENT
    assert artifact(tmp_path)['content'] == CONTENT
    assert artifact(tmp_path)['state'] == 'eof'
    client.close()


def test_bounded_capture_keeps_exact_unicode_prefix_and_whitelisted_details(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'PRIVATE_RESPONSE_CONTENT_LIMIT', 5)
    result = {'choices': [{'message': {'content': [{'type': 'text', 'text': '甲乙丙'},
        {'type': 'reasoning', 'text': REASONING}, {'type': 'text', 'text': '丁戊己'}]},
        'finish_reason': 'untrusted arbitrary provider text'}], 'usage': {'prompt_tokens': True,
        'completion_tokens': -1, 'total_tokens': 6,
        'prompt_tokens_details': {'cached_tokens': 2, 'secret': SECRET},
        'completion_tokens_details': {'reasoning_tokens': 3, 'text': REASONING}}}
    completion = observer(tmp_path, monkeypatch, lambda **_request: result)
    assert completion.complete() is result
    saved = artifact(tmp_path)
    assert saved['content'] == '甲乙丙丁戊' and saved['content_truncated'] is True
    assert saved['content_characters_observed'] == 6 and saved['content_limit_characters'] == 5
    assert saved['finish_reasons'] == ['other']
    assert saved['usage'] == {'total_tokens': 6, 'prompt_tokens_details': {'cached_tokens': 2},
                             'completion_tokens_details': {'reasoning_tokens': 3}}
    assert REASONING not in json.dumps(saved) and SECRET not in json.dumps(saved)


def test_retries_receive_distinct_immutable_completion_numbers(tmp_path, monkeypatch):
    calls = []
    failure = TimeoutError(SECRET)
    def provider(**_request):
        calls.append(True)
        if len(calls) == 1:
            raise failure
        return Stream([chunk(CONTENT), chunk(finish='stop')])
    completion = observer(tmp_path, monkeypatch, provider)
    with pytest.raises(TimeoutError) as caught:
        completion.complete(stream=True)
    assert caught.value is failure
    list(completion.complete(stream=True))
    assert artifact(tmp_path)['state'] == 'failed_before_return'
    assert artifact(tmp_path, 2)['content'] == CONTENT
    assert len(calls) == 2 and len(completion.rows) == 2
    assert SECRET not in (tmp_path / 'wires.json').read_text()


@pytest.mark.parametrize('collision', [True, False])
def test_capture_write_failure_does_not_change_provider_result_or_retry(tmp_path, monkeypatch, collision):
    private = tmp_path / 'private-responses'
    if collision:
        private.mkdir()
        (private / 'completion-1.json').write_text('existing immutable bytes')
    else:
        private.write_text('not a directory')
    result, calls = {'choices': [{'message': {'content': CONTENT}}]}, []
    def provider(**_request):
        calls.append(True)
        return result
    completion = observer(tmp_path, monkeypatch, provider)
    assert completion.complete() is result and len(calls) == 1
    assert completion.rows[0]['response_capture_code'] == (
        'private_response_exists' if collision else 'private_response_write_failed')
    if collision:
        assert (private / 'completion-1.json').read_text() == 'existing immutable bytes'


def test_observation_failure_returns_original_and_only_fixed_safe_code(tmp_path, monkeypatch, capsys):
    class BadResponse:
        @property
        def choices(self):
            raise ValueError(SECRET)
    result = BadResponse()
    completion = observer(tmp_path, monkeypatch, lambda **_request: result)
    assert completion.complete() is result
    assert artifact(tmp_path)['capture_code'] == 'private_response_observation_failed'
    assert SECRET not in (tmp_path / 'wires.json').read_text()
    assert capsys.readouterr() == ('', '')


def test_stream_capture_write_failure_does_not_change_iteration_or_close(tmp_path, monkeypatch):
    (tmp_path / 'private-responses').write_text('not a directory')
    provider = Stream([chunk(CONTENT), chunk(finish='stop')])
    calls = []
    def complete(**_request):
        calls.append(True)
        return provider
    completion = observer(tmp_path, monkeypatch, complete)
    wrapped = completion.complete(stream=True)
    received = list(wrapped)
    wrapped.close()
    assert len(received) == 2 and len(calls) == 1 and provider.close_calls == 1
    assert completion.rows[0]['response_capture_code'] == 'private_response_write_failed'


def test_tool_arguments_are_outside_the_explicit_content_only_capture(tmp_path, monkeypatch):
    result = {'choices': [{'message': {'content': None,
        'tool_calls': [{'function': {'arguments': SECRET}}]}, 'finish_reason': 'tool_calls'}]}
    completion = observer(tmp_path, monkeypatch, lambda **_request: result)
    assert completion.complete() is result
    saved = artifact(tmp_path)
    assert saved['content_scope'] == 'first_choice_content' and saved['content'] == ''
    assert saved['finish_reasons'] == ['tool_calls'] and SECRET not in json.dumps(saved)


def test_cli_capture_flag_is_explicit_and_default_off(monkeypatch, tmp_path):
    base = ['rag_holdout']
    for option in ('index', 'output', 'data-root', 'config-root', 'config-db', 'models-root'):
        base += ['--' + option, str(tmp_path / option)]
    base += ['--max-calls', '1']
    seen = []
    monkeypatch.setattr(runner, 'run', lambda args: seen.append(args.capture_private_responses))
    monkeypatch.setattr('sys.argv', base)
    runner.main()
    monkeypatch.setattr('sys.argv', base + ['--capture-private-responses'])
    runner.main()
    assert seen == [False, True]
