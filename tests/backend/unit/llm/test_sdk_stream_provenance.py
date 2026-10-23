"""Real LiteLLM/OpenAI streams with synthetic HTTP bytes and no external calls."""
import json
from time import monotonic
from types import SimpleNamespace

import httpx
import pytest

from backend.memory_app.model_config import _load_litellm_completion
from backend.shared.llm.litellm_gateway import _extract_normalized_usage, _extract_prompt_cache_observation
from backend.shared.llm.model_transport import ModelTransportTimeout, _OwnedModelStream, _observe_sdk_provider


USAGE = {'prompt_tokens': 23, 'completion_tokens': 11, 'total_tokens': 34}


class ProviderBytes(httpx.SyncByteStream):
    def __init__(self, frames):
        self.body = b''.join(frames)
        self.iterations, self.close_calls = 0, 0

    def __iter__(self):
        self.iterations += 1
        for offset in range(0, len(self.body), 17):
            yield self.body[offset:offset + 17]

    def close(self):
        self.close_calls += 1


def frame(*, text=None, finish=None, usage=None, usage_only=False, choice_index=0):
    value = {'id': 'synthetic-provenance', 'object': 'chat.completion.chunk', 'created': 1,
        'model': 'gpt-4o-mini', 'choices': [] if usage_only else [
            {'delta': {} if text is None else {'content': text}, 'finish_reason': finish,
             **({'index': choice_index} if choice_index is not None else {})}]}
    if usage is not None:
        value['usage'] = usage
    return ('data: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode('utf-8')


@pytest.fixture
def sdk_stream(monkeypatch):
    monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
    streams, bodies, clients, requests = [], [], [], []

    def make(*, usage=None, terminal=True, done=True, text='{"answer":"done"}', choice_index=0,
             provider_error=False, include_usage=True):
        frames = [frame(text=text, choice_index=choice_index)]
        if terminal:
            frames.append(frame(finish='stop', choice_index=choice_index))
        if usage is not None:
            frames.append(frame(usage=usage, usage_only=True))
        if provider_error:
            frames.append(b'data: {"error":{"message":"synthetic","type":"server_error"}}\n\n')
        if done:
            frames.append(b'data: [DONE]\n\n')
        body = ProviderBytes(frames)
        bodies.append(body)

        def respond(request):
            requests.append(request)
            assert request.url.host == 'synthetic.invalid'
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=body)

        def client():
            value = httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)
            clients.append(value)
            return value

        deadline = monotonic() + 20
        timeout = ModelTransportTimeout(remaining=lambda: max(.001, deadline - monotonic()),
            limits={'header_timeout': 10, 'idle_timeout': 10}, owned_client_factory=client)
        stream = _load_litellm_completion(model='openai/gpt-4o-mini',
            messages=[{'role': 'user', 'content': 'Synthetic provenance check'}],
            api_base='https://synthetic.invalid/v1', api_key='synthetic-only',
            stream=True, stream_options={'include_usage': include_usage}, timeout=timeout,
            max_tokens=100, num_retries=0)
        streams.append(stream)
        assert isinstance(stream, _OwnedModelStream)
        assert stream.provider_facts is not None
        # Installing the witness must not pull any response body ahead of the
        # existing SDK consumer, nor dispatch another model request.
        assert body.iterations == 0
        return stream

    yield SimpleNamespace(make=make, bodies=bodies, clients=clients, requests=requests)
    for stream in streams:
        stream.close()
    assert len(requests) == len(streams)
    assert all(client.is_closed for client in clients)
    assert all(body.close_calls == 1 for body in bodies)


