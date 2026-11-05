from copy import deepcopy
import json
import sqlite3

import pytest

from backend.memory_app.v2.external_events import ExternalEventParser
from backend.memory_app.v2.external_progress import (ExternalProgressWriteError, external_usage,
    normalized_event, progress_frame, projection_storage, put_projection, validate_progress)
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork


def snapshot(events=(), *, source='codex', reported=None, observed=None, truncated=0, terminal=None):
    return {'version': 'external-progress@1', 'sequence': len(events) + truncated, 'total': None,
        'truncated': truncated, 'events': [{'sequence': index, **event}
            for index, event in enumerate(events, truncated + 1)],
        'usage': external_usage(source, reported, observed), 'terminal': terminal}


@pytest.mark.parametrize('source', ['codex', 'claude-code', 'deepseek-harness'])
def test_reported_zero_partial_and_missing_counters_keep_distinct_meanings(source):
    complete = external_usage(source, {'input_tokens': 0, 'output_tokens': 0})
    assert complete == {'source': source, 'status': 'reported',
        'tokens': {'input_tokens': 0, 'output_tokens': 0}, 'amount': None}
    partial = external_usage(source, observed={'input_tokens': 12})
    assert partial['status'] == 'partial' and partial['tokens'] == {'input_tokens': 12}
    assert external_usage(source)['status'] == 'unknown'
    assert external_usage(source)['tokens'] == {}
    assert external_usage(source, {'input_tokens': 1})['status'] == 'partial'


@pytest.mark.parametrize('executor,kind', [('claude-code', 'result'), ('codex', 'turn.failed')])
def test_terminal_protocol_partial_is_observed_without_inventing_other_counters(executor, kind):
    parser = ExternalEventParser(executor)
    frame = {'type': kind, 'usage': {'input_tokens': 12}}
    if executor == 'claude-code':
        frame.update(subtype='success', is_error=True)
    result = parser.feed_line(json.dumps(frame))
    assert result[-1] == {'kind': 'finished', 'status': 'failed'}
    assert parser.usage is None and parser.observed_usage == {'input_tokens': 12}
    copy = parser.observed_usage
    copy['input_tokens'] = 99
    assert parser.observed_usage == {'input_tokens': 12}


@pytest.mark.parametrize('value', [None, -1, True, '12', 2 ** 54])
def test_nonrepresentable_counter_is_unknown_and_never_zero(value):
    usage = external_usage('codex', observed={'input_tokens': value, 'output_tokens': 3})
    assert usage['tokens'] == {'output_tokens': 3} and usage['status'] == 'partial'


@pytest.mark.parametrize('event', [{'kind': 'tool', 'name': 'file_read', 'arguments': 'private'},
    {'kind': 'tool', 'name': '/private/path'}, {'kind': 'step', 'stage': 'private exception'},
    {'kind': 'finished', 'status': 'success'}, {'kind': 'started', 'environment': {}}])
def test_raw_arguments_paths_exceptions_and_unknown_states_cannot_enter_progress(event):
    with pytest.raises(ValueError):
        normalized_event(event)
    assert normalized_event({'kind': 'message', 'text': 'not a progress payload'}) is None


def test_unknown_total_and_retained_observation_sequence_are_strict_and_detached():
    value = snapshot([{'kind': 'tool', 'name': 'file_write'}], truncated=200)
    result = validate_progress(value, executor='codex')
    assert result['total'] is None and result['events'][0]['sequence'] == 201
    result['events'][0]['name'] = 'file_read'
    assert value['events'][0]['name'] == 'file_write'
    for changes in ({'total': 3}, {'sequence': 202}, {'truncated': 199}, {'terminal': 'unknown'}):
        with pytest.raises(ValueError):
            validate_progress(value | changes)


def test_frame_binds_product_project_and_executor_without_raw_fields():
    value = snapshot([{'kind': 'step', 'stage': 'turn'}])
    event = {'sequence': 2, 'kind': 'task_progress', 'turn_id': 'product-1', 'project_id': 'alpha',
        'kernel_turn_id': 'turn-' + 'a' * 32, 'executor': 'codex', 'progress': value}
    projected = progress_frame(event, turn_id='product-1', project='alpha', executor='codex')
    assert projected['progress'] == value and 'kind' not in projected
    for changes in ({'turn_id': 'other'}, {'project_id': 'other'}, {'executor': 'claude-code'}, {'raw': 'private'}):
        with pytest.raises(ValueError):
            progress_frame(event | changes, turn_id='product-1', project='alpha', executor='codex')
    corrupt = deepcopy(event)
    corrupt['progress']['usage']['status'] = 'reported'
    with pytest.raises(ValueError):
        progress_frame(corrupt, turn_id='product-1', project='alpha', executor='codex')


def test_real_readonly_database_write_fault_has_its_own_projection_error(tmp_path):
    path = tmp_path / 'progress.sqlite3'
    store = SQLiteStructuredRecordStore(path)
    with store.begin() as tx:
        tx.commit()
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('BEGIN')
    event = normalized_event({'kind': 'tool', 'name': 'file_read'})
    with SQLiteStructuredRecordUnitOfWork(connection) as tx:
        with pytest.raises(ExternalProgressWriteError, match='external_progress_storage_unavailable'):
            put_projection(tx, 'v2_external_progress', 'read-only-case', snapshot([event]), expected_revision=0)
    assert store.read('v2_external_progress', 'read-only-case') is None


def test_real_revision_conflict_is_not_downgraded_to_a_projection_outage(tmp_path):
    store = SQLiteStructuredRecordStore(tmp_path / 'progress.sqlite3')
    with store.begin() as tx:
        with pytest.raises(SQLiteUnitOfWorkConflict):
            put_projection(tx, 'v2_external_progress', 'conflict-case', snapshot(), expected_revision=9)


def test_protocol_validation_failure_is_not_a_projection_storage_failure():
    with pytest.raises(ValueError) as rejected:
        normalized_event({'kind': 'tool', 'name': 'file_read', 'private': 'excluded'})
    assert not isinstance(rejected.value, ExternalProgressWriteError)


@pytest.mark.parametrize('operation', ['read', 'begin'])
def test_real_unavailable_database_read_and_begin_are_projection_failures(tmp_path, operation):
    path = tmp_path / 'blocked'
    path.mkdir()
    @projection_storage
    def access():
        connection = sqlite3.connect(path)
        if operation == 'begin':
            connection.execute('BEGIN IMMEDIATE')
        else:
            connection.execute('SELECT 1')
    with pytest.raises(ExternalProgressWriteError, match='external_progress_storage_unavailable'):
        access()


def test_projection_storage_keeps_real_binding_validation_error():
    @projection_storage
    def validate():
        normalized_event({'kind': 'step', 'stage': 'untrusted'})
    with pytest.raises(ValueError) as rejected:
        validate()
    assert not isinstance(rejected.value, ExternalProgressWriteError)
