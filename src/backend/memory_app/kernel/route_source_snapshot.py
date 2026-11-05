import json
import sqlite3
from contextlib import contextmanager, nullcontext

from backend.recognition import RecognitionConflict
from backend.recognition.external_turn_facts import source_store
from core.ai_kernel.contracts import (
    validate_turn_request, validate_event_transition, validate_model_wire_attempt_dispatch,
    validate_model_wire_attempt_receipt,
)
from core.ai_kernel.product_admission import MANIFEST_KIND
from core.ai_kernel.product_snapshot import admission_base_refs, restore_admission_snapshot, read_model_attempt_bindings
from core.ai_kernel.turn_receipt_projection import receipt_from_events
from core.storage_provider.observability import observe_connection


@contextmanager
def native_source_connection(records, *, reader=None):
    from ..route_correction_scopes import _owner
    _owner(records, reader)
    root = source_store(records).root.parent
    databases = [path for path in (root / '.rebuild-data/ai-turns.sqlite3', root / 'ai-turns.sqlite3') if path.is_file()]
    if len(databases) != 1:
        raise RecognitionConflict('route_correction_native_unverifiable')
    connection = None
    try:
        connection = observe_connection(sqlite3.connect(databases[0].resolve().as_uri() + '?mode=ro', uri=True, timeout=5))
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        yield connection
    except RecognitionConflict:
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
        raise RecognitionConflict('route_correction_native_unverifiable') from error
    finally:
        if connection is not None:
            connection.close()


def native_source_snapshot(records, turn_id, *, reader=None):
    with native_source_connection(records, reader=reader) as connection:
        raw = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?', (turn_id,)).fetchone()
        immutable = {kind: (reference, json.loads(body)) for kind, reference, body in connection.execute(
            'SELECT kind,payload_ref,payload_json FROM ai_turn_immutable_payloads WHERE turn_id=?', (turn_id,))}
        if raw is None:
            # 缺少请求只在现存数据库且没有任何孤立执行事实时证明未接纳。
            if immutable or any(connection.execute('SELECT 1 FROM ' + table + ' WHERE turn_id=? LIMIT 1',
                    (turn_id,)).fetchone() for table in ('ai_turn_events', 'ai_turn_run_leases',
                        'ai_model_attempt_reservations', 'ai_turn_payloads', 'ai_turn_pending', 'effect')):
                raise RecognitionConflict('route_correction_native_unverifiable')
            return {'request': None, 'immutable': {}, 'admission': None}
        request = validate_turn_request(json.loads(raw[0]))
        if request['turn_id'] != turn_id:
            raise RecognitionConflict('route_correction_native_unverifiable')
        admission_base_refs(request, immutable)
        bundle = restore_admission_snapshot(request, immutable) if MANIFEST_KIND in immutable else None
        return {'request': request, 'immutable': immutable, 'admission': bundle}


def require_closed_answer(records, row, *, reader=None):
    from .receipt_projection import frozen_public_turn_requests
    try:
        entries = frozen_public_turn_requests(records, source_store(records).root.parent, row.object_id, reader=reader)
    except (ValueError, sqlite3.Error) as error:
        raise RecognitionConflict('route_correction_native_unverifiable') from error
    if len(entries) != 1 or entries[0]['public_revision'] != row.revision:
        raise RecognitionConflict('route_correction_native_unverifiable')
    expected = entries[0]['native_request']
    return closed_native_answer(records, expected, reader=reader)


def closed_native_answer(records, expected, *, reader=None):
    with native_source_connection(records, reader=reader) as connection:
        return _closed_answer_fact(records, expected, connection, reader=reader)


