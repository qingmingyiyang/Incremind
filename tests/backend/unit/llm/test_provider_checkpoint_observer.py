"""Observe accepted provider cursors before output, without a second create."""
from contextlib import contextmanager
import pytest

from backend.shared.llm.openai_responses import ProviderBackgroundOptions
from tests.backend.unit.llm.test_provider_background import BackgroundProvider, background_call


@contextmanager
def local_provider(mode):
    provider = BackgroundProvider(mode)
    try:
        yield provider
    finally:
        provider.close()


@pytest.mark.parametrize('mode,sequences', [('full', [0, 1, 2]), ('resume', [0, 1, 2, 3])])
def test_real_provider_reports_each_validated_cursor_once(tmp_path, mode, sequences):
    observed, wire = [], {}
    with local_provider(mode) as provider:
        receipt, terminal, clients, responses, _deltas, _decisions = background_call(
            tmp_path, provider, provider_observer=lambda cursor: observed.append(dict(cursor)),
            observation=wire)
        assert receipt.status == 'completed'
        assert observed == [{'response_id': 'resp_local', 'sequence_number': number} for number in sequences]
        assert len([call for call in provider.calls if call[0] == 'POST']) == 1
        assert len([call for call in provider.calls if call[0] == 'GET']) == (mode == 'resume')
    assert len(wire['dispatches']) == len(terminal) == 1
    assert all(client.is_closed for client in clients) and all(response.is_closed for response in responses)


@pytest.mark.parametrize('fail_at', [0, 1])
def test_cursor_storage_failure_stops_before_output_and_never_recreates(tmp_path, fail_at):
    observed, wire = [], {}
    def observe(cursor):
        observed.append(dict(cursor))
        if cursor['sequence_number'] == fail_at:
            raise ConnectionError('synthetic_cursor_store_unavailable')
    with local_provider('resume') as provider:
        receipt, terminal, clients, responses, deltas, decisions = background_call(
            tmp_path, provider, provider_observer=observe, observation=wire)
        assert receipt.status == 'failed'
        assert provider.calls == [('POST', '/v1/responses')]
        assert observed == [{'response_id': 'resp_local', 'sequence_number': number}
                            for number in range(fail_at + 1)]
    assert len(wire['dispatches']) == len(terminal) == 1
    assert deltas == decisions == []
    assert all(client.is_closed for client in clients) and all(response.is_closed for response in responses)


def test_invalid_wire_cursor_is_not_reported_to_the_owner(tmp_path):
    observed, wire = [], {}
    with local_provider('bad_bool') as provider:
        receipt, terminal, _clients, _responses, _deltas, _decisions = background_call(
            tmp_path, provider, provider_observer=lambda cursor: observed.append(dict(cursor)), observation=wire)
        assert receipt.status == 'failed' and provider.calls == [('POST', '/v1/responses')]
    assert observed == [{'response_id': 'resp_local', 'sequence_number': 0}]
    assert len(wire['dispatches']) == len(terminal) == 1


def test_observer_must_be_callable_when_present():
    with pytest.raises(ValueError, match='provider_background_options_invalid'):
        ProviderBackgroundOptions(checkpoint=lambda: None, resume=lambda value: False, observe='invalid')
