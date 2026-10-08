"""Actual governed configuration wrapping a failing external provider stream."""
import pytest

from tests.memory_app.test_governed_retry import governed_runtime


def test_governed_thinking_natural_eof_enters_frozen_thinking_error_budget(tmp_path):
    state = governed_runtime(tmp_path, streaming=True, eof_phase='thinking')
    assert state['receipt'].status == 'completed'
    assert len(state['calls']) == len(state['attempts']) == 2
    assert state['deltas'] == ['done']
    assert [row['status'] for row in state['attempts']] == ['failed_transport', 'succeeded']
    requests = [row for row in state['retry_requests'] if row['kind'] != 'limits']
    assert len(requests) == 1 and requests[0]['kind'] == 'connection'
    assert requests[0]['phase'] == 'thinking' and requests[0]['counts'] == {}
    assert all(stream.closed for stream in state['streams'])
    assert state['outputs'][0][1]['usage'] == {'input_tokens': 8, 'output_tokens': 4, 'total_tokens': 12}


@pytest.mark.parametrize('retry,phase', [(False, 'thinking'), (True, 'body')])
def test_governed_default_none_and_body_eof_keep_single_wire(tmp_path, retry, phase):
    state = governed_runtime(tmp_path, streaming=True, retry=retry, eof_phase=phase)
    assert state['receipt'].status == 'failed'
    assert len(state['calls']) == len(state['attempts']) == 1
    assert [row['status'] for row in state['attempts']] == ['failed_transport']
    assert state['deltas'] == (['partial\n\n'] if phase == 'body' else [])
    assert all(stream.closed for stream in state['streams'])


@pytest.mark.parametrize('close_error', [ConnectionError, OSError])
def test_governed_failed_provider_close_never_opens_a_second_attempt(tmp_path, close_error):
    state = governed_runtime(tmp_path, streaming=True,
        close_error=close_error('synthetic provider refused closure'))
    assert state['receipt'].status == 'failed'
    assert len(state['attempts']) == 1
    assert len(state['calls']) == len(state['streams']) == 1
    assert state['streams'][0].close_calls == 1 and state['streams'][0].closed is False
    assert state['deltas'] == []
    assert not [row for row in state['retry_requests'] if row['kind'] != 'limits']


def test_governed_missing_provider_close_cannot_borrow_exhausted_wrapper_close(tmp_path):
    state = governed_runtime(tmp_path, streaming=True, close_supported=False)
    assert state['receipt'].status == 'failed'
    assert len(state['attempts']) == len(state['calls']) == len(state['streams']) == 1
    assert state['streams'][0].closed is False and state['streams'][0].close_calls == 0
    assert not [row for row in state['retry_requests'] if row['kind'] != 'limits']


def test_raw_httpx_close_failure_is_sticky_and_not_reclassified_as_header_timeout():
    import httpx
    from backend.shared.llm.model_transport import ModelTransportError, ModelTransportTimeout
    class ProviderBytes(httpx.SyncByteStream):
        calls = 0
        def __iter__(self):
            yield b'synthetic bytes'
        def close(self):
            self.calls += 1
            raise OSError('synthetic close failure')
    inner = ProviderBytes()
    response = httpx.Response(200, request=httpx.Request('POST', 'https://synthetic.invalid'), stream=inner)
    timeout = ModelTransportTimeout(remaining=lambda: 10,
        limits={'header_timeout': 7, 'idle_timeout': 3})
    timeout.bind_response(response)
    try:
        raise httpx.ReadTimeout('synthetic original read timeout')
    except httpx.ReadTimeout:
        with pytest.raises(ModelTransportError) as failure:
            response.close()
    assert failure.value.kind == 'provider_close_failed'
    assert timeout.header_error(failure.value).kind == 'provider_close_failed'
    with pytest.raises(ModelTransportError, match='provider_close_failed'):
        response.stream.close()
    assert inner.calls == 1
    assert response.stream.closed is False and response.stream.close_failed is True
