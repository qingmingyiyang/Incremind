from __future__ import annotations

from datetime import timedelta
from threading import current_thread
from time import sleep, monotonic

import pytest

from backend.api.ai_turn_runner import AITurnRunner, AITurnRunnerCapacityError
from core.ai_kernel import RunLeaseRevoked, TurnReceipt
from tests.backend.unit.api.test_ai_turn_runner import _Runtime, _request, _action


def _child(number=1):
    request = _request()
    request.update(turn_id=f'turn-{number:032x}', operation_id=f'op-child-test-{number:08d}', idempotency_key=f'child-test-{number:08d}')
    request['agent_binding'] = {
        'schema_version': '1.0.0', 'kind': 'internal_agent_run_v1',
        'run_id': f'child-run-{number}', 'role': 'subagent', 'profile_id': 'subagent.explorer',
        'profile_revision': 1, 'model_tier': 'fast', 'depth': 1, 'cancel_epoch': 0,
        'budget_snapshot_ref': 'crp://session/test/budget', 'parent_run_id': 'parent-run',
        'link_id': f'child-link-{number}', 'reservation_id': f'reservation-{number}',
        'spawn_operation_id': f'op-child-test-{number:08d}',
    }
    return request


def test_pool_capacity_is_independent_and_shutdown_stops_both_pools():
    runtime = _Runtime()
    observed = []
    runner = AITurnRunner(runtime, max_workers=1, max_pending=1,
        max_child_workers=1, max_child_pending=1, terminal_observer=observed.append)
    try:
        runner.accept_and_submit(_request())
        runner.accept_and_submit(_child())
        assert len(runner.active_turn_ids) == 2
        with pytest.raises(AITurnRunnerCapacityError):
            runner.accept_and_submit(_child(2))
        runtime.gate.set()
        runner.shutdown(timeout_seconds=1)
        assert runner.active_turn_ids == ()
        assert runtime.leases == {}
        assert len(observed) == 2
        assert not runner._heartbeat_thread.is_alive()
        assert runner._executor._shutdown and runner._child_executor._shutdown
        for pool in (runner._executor, runner._child_executor):
            for thread in pool._threads:
                thread.join(1)
                assert not thread.is_alive()
    finally:
        runtime.gate.set()
        runner.shutdown()


@pytest.mark.parametrize('workers,pending', [(0, 16), (True, 16), (2, 1), (1, True)])
def test_child_pool_configuration_is_validated(workers, pending):
    with pytest.raises(ValueError, match='child'):
        AITurnRunner(_Runtime(), max_child_workers=workers, max_child_pending=pending)


def test_child_heartbeat_renews_then_releases_exact_lease():
    runtime = _Runtime()
    runner = AITurnRunner(runtime, lease_ttl=timedelta(seconds=.09), heartbeat_interval_seconds=.02)
    child = _child()
    try:
        runner.accept_and_submit(child)
        assert runtime.started.wait(1)
        deadline = monotonic() + 1
        while runtime.renewals < 2 and monotonic() < deadline:
            sleep(.01)
        assert runtime.renewals >= 2
        runtime.gate.set()
        assert runner.wait_for_terminal(child['turn_id'], timeout_seconds=1).status == 'completed'
    finally:
        runtime.gate.set()
        runner.shutdown()
    assert not runtime.leases


def test_child_revoked_lease_is_retained_without_fallback():
    class Runtime(_Runtime):
        def run_accepted_turn(self, turn_id, lease):
            self.started.set()
            raise RunLeaseRevoked()
        def fail_accepted_turn(self, *args):
            raise AssertionError('revoked child must not converge through a second lease')
    runtime = Runtime()
    runner = AITurnRunner(runtime)
    child = _child()
    runner.accept_and_submit(child)
    assert runtime.started.wait(1)
    runner.shutdown()
    assert child['turn_id'] in runtime.leases


def test_child_approval_action_retains_child_pool_when_main_is_saturated():
    class Runtime(_Runtime):
        def accept_turn(self, payload):
            receipt = super().accept_turn(payload)
            if payload.get('agent_binding'):
                receipt = TurnReceipt(receipt.turn_id, receipt.session_id, receipt.operation_id, 'waiting_approval', 1, False)
                self.receipts[receipt.turn_id] = receipt
            return receipt
        def apply_action(self, action, lease):
            assert current_thread().name.startswith('ai-turn-child')
            return TurnReceipt(action['turn_id'], 'session-child', 'op-child', 'completed', 2, False)
    runtime = Runtime()
    runner = AITurnRunner(runtime, max_workers=1, max_pending=1, max_child_workers=1)
    try:
        runner.accept_and_submit(_request())
        assert runtime.started.wait(1)
        child = _child()
        runner.accept_and_submit(child)
        action = {**_action(), 'turn_id': child['turn_id']}
        assert runner.apply_action_and_wait(action).status == 'completed'
        assert child['turn_id'] not in runtime.leases
    finally:
        runtime.gate.set()
        runner.shutdown()


def test_shutdown_cancels_queued_child_and_keeps_running_child_heartbeat():
    runtime = _Runtime()
    runner = AITurnRunner(runtime, max_child_workers=1, max_child_pending=2,
        lease_ttl=timedelta(seconds=.09), heartbeat_interval_seconds=.02)
    try:
        runner.accept_and_submit(_child(1))
        assert runtime.started.wait(1)
        runner.accept_and_submit(_child(2))
        assert _child(2)['turn_id'] in runtime.leases
        runner.shutdown(timeout_seconds=0)
        deadline = monotonic() + 1
        while _child(2)['turn_id'] in runtime.leases and monotonic() < deadline:
            sleep(.01)
        assert _child(2)['turn_id'] not in runtime.leases
        renewals = runtime.renewals
        sleep(.05)
        assert runtime.renewals > renewals
        assert _child(1)['turn_id'] in runtime.leases
    finally:
        runtime.gate.set()
        deadline = monotonic() + 1
        while runner.active_turn_ids and monotonic() < deadline:
            sleep(.01)
        assert not runner.active_turn_ids
        assert not runtime.leases


def test_parent_cancel_survives_child_lease_released_during_forwarding():
    class Runtime(_Runtime):
        def request_background_cancel(self, turn_id, lease=None, *, reason=None):
            if turn_id == _child()['turn_id']:
                raise RunLeaseRevoked()
            return True
    runtime = Runtime()
    runner = AITurnRunner(runtime)
    parent = _request()
    parent['agent_binding'] = {
        'schema_version': '1.0.0', 'kind': 'internal_agent_run_v1', 'run_id': 'parent-run',
        'role': 'main', 'profile_id': 'main.orchestrator', 'profile_revision': 1,
        'model_tier': 'deep', 'depth': 0, 'cancel_epoch': 0,
        'budget_snapshot_ref': 'crp://session/test/parent-budget',
    }
    try:
        runner.accept_and_submit(parent)
        runner.accept_and_submit(_child())
        assert runtime.started.wait(1)
        assert runner.request_turn_cancel(parent['turn_id'])
    finally:
        runtime.gate.set()
        runner.shutdown()
