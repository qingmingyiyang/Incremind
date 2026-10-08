from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import sqlite3
import traceback
from pathlib import Path
from threading import Event
import asyncio
from time import monotonic, sleep

import pytest

from core.storage_provider.connection_scope import connection_scope
from core.storage_provider.connection_scope import create_scoped_task
from core.storage_provider.connection_scope import _SCOPE
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.memory_app.source_egress import recognition_service
from backend.memory_app.storage_authority import resolve_recognition_document_store
from core.document_engine import SQLiteDocumentRepository
from tests.memory_app.v2.test_workbench_ask import Model, assemble, publish, ask


@pytest.fixture
def env(tmp_path):
    """在请求计数前用原 resolver 完成空 vault 的真实共享库装配。"""
    records, document_namespace = resolve_recognition_document_store(tmp_path)
    documents = SQLiteDocumentRepository(records, namespace_id=document_namespace)
    service, model = recognition_service(records), Model()
    app, domains = assemble(tmp_path, records, documents, service, model)
    with TestClient(app) as http:
        yield SimpleNamespace(root=tmp_path, records=records, documents=documents, service=service,
                              model=model, http=http, domains=domains)


def wait_for_units(units):
    deadline = monotonic() + 5
    while any(not unit.ended for unit in units) and monotonic() < deadline:
        sleep(.005)
    assert units and all(unit.ended for unit in units), 'request resources did not finish'


def test_returned_connection_can_move_to_another_request_thread(tmp_path):
    store = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with connection_scope():
        first = store._connect()
        raw = first.connection
        first.close()
        def read():
            lease = store._connect()
            try:
                assert lease.connection is raw
                assert lease.execute('PRAGMA synchronous').fetchone()[0] == 2
            finally:
                lease.close()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(copy_context().run, read).result()
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute('SELECT 1')


def test_initialized_store_does_not_resolve_filesystem_on_each_read(tmp_path, monkeypatch):
    store = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    original, calls = Path.resolve, []
    def resolve(path, *args, **kwargs):
        calls.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolve)
    with connection_scope():
        store.list('checks')
        store.list('checks')
    assert calls == []


def test_connection_context_returns_lease_on_success_and_error(tmp_path):
    store = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with connection_scope():
        with store._connect() as first:
            raw = first.connection
        assert first.closed
        with pytest.raises(RuntimeError):
            with store._connect() as second:
                assert second.connection is raw
                raise RuntimeError('synthetic')
        assert second.closed
        assert not raw.in_transaction


def test_kernel_and_effect_profiles_share_only_idle_connection(tmp_path):
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore
    with connection_scope():
        kernel = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
        with kernel._connect() as first:
            raw = first.connection
            assert first.row_factory is None
            assert first.isolation_level == ''
        with kernel.effect_runner.log._connect() as effects:
            assert effects.connection is raw
            assert effects.row_factory is sqlite3.Row
            assert effects.isolation_level is None
            assert effects.execute('PRAGMA synchronous').fetchone()[0] == 2
        with kernel._connect() as again:
            assert again.connection is raw
            assert again.row_factory is None and again.isolation_level == ''


def test_different_store_schemas_initialize_when_they_share_a_file(tmp_path):
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore
    path = tmp_path / 'shared.sqlite3'
    with connection_scope():
        SQLiteAITurnStore(path)
        records = SQLiteStructuredRecordStore(path)
        with records.begin() as tx:
            tx.put('checks', 'ready', {'value': 1}, expected_revision=0)
            tx.commit()
        assert records.read('checks', 'ready').payload['value'] == 1


def test_child_cancelled_before_start_releases_captured_owner(tmp_path):
    store = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    async def run():
        async def never_started():
            raise AssertionError('cancelled child ran')
        with connection_scope() as unit:
            store.list('checks')
            task = create_scoped_task(never_started())
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert unit.ended and not unit.idle and not any(unit.active.values())
    asyncio.run(run())


def test_cross_thread_reader_does_not_wait_for_its_callers_write(tmp_path):
    store = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with connection_scope(), ThreadPoolExecutor(max_workers=1) as pool:
        with store.begin() as tx:
            tx.put('checks', 'pending', {}, expected_revision=0)
            # Parent waits here with an uncommitted write. Waiting indefinitely
            # for its lease would deadlock; the reader must stay independent.
            result = pool.submit(copy_context().run, store.read, 'checks', 'pending')
            assert result.result(timeout=2) is None
            tx.commit()
        assert store.read('checks', 'pending').revision == 1


