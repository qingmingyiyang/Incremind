"""Local MCP HTTP calls use the original completed delivery and owner."""
from copy import deepcopy
import sys
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.memory_app.v2.test_external_context import (
    env, prepare, TURN, settings, document,
)


PREFIX = '/api/v2/external-agent/mcp/'


def delivered(env):
    api, runtime, runner, frozen = prepare(env)
    result = api.execute(TURN, runtime=runtime, runner=runner)
    return api, frozen, result


def call(env, tool, arguments, client='codex'):
    return env.http.post(PREFIX + tool, json={'client': client, 'arguments': arguments})


def test_mcp_report_use_binds_the_original_client_and_delivery(env):
    _api, _frozen, _result = delivered(env)
    wrong = call(env, 'report_use', {'turn_id': TURN, 'ids': ['M1']}, client='claude')
    assert wrong.status_code == 409
    assert env.records.list('v2_external_agent_citations') == ()
    result = call(env, 'report_use', {'turn_id': TURN, 'ids': ['M1']})
    assert result.status_code == 200
    assert result.json()['turn_id'] == TURN
    assert result.json()['result']['ids'] == ['M1']
    assert len(env.records.list('v2_external_agent_citations')) == 1
    assert env.model.calls == 0


def test_mcp_read_drills_the_exact_original_number_into_a_new_turn(env):
    _api, original, _result = delivered(env)
    old = deepcopy(env.http.app.state.ai_turn_store.events_after(TURN))
    response = call(env, 'read', {'id': {'turn_id': TURN, 'id': 'M1'},
                                 'window': {'start': 0, 'end': 5}})
    assert response.status_code == 200
    output = response.json()
    assert output['turn_id'] != TURN
    assert output['result']['entries'][0]['excerpt'] == '完整且可定'
    store = env.http.app.state.ai_turn_store
    assert store.get_request(TURN) == original and store.events_after(TURN) == old
    assert store.get_request(output['turn_id'])['desired_outcome'] == 'external.context'
    assert store.events_after(output['turn_id'])[-1]['type'] == 'turn.completed'
    assert len(env.records.list('v2_external_agent_deliveries')) == 2
    assert env.model.calls == 0


def test_mcp_read_rejects_foreign_number_and_boolean_window(env):
    delivered(env)
    for arguments in [
        {'id': {'turn_id': TURN, 'id': 'M2'}},
        {'id': {'turn_id': TURN, 'id': 'M1'}, 'window': {'start': False, 'end': 2}},
        {'id': {'turn_id': 'turn-foreign', 'id': 'M1'}},
    ]:
        assert call(env, 'read', arguments).status_code in {400, 409}
    assert len(env.records.list('v2_external_agent_deliveries')) == 1
    assert env.model.calls == 0


def test_mcp_closed_egress_blocks_read_but_preserves_factual_use_reporting(env):
    delivered(env)
    settings(env, allow_remote=False)
    assert call(env, 'read', {'id': {'turn_id': TURN, 'id': 'M1'}}).status_code == 409
    response = call(env, 'report_use', {'turn_id': TURN, 'ids': ['M1']})
    assert response.status_code == 200
    assert len(env.records.list('v2_external_agent_deliveries')) == 1
    assert len(env.records.list('v2_external_agent_citations')) == 1
    assert env.model.calls == 0


def test_mcp_http_rejects_malformed_client_without_any_turn(env):
    for client in [[], {}, None, 1]:
        response = call(env, 'read', {'id': {'turn_id': TURN, 'id': 'M1'}}, client=client)
        assert response.status_code == 400
        assert response.json() == {'detail': 'external_agent_request_invalid'}
    assert env.records.list('v2_external_agent_deliveries') == ()
    assert env.model.calls == 0


def test_mcp_read_receipt_preserves_the_parent_number_and_actual_outcome(env):
    api, _frozen, _result = delivered(env)
    response = call(env, 'read', {'id': {'turn_id': TURN, 'id': 'M1'}})
    assert response.status_code == 200
    child = response.json()['turn_id']
    parent_ref, _archive, parent_outcome = api._completed(TURN)
    _child_ref, child_archive = api._archive(child)
    assert child_archive['origin'] == {'turn_id': TURN, 'id': 'M1',
        'immutable_ref': parent_ref, 'outcome_ref': parent_outcome}
    assert child_archive['request']['turn_id'] == child
    assert len(env.records.list('v2_external_agent_deliveries')) == 2
    assert env.model.calls == 0


