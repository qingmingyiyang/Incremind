from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import sqlite3

import pytest

from core.storage_provider.observability import current_observation, observe_connection, observation_scope, stage
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore
from backend.memory_app.v2.turn_timings import turn_timing


def test_default_enabled_persists_counts_and_ten_stages(tmp_path, monkeypatch):
    monkeypatch.delenv('CHRIPTMAS_TURN_TIMINGS', raising=False)
    records = SQLiteStructuredRecordStore(tmp_path / 'records.db')
    with turn_timing(records, 'ask', turn_id='turn-one') as observation:
        with stage('read_records'):
            records.list('inputs')
        assert current_observation() is observation
    row = records.read('v2_turn_timings', 'turn-one')
    assert row.payload['operation'] == 'ask'
    assert row.payload['connection_count'] == 1
    assert row.payload['statement_count'] > 1
    assert row.payload['total_ms'] >= row.payload['stages_ms']['read_records'] >= 0
    assert set(row.payload['stages_ms']) == {'read_records', 'build_entries', 'keyword', 'vector', 'ladder', 'prompt_build', 'gateway_send', 'first_token', 'generation', 'persist'}
    assert current_observation() is None


def test_disabled_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv('CHRIPTMAS_TURN_TIMINGS', '0')
    records = SQLiteStructuredRecordStore(tmp_path / 'records.db')
    with turn_timing(records, 'ask') as observation:
        assert observation is None
        records.list('inputs')
    assert records.list('v2_turn_timings') == ()


def test_failure_is_safe_and_does_not_mask_result(caplog):
    class BrokenStore:
        def begin(self):
            raise RuntimeError('private body and SQL secret')
    with turn_timing(BrokenStore(), 'ask'):
        answer = 'answer'
    assert answer == 'answer'
    assert 'timing_write_failed' in caplog.text
    assert 'private body' not in caplog.text


def test_original_exception_survives_failure():
    class BrokenStore:
        def begin(self):
            raise RuntimeError('private detail')
    with pytest.raises(ValueError, match='business failure'):
        with turn_timing(BrokenStore(), 'ask'):
            raise ValueError('business failure')