def _closed_answer_fact(records, expected, connection, *, reader=None):
    identity = expected['turn_id']
    if expected['desired_outcome'] != 'project.answer':
        raise RecognitionConflict('route_correction_native_unverifiable')
    with nullcontext(connection):
        if not connection.in_transaction:
            raise RecognitionConflict('route_correction_native_unverifiable')
        raw = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?', (identity,)).fetchone()
        if raw is None or validate_turn_request(json.loads(raw[0])) != expected:
            raise RecognitionConflict('route_correction_native_unverifiable')
        events, prior = [], None
        for sequence, body in connection.execute('SELECT sequence,event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence',
                (identity,)):
            event = validate_event_transition(prior, json.loads(body))
            if (event['turn_id'] != identity or event['session_id'] != expected['session_id']
                    or event['sequence'] != sequence):
                raise RecognitionConflict('route_correction_native_unverifiable')
            events.append(event)
            prior = event
        terminal = {'turn.completed': 'completed', 'turn.failed': 'failed', 'turn.cancelled': 'cancelled'}
        if (not events or events[-1]['type'] not in terminal
                or events[-1]['data']['status'] != terminal[events[-1]['type']]
                or receipt_from_events(events, turn_id=identity, request=expected).status != terminal[events[-1]['type']]):
            raise RecognitionConflict('route_correction_source_unfinished')
        # 终态事件须同时具有已关闭的运行归属和实际模型尝试。
        if any(connection.execute('SELECT 1 FROM ' + table + ' WHERE turn_id=? LIMIT 1', (identity,)).fetchone()
                for table in ('ai_turn_run_leases', 'ai_turn_pending')):
            raise RecognitionConflict('route_correction_source_unfinished')
        if (connection.execute("SELECT 1 FROM ai_turn_recovery_queue WHERE turn_id=? AND status IN ('pending','running')",
                (identity,)).fetchone()
                or connection.execute("SELECT 1 FROM ai_expert_job_waits WHERE turn_id=? AND status!='terminal_observed'",
                    (identity,)).fetchone()):
            raise RecognitionConflict('route_correction_source_unfinished')
        attempts = read_model_attempt_bindings(connection, identity)
        unsettled = tuple(connection.execute("SELECT operation_id,state FROM effect WHERE turn_id=? "
            "AND state NOT IN ('SETTLED_OK','SETTLED_ERR','COMPENSATED','ABANDONED')", (identity,)))
        discarded = set()
        if unsettled:
            from .answer_source_proof import discarded_auxiliary_attempts
            discarded = discarded_auxiliary_attempts(reader if reader is not None else records,
                expected, connection, events, attempts)
        if any(state != 'UNKNOWN' or operation not in discarded for operation, state in unsettled):
            raise RecognitionConflict('route_correction_source_unfinished')
        for attempt in attempts:
            effect = connection.execute('SELECT turn_id,state FROM effect WHERE operation_id=?', (attempt['attempt_id'],)).fetchone()
            if (attempt['status'] != 'terminal' or attempt['dispatch'] is None or attempt['terminal'] is None
                    or (attempt['effect_state'] not in {'SETTLED_OK', 'SETTLED_ERR'}
                        and attempt['attempt_id'] not in discarded)
                    or effect is None or effect[0] != identity or effect[1] != attempt['effect_state']):
                raise RecognitionConflict('route_correction_source_unfinished')
            dispatch = validate_model_wire_attempt_dispatch(attempt['dispatch'])
            closed = validate_model_wire_attempt_receipt(attempt['terminal'])
            if (any(dispatch[key] != closed[key] for key in
                    ('turn_id', 'model_request_id', 'attempt_id', 'attempt_number', 'routing_snapshot_revision', 'provider_id', 'model_id'))
                    or any(dispatch[key] != (identity if key == 'turn_id' else attempt[key])
                        for key in ('turn_id', 'model_request_id', 'attempt_id', 'attempt_number'))
                    or not any(event['type'] == 'model.attempt.terminal'
                        and event['data'].get('receipt_ref') == attempt['terminal_ref'] for event in events)):
                raise RecognitionConflict('route_correction_native_unverifiable')
        return terminal[events[-1]['type']]