def test_mcp_child_cannot_bind_a_json_source_recreated_after_parent_validation(env):
    from backend.memory_app.original_sources import source_store
    from backend.memory_app.v2.external_context import ARCHIVE
    from backend.memory_app.v2.external_agent_guard import ExternalAgentGuard
    from backend.memory_app.transaction_records import TransactionRecords

    _document_id, item = document(env)
    row = env.records.read('workspace_items', item['id'])
    store = source_store(env.records)
    source = store.read('sources', row.payload['source_id'])
    selection = {'type': 'original_source', 'id': source['id'], 'revision': 1,
                 'project_id': 'alpha', 'layer': 'L0', 'windows': []}
    api, runtime, runner, frozen = prepare(env, selections=[selection])
    api.execute(TURN, runtime=runtime, runner=runner)
    child = 'turn-' + 'b' * 32
    observed = []

    def after_actual_parent_validation(frame, event, _argument):
        if (event == 'return' and frame.f_code is ExternalAgentGuard.validate.__code__
                and frame.f_locals.get('turn_id') == TURN
                and isinstance(frame.f_locals['self'].records, TransactionRecords)
                and not observed):
            # A real Python return observer leaves the Guard method untouched.
            # Its actual read succeeds on A, then real JSON storage recreates B.
            observed.append(True)
            store.delete('sources', source['id'])
            store.write('sources', source['id'], source, expected_revision=0)

    previous = sys.getprofile()
    try:
        sys.setprofile(after_actual_parent_validation)
        with pytest.raises(ValueError):
            api.prepare(child, frozen['capability_request']['arguments'], [selection],
                session_id='session-child', operation_id='op-child', idempotency_key=child,
                created_at=frozen['created_at'], origin={'turn_id': TURN, 'id': 'M1'})
    finally:
        sys.setprofile(previous)
    assert observed == [True]
    assert api.turns.get_immutable_payload(child, ARCHIVE) is None
    assert env.records.read('v2_external_agent_bindings', child) is None
    assert len(env.records.list('v2_external_agent_deliveries')) == 1
    assert len(env.records.list('v2_external_agent_reservations')) == 1


def test_mcp_explicit_null_window_cannot_bypass_the_declared_schema(env):
    delivered(env)
    response = call(env, 'read', {'id': {'turn_id': TURN, 'id': 'M1'}, 'window': None})
    assert response.status_code == 400
    assert response.json() == {'detail': 'external_agent_window_invalid'}
    assert len(env.records.list('v2_external_agent_deliveries')) == 1


def test_mcp_kernel_wait_does_not_block_the_asgi_event_loop(env):
    api, _frozen, _result = delivered(env)
    entered = threading.Event()

    @env.http.app.get('/mcp-test-probe')
    async def probe():
        return {'ready': True}

    def observe_actual_accept(frame, event, _argument):
        if (event == 'call' and frame.f_code.co_name == 'accept_turn'
                and frame.f_locals.get('self') is api.runtime
                and frame.f_locals.get('request', {}).get('turn_id') != TURN):
            entered.set()

    # Hold the real Kernel database writer, without replacing Runtime or runner.
    lock = sqlite3.connect(env.root / '.rebuild-data' / 'ai-turns.sqlite3', isolation_level=None)
    lock.execute('BEGIN IMMEDIATE')
    previous_thread, previous_main = threading.getprofile(), sys.getprofile()
    with ThreadPoolExecutor(max_workers=2) as calls:
        try:
            threading.setprofile_all_threads(observe_actual_accept)
            read_future = calls.submit(call, env, 'read', {'id': {'turn_id': TURN, 'id': 'M1'}})
            assert entered.wait(10), 'The actual child Kernel acceptance did not start'
            probe_future = calls.submit(env.http.get, '/mcp-test-probe')
            assert probe_future.result(timeout=2).json() == {'ready': True}
        finally:
            lock.rollback()
            lock.close()
            threading.setprofile_all_threads(previous_thread)
            sys.setprofile(previous_main)
        assert read_future.result(timeout=20).status_code == 200
