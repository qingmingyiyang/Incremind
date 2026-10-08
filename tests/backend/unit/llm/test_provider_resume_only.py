"""Explicit owner cursors resume a response through real GET-only wire attempts."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from threading import Thread

import pytest

from tests.backend.unit.llm.test_provider_background import BackgroundProvider, background_call


CURSOR = {'response_id': 'resp_local', 'sequence_number': 1}
USAGE = {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}


class ResumeProvider:
    def __init__(self, mode='tail'):
        self.mode, self.calls = mode, []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.calls.append(('POST', self.path))
                self.send_error(500)

            def do_GET(self):
                owner.calls.append(('GET', self.path))
                delta = {'type': 'response.output_text.delta', 'sequence_number': 2, 'delta': 'lo"}'}
                events = [delta, BackgroundProvider.completed(3)]
                if owner.mode == 'completed_only':
                    events = [BackgroundProvider.completed(2)]
                elif owner.mode == 'reconnect':
                    delta['delta'] = 'lo'
                    if len(owner.calls) == 1:
                        events = [delta]
                    else:
                        events = [delta, {'type': 'response.output_text.delta',
                            'sequence_number': 3, 'delta': '"}'}, BackgroundProvider.completed(4)]
                elif owner.mode == 'foreign':
                    events[-1]['response']['id'] = 'resp_foreign'
                elif owner.mode in {'bool', 'gap', 'old'}:
                    events[0]['sequence_number'] = {'bool': True, 'gap': 3, 'old': 1}[owner.mode]
                elif owner.mode == 'refusal':
                    events = [{'type': 'response.refusal.delta', 'sequence_number': 2, 'delta': 'refused'}]
                elif owner.mode == 'mismatch':
                    events[0]['delta'] = 'wrong"}'
                raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@contextmanager
def local_provider(mode='tail'):
    provider = ResumeProvider(mode)
    try:
        yield provider
    finally:
        provider.close()


def effect_states(tmp_path):
    with sqlite3.connect(f"file:{tmp_path / 'turns.sqlite3'}?mode=ro", uri=True) as connection:
        return connection.execute('SELECT state FROM effect').fetchall()


def completed_summaries(tmp_path, turn_id):
    with sqlite3.connect(f"file:{tmp_path / 'turns.sqlite3'}?mode=ro", uri=True) as connection:
        events = [json.loads(row[0]) for row in connection.execute(
            'SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence', (turn_id,))]
    return [event['data']['summary'] for event in events if event['type'] == 'turn.completed']


@pytest.mark.parametrize('mode,sequences', [('tail', [2, 3]), ('completed_only', [2])])
def test_get_only_rebuilds_complete_json_through_original_decoder_once(tmp_path, mode, sequences):
    observed, wire = [], {}
    with local_provider(mode) as provider:
        receipt, terminal, clients, responses, deltas, decisions = background_call(
            tmp_path, provider, resume_only=True, resume_cursor=CURSOR, observation=wire,
            provider_observer=lambda value: observed.append(dict(value)))
        assert receipt.status == 'completed'
        assert completed_summaries(tmp_path, receipt.turn_id) == ['hello']
        assert provider.calls == [('GET', '/v1/responses/resp_local?stream=true&starting_after=1')]
    assert deltas == ['hello'] and decisions == []
    assert observed == [{'response_id': 'resp_local', 'sequence_number': number} for number in sequences]
    assert len(wire['dispatches']) == len(terminal) == 1
    assert terminal[0]['status'] == 'succeeded' and terminal[0]['usage'] == USAGE
    assert effect_states(tmp_path) == [('SETTLED_OK',)]
    assert len(clients) == len(responses) == 1
    assert clients[0].is_closed and responses[0].is_closed


def test_get_only_reconnect_deduplicates_without_recreating_or_releasing_tail(tmp_path):
    observed, wire = [], {}
    with local_provider('reconnect') as provider:
        receipt, terminal, clients, responses, deltas, decisions = background_call(
            tmp_path, provider, resume_only=True, resume_cursor=CURSOR, observation=wire,
            provider_observer=lambda value: observed.append(dict(value)))
        assert receipt.status == 'completed'
        assert provider.calls == [
            ('GET', '/v1/responses/resp_local?stream=true&starting_after=1'),
            ('GET', '/v1/responses/resp_local?stream=true&starting_after=2')]
    assert deltas == ['hello'] and len(decisions) == 1
    assert observed == [{'response_id': 'resp_local', 'sequence_number': number} for number in (2, 3, 4)]
    assert len(wire['dispatches']) == len(terminal) == 1
    assert terminal[0]['status'] == 'succeeded' and terminal[0]['usage'] == USAGE
    assert len(clients) == len(responses) == 2
    assert all(client.is_closed for client in clients) and all(response.is_closed for response in responses)


@pytest.mark.parametrize('cursor', [None, {}, {'response_id': 'foreign', 'sequence_number': 1},
    {'response_id': 'resp_local/another', 'sequence_number': 1},
    {'response_id': 'resp_local', 'sequence_number': True},
    {'response_id': 'resp_local', 'sequence_number': -1},
    {'response_id': 'resp_local', 'sequence_number': '1'},
    {'response_id': 'resp_local', 'sequence_number': 9_007_199_254_740_992}])
def test_missing_or_invalid_explicit_cursor_never_sends_any_request(tmp_path, cursor):
    wire = {}
    with local_provider() as provider:
        receipt, terminal, clients, responses, deltas, decisions = background_call(
            tmp_path, provider, resume_only=True, resume_cursor=cursor, observation=wire)
        assert receipt.status == 'failed' and provider.calls == []
    assert wire['dispatches'] == terminal == clients == responses == deltas == decisions == []


@pytest.mark.parametrize('mode', ['foreign', 'bool', 'gap', 'old', 'refusal', 'mismatch'])
def test_untrusted_resumed_wire_fails_before_output_without_new_create(tmp_path, mode):
    wire = {}
    with local_provider(mode) as provider:
        receipt, terminal, clients, responses, deltas, decisions = background_call(
            tmp_path, provider, resume_only=True, resume_cursor=CURSOR, observation=wire)
        assert receipt.status == 'failed'
        assert provider.calls == [('GET', '/v1/responses/resp_local?stream=true&starting_after=1')]
    assert len(wire['dispatches']) == len(terminal) == 1
    assert terminal[0]['status'] == 'failed_transport' and terminal[0].get('usage') is None
    assert deltas == decisions == [] and effect_states(tmp_path) == [('UNKNOWN',)]
    assert all(client.is_closed for client in clients) and all(response.is_closed for response in responses)


def test_resumed_cursor_storage_failure_does_not_release_tail_or_create(tmp_path):
    def observe(_value):
        raise ConnectionError('synthetic_resume_cursor_store_unavailable')
    wire = {}
    with local_provider() as provider:
        receipt, terminal, clients, responses, deltas, decisions = background_call(
            tmp_path, provider, resume_only=True, resume_cursor=CURSOR, observation=wire,
            provider_observer=observe)
        assert receipt.status == 'failed'
        assert provider.calls == [('GET', '/v1/responses/resp_local?stream=true&starting_after=1')]
    assert len(wire['dispatches']) == len(terminal) == 1
    assert terminal[0]['status'] == 'failed_transport' and terminal[0].get('usage') is None
    assert deltas == decisions == [] and effect_states(tmp_path) == [('UNKNOWN',)]
    assert all(client.is_closed for client in clients) and all(response.is_closed for response in responses)


def test_resumed_cleanup_failure_records_usage_once_without_another_wire(tmp_path):
    wire = {}
    with local_provider() as provider:
        receipt, terminal, clients, responses, deltas, decisions = background_call(
            tmp_path, provider, resume_only=True, resume_cursor=CURSOR, observation=wire, close_fails=True)
        assert receipt.status == 'failed'
        assert provider.calls == [('GET', '/v1/responses/resp_local?stream=true&starting_after=1')]
    assert len(wire['dispatches']) == len(terminal) == 1
    assert terminal[0]['status'] == 'failed_transport' and terminal[0]['usage'] == USAGE
    assert deltas == ['hello'] and decisions == [] and effect_states(tmp_path) == [('UNKNOWN',)]
    assert all(client.is_closed for client in clients) and all(response.is_closed for response in responses)


def test_resumed_permission_revocation_stops_next_get_and_preserves_unknown_cost(tmp_path):
    wire = {}
    with local_provider('reconnect') as provider:
        receipt, terminal, clients, responses, deltas, decisions = background_call(
            tmp_path, provider, resume_only=True, resume_cursor=CURSOR, observation=wire, withdraw=True)
        assert receipt.status == 'failed'
        assert provider.calls == [('GET', '/v1/responses/resp_local?stream=true&starting_after=1')]
    assert len(wire['dispatches']) == len(terminal) == 1
    assert terminal[0].get('usage') is None and deltas == [] and len(decisions) == 1
    assert effect_states(tmp_path) == [('UNKNOWN',)]
    assert all(client.is_closed for client in clients) and all(response.is_closed for response in responses)


def test_resumed_observer_cancellation_closes_without_new_wire(tmp_path):
    def observe(_value):
        raise asyncio.CancelledError()
    wire = {}
    with local_provider() as provider:
        with pytest.raises(asyncio.CancelledError):
            background_call(tmp_path, provider, resume_only=True, resume_cursor=CURSOR,
                provider_observer=observe, observation=wire)
        assert provider.calls == [('GET', '/v1/responses/resp_local?stream=true&starting_after=1')]
    assert len(wire['dispatches']) == len(wire['terminal']) == 1
    assert wire['terminal'][0]['status'] == 'consumer_cancelled'
    assert wire['terminal'][0].get('usage') is None and wire['deltas'] == wire['decisions'] == []
    assert all(client.is_closed for client in wire['clients'])
    assert all(response.is_closed for response in wire['responses'])
