import asyncio
import json
import socket
from datetime import datetime, timezone

import httpx
import pytest
from openai import APIError, AsyncOpenAI, AsyncStream, OpenAI, Stream
from openai.types.responses import ResponseStreamEvent
from pydantic import BaseModel, RootModel

from backend.shared.llm.litellm_gateway import (
    LiteLLMCompletionGateway, _normalize_reasoning_effort, _extract_prompt_cache_observation,
    _is_response_format_error, _build_structured_request_modes,
    _model_transport_failure,
)
from backend.shared.llm.model_capabilities import (
    ModelCapabilities, is_official_terra, resolve_model_capabilities,
)
from backend.shared.llm.openai_responses import _provider_error, _usage, ResponsesError
from backend.shared.llm.openai_sdk import OpenAIResponsesCompletion, ResponseEventProjection, _sdk_stream_error


class Answer(BaseModel):
    answer: str


def test_only_documented_official_terra_uses_sdk():
    assert is_official_terra('openai', 'openai/gpt-6.1-terra', 'https://api.openai.com/v1/')
    for provider, model, base in (
        ('openai', 'gpt-4', 'https://api.openai.com/v1'),
        ('other', 'gpt-6.1-terra', 'https://api.openai.com/v1'),
        ('openai', 'gpt-6.1-terra', 'https://api.openai.com.evil.invalid/v1'),
        ('openai', 'gpt-6.1-terra', 'https://api.openai.com/v1/proxy'),
    ):
        assert not is_official_terra(provider, model, base)


def test_terra_capabilities_preserve_explicit_none_without_changing_legacy():
    profile = resolve_model_capabilities('openai', 'gpt-6.1-terra', 'https://api.openai.com/v1')
    assert profile.reasoning_efforts == ('none', 'low', 'medium', 'high', 'xhigh', 'max')
    assert _normalize_reasoning_effort('none', profile.reasoning_efforts) == 'none'
    assert _normalize_reasoning_effort('none', ModelCapabilities().reasoning_efforts) is None
    assert resolve_model_capabilities('openai', 'gpt-6.1-terra', 'https://proxy.invalid/v1') == ModelCapabilities()


def test_sdk_payload_uses_responses_schema_and_image_parts_without_mutation():
    messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': [
        {'type': 'text', 'text': 'read'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aA==', 'detail': 'low'}},
    ]}]
    body = OpenAIResponsesCompletion().prepare_body({'model': 'openai/gpt-6.1-terra',
        'messages': messages, 'response_format': Answer, 'max_tokens': 99, 'temperature': 0})
    assert body['model'] == 'gpt-6.1-terra'
    assert body['store'] is False and body['stream'] is True
    assert body['input'][0] == {'role': 'developer', 'content': 'rules'}
    assert body['input'][1]['content'] == [
        {'type': 'input_text', 'text': 'read'},
        {'type': 'input_image', 'image_url': 'data:image/png;base64,aA==', 'detail': 'low'},
    ]
    assert messages[1]['content'][1]['type'] == 'image_url'
    assert body['text']['format']['type'] == 'json_schema'
    assert body['text']['format']['strict'] is True
    assert body['max_output_tokens'] == 99
    assert 'temperature' not in body
    body = OpenAIResponsesCompletion().prepare_body({'model': 'gpt-6.1-terra',
        'messages': messages, 'temperature': 0, 'reasoning_effort': 'none'})
    assert body['reasoning'] == {'effort': 'none'} and body['temperature'] == 0


