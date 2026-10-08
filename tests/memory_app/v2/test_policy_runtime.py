"""Durable product execution binds its frozen policy versions on every run."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from backend.memory_app.v2.policies import ACTIVE, get, override, register, version
from core.ai_kernel import ScopedCapabilityRegistry
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from tests.rebuild.test_product_turn_kinds import request


@pytest.fixture(scope='module', autouse=True)
def variants():
    register('rank', '@9201')(lambda value: 'frozen-one')
    register('rank', '@9202')(lambda value: 'frozen-two')


def runtime(path, operation):
    from backend.memory_app.kernel.policy_runtime import ProductPolicyRuntime
    store = SQLiteAITurnStore(path)

    class Planner:
        def plan(self, value, events, capabilities, payloads, execution_control):
            operation(value)
            return {'type': 'complete', 'summary': 'policy observed'}

    return ProductPolicyRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store), store


def frozen(identity, selected=None):
    value = request('project.answer', turn_id='turn-' + identity * 32,
        operation_id='op-policy-' + identity, idempotency_key='policy-' + identity,
        capabilities=[])
    if selected:
        value['policy_versions'] = {'rank': selected}
    return value


def test_sqlite_restart_in_background_keeps_frozen_version_after_active_and_override_change(tmp_path, monkeypatch):
    path = tmp_path / 'turns.sqlite3'
    original, _ = runtime(path, lambda value: None)
    accepted = original.accept_turn(frozen('a', '@9201'))
    seen = []
    restored, store = runtime(path, lambda value: seen.append((version('rank'), get('rank')(None))))
    monkeypatch.setitem(ACTIVE, 'rank', '@9202')

    def resume():
        with override(rank='@9202'):
            receipt = restored.run_accepted_turn(accepted.turn_id)
            assert version('rank') == '@9202'
            return receipt

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(resume).result(timeout=10)
    assert result.status == 'completed'
    assert seen == [('@9201', 'frozen-one')]
    assert store.get_request(accepted.turn_id)['policy_versions'] == {'rank': '@9201'}


def test_concurrent_sqlite_turns_have_independent_policy_maps(tmp_path):
    barrier = Barrier(2)
    seen = {}

    def observe(value):
        barrier.wait(timeout=5)
        seen[value['turn_id']] = (version('rank'), get('rank')(None))

    runner, _ = runtime(tmp_path / 'turns.sqlite3', observe)
    first = runner.accept_turn(frozen('b', '@9201'))
    second = runner.accept_turn(frozen('c', '@9202'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(runner.run_accepted_turn, [first.turn_id, second.turn_id]))
    assert [result.status for result in results] == ['completed', 'completed']
    assert seen == {first.turn_id: ('@9201', 'frozen-one'), second.turn_id: ('@9202', 'frozen-two')}
    assert version('rank') == ACTIVE['rank']


def test_failed_planner_restores_callers_override(tmp_path):
    seen = []

    def fail(value):
        seen.append(version('rank'))
        raise RuntimeError('synthetic planner failure')

    runner, _ = runtime(tmp_path / 'turns.sqlite3', fail)
    with override(rank='@9202'):
        result = runner.submit_turn(frozen('d', '@9201'))
        assert version('rank') == '@9202'
    assert result.status == 'failed'
    assert seen == ['@9201']
    assert version('rank') == ACTIVE['rank']


def test_missing_frozen_version_is_rejected_before_execution_and_restores_context(tmp_path):
    seen = []
    runner, _ = runtime(tmp_path / 'turns.sqlite3', seen.append)
    accepted = runner.accept_turn(frozen('e', '@9299'))
    with override(rank='@9202'):
        with pytest.raises(ValueError, match='unknown_policy_version'):
            runner.run_accepted_turn(accepted.turn_id)
        assert version('rank') == '@9202'
    assert seen == []


def test_legacy_product_turn_without_map_uses_historical_version_without_rewriting_request(tmp_path):
    seen = []
    runner, store = runtime(tmp_path / 'turns.sqlite3', lambda value: seen.append(version('rank')))
    value = frozen('f')
    with override(rank='@9202'):
        result = runner.submit_turn(value)
    assert result.status == 'completed'
    assert seen == ['@1']
    assert 'policy_versions' not in store.get_request(result.turn_id)