def test_thread_context_counts_and_connection_reuse(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.db')
    connection = sqlite3.connect(':memory:', check_same_thread=False)
    observe_connection(connection)
    def work():
        connection.execute('SELECT 1').fetchall()
        other = sqlite3.connect(':memory:')
        observe_connection(other)
        other.execute('SELECT 2').fetchall()
        other.close()
    try:
        with turn_timing(records, 'ask', turn_id='thread-one'):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(copy_context().run, work).result()
        row = records.read('v2_turn_timings', 'thread-one').payload
        assert row['connection_count'] == 1
        assert row['statement_count'] == 2
    finally:
        connection.close()

def test_nested_stages_exclude_child_and_track_observations(tmp_path, monkeypatch):
    from core.storage_provider import observability
    clock = iter([0, 1, 2, 4, 5, 6])
    monkeypatch.setattr(observability, 'perf_counter', lambda: next(clock))
    records = SQLiteStructuredRecordStore(tmp_path / 'records.db')
    with turn_timing(records, 'ask', turn_id='nested'):
        with stage('build_entries'):
            with stage('keyword'):
                pass
    row = records.read('v2_turn_timings', 'nested').payload
    assert row['stages_ms']['build_entries'] == 2000
    assert row['stages_ms']['keyword'] == 2000
    assert row['stage_observations']['vector'] == 0
    assert row['stage_observations']['keyword'] == 1


def test_deferred_context_finishes_once_after_exit(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.db')
    with turn_timing(records, 'remember', turn_id='deferred') as observation:
        observation.defer_finish()
    assert records.read('v2_turn_timings', 'deferred') is None
    observation.finish()
    observation.finish()
    assert records.read('v2_turn_timings', 'deferred').revision == 1


@pytest.mark.parametrize('point', ['start', 'snapshot', 'stage'])
def test_observer_failures_never_change_answer(tmp_path, monkeypatch, point, caplog):
    from core.storage_provider import observability
    def fail(*args, **kwargs):
        raise RuntimeError('private payload')
    records = SQLiteStructuredRecordStore(tmp_path / 'records.db')
    if point == 'start':
        monkeypatch.setattr(observability, 'perf_counter', fail)
    elif point == 'snapshot':
        monkeypatch.setattr(observability.Observation, 'snapshot', fail)
    else:
        monkeypatch.setattr(observability.Observation, 'add_duration', fail)
    with turn_timing(records, 'ask'):
        with stage('keyword'):
            answer = 42
    assert answer == 42
    assert 'private payload' not in caplog.text


def test_async_create_task_and_to_thread_propagate_context(tmp_path):
    import asyncio
    records = SQLiteStructuredRecordStore(tmp_path / 'records.db')
    # Prepare schema outside observation to keep connection expectations simple.
    records.list('inputs')
    async def run():
        async def child():
            await asyncio.to_thread(records.list, 'inputs')
        with turn_timing(records, 'ask', turn_id='async'):
            await asyncio.create_task(child())
    asyncio.run(run())
    row = records.read('v2_turn_timings', 'async').payload
    assert row['connection_count'] == 1
    assert row['statement_count'] > 0


def test_concurrent_requests_keep_counters_separate(tmp_path):
    import asyncio
    records = SQLiteStructuredRecordStore(tmp_path / 'records.db')
    records.list('inputs')
    async def run_one(turn_id, count):
        with turn_timing(records, 'ask', turn_id=turn_id):
            for _ in range(count):
                await asyncio.to_thread(records.list, 'inputs')
    async def run():
        await asyncio.gather(run_one('one', 1), run_one('two', 2))
    asyncio.run(run())
    assert records.read('v2_turn_timings', 'one').payload['connection_count'] == 1
    assert records.read('v2_turn_timings', 'two').payload['connection_count'] == 2

@pytest.mark.parametrize('order', [('root', 'execute', 'remember'), ('remember', 'execute', 'root'), ('execute', 'root', 'remember')])
def test_two_deferred_holders_wait_for_all_owners(tmp_path, order):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.db')
    with ExitStack() as contexts:
        observation = contexts.enter_context(turn_timing(records, 'remember', turn_id='holders'))
        observation.defer_finish()
        observation.defer_finish()
        for index, owner in enumerate(order):
            if owner == 'root':
                contexts.close()
            else:
                observation.finish()
            with observation_scope(None):
                row = records.read('v2_turn_timings', 'holders')
            assert (row is not None) == (index == 2)
        observation.finish()
        assert records.read('v2_turn_timings', 'holders').revision == 1


def test_cancelled_root_keeps_shielded_child_observable(tmp_path):
    import asyncio
    records = SQLiteStructuredRecordStore(tmp_path / 'records.db')
    records.list('inputs')
    async def run():
        child_started = asyncio.Event()
        release_child = asyncio.Event()
        children = []
        async def child(observation):
            try:
                child_started.set()
                await release_child.wait()
                await asyncio.to_thread(records.list, 'inputs')
            finally:
                observation.finish()
        async def root():
            with turn_timing(records, 'ask', turn_id='cancelled') as observation:
                observation.defer_finish()
                task = asyncio.create_task(child(observation))
                children.append(task)
                await asyncio.shield(task)
        parent = asyncio.create_task(root())
        await child_started.wait()
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await parent
        assert records.read('v2_turn_timings', 'cancelled') is None
        release_child.set()
        await children[0]
    asyncio.run(run())
    row = records.read('v2_turn_timings', 'cancelled')
    assert row.payload['connection_count'] == 1
    assert row.revision == 1


def test_kernel_receipt_projection_counts_readonly_connection(tmp_path):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    database = tmp_path / 'ai-turns.sqlite3'
    sqlite3.connect(database).close()
    records = SQLiteStructuredRecordStore(tmp_path / 'timings.sqlite3')
    with turn_timing(records, 'ask', turn_id='projection-empty'):
        assert kernel_call_groups(tmp_path) == []
    measured = records.read('v2_turn_timings', 'projection-empty').payload
    assert measured['connection_count'] == 1
    assert measured['statement_count'] > 0
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT name FROM sqlite_master').fetchall() == []


def test_sqlite_write_failure_logs_safe_diagnostic(tmp_path, caplog):
    connection = sqlite3.connect(tmp_path / 'readonly.db')
    connection.execute('PRAGMA query_only=ON')

    class ReadOnlyStore:
        def begin(self):
            connection.execute('CREATE TABLE private_payload(secret TEXT)')

    try:
        with turn_timing(ReadOnlyStore(), 'ask'):
            result = 'answer survives'
        assert result == 'answer survives'
        assert 'error_type=OperationalError' in caplog.text
        assert 'sqlite_code=8' in caplog.text
        assert 'private_payload' not in caplog.text
        assert 'readonly database' not in caplog.text
    finally:
        connection.close()