def test_responses_usage_preserves_unknown_and_explicit_zero():
    assert _usage({}) == {}
    assert _usage({'usage': {'input_tokens': 0}}) == {'prompt_tokens': 0}
    assert _usage({'usage': {'input_tokens': 2, 'output_tokens': 3, 'total_tokens': 5,
        'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 1},
        'output_tokens_details': {'reasoning_tokens': 2}}}) == {
            'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5,
            'prompt_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 1},
            'completion_tokens_details': {'reasoning_tokens': 2},
        }
    assert _usage({'usage': {'input_tokens': True, 'output_tokens': -1}}) == {}
    assert _extract_prompt_cache_observation({'usage': _usage({'usage': {
        'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 1},
    }})}) == {'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 1}


def test_event_projection_requires_completed_and_exact_final_text():
    projection = ResponseEventProjection()
    assert projection.accept({'type': 'response.output_text.delta', 'delta': 'answer'})[0]['choices'][0]['delta']['content'] == 'answer'
    with pytest.raises(ValueError, match='stream_mismatch'):
        projection.accept({'type': 'response.completed', 'response': {'status': 'completed',
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'changed'}]}]}})
    assert projection.finished is False


def test_sdk_sync_and_async_reject_missing_credentials_without_network():
    request = {'model': 'gpt-6.1-terra', 'messages': [{'role': 'user', 'content': 'ping'}],
        'api_key': '', 'timeout': 1}
    adapter = OpenAIResponsesCompletion()
    with pytest.raises(ValueError, match='api_key_required'):
        adapter(**request)
    with pytest.raises(ValueError, match='api_key_required'):
        asyncio.run(adapter.acomplete(**request))


def test_sdk_constructor_rejects_borrowed_http_client_for_managed_timeout():
    from backend.shared.llm.model_transport import ModelTransportTimeout, ModelTransportError
    with httpx.Client(trust_env=False) as client:
        adapter = OpenAIResponsesCompletion(client=client)
        timeout = ModelTransportTimeout(remaining=lambda: 1,
            limits={'header_timeout': 1, 'idle_timeout': 1})
        with pytest.raises(ModelTransportError, match='provider_rejected'):
            adapter(model='gpt-6.1-terra', messages=[], api_key='local-test-only', timeout=timeout)


def test_business_workspace_usage_limit_never_becomes_retryable_rate_limit():
    error = _provider_error(429, {'error': {
        'code': 'subscription_sharing_workplace_individual_usage_limit_exceeded',
        'message': 'workspace limit',
    }})
    assert error.category == 'usage_limit' and error.retryable is False
    assert _model_transport_failure(error, datetime.now(timezone.utc)) == ('quota', None)


def test_format_rejection_uses_structured_param_and_permanent_errors_take_priority():
    for param in ('text.format', 'text.format.schema', 'response_format'):
        error = _provider_error(400, {'error': {'param': param, 'code': 'invalid_json_schema'}})
        assert _is_response_format_error(error)
        assert 'param' not in vars(error)
    for status, code in ((401, 'invalid_json_schema'), (429, 'rate_limit_exceeded'),
            (400, 'insufficient_quota'), (400, 'content_filter')):
        error = _provider_error(status, {'error': {'param': 'text.format.schema', 'code': code}})
        assert not _is_response_format_error(error)
    assert not _is_response_format_error(_provider_error(400, {'error': {
        'message': 'text.format schema error with private input', 'code': 'invalid_request_error'}}))


def test_non_object_terra_schema_uses_one_prompt_mode_before_any_dispatch():
    gateway = LiteLLMCompletionGateway(provider='openai', model='gpt-6.1-terra',
        base_url='https://api.openai.com/v1', api_key='not-sent')
    modes = _build_structured_request_modes(cache_key='terra-root-schema', messages=[],
        response_model=RootModel[str], validation_error=None,
        supported=gateway._structured_modes(RootModel[str]))
    assert len(modes) == 1 and modes[0][0] == 'prompt' and modes[0][2] is None
    assert '只输出一个 JSON 值' in modes[0][1][0]['content']
    assert gateway._structured_modes(Answer) == ('schema', 'json_object', 'prompt')
    assert RootModel[str].model_validate_json('"ok"').root == 'ok'
    recovered = _build_structured_request_modes(cache_key='terra-root-schema', messages=[],
        response_model=RootModel[str], validation_error='string required',
        supported=gateway._structured_modes(RootModel[str]))
    assert '仍然只输出一个 JSON 值' in recovered[0][1][-1]['content']
    assert all('JSON 对象' not in message['content'] for message in recovered[0][1])


