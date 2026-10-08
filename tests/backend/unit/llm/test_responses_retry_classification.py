"""Native Responses errors over real HTTPX sockets and SQLite attempt receipts."""
import pytest

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