@pytest.mark.parametrize('usage', [USAGE,
    {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
    {'prompt_tokens': 0},
])
def test_real_provider_usage_is_preserved_including_zero_and_omitted_fields(sdk_stream, usage):
    chunks = list(sdk_stream.make(usage=usage))
    reported = [chunk.usage for chunk in chunks if chunk.usage is not None]
    assert reported and all(value == usage for value in reported)
    expected = {target: usage[source] for source, target in (
        ('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens'), ('total_tokens', 'total_tokens'))
        if source in usage}
    assert _extract_normalized_usage(chunks[-1]) == expected


def test_true_stop_without_provider_usage_does_not_publish_sdk_estimates(sdk_stream):
    chunks = list(sdk_stream.make())
    assert any(any(choice.finish_reason == 'stop' for choice in chunk.choices) for chunk in chunks)
    assert all(chunk.usage is None for chunk in chunks)
    assert all(_extract_normalized_usage(chunk) == {} for chunk in chunks)


def test_true_stop_can_end_at_eof_without_a_done_sentinel(sdk_stream):
    chunks = list(sdk_stream.make(done=False))
    assert any(any(choice.finish_reason == 'stop' for choice in chunk.choices) for chunk in chunks)
    assert all(chunk.usage is None for chunk in chunks)


def test_single_provider_choice_can_report_a_real_stop_without_an_index(sdk_stream):
    chunks = list(sdk_stream.make(choice_index=None, usage=USAGE))
    assert any(any(choice.finish_reason == 'stop' for choice in chunk.choices) for chunk in chunks)
    assert chunks[-1].usage == USAGE


def test_another_provider_choice_cannot_grant_the_primary_terminal(sdk_stream):
    with pytest.raises(ConnectionError, match='provider stream ended before its terminal event'):
        list(sdk_stream.make(choice_index=1))


@pytest.mark.parametrize('text', ['{"answer":"done"}', '{"answer":'])
@pytest.mark.parametrize('done', [False, True])
def test_sdk_synthesized_stop_cannot_turn_a_missing_provider_terminal_into_success(sdk_stream, text, done):
    stream = sdk_stream.make(terminal=False, done=done, text=text)
    delivered = []
    with pytest.raises(ConnectionError, match='provider stream ended before its terminal event'):
        for chunk in stream:
            delivered.append(chunk)
    assert all(all(choice.finish_reason is None for choice in chunk.choices) for chunk in delivered)
    assert all(chunk.usage is None for chunk in delivered)
    assert stream.closed


@pytest.mark.parametrize('usage', [USAGE, {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}])
@pytest.mark.parametrize('done', [False, True])
def test_missing_terminal_preserves_real_usage_as_metadata_before_failure(sdk_stream, usage, done):
    stream = sdk_stream.make(terminal=False, usage=usage, done=done)
    delivered = []
    with pytest.raises(ConnectionError, match='provider stream ended before its terminal event'):
        for chunk in stream:
            delivered.append(chunk)
    assert all(all(choice.finish_reason is None for choice in chunk.choices) for chunk in delivered)
    reported = [chunk for chunk in delivered if chunk.usage is not None]
    assert reported and reported[-1].choices == [] and reported[-1].usage == usage
    assert _extract_normalized_usage(reported[-1]) == {
        'input_tokens': usage['prompt_tokens'], 'output_tokens': usage['completion_tokens'],
        'total_tokens': usage['total_tokens']}
    assert stream.closed


def test_reported_usage_precedes_the_original_sdk_error_without_a_fake_terminal(sdk_stream):
    from openai import APIError
    # This public adapter option defers LiteLLM's usage chunk. The provider can
    # still report actual usage before a later SSE error; it must not be lost.
    stream = sdk_stream.make(terminal=False, done=False, usage=USAGE, provider_error=True, include_usage=False)
    delivered = []
    while True:
        chunk = next(stream)
        delivered.append(chunk)
        if chunk.usage is not None:
            break
    assert chunk.choices == [] and chunk.usage == USAGE
    assert sum(item.usage is not None for item in delivered) == 1
    assert all(all(choice.finish_reason is None for choice in item.choices) for item in delivered)
    assert stream.closed
    original = stream.pending_error
    assert isinstance(original, APIError)
    with pytest.raises(type(original)) as rejected:
        next(stream)
    assert rejected.value is original
    assert len(sdk_stream.requests) == 1 and sdk_stream.bodies[0].close_calls == 1


def test_reported_cache_counters_survive_the_usage_projection(sdk_stream):
    usage = {**USAGE, 'prompt_cache_hit_tokens': 7, 'prompt_cache_miss_tokens': 16}
    chunks = list(sdk_stream.make(usage=usage))
    assert chunks[-1].usage == usage
    assert _extract_prompt_cache_observation(chunks[-1]) == {
        'cache_read_input_tokens': 7, 'cache_miss_input_tokens': 16}


def test_custom_completion_shapes_remain_unchanged():
    chunk = {'choices': [{'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 9}}
    class Custom:
        def __init__(self):
            self.completion_stream = iter([chunk])
        def __iter__(self):
            return self.completion_stream
    custom = Custom()
    assert _observe_sdk_provider(custom) is None
    attempt = SimpleNamespace(check=lambda: None, close=lambda: None)
    stream = _OwnedModelStream(custom, attempt)
    assert next(stream) is chunk
    with pytest.raises(StopIteration):
        next(stream)