def test_sdk_real_connection_refusal_closes_owned_client_and_never_retries():
    from backend.shared.llm.model_transport import ModelTransportTimeout
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    requests = []
    client = httpx.Client(trust_env=False, event_hooks={'request': [lambda request: requests.append(request.method)]})
    timeout = ModelTransportTimeout(remaining=lambda: 15,
        limits={'header_timeout': 10, 'idle_timeout': 10}, owned_client_factory=lambda: client)
    adapter = OpenAIResponsesCompletion(api_base=f'http://127.0.0.1:{port}/v1')
    with pytest.raises(ResponsesError) as captured:
        adapter(model='gpt-6.1-terra', messages=[{'role': 'user', 'content': 'ping'}],
            api_key='local-test-only', timeout=timeout)
    assert captured.value.category == 'transient'
    assert requests == ['POST']
    assert client.is_closed
    with pytest.raises(ResponsesError) as captured:
        asyncio.run(adapter.acomplete(model='gpt-6.1-terra',
            messages=[{'role': 'user', 'content': 'ping'}], api_key='local-test-only', timeout=15))
    assert captured.value.category == 'transient'


@pytest.mark.parametrize('started', [False, True])
@pytest.mark.parametrize('nested', [False, True])
def test_real_sdk_sse_decoder_preserves_output_phase_for_both_error_shapes(started, nested):
    projection = ResponseEventProjection()
    events = [{'type': 'response.output_text.delta', 'delta': 'partial'}] if started else []
    error = {'code': 'server_busy', 'message': 'private diagnostic'}
    events.append({'error': error} if nested else {'type': 'error', **error})
    wire = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
    response = httpx.Response(200, content=wire, headers={'content-type': 'text/event-stream'},
        request=httpx.Request('POST', 'https://api.openai.com/v1/responses'))
    with OpenAI(api_key='not-sent', max_retries=0) as client:
        stream = Stream(cast_to=ResponseStreamEvent, response=response, client=client)
        with pytest.raises(ResponsesError) as captured:
            try:
                for event in stream:
                    projection.accept(event.model_dump(exclude_unset=True))
            except APIError as error:
                raise _sdk_stream_error(error, projection) from None
    facts = captured.value
    assert facts.category == 'capacity'
    assert facts.output_started is started and facts.retryable is (not started)
    assert 'private' not in str(facts) + repr(vars(facts))
    assert response.is_closed


@pytest.mark.parametrize('started', [False, True])
def test_real_async_sdk_sse_decoder_keeps_safe_error_and_output_phase(started):
    async def decode():
        projection = ResponseEventProjection()
        events = [{'type': 'response.output_text.delta', 'delta': 'partial'}] if started else []
        events.append({'error': {'code': 'server_busy', 'message': 'private diagnostic'}})
        wire = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
        response = httpx.Response(200, content=wire, headers={'content-type': 'text/event-stream'},
            request=httpx.Request('POST', 'https://api.openai.com/v1/responses'))
        async with AsyncOpenAI(api_key='not-sent', max_retries=0) as client:
            stream = AsyncStream(cast_to=ResponseStreamEvent, response=response, client=client)
            try:
                async for event in stream:
                    projection.accept(event.model_dump(exclude_unset=True))
            except APIError as error:
                facts = _sdk_stream_error(error, projection)
                assert facts.category == 'capacity' and facts.output_started is started
                assert facts.retryable is (not started)
                assert 'private' not in str(facts) + repr(vars(facts))
            else:
                pytest.fail('SDK decoder did not reject the error event')
        assert response.is_closed
    asyncio.run(decode())