def test_parallel_readers_only_share_returned_connections(tmp_path):
    store = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    entered, release = Event(), Event()
    def hold():
        with store._connect() as lease:
            raw = lease.connection
            entered.set()
            assert release.wait(2)
            return raw
    with connection_scope(), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(copy_context().run, hold)
        assert entered.wait(2)
        try:
            second = store._connect()
            raw = second.connection
            second.close()
        finally:
            release.set()
        assert future.result(timeout=2) is not raw


def test_detached_child_owns_scope_after_http_scope_exits(tmp_path):
    store = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    async def run():
        gate = asyncio.Event()
        with connection_scope() as scope:
            first = store._connect()
            raw = first.connection
            first.close()
            async def child():
                await gate.wait()
                with store._connect() as lease:
                    assert lease.connection is raw
            task = create_scoped_task(child())
        assert not scope.ended
        gate.set()
        await task
        await asyncio.sleep(0)
        assert scope.ended
        with pytest.raises(sqlite3.ProgrammingError):
            raw.execute('SELECT 1')
    asyncio.run(run())


@pytest.mark.parametrize('finish_kind', ['worker', 'cleanup'])
def test_last_timing_holder_persists_inside_its_unit(tmp_path, monkeypatch, finish_kind):
    from backend.api.ai_turn_runner import AITurnRunner
    from backend.memory_app.v2.turn_timings import TurnObservation
    from threading import Lock
    store = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    opened, original = [], sqlite3.connect
    def connect(*args, **kwargs):
        opened.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    with connection_scope() as unit:
        store.list('checks')
        unit.retain()
    observation = TurnObservation(store, 'ask', 'last-holder')
    runner = object.__new__(AITurnRunner)
    if finish_kind == 'worker':
        runner._invoke_observed(observation, store.list, 'checks', unit=unit)
    else:
        runner._lock = Lock()
        runner._lease_units = {'lease': unit}
        runner._lease_observations = {'lease': observation}
        runner._lease_contexts = {'lease': copy_context()}
        runner._forget_observation('lease')
    assert observation.finished and unit.ended
    assert len(opened) == 1


def test_readonly_projection_waits_for_a_short_live_lease(tmp_path, monkeypatch):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore
    store = SQLiteAITurnStore(tmp_path / 'ai-turns.sqlite3')
    entered, started = Event(), Event()
    opened, original = [], sqlite3.connect
    def connect(*args, **kwargs):
        opened.append(args[0])
        return original(*args, **kwargs)
    def hold():
        with store._connect():
            entered.set()
            assert started.wait(2)
            sleep(.01)
    with connection_scope(), ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(sqlite3, 'connect', connect)
        future = pool.submit(copy_context().run, hold)
        assert entered.wait(2)
        started.set()
        assert kernel_call_groups(tmp_path) == []
        future.result(timeout=2)
    assert len(opened) == 1


@pytest.mark.parametrize('stream', [False, True])
def test_request_connection_budget_with_multiple_materials(env, monkeypatch, stream):
    publish(env, text='alpha beta gamma first')
    publish(env, text='alpha beta gamma second')
    publish(env, text='alpha beta gamma third')
    original, opened, units = sqlite3.connect, [], set()
    def connect(database, *args, **kwargs):
        opened.append(Path(str(database)).name)
        if _SCOPE.get() is not None:
            units.add(_SCOPE.get())
        return original(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    response = env.http.post('/api/v2/workbench/turns',
        headers={'Accept': 'text/event-stream' if stream else 'application/json',
                 'Idempotency-Key': 'multi-materials'},
        json={'project_id': 'alpha', 'text': 'alpha beta gamma?'})
    assert response.status_code == 200, response.text
    assert env.model.calls == 1
    wait_for_units(units)
    assert len(opened) <= 3, opened


def test_real_answer_opens_at_most_three_connections(env, monkeypatch):
    publish(env)
    # Count all SQLite opens, not only instrumented stores. Preparing the
    # synthetic fixture is outside the request under test.
    original = sqlite3.connect
    opened = []
    units = set()
    def connect(database, *args, **kwargs):
        opened.append((Path(str(database)).name, [(Path(frame.filename).name, frame.lineno, frame.name)
                       for frame in traceback.extract_stack(limit=6)[:-1]]))
        if _SCOPE.get() is not None:
            units.add(_SCOPE.get())
        return original(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    response = ask(env)
    assert response.status_code == 200, response.text
    assert env.model.calls == 1
    wait_for_units(units)
    assert len(opened) <= 3, '\n'.join(map(str, opened))
