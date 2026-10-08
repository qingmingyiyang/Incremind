"""Real runner pools retain request SQL observation through lease cleanup."""
from datetime import timedelta
from threading import Event, Lock, Thread
from time import monotonic, sleep

import pytest

from backend.api.ai_turn_runner import AITurnRunner
from backend.memory_app.v2.turn_timings import turn_timing
from core.storage_provider import SQLiteStructuredRecordStore
from core.storage_provider.observability import current_observation
from core.ai_kernel import TurnReceipt
from tests.backend.unit.api.test_ai_turn_runner import _Runtime, _request, _action
from tests.backend.unit.api.test_ai_turn_runner_child_pool import _child


class CountingRuntime(_Runtime):
    def __init__(self, records):
        super().__init__()
        self.records = records
        self.observed = []
        self.mutex = Lock()
        self.heartbeat_seen = Event()

    def record(self, phase, turn):
        observer = current_observation()
        self.records.list('synthetic_runner_reads')
        with self.mutex:
            self.observed.append((phase, turn, observer.turn_id if observer else None))

    def run_accepted_turn(self, turn, lease=None):
        self.record('worker', turn)
        return super().run_accepted_turn(turn, lease)

    def apply_action(self, action, lease=None):
        turn = action['turn_id']
        self.record('action', turn)
        self.started.set()
        assert self.gate.wait(3)
        receipt = TurnReceipt(turn, 'session-test', 'op-test', 'completed', 6, False)
        self.receipts[turn] = receipt
        return receipt

    def events_after(self, turn, sequence=0):
        return ()

    def renew_run_lease(self, token, **kwargs):
        self.record('heartbeat', token.turn_id)
        self.heartbeat_seen.set()
        return super().renew_run_lease(token, **kwargs)

    def release_strict_run_lease(self, token):
        self.record('cleanup', token.turn_id)
        return super().release_strict_run_lease(token)


def await_row(records, key):
    deadline = monotonic() + 3
    while monotonic() < deadline:
        row = records.read('v2_turn_timings', key)
        if row:
            return row.payload
        sleep(.005)
    raise AssertionError('runner observation never finished')


@pytest.mark.parametrize('mode', ['main', 'child', 'async_action', 'sync_action'])
def test_runner_counts_worker_heartbeat_and_cleanup(tmp_path, mode):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.db')
    runtime = CountingRuntime(records)
    runner = AITurnRunner(runtime, lease_ttl=timedelta(seconds=.3), heartbeat_interval_seconds=.02)
    errors = []
    caller = None
    try:
        if mode in {'main', 'child'}:
            with turn_timing(records, 'task', turn_id='product-turn'):
                runner.accept_and_submit(_child() if mode == 'child' else _request())
        else:
            def call():
                try:
                    with turn_timing(records, 'task', turn_id='product-turn'):
                        method = runner.accept_action_and_submit if mode == 'async_action' else runner.apply_action_and_wait
                        method(_action())
                except BaseException as error:
                    errors.append(error)
            caller = Thread(target=call)
            caller.start()
        assert runtime.started.wait(2)
        assert runtime.heartbeat_seen.wait(2)
        assert records.read('v2_turn_timings', 'product-turn') is None
        runtime.gate.set()
        if caller:
            caller.join(3)
            assert not caller.is_alive()
        assert not errors
        payload = await_row(records, 'product-turn')
        phases = {phase for phase, _, _ in runtime.observed}
        assert {'heartbeat', 'cleanup', 'action' if 'action' in mode else 'worker'} <= phases
        assert all(identity == 'product-turn' for _, _, identity in runtime.observed)
        assert payload['connection_count'] == len(runtime.observed)
        assert payload['statement_count'] >= payload['connection_count']
    finally:
        runtime.gate.set()
        if caller:
            caller.join(3)
        runner.shutdown()


def test_concurrent_main_and_child_observations_stay_separate(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.db')
    runtime = CountingRuntime(records)
    runner = AITurnRunner(runtime, heartbeat_interval_seconds=.02)
    requests = [(_request(), 'product-main'), (_child(), 'product-child')]
    try:
        for request, identity in requests:
            with turn_timing(records, 'task', turn_id=identity):
                runner.accept_and_submit(request)
        assert runtime.heartbeat_seen.wait(2)
        runtime.gate.set()
        for request, identity in requests:
            payload = await_row(records, identity)
            events = [event for event in runtime.observed if event[1] == request['turn_id']]
            assert events and all(event[2] == identity for event in events)
            assert payload['connection_count'] == len(events)
    finally:
        runtime.gate.set()
        runner.shutdown()
