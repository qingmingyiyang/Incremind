"""Native Responses errors over real HTTPX sockets and SQLite attempt receipts."""
import pytest
from datetime import datetime, timezone

from backend.shared.llm.litellm_gateway import _model_transport_failure

from tests.backend.unit.llm.test_model_transport_watchdog import run_http


@pytest.mark.parametrize('mode,delay', [('server', 2), ('rate_limit', 0), ('early_eof', 1)])
def test_responses_transient_error_metadata_and_eof_retry_after_closed_terminal(tmp_path, mode, delay):
    receipt, calls, retries, deltas, terminals, responses = run_http(tmp_path, mode, 'responses',
        native_capabilities=True)
    assert receipt.status == 'completed'
    assert calls == len(terminals) == 2 and len(retries) == 1
    assert retries[0]['budget'] == 'before_output' and retries[0]['delay'] == delay
    assert [row['status'] for row in terminals] == ['failed_transport', 'succeeded']
    assert len({row['attempt_id'] for row in terminals}) == 2
    assert deltas == ['done'] and responses and all(response.is_closed for response in responses)


@pytest.mark.parametrize('mode', ['quota', 'unauthorized', 'forbidden', 'refusal', 'explicit_incomplete', 'body_eof'])
def test_responses_permanent_rejections_and_body_eof_never_dispatch_again(tmp_path, mode):
    receipt, calls, retries, deltas, terminals, responses = run_http(tmp_path, mode, 'responses',
        native_capabilities=True)
    assert receipt.status == 'failed'
    assert calls == len(terminals) == 1 and retries == []
    assert [row['status'] for row in terminals] == ['failed_transport']
    assert deltas == (['partial\n\n'] if mode == 'body_eof' else [])
    assert responses and all(response.is_closed for response in responses)


def test_native_complete_keeps_partial_body_eof_permanent_before_returning_text(tmp_path):
    receipt, calls, retries, deltas, terminals, responses = run_http(tmp_path, 'body_eof', 'responses',
        native_capabilities=True, streaming=False)
    assert receipt.status == 'failed'
    assert calls == len(terminals) == 1 and retries == [] and deltas == []
    assert [row['status'] for row in terminals] == ['failed_transport']
    assert responses and all(response.is_closed for response in responses)


@pytest.mark.parametrize('protocol,mode,retry', [
    ('responses', 'busy_http', True), ('chat', 'busy_http', True),
    ('responses', 'busy_stream', True), ('responses', 'busy_quota', False),
    ('responses', 'busy_auth', False), ('responses', 'busy_refusal', False),
    ('responses', 'busy_unsupported', False), ('responses', 'busy_body', False),
    ('responses', 'busy_tool', False),
])
@pytest.mark.parametrize('streaming', [False, True])
def test_busy_uses_existing_audited_retry_only_before_output(tmp_path, protocol, mode, retry, streaming):
    observation, clients = {}, []
    receipt, calls, retries, deltas, terminals, responses = run_http(tmp_path, mode, protocol,
        native_capabilities=True, streaming=streaming, observation=observation, owned_clients=clients)
    expected = 2 if retry else 1
    assert receipt.status == ('completed' if retry else 'failed')
    assert calls == len(terminals) == observation['guards'] == observation['finishes'] == expected
    assert len(retries) == (1 if retry else 0)
    assert [row['status'] for row in terminals] == (['failed_transport', 'succeeded'] if retry else ['failed_transport'])
    assert len({row['attempt_id'] for row in terminals}) == expected
    assert clients and all(client.is_closed for client in clients)
    assert responses and all(response.is_closed for response in responses)
    assert deltas == (['done'] if retry else ['partial\n\n'] if mode == 'busy_body' and streaming else [])


@pytest.mark.parametrize('metadata,expected', [
    ({'code': 'server_busy'}, 'server'),
    ({'type': 'server_busy'}, 'server'),
    ({'body': {'error': {'code': 'server_busy'}}}, 'server'),
    ({'body': {'error': {'type': 'server_busy'}}}, 'server'),
    ({'body': {'detail': 'server_busy'}}, 'server'),
    ({'body': {'error': {'message': 'servers are currently busy'}}}, 'server'),
    ({'body': {'error': {'message': 'quota exceeded; servers are currently busy'}}}, 'quota'),
    ({'body': {'error': {'message': 'out of budget; server_busy'}}}, 'quota'),
    ({'body': {'detail': 'GoUsageLimitError; servers are currently busy'}}, 'quota'),
    ({'body': {'detail': 'FreeUsageLimitError; servers are currently busy'}}, 'quota'),
    ({'body': {'detail': 'Monthly usage limit reached; servers are currently busy'}}, 'quota'),
    ({'body': {'error': {'type': 'GoUsageLimitError', 'message': 'servers are currently busy'}}}, 'quota'),
    ({'body': {'error': {'type': 'FreeUsageLimitError', 'message': 'servers are currently busy'}}}, 'quota'),
    ({'body': {'error': {'code': 'invalid_request_error', 'message': 'quota exceeded; servers are currently busy'}}}, 'provider_rejected'),
    ({'body': {'error': {'type': 'invalid_request_error', 'message': 'unsupported parameter; servers are currently busy'}}}, 'provider_rejected'),
    ({'code': 'server_busy', 'type': 'insufficient_quota'}, 'quota'),
    ({'code': 'server_busy', 'status_code': 401}, 'authentication'),
    ({'body': {'error': {'code': 'content_filter', 'message': 'servers are currently busy'}}}, 'content_filter'),
    ({'body': {'error': {'code': 'subscription_sharing_unsupported_capability', 'type': 'server_busy'}}}, 'provider_rejected'),
    ({'body': {'error': ['server_busy']}}, 'provider_rejected'),
    ({}, 'provider_rejected'),
])
def test_sdk_structured_busy_metadata_preserves_permanent_priority(metadata, expected):
    error = RuntimeError('private arbitrary exception text server_busy')
    error.status_code = 400
    for name, value in metadata.items():
        setattr(error, name, value)
    assert _model_transport_failure(error, datetime.now(timezone.utc)) == (expected, None)
