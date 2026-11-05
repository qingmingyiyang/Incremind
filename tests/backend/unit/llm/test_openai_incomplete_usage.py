import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

import httpx
from openai import AsyncOpenAI, AsyncStream, OpenAI, Stream
from openai.types.responses import ResponseStreamEvent
import pytest

from backend.memory_app.v2.policies.retry import v2
from backend.shared.llm.litellm_gateway import (
    ModelRetryControl, _model_transport_failure, _record_structured_failure, _ProviderAttemptTerminal,
)
from backend.shared.llm.openai_sdk import ResponseEventProjection
from backend.shared.llm.openai_responses import ResponsesError


def _event(*, reason='max_output_tokens', usage=None):
    response = {'status': 'incomplete', 'incomplete_details': {'reason': reason},
        'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'private terminal text'}]}]}
    if usage is not None:
        response['usage'] = usage
    return {'type': 'response.incomplete', 'response': response, 'sequence_number': 1}


def _usage():
    return {'input_tokens': 17, 'output_tokens': 7, 'total_tokens': 24,
        'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 2},
        'output_tokens_details': {'reasoning_tokens': 7}}


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('prefix', ['{"answer":"partial', '{"answer":"complete-looking"}'])
def test_real_sdk_decoder_keeps_incomplete_failure_usage_without_returning_success(asynchronous, prefix):
    # 只把显式纯输入交给原 SDK 解码器，不安装 HTTP 运输或替换客户端。
    events = [{'type': 'response.output_text.delta', 'delta': prefix, 'sequence_number': 0},
        _event(usage=_usage())]
    wire = ''.join('data: ' + json.dumps(row) + '\n\n' for row in events).encode()
    projection, seen = ResponseEventProjection(), []
    response = httpx.Response(200, content=wire, headers={'content-type': 'text/event-stream'},
        request=httpx.Request('POST', 'https://api.openai.com/v1/responses'))

    async def decode_async():
        async with AsyncOpenAI(api_key='parser-input-only', max_retries=0) as client:
            stream = AsyncStream(cast_to=ResponseStreamEvent, response=response, client=client)
            async for event in stream:
                seen.extend(projection.accept(event.model_dump(exclude_unset=True)))

    with pytest.raises(ResponsesError) as captured:
        if asynchronous:
            asyncio.run(decode_async())
        else:
            with OpenAI(api_key='parser-input-only', max_retries=0) as client:
                stream = Stream(cast_to=ResponseStreamEvent, response=response, client=client)
                for event in stream:
                    seen.extend(projection.accept(event.model_dump(exclude_unset=True)))
    error = captured.value
    assert error.code == 'max_output_tokens' and error.category == 'incomplete'
    assert error.retryable is False and error.output_started is True
    assert error.usage == {'prompt_tokens': 17, 'completion_tokens': 7, 'total_tokens': 24,
        'prompt_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 2},
        'completion_tokens_details': {'reasoning_tokens': 7}}
    assert projection.text == prefix and projection.finished is False
    assert [row['choices'][0]['finish_reason'] for row in seen] == [None]
    assert 'private' not in str(error) + repr(vars(error))
    assert response.is_closed


@pytest.mark.parametrize(('reason', 'code', 'category'), [
    ('max_output_tokens', 'max_output_tokens', 'incomplete'),
    ('content_filter', 'content_filter', 'refusal'),
    ('private unknown reason', None, 'incomplete'),
    (None, None, 'incomplete'),
])
def test_incomplete_reason_is_bounded_and_never_retryable(reason, code, category):
    with pytest.raises(ResponsesError) as captured:
        ResponseEventProjection().accept(_event(reason=reason))
    error = captured.value
    assert error.code == code and error.category == category
    assert error.retryable is False and error.usage == {}
    assert 'private' not in str(error) + repr(vars(error))


def test_incomplete_counts_preserve_zero_and_drop_invalid_or_unreported_fields():
    usage = {'input_tokens': 0, 'output_tokens': True, 'total_tokens': -1,
        'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': False, 'private': 'private'},
        'output_tokens_details': {'reasoning_tokens': 0}, 'private': 'private'}
    with pytest.raises(ResponsesError) as captured:
        ResponseEventProjection().accept(_event(usage=usage))
    assert captured.value.usage == {'prompt_tokens': 0,
        'prompt_tokens_details': {'cached_tokens': 0}, 'completion_tokens_details': {'reasoning_tokens': 0}}


@pytest.mark.parametrize('response', [None, [], {'status': 'completed', 'usage': _usage()}])
def test_incomplete_event_cannot_attest_another_terminal_shape(response):
    with pytest.raises(ValueError, match='subscription_response_invalid'):
        ResponseEventProjection().accept({'type': 'response.incomplete', 'response': response})


def test_actual_retry_control_retains_known_failure_usage_and_declines_replay():
    control = ModelRetryControl(policy=v2, checkpoint=lambda: None)
    control.start(20)
    control.before_attempt()
    with pytest.raises(ResponsesError) as captured:
        ResponseEventProjection().accept(_event(usage=_usage()))
    error = captured.value
    control.provider_failed(error)
    assert control.attempt_usage == {'input_tokens': 17, 'output_tokens': 7, 'total_tokens': 24}
    assert control.attempt_cache == {'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 2}
    assert control.recorded_usage(control.attempt_usage) == control.attempt_usage
    assert control.terminal_seen is False and control.phase == 'before_output'
    assert _model_transport_failure(error, datetime.now(timezone.utc)) == ('provider_rejected', None)
    assert control.retry(error) is False and control.retry_number == 0


@pytest.mark.parametrize('path', ['retry', 'single', 'terminal'])
@pytest.mark.parametrize(('reported', 'expected'), [
    (None, None),
    (_usage(), {'input_tokens': 17, 'output_tokens': 7, 'total_tokens': 24}),
    ({'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0,
        'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}},
        {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}),
    ({'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}}, None),
    ({'input_tokens': 17}, {'input_tokens': 17}),
])
def test_real_sqlite_wire_keeps_failure_and_reported_counts(tmp_path, path, reported, expected):
    from backend.memory_app.model_config import _GenerationEgressLease
    from core.ai_kernel import ScopedCapabilityRegistry, SQLiteAITurnStore, SynchronousAIRuntime

    store = SQLiteAITurnStore(tmp_path / 'wire.sqlite3')
    # 本节点只执行原生接纳和失败收口，未配置执行 planner，也不产生模型结果。
    runtime = SynchronousAIRuntime(planner=None, registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store)
    repo = Path(__file__).resolve().parents[4]
    request = json.loads((repo / 'core-contracts/ai/fixtures/turn-request/valid-project-answer.json').read_text('utf-8'))
    runtime.accept_turn(request)
    now = datetime.now(timezone.utc)
    lease = store.try_acquire_run_lease(request['turn_id'], 'incomplete-check',
        now=now, stale_after=now + timedelta(seconds=120))
    assert lease is not None
    context = runtime._run_lease_context.set(lease)
    try:
        control = runtime._begin_planner_control(request['turn_id'], step_id='step-' + uuid4().hex,
            model_request_id='model-request-' + uuid4().hex)
        route = store.put(request['turn_id'], 'incomplete-parser-route', {'provider': 'openai'})
        control.model_call_routed(snapshot_ref=route, snapshot_revision='a' * 64,
            prompt_cache_scope_identity='b' * 64, provider='openai', model='gpt-6.1-terra', execution_location='remote')
        control.model_call_started(provider='openai', model='gpt-6.1-terra')
        wire = control.begin_model_wire_attempt()
        retry = ModelRetryControl(policy=v2, checkpoint=lambda: None) if path == 'retry' else None
        if retry is not None:
            retry.start(20)
            retry.before_attempt()

        def consume_parser_input():
            try:
                ResponseEventProjection().accept(_event(usage=reported))
            except ResponsesError as error:
                if retry is not None:
                    retry.provider_failed(error)
                if path == 'terminal':
                    _ProviderAttemptTerminal(_GenerationEgressLease(), None, wire).failed(error,
                        error_code='provider_stream_failed')
                else:
                    _record_structured_failure(wire, error, retry)
                raise

        with pytest.raises(ResponsesError):
            wire.invoke_wire(consume_parser_input)
        events = [row for row in store.events_after(request['turn_id']) if row['type'] == 'model.attempt.terminal']
        assert len(events) == 1
        receipt = store.get(events[0]['data']['receipt_ref'])
        assert receipt['status'] == 'failed_transport' and receipt['error_code'] == 'ai.provider_stream_failed'
        assert receipt['usage'] == expected
        assert receipt['usage_status'] == ('unavailable' if expected is None else 'reported')
        assert receipt['output_stored'] is False
        assert not any(row['type'] == 'model.completed' for row in store.events_after(request['turn_id']))
        if reported is not None and 'input_tokens_details' in reported:
            assert receipt['cache_metadata']['cache_read_input_tokens'] == 0
            assert receipt['cache_status'] == 'reported'
        else:
            assert receipt['cache_metadata'] is None and receipt['cache_status'] == 'unavailable'
    finally:
        runtime._run_lease_context.reset(context)
